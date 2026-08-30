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
    fb2Nodes: 250_000,
    fb2Depth: 256,
    imageChunks: 16_384,
    imageDimension: 16_384,
    imagePixels: 40_000_000,
    imageFrames: 256,
    publicationImagePixelFrames: 200_000_000,
    publicationImageFrames: 1024,
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

const invalidRaster = () => fail('A publication image is invalid or exceeds reader limits.')
const ascii = (bytes, start, length) => String.fromCharCode(...bytes.subarray(start, start + length))
const uint16BE = (bytes, offset) => (bytes[offset] << 8) | bytes[offset + 1]
const uint16LE = (bytes, offset) => bytes[offset] | (bytes[offset + 1] << 8)
const uint24LE = (bytes, offset) => bytes[offset]
    | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16)
const uint32BE = (bytes, offset) => ((bytes[offset] * 0x1000000)
    + (bytes[offset + 1] << 16) + (bytes[offset + 2] << 8) + bytes[offset + 3])
const uint32LE = (bytes, offset) => (bytes[offset] + (bytes[offset + 1] << 8)
    + (bytes[offset + 2] << 16) + (bytes[offset + 3] * 0x1000000))

const rasterDimensions = (width, height) => {
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height)
        || width < 1 || height < 1
        || width > LIMITS.imageDimension || height > LIMITS.imageDimension
        || width * height > LIMITS.imagePixels) invalidRaster()
    return { width, height }
}

const parseJPEG = (bytes, allowTrailing = false) => {
    if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) invalidRaster()
    const startOfFrame = new Set([
        0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
        0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf,
    ])
    let offset = 2, dimensions, deferredWidth = null, sawScan = false
    let sawDNL = false, expectDNL = false
    while (offset < bytes.length) {
        if (bytes[offset] !== 0xff) invalidRaster()
        while (bytes[offset] === 0xff) offset++
        if (offset >= bytes.length) invalidRaster()
        const marker = bytes[offset++]
        if (expectDNL && marker !== 0xdc) invalidRaster()
        if (marker === 0xd9) {
            if (!dimensions || deferredWidth !== null || !sawScan
                || (!allowTrailing && offset !== bytes.length)) invalidRaster()
            return { ...dimensions, frames: 1, end: offset }
        }
        if (marker === 0x00 || marker === 0xd8
            || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) invalidRaster()
        if (offset + 2 > bytes.length) invalidRaster()
        const length = uint16BE(bytes, offset)
        if (length < 2 || offset + length > bytes.length) invalidRaster()
        if (startOfFrame.has(marker)) {
            if (length < 8 || sawScan) invalidRaster()
            const components = bytes[offset + 7]
            if (!components || length !== 8 + components * 3) invalidRaster()
            const width = uint16BE(bytes, offset + 5)
            const height = uint16BE(bytes, offset + 3)
            if (!height) {
                if (dimensions || deferredWidth !== null || sawDNL
                    || !width || width > LIMITS.imageDimension) invalidRaster()
                deferredWidth = width
            } else {
                if (deferredWidth !== null || sawDNL) invalidRaster()
                const parsed = rasterDimensions(width, height)
                if (dimensions && (dimensions.width !== parsed.width
                    || dimensions.height !== parsed.height)) invalidRaster()
                dimensions = parsed
            }
        } else if (marker === 0xdc) {
            if (!expectDNL || deferredWidth === null || dimensions || sawDNL || length !== 4)
                invalidRaster()
            dimensions = rasterDimensions(deferredWidth, uint16BE(bytes, offset + 2))
            deferredWidth = null
            sawDNL = true
            expectDNL = false
        }
        offset += length
        if (marker !== 0xda) continue
        if (!dimensions && deferredWidth === null) invalidRaster()
        sawScan = true
        while (offset < bytes.length) {
            if (bytes[offset++] !== 0xff) continue
            while (bytes[offset] === 0xff) offset++
            if (offset >= bytes.length) invalidRaster()
            const next = bytes[offset]
            if (next === 0x00 || (next >= 0xd0 && next <= 0xd7)) {
                offset++
                continue
            }
            offset--
            break
        }
        expectDNL = deferredWidth !== null
    }
    invalidRaster()
}

const CRC32_TABLE = Uint32Array.from({ length: 256 }, (_, value) => {
    let crc = value
    for (let bit = 0; bit < 8; bit++) crc = (crc & 1) ? 0xedb88320 ^ (crc >>> 1) : crc >>> 1
    return crc >>> 0
})

const pngCRC = (bytes, start, end) => {
    let crc = 0xffffffff
    for (let index = start; index < end; index++)
        crc = CRC32_TABLE[(crc ^ bytes[index]) & 0xff] ^ (crc >>> 8)
    return (crc ^ 0xffffffff) >>> 0
}

const PNG_CANONICAL_CHUNKS = new Set([
    'IHDR', 'PLTE', 'tRNS', 'acTL', 'fcTL', 'IDAT', 'fdAT', 'IEND',
])
const concatenateByteRanges = (prefix, source, ranges) => {
    const size = prefix.length + ranges.reduce(
        (total, [start, end]) => total + end - start, 0)
    const result = new Uint8Array(size)
    result.set(prefix)
    let offset = prefix.length
    for (const [start, end] of ranges) {
        const part = source.subarray(start, end)
        result.set(part, offset)
        offset += part.length
    }
    return result
}

const parsePNG = (bytes, canonicalize = false) => {
    const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
    if (bytes.length < 45 || !signature.every((value, index) => bytes[index] === value))
        invalidRaster()
    let offset = 8, dimensions, colorType, paletteEntries = 0
    let sawPLTE = false, sawTRNS = false, chunkCount = 0
    let idatBytes = 0, sawIDAT = false, sawIEND = false
    let idatEnded = false, animationFrames = null, frameControls = 0
    let animationSequence = 0, defaultImageIsSeparate = null, activeFrameHasData = false
    let activeFrameUsesIDAT = false
    const canonicalChunks = []
    while (offset < bytes.length) {
        if (++chunkCount > LIMITS.imageChunks || offset + 12 > bytes.length)
            invalidRaster()
        const length = uint32BE(bytes, offset)
        const typeOffset = offset + 4
        const type = ascii(bytes, typeOffset, 4)
        const dataOffset = offset + 8
        const end = dataOffset + length
        if (!/^[A-Za-z]{4}$/.test(type) || type[2] !== type[2].toUpperCase()
            || !Number.isSafeInteger(end) || end + 4 > bytes.length
            || pngCRC(bytes, typeOffset, end) !== uint32BE(bytes, end)) invalidRaster()
        if (offset === 8 && type !== 'IHDR') invalidRaster()
        if (sawIDAT && type !== 'IDAT') idatEnded = true
        if (type === 'IHDR') {
            if (dimensions || length !== 13) invalidRaster()
            dimensions = rasterDimensions(
                uint32BE(bytes, dataOffset), uint32BE(bytes, dataOffset + 4))
            const depth = bytes[dataOffset + 8], color = bytes[dataOffset + 9]
            const depths = new Map([
                [0, [1, 2, 4, 8, 16]], [2, [8, 16]], [3, [1, 2, 4, 8]],
                [4, [8, 16]], [6, [8, 16]],
            ])
            if (!depths.get(color)?.includes(depth) || bytes[dataOffset + 10] !== 0
                || bytes[dataOffset + 11] !== 0 || ![0, 1].includes(bytes[dataOffset + 12]))
                invalidRaster()
            colorType = color
        } else if (type === 'PLTE') {
            if (sawPLTE || sawIDAT || !length || length > 768 || length % 3)
                invalidRaster()
            sawPLTE = true
            paletteEntries = length / 3
        } else if (type === 'tRNS') {
            if (sawTRNS || sawIDAT
                || (colorType === 0 && length !== 2)
                || (colorType === 2 && length !== 6)
                || (colorType === 3 && (!sawPLTE || !length || length > paletteEntries))
                || ![0, 2, 3].includes(colorType)) invalidRaster()
            sawTRNS = true
        } else if (type === 'acTL') {
            if (!dimensions || animationFrames !== null || sawIDAT || length !== 8)
                invalidRaster()
            animationFrames = uint32BE(bytes, dataOffset)
            if (!animationFrames || animationFrames > LIMITS.imageFrames) invalidRaster()
        } else if (type === 'fcTL') {
            if (animationFrames === null || length !== 26
                || (frameControls && !activeFrameHasData)) invalidRaster()
            if (!frameControls) defaultImageIsSeparate = sawIDAT
            frameControls++
            if (frameControls > animationFrames
                || uint32BE(bytes, dataOffset) !== animationSequence++) invalidRaster()
            const width = uint32BE(bytes, dataOffset + 4)
            const height = uint32BE(bytes, dataOffset + 8)
            const x = uint32BE(bytes, dataOffset + 12)
            const y = uint32BE(bytes, dataOffset + 16)
            rasterDimensions(width, height)
            if (x + width > dimensions.width || y + height > dimensions.height
                || bytes[dataOffset + 24] > 2 || bytes[dataOffset + 25] > 1
                || (!sawIDAT && (width !== dimensions.width
                    || height !== dimensions.height || x || y))) invalidRaster()
            activeFrameHasData = false
            activeFrameUsesIDAT = !sawIDAT
        } else if (type === 'fdAT') {
            if (animationFrames === null || !sawIDAT || !frameControls || length <= 4
                || activeFrameUsesIDAT
                || uint32BE(bytes, dataOffset) !== animationSequence++) invalidRaster()
            activeFrameHasData = true
        } else if (type === 'IDAT') {
            if (!dimensions || idatEnded
                || (frameControls && defaultImageIsSeparate !== false)) invalidRaster()
            sawIDAT = true
            idatBytes += length
            if (frameControls) activeFrameHasData = true
        } else if (type === 'IEND') {
            if (length || !idatBytes || sawIEND || end + 4 !== bytes.length
                || (frameControls && !activeFrameHasData)) invalidRaster()
            sawIEND = true
        } else if (type === 'iCCP' || type === 'zTXt') {
            if (!canonicalize) invalidRaster()
        } else if (type === 'iTXt') {
            if (!canonicalize) {
                const keywordEnd = bytes.indexOf(0, dataOffset)
                if (keywordEnd <= dataOffset || keywordEnd - dataOffset > 79
                    || keywordEnd + 2 >= end || bytes[keywordEnd + 1] > 1
                    || bytes[keywordEnd + 2] !== 0) invalidRaster()
                const languageEnd = bytes.indexOf(0, keywordEnd + 3)
                const translatedEnd = languageEnd < 0 ? -1 : bytes.indexOf(0, languageEnd + 1)
                if (languageEnd >= end || translatedEnd < 0 || translatedEnd >= end
                    || bytes[keywordEnd + 1] === 1) invalidRaster()
            }
        } else if (type[0] === type[0].toUpperCase()
            && !['PLTE'].includes(type)) invalidRaster()
        if (canonicalize && PNG_CANONICAL_CHUNKS.has(type))
            canonicalChunks.push([offset, end + 4])
        offset = end + 4
    }
    const frames = animationFrames === null
        ? 1 : animationFrames + (defaultImageIsSeparate ? 1 : 0)
    if (!dimensions || !sawIEND || frames > LIMITS.imageFrames
        || (colorType === 3 && !sawPLTE)
        || (animationFrames !== null && (frameControls !== animationFrames
            || defaultImageIsSeparate === null))) invalidRaster()
    const result = { ...dimensions, frames }
    if (canonicalize) {
        result.bytes = concatenateByteRanges(
            Uint8Array.from(signature), bytes, canonicalChunks)
    }
    return result
}

const skipGIFSubBlocks = (bytes, start, requireData = false) => {
    let offset = start, total = 0
    while (offset < bytes.length) {
        const length = bytes[offset++]
        if (!length) {
            if (requireData && !total) invalidRaster()
            return offset
        }
        if (offset + length > bytes.length) invalidRaster()
        total += length
        offset += length
    }
    invalidRaster()
}

const parseGIF = bytes => {
    if (bytes.length < 14 || !['GIF87a', 'GIF89a'].includes(ascii(bytes, 0, 6)))
        invalidRaster()
    const dimensions = rasterDimensions(uint16LE(bytes, 6), uint16LE(bytes, 8))
    const packed = bytes[10]
    let offset = 13 + ((packed & 0x80) ? 3 * (2 << (packed & 7)) : 0)
    if (offset > bytes.length) invalidRaster()
    let frames = 0
    while (offset < bytes.length) {
        const marker = bytes[offset++]
        if (marker === 0x3b) {
            if (!frames || offset !== bytes.length) invalidRaster()
            return { ...dimensions, frames }
        }
        if (marker === 0x2c) {
            if (offset + 9 > bytes.length) invalidRaster()
            const left = uint16LE(bytes, offset), top = uint16LE(bytes, offset + 2)
            const width = uint16LE(bytes, offset + 4), height = uint16LE(bytes, offset + 6)
            rasterDimensions(width, height)
            if (left + width > dimensions.width || top + height > dimensions.height)
                invalidRaster()
            const imagePacked = bytes[offset + 8]
            offset += 9 + ((imagePacked & 0x80) ? 3 * (2 << (imagePacked & 7)) : 0)
            if (offset >= bytes.length || bytes[offset] < 2 || bytes[offset] > 8)
                invalidRaster()
            offset = skipGIFSubBlocks(bytes, offset + 1, true)
            frames++
            if (frames > LIMITS.imageFrames) invalidRaster()
            continue
        }
        if (marker !== 0x21 || offset >= bytes.length) invalidRaster()
        const label = bytes[offset++]
        if (label === 0xf9) {
            if (offset + 6 > bytes.length || bytes[offset] !== 4 || bytes[offset + 5] !== 0)
                invalidRaster()
            offset += 6
        } else if (label === 0x01 || label === 0xff) {
            const expected = label === 0x01 ? 12 : 11
            if (offset >= bytes.length || bytes[offset] !== expected
                || offset + 1 + expected > bytes.length) invalidRaster()
            offset = skipGIFSubBlocks(bytes, offset + 1 + expected)
        } else offset = skipGIFSubBlocks(bytes, offset)
    }
    invalidRaster()
}

const parseVP8 = (bytes, start, length) => {
    if (length < 10 || (bytes[start] & 1) !== 0
        || bytes[start + 3] !== 0x9d || bytes[start + 4] !== 0x01
        || bytes[start + 5] !== 0x2a) invalidRaster()
    return rasterDimensions(
        uint16LE(bytes, start + 6) & 0x3fff,
        uint16LE(bytes, start + 8) & 0x3fff)
}

const parseVP8L = (bytes, start, length) => {
    if (length < 5 || bytes[start] !== 0x2f) invalidRaster()
    const bits = uint32LE(bytes, start + 1) >>> 0
    if (bits >>> 29 !== 0) invalidRaster()
    return rasterDimensions((bits & 0x3fff) + 1, ((bits >>> 14) & 0x3fff) + 1)
}

const parseWebPFrame = (bytes, start, end) => {
    let offset = start, dimensions, alpha = false
    while (offset < end) {
        if (offset + 8 > end) invalidRaster()
        const type = ascii(bytes, offset, 4)
        const length = uint32LE(bytes, offset + 4)
        const data = offset + 8, next = data + length + (length & 1)
        if (next > end) invalidRaster()
        if (type === 'ALPH') {
            if (alpha || dimensions) invalidRaster()
            alpha = true
        } else if (type === 'VP8 ') {
            if (dimensions) invalidRaster()
            dimensions = parseVP8(bytes, data, length)
        } else if (type === 'VP8L') {
            if (dimensions || alpha) invalidRaster()
            dimensions = parseVP8L(bytes, data, length)
        } else invalidRaster()
        offset = next
    }
    if (!dimensions || offset !== end) invalidRaster()
    return dimensions
}

const parseWebP = bytes => {
    if (bytes.length < 20 || ascii(bytes, 0, 4) !== 'RIFF'
        || ascii(bytes, 8, 4) !== 'WEBP' || uint32LE(bytes, 4) + 8 !== bytes.length)
        invalidRaster()
    let offset = 12, canvas, encoded, extended = false, animated = false
    let animationHeader = false, frames = 0, firstChunk = true
    while (offset < bytes.length) {
        if (offset + 8 > bytes.length) invalidRaster()
        const type = ascii(bytes, offset, 4)
        const length = uint32LE(bytes, offset + 4)
        const data = offset + 8, end = data + length, next = end + (length & 1)
        if (!/^[\x20-\x7e]{4}$/.test(type) || next > bytes.length) invalidRaster()
        if (type === 'VP8X') {
            if (!firstChunk || extended || length !== 10) invalidRaster()
            const flags = bytes[data]
            if (flags & 0xc1) invalidRaster()
            animated = Boolean(flags & 0x02)
            canvas = rasterDimensions(
                uint24LE(bytes, data + 4) + 1, uint24LE(bytes, data + 7) + 1)
            extended = true
        } else if (type === 'VP8 ' || type === 'VP8L') {
            if (encoded || frames) invalidRaster()
            encoded = type === 'VP8 '
                ? parseVP8(bytes, data, length) : parseVP8L(bytes, data, length)
        } else if (type === 'ANIM') {
            if (!extended || !animated || animationHeader || length !== 6 || frames)
                invalidRaster()
            animationHeader = true
        } else if (type === 'ANMF') {
            if (!animationHeader || length < 16) invalidRaster()
            const x = uint24LE(bytes, data) * 2, y = uint24LE(bytes, data + 3) * 2
            const width = uint24LE(bytes, data + 6) + 1
            const height = uint24LE(bytes, data + 9) + 1
            rasterDimensions(width, height)
            if (x + width > canvas.width || y + height > canvas.height) invalidRaster()
            const frame = parseWebPFrame(bytes, data + 16, end)
            if (frame.width !== width || frame.height !== height) invalidRaster()
            frames++
            if (frames > LIMITS.imageFrames) invalidRaster()
        } else if (!['ALPH', 'ICCP', 'EXIF', 'XMP '].includes(type)) invalidRaster()
        firstChunk = false
        offset = next
    }
    if (offset !== bytes.length) invalidRaster()
    if (extended) {
        if (animated) {
            if (!animationHeader || !frames || encoded) invalidRaster()
            return { ...canvas, frames }
        }
        if (!encoded || animationHeader || frames
            || encoded.width !== canvas.width || encoded.height !== canvas.height) invalidRaster()
        return { ...canvas, frames: 1 }
    }
    if (!encoded || animationHeader || frames) invalidRaster()
    return { ...encoded, frames: 1 }
}

export const createRasterBudget = () => ({ pixelFrames: 0, frames: 0 })

export const canonicalizeFB2RasterImage = async (source, _declaredMediaType, budget = null) => {
    const sourceSize = source instanceof Uint8Array ? source.byteLength : source?.size
    if (!Number.isSafeInteger(sourceSize) || sourceSize < 1) invalidRaster()
    let bytes
    try {
        bytes = source instanceof Uint8Array
            ? source : new Uint8Array(await source.arrayBuffer())
    } catch (error) {
        throw new PublicationError(
            'A publication image is invalid or exceeds reader limits.', { cause: error })
    }

    let type, parsed, canonicalBytes
    if (bytes[0] === 0xff && bytes[1] === 0xd8) {
        type = 'image/jpeg'
        parsed = parseJPEG(bytes, true)
        canonicalBytes = bytes.slice(0, parsed.end)
    } else if ([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]
        .every((value, index) => bytes[index] === value)) {
        type = 'image/png'
        parsed = parsePNG(bytes, true)
        canonicalBytes = parsed.bytes
    } else if (ascii(bytes, 0, 6) === 'GIF87a' || ascii(bytes, 0, 6) === 'GIF89a') {
        type = 'image/gif'
        parsed = parseGIF(bytes)
        canonicalBytes = bytes
    } else if (ascii(bytes, 0, 4) === 'RIFF' && ascii(bytes, 8, 4) === 'WEBP') {
        type = 'image/webp'
        parsed = parseWebP(bytes)
        canonicalBytes = bytes
    } else invalidRaster()

    const pixelFrames = parsed.width * parsed.height * parsed.frames
    if (!Number.isSafeInteger(pixelFrames)) invalidRaster()
    if (budget) {
        if (budget.pixelFrames + pixelFrames > LIMITS.publicationImagePixelFrames
            || budget.frames + parsed.frames > LIMITS.publicationImageFrames) invalidRaster()
        budget.pixelFrames += pixelFrames
        budget.frames += parsed.frames
    }
    return { type, bytes: canonicalBytes, pixelFrames, frames: parsed.frames }
}

export const validateRasterImage = async (source, mediaType, budget) => {
    const type = normalizedMediaType(mediaType)
    const sourceSize = source instanceof Uint8Array ? source.byteLength : source?.size
    if (!RASTER_TYPES.has(type) || !sourceSize) invalidRaster()
    let bytes
    try {
        bytes = source instanceof Uint8Array
            ? source : new Uint8Array(await source.arrayBuffer())
    } catch (error) {
        throw new PublicationError(
            'A publication image is invalid or exceeds reader limits.', { cause: error })
    }
    const parsed = type === 'image/jpeg' ? parseJPEG(bytes)
        : type === 'image/png' ? parsePNG(bytes)
            : type === 'image/gif' ? parseGIF(bytes) : parseWebP(bytes)
    if (parsed.frames > LIMITS.imageFrames) invalidRaster()
    const pixelFrames = parsed.width * parsed.height * parsed.frames
    if (!Number.isSafeInteger(pixelFrames)
        || budget.pixelFrames + pixelFrames > LIMITS.publicationImagePixelFrames
        || budget.frames + parsed.frames > LIMITS.publicationImageFrames) invalidRaster()
    budget.pixelFrames += pixelFrames
    budget.frames += parsed.frames
    return parsed
}

export const safeToken = value => typeof value === 'string'
    && value.length <= 256 && /^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(value)

export const safeLanguage = value => typeof value === 'string'
    && value.length <= 64 && /^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/.test(value)

export const safeDirection = value => ['ltr', 'rtl', 'auto'].includes(value)
