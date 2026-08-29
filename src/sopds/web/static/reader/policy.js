export const LIMITS = Object.freeze({
    sourceBytes: 64 * 1024 * 1024,
    zipEntries: 2000,
    expandedBytes: 128 * 1024 * 1024,
    stylesheetBytes: 256 * 1024,
    totalStylesheetBytes: 2 * 1024 * 1024,
    cssRules: 2000,
    selectorLength: 1024,
    declarationsPerRule: 128,
    cssOutputBytes: 1024 * 1024,
    tocNodes: 2000,
    tocDepth: 16,
    tocMarkupBytes: 256 * 1024,
    tocTextBytes: 256 * 1024,
    tocLabelBytes: 4096,
})

export class PublicationError extends Error {
    constructor(message, options) {
        super(message, options)
        this.name = 'PublicationError'
    }
}

export const MEDIA_TYPES = Object.freeze({
    fb2: 'application/x-fictionbook+xml',
    epub: 'application/epub+zip',
})

const RASTER_TYPES = new Set([
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
])

const fail = message => { throw new PublicationError(message) }

export const normalizedMediaType = value =>
    value?.split(';', 1)[0].trim().toLowerCase() ?? ''

export const requireExpectedMediaType = (value, format) => {
    const actual = normalizedMediaType(value)
    if (actual !== MEDIA_TYPES[format]) fail('The source has an unexpected media type.')
    return actual
}

const decodePathPart = part => {
    let decoded
    try {
        decoded = decodeURIComponent(part)
    } catch {
        fail('The publication contains an invalid encoded path.')
    }
    if (decoded.includes('/') || decoded.includes('\\') || decoded.includes('?')
        || decoded.includes('#') || /[\u0000-\u001f\u007f]/u.test(decoded))
        fail('The publication contains an unsafe encoded path.')
    return decoded
}

export const canonicalArchivePath = value => {
    if (typeof value !== 'string' || !value || value.includes('\\')
        || value.includes('?') || value.includes('#')
        || /[\u0000-\u001f\u007f]/u.test(value)
        || value.startsWith('/') || value.startsWith('//') || /^[A-Za-z]:/.test(value))
        fail('The publication contains an unsafe archive path.')
    const parts = value.split('/')
    if (parts.some(part => !part || part === '.' || part === '..'))
        fail('The publication contains an unsafe archive path.')
    const canonical = parts.join('/').normalize('NFC')
    if (!canonical || canonical.includes('?') || canonical.includes('#')
        || canonical.split('/', 1)[0].includes(':'))
        fail('The publication path is invalid.')
    return canonical
}

const decodeFragment = value => {
    let decoded
    try {
        decoded = decodeURIComponent(value)
    } catch {
        fail('The publication contains an invalid encoded fragment.')
    }
    if (decoded.length > 256 || /[\s#"'<>\0]/u.test(decoded))
        fail('The publication contains an unsafe fragment.')
    return decoded
}

const splitReference = reference => {
    const hashAt = reference.indexOf('#')
    const path = hashAt < 0 ? reference : reference.slice(0, hashAt)
    const encodedFragment = hashAt < 0 ? '' : reference.slice(hashAt + 1)
    if (path.includes('?')) fail('The publication contains an unsafe URL.')
    return {
        path,
        fragment: encodedFragment ? decodeFragment(encodedFragment) : '',
    }
}

export const isExternalReference = value => {
    if (typeof value !== 'string') return false
    const trimmed = value.trim()
    return trimmed.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(trimmed)
}

export const resolvePackageReference = (reference, basePath) => {
    if (typeof reference !== 'string' || reference !== reference.trim()
        || !reference || reference.includes('\0') || reference.includes('\\'))
        fail('The publication contains an invalid URL.')
    if (isExternalReference(reference) || reference.startsWith('/')) return null
    const { path, fragment } = splitReference(reference)
    const base = basePath.slice(0, basePath.lastIndexOf('/') + 1).split('/').filter(Boolean)
    const parts = path ? path.split('/') : []
    for (const encoded of parts) {
        const part = decodePathPart(encoded)
        if (!part || part === '.') continue
        if (part === '..') {
            if (!base.length) fail('A publication URL escapes the package root.')
            base.pop()
        } else base.push(part)
    }
    const canonical = (path ? base.join('/') : basePath).normalize('NFC')
    if (!canonical || canonical.split('/', 1)[0].includes(':'))
        fail('The publication URL is invalid.')
    return { path: canonical, fragment }
}

const encodePathPart = part => encodeURIComponent(part)
    .replace(/[!'()*]/g, character => `%${character.charCodeAt(0).toString(16).toUpperCase()}`)

export const encodePackagePath = path => path.split('/').map(encodePathPart).join('/')

export const encodePackageFragment = fragment => encodePathPart(fragment)

export const relativePackagePath = (from, to) => {
    const source = from.slice(0, from.lastIndexOf('/') + 1).split('/').filter(Boolean)
    const target = to.split('/').filter(Boolean)
    let common = 0
    while (common < source.length && source[common] === target[common]) common++
    return [
        ...Array(source.length - common).fill('..'),
        ...target.slice(common).map(encodePathPart),
    ].join('/')
}

export const splitEncodedPackageHref = href => {
    if (typeof href !== 'string' || !href) fail('The publication link is invalid.')
    const resolved = resolvePackageReference(href, '')
    if (!resolved) fail('The publication link is invalid.')
    return resolved
}

export const isRasterMediaType = value => RASTER_TYPES.has(normalizedMediaType(value))

export const hasRasterSignature = async (blob, mediaType) => {
    const type = normalizedMediaType(mediaType)
    if (!RASTER_TYPES.has(type)) return false
    const bytes = new Uint8Array(await blob.slice(0, 16).arrayBuffer())
    if (type === 'image/jpeg')
        return bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff
    if (type === 'image/png')
        return bytes.length >= 8 && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
            .every((value, index) => bytes[index] === value)
    if (type === 'image/gif') {
        const header = String.fromCharCode(...bytes.slice(0, 6))
        return header === 'GIF87a' || header === 'GIF89a'
    }
    if (type === 'image/webp')
        return bytes.length >= 12
            && String.fromCharCode(...bytes.slice(0, 4)) === 'RIFF'
            && String.fromCharCode(...bytes.slice(8, 12)) === 'WEBP'
    return false
}

export const safeToken = value => typeof value === 'string'
    && value.length <= 256 && /^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(value)

export const safeLanguage = value => typeof value === 'string'
    && value.length <= 64 && /^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/.test(value)

export const safeDirection = value => ['ltr', 'rtl', 'auto'].includes(value)
