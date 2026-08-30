import { makeFB2 } from '../vendor/foliate/fb2.js'
import {
    LIMITS,
    MEDIA_TYPES,
    PublicationError,
    canonicalArchivePath,
    canonicalizeFB2RasterImage,
    createRasterBudget,
    encodePackageFragment,
    encodePackagePath,
    isExternalReference,
    isRasterMediaType,
    normalizedMediaType,
    relativePackagePath,
    requireExpectedMediaType,
    resolvePackageReference,
    safeDirection,
    safeLanguage,
    safeToken,
    splitEncodedPackageHref,
    validateRasterImage,
} from './policy.js'

const XML = 'application/xml'
const XHTML = 'application/xhtml+xml'
const XLINK_NS = 'http://www.w3.org/1999/xlink'
const EPUB_NS = 'http://www.idpf.org/2007/ops'
const XHTML_NS = 'http://www.w3.org/1999/xhtml'
const CONTAINER_NS = 'urn:oasis:names:tc:opendocument:xmlns:container'
const OPF_NS = 'http://www.idpf.org/2007/opf'
const DC_NS = 'http://purl.org/dc/elements/1.1/'
const NCX_NS = 'http://www.daisy.org/z3986/2005/ncx/'
const FB2_NS = 'http://www.gribuser.ru/xml/fictionbook/2.0'

const fail = message => { throw new PublicationError(message) }
const checkAbort = signal => {
    if (signal?.aborted) fail('Opening the book was cancelled.')
}
const text = element => element?.textContent?.replace(/[\t\n\f\r ]+/g, ' ').trim() ?? ''
const safeFragment = value => typeof value === 'string' && value.length > 0
    && value.length <= 256 && !/[\s#"'<>]/u.test(value)
const hasVisibleUnicodeText = value => /[\p{L}\p{M}\p{N}\p{P}\p{S}]/u.test(value)

const decodeXMLBlob = async (blob, label, signal) => {
    const prefix = new Uint8Array(await blob.slice(0, 1024).arrayBuffer())
    checkAbort(signal)
    let signatureEncoding
    if ((prefix[0] === 0xff && prefix[1] === 0xfe)
        || (prefix[0] === 0x3c && prefix[1] === 0x00)) signatureEncoding = 'utf-16le'
    else if ((prefix[0] === 0xfe && prefix[1] === 0xff)
        || (prefix[0] === 0x00 && prefix[1] === 0x3c)) signatureEncoding = 'utf-16be'
    else if (prefix[0] === 0xef && prefix[1] === 0xbb && prefix[2] === 0xbf)
        signatureEncoding = 'utf-8'
    try {
        const declarationPrefix = signatureEncoding?.startsWith('utf-16')
            ? new TextDecoder(signatureEncoding).decode(prefix)
            : String.fromCharCode(...prefix)
        const declared = declarationPrefix.match(
            /^\uFEFF?<\?xml\s+[^>]*encoding\s*=\s*["']([A-Za-z0-9._-]+)["']/i,
        )?.[1]
        const declaredLower = declared?.toLowerCase()
        if (signatureEncoding && declaredLower
            && !(signatureEncoding === 'utf-8' && ['utf-8', 'utf8'].includes(declaredLower))
            && !(signatureEncoding.startsWith('utf-16')
                && ['utf-16', signatureEncoding].includes(declaredLower)))
            fail(`${label} has a conflicting XML encoding declaration.`)
        if (!signatureEncoding && declaredLower === 'utf-16')
            fail(`${label} uses UTF-16 without a byte-order signature.`)
        const encoding = signatureEncoding ?? declared ?? 'utf-8'
        const decoder = new TextDecoder(encoding, { fatal: true })
        const bytes = new Uint8Array(await blob.arrayBuffer())
        checkAbort(signal)
        return decoder.decode(bytes)
    } catch (error) {
        if (error instanceof PublicationError) throw error
        throw new PublicationError(`${label} uses an invalid or unsupported encoding.`, { cause: error })
    }
}

const SAFE_XHTML_DOCTYPES = new Map([
    ['-//W3C//DTD XHTML 1.1//EN', 'http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd'],
    ['-//W3C//DTD XHTML 1.0 Strict//EN', 'http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd'],
    ['-//W3C//DTD XHTML 1.0 Transitional//EN',
        'http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd'],
])
const XML_PREDEFINED_ENTITIES = new Set(['amp', 'apos', 'gt', 'lt', 'quot'])
const XHTML_NAMED_ENTITY_CODES = new Map(`
AElig=198 Aacute=193 Acirc=194 Agrave=192 Alpha=913 Aring=197 Atilde=195 Auml=196
Beta=914 Ccedil=199 Chi=935 Dagger=8225 Delta=916 ETH=208 Eacute=201 Ecirc=202
Egrave=200 Epsilon=917 Eta=919 Euml=203 Gamma=915 Iacute=205 Icirc=206 Igrave=204
Iota=921 Iuml=207 Kappa=922 Lambda=923 Mu=924 Ntilde=209 Nu=925 OElig=338
Oacute=211 Ocirc=212 Ograve=210 Omega=937 Omicron=927 Oslash=216 Otilde=213 Ouml=214
Phi=934 Pi=928 Prime=8243 Psi=936 Rho=929 Scaron=352 Sigma=931 THORN=222
Tau=932 Theta=920 Uacute=218 Ucirc=219 Ugrave=217 Upsilon=933 Uuml=220 Xi=926
Yacute=221 Yuml=376 Zeta=918 aacute=225 acirc=226 acute=180 aelig=230 agrave=224
alefsym=8501 alpha=945 and=8743 ang=8736 aring=229 asymp=8776 atilde=227 auml=228
bdquo=8222 beta=946 brvbar=166 bull=8226 cap=8745 ccedil=231 cedil=184 cent=162
chi=967 circ=710 clubs=9827 cong=8773 copy=169 crarr=8629 cup=8746 curren=164
dArr=8659 dagger=8224 darr=8595 deg=176 delta=948 diams=9830 divide=247 eacute=233
ecirc=234 egrave=232 empty=8709 emsp=8195 ensp=8194 epsilon=949 equiv=8801 eta=951
eth=240 euml=235 euro=8364 exist=8707 fnof=402 forall=8704 frac12=189 frac14=188
frac34=190 frasl=8260 gamma=947 ge=8805 hArr=8660 harr=8596 hearts=9829 hellip=8230
iacute=237 icirc=238 iexcl=161 igrave=236 image=8465 infin=8734 int=8747 iota=953
iquest=191 isin=8712 iuml=239 kappa=954 lArr=8656 lambda=955 lang=9001 laquo=171
larr=8592 lceil=8968 ldquo=8220 le=8804 lfloor=8970 lowast=8727 loz=9674 lrm=8206
lsaquo=8249 lsquo=8216 macr=175 mdash=8212 micro=181 middot=183 minus=8722 mu=956
nabla=8711 nbsp=160 ndash=8211 ne=8800 ni=8715 not=172 notin=8713 nsub=8836
ntilde=241 nu=957 oacute=243 ocirc=244 oelig=339 ograve=242 oline=8254 omega=969
omicron=959 oplus=8853 or=8744 ordf=170 ordm=186 oslash=248 otilde=245 otimes=8855
ouml=246 para=182 part=8706 permil=8240 perp=8869 phi=966 pi=960 piv=982
plusmn=177 pound=163 prime=8242 prod=8719 prop=8733 psi=968 rArr=8658 radic=8730
rang=9002 raquo=187 rarr=8594 rceil=8969 rdquo=8221 real=8476 reg=174 rfloor=8971
rho=961 rlm=8207 rsaquo=8250 rsquo=8217 sbquo=8218 scaron=353 sdot=8901 sect=167
shy=173 sigma=963 sigmaf=962 sim=8764 spades=9824 sub=8834 sube=8838 sum=8721
sup=8835 sup1=185 sup2=178 sup3=179 supe=8839 szlig=223 tau=964 there4=8756
theta=952 thetasym=977 thinsp=8201 thorn=254 tilde=732 times=215 trade=8482 uArr=8657
uacute=250 uarr=8593 ucirc=251 ugrave=249 uml=168 upsih=978 upsilon=965 uuml=252
weierp=8472 xi=958 yacute=253 yen=165 yuml=255 zeta=950 zwj=8205 zwnj=8204
`.trim().split(/\s+/).map(entry => entry.split('=')))

const replaceXHTMLNamedEntities = (source, label) => source.replace(
    /<!--[\s\S]*?(?:-->|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|&([A-Za-z][A-Za-z0-9]+);/g,
    (match, name) => {
        if (!name || XML_PREDEFINED_ENTITIES.has(name)) return match
        const code = XHTML_NAMED_ENTITY_CODES.get(name)
        if (!code) fail(`${label} contains an unknown named entity.`)
        return `&#${code};`
    },
)

const stripSafeXHTMLDoctype = (source, label) => {
    const marker = /<!\s*DOCTYPE\b/i.exec(source)
    if (!marker) return source
    const prefix = source.slice(0, marker.index)
    if (!/^\uFEFF?\s*(?:<\?xml[^?]*\?>\s*)?(?:<!--[\s\S]*?-->\s*)*$/.test(prefix))
        fail(`${label} contains a forbidden XML declaration.`)
    const declarationSource = source.slice(marker.index)
    const simple = /^<!DOCTYPE\s+html\s*>/.exec(declarationSource)
    const declared = /^<!DOCTYPE\s+html\s+PUBLIC\s+(["'])([^"']+)\1\s+(["'])([^"']+)\3\s*>/
        .exec(declarationSource)
    const declaration = simple ?? declared
    if (!declaration || (declared && SAFE_XHTML_DOCTYPES.get(declared[2]) !== declared[4]))
        fail(`${label} contains a forbidden XML declaration.`)
    const body = declarationSource.slice(declaration[0].length)
    const stripped = prefix + (declared ? replaceXHTMLNamedEntities(body, label) : body)
    if (/<!\s*DOCTYPE\b/i.test(stripped))
        fail(`${label} contains a forbidden XML declaration.`)
    return stripped
}

const parseXML = (source, label, { allowXHTMLDoctype = false } = {}) => {
    if (/<!\s*ENTITY\b/i.test(source))
        fail(`${label} contains a forbidden XML declaration.`)
    const parseSource = allowXHTMLDoctype
        ? stripSafeXHTMLDoctype(source, label) : source
    if (/<!\s*DOCTYPE\b/i.test(parseSource))
        fail(`${label} contains a forbidden XML declaration.`)
    const document = new DOMParser().parseFromString(parseSource, XML)
    if (document.querySelector('parsererror') || !document.documentElement)
        fail(`${label} is malformed XML.`)
    const walker = document.createTreeWalker(document, NodeFilter.SHOW_PROCESSING_INSTRUCTION)
    if (walker.nextNode()) fail(`${label} contains a processing instruction.`)
    return document
}

const parseFB2XML = source => {
    try {
        return parseXML(source, 'FB2', { allowXHTMLDoctype: false })
    } catch (error) {
        if (!(error instanceof PublicationError) || error.message !== 'FB2 is malformed XML.')
            throw error
        const repaired = source
            .replace(/&(?!#\d+;|#x[\da-f]+;|amp;|apos;|gt;|lt;|quot;)/gi, '&amp;')
            .replace(/<(?![!?/A-Z_:])/gi, '&lt;')
            .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, '\uFFFD')
        if (repaired === source) throw error
        return parseXML(repaired, 'FB2', { allowXHTMLDoctype: false })
    }
}

const elementChildren = element => [...element.children]
const child = (element, name, namespace = element.namespaceURI) => elementChildren(element)
    .find(item => item.localName === name && item.namespaceURI === namespace)
const children = (element, name, namespace = element.namespaceURI) => elementChildren(element)
    .filter(item => item.localName === name && item.namespaceURI === namespace)
const packageHref = ({ path, fragment }) => `${encodePackagePath(path)}${
    fragment ? `#${encodePackageFragment(fragment)}` : ''}`
const requiredAttribute = (element, name) => {
    const value = element.getAttribute(name)
    if (!value) fail('The publication is missing required metadata.')
    return value
}

const linkAbortSignal = (controller, signal) => {
    if (!signal) return () => {}
    if (signal.aborted) controller.abort(signal.reason)
    const abort = () => controller.abort(signal.reason)
    signal.addEventListener('abort', abort, { once: true })
    return () => signal.removeEventListener('abort', abort)
}

const boundedFetch = async ({ sourceUrl, format, signal }) => {
    const url = new URL(sourceUrl, location.href)
    if (url.origin !== location.origin || url.username || url.password || url.hash
        || url.search || !/^\/books\/[^/]+\/download$/.test(url.pathname))
        fail('The reader source URL is invalid.')

    const controller = new AbortController()
    const unlink = linkAbortSignal(controller, signal)
    try {
        const response = await fetch(url.href, {
            cache: 'no-store',
            credentials: 'same-origin',
            redirect: 'error',
            signal: controller.signal,
        })
        checkAbort(signal)
        const finalURL = new URL(response.url)
        if (finalURL.origin !== location.origin
            || finalURL.pathname !== url.pathname || finalURL.search || finalURL.hash) {
            controller.abort()
            fail('The reader source redirected unexpectedly.')
        }
        if (!response.ok) fail('The original book is unavailable.')
        requireExpectedMediaType(response.headers.get('Content-Type'), format)
        const revision = response.headers.get('X-SOPDS-Source-Revision')
        if (!revision || revision.length > 256 || !/^[A-Za-z0-9_-]+$/.test(revision))
            fail('The source revision is missing or invalid.')

        const declared = response.headers.get('Content-Length')
        if (declared !== null) {
            const length = Number(declared)
            if (!Number.isSafeInteger(length) || length < 0)
                fail('The source length is invalid.')
            if (length > LIMITS.sourceBytes) {
                controller.abort()
                fail('The book exceeds the 64 MiB reader limit.')
            }
        }
        if (!response.body) fail('The source response has no readable body.')

        const reader = response.body.getReader()
        const chunks = []
        let size = 0
        try {
            while (true) {
                const { value, done } = await reader.read()
                checkAbort(signal)
                if (done) break
                size += value.byteLength
                if (size > LIMITS.sourceBytes) {
                    controller.abort()
                    await reader.cancel()
                    fail('The book exceeds the 64 MiB reader limit.')
                }
                chunks.push(value)
            }
        } catch (error) {
            controller.abort()
            throw error
        } finally {
            reader.releaseLock()
        }
        if (!size) fail('The source book is empty.')
        const name = `publication.${format}`
        const file = new File(chunks, name, { type: MEDIA_TYPES[format] })
        checkAbort(signal)
        return { file, revision, controller }
    } catch (error) {
        controller.abort()
        if (error instanceof PublicationError) throw error
        if (error?.name === 'AbortError')
            throw new PublicationError('Opening the book was cancelled.', { cause: error })
        throw new PublicationError('The original book could not be read.', { cause: error })
    } finally {
        unlink()
    }
}

const FB2_BODY_ELEMENTS = new Set([
    'body', 'section', 'title', 'epigraph', 'image', 'annotation', 'p', 'poem',
    'subtitle', 'cite', 'empty-line', 'table', 'tr', 'th', 'td', 'text-author',
    'date', 'stanza', 'v', 'strong', 'emphasis', 'style', 'a', 'strikethrough',
    'sub', 'sup', 'code', 'br',
])
const FB2_METADATA_ELEMENTS = new Set([
    'description', 'title-info', 'src-title-info', 'document-info', 'publish-info',
    'genre', 'author', 'translator', 'book-title', 'book-name', 'annotation', 'keywords',
    'date', 'coverpage', 'image', 'lang', 'src-lang', 'sequence', 'first-name', 'middle-name',
    'last-name', 'nickname', 'email', 'home-page', 'id', 'version', 'program-used',
    'src-url', 'src-ocr', 'publisher', 'city', 'year', 'isbn', 'history',
])
const FB2_INLINE_ELEMENTS = new Set([
    'strong', 'emphasis', 'style', 'a', 'strikethrough', 'sub', 'sup', 'code', 'image',
])
const FB2_DANGEROUS_ELEMENTS = new Set([
    'script', 'style', 'iframe', 'object', 'embed', 'form', 'svg', 'math', 'video', 'audio',
    'canvas', 'template', 'link', 'base', 'meta', 'input', 'button', 'select', 'textarea',
])
const FB2_BODY_BOUNDARIES = new Set(['image', 'title', 'epigraph', 'section'])
const FB2_FLOW_BLOCKS = new Set([
    'section', 'title', 'epigraph', 'image', 'annotation', 'p', 'poem', 'subtitle', 'cite',
    'empty-line', 'table', 'text-author',
])

const makeSafeFB2 = async (file, signal) => {
    checkAbort(signal)
    const source = await decodeXMLBlob(file, 'FB2', signal)
    checkAbort(signal)
    const original = parseFB2XML(source)
    const root = original.documentElement
    if (root.localName?.toLowerCase() !== 'fictionbook')
        fail('The publication does not have a recognizable FB2 root.')
    const pendingNodes = [[root, 0]]
    let sourceNodeCount = 0
    while (pendingNodes.length) {
        const [node, depth] = pendingNodes.pop()
        if (++sourceNodeCount > LIMITS.fb2Nodes || depth > LIMITS.fb2Depth)
            fail('The FB2 publication structure exceeds reader limits.')
        if (sourceNodeCount % 2048 === 0) checkAbort(signal)
        for (let index = node.childNodes.length - 1; index >= 0; index--)
            pendingNodes.push([node.childNodes[index], depth + 1])
    }

    const fb2Name = element => element.localName?.toLowerCase() ?? ''
    const rootChildren = elementChildren(root)
    const descriptions = rootChildren.filter(element => fb2Name(element) === 'description')
    const bodies = rootChildren.filter(element => fb2Name(element) === 'body')
    const binaries = rootChildren.filter(element => fb2Name(element) === 'binary')
    const namespace = FB2_NS
    const isDroppedElement = element => {
        const name = fb2Name(element)
        return FB2_DANGEROUS_ELEMENTS.has(name)
            && !(name === 'style' && element.namespaceURI === namespace)
    }
    const hasDroppedAncestor = element => {
        for (let current = element; current && current !== root; current = current.parentElement)
            if (isDroppedElement(current)) return true
        return false
    }
    const canonicalNames = new Set([
        ...FB2_BODY_ELEMENTS, ...FB2_METADATA_ELEMENTS, 'binary', 'br', 'v',
    ])
    const idWinners = new Map()
    const claimedIDs = new Set()
    const claimID = element => {
        if (hasDroppedAncestor(element) || !canonicalNames.has(fb2Name(element))) return
        const id = element.getAttribute('id')
        if (!safeFragment(id) || claimedIDs.has(id)) return
        claimedIDs.add(id)
        idWinners.set(element, id)
    }
    // Binary IDs are the targets of image references. Claim them before other
    // elements so a decorative element cannot hide a referenced binary.
    for (const binary of binaries) claimID(binary)
    for (const element of root.getElementsByTagName('*')) claimID(element)

    const safe = document.implementation.createDocument(namespace, 'FictionBook')
    const safeRoot = safe.documentElement
    safeRoot.setAttributeNS('http://www.w3.org/2000/xmlns/', 'xmlns:l', XLINK_NS)
    const copyID = (sourceElement, targetElement) => {
        const id = idWinners.get(sourceElement)
        if (id) targetElement.setAttribute('id', id)
    }

    const direct = (element, name) => elementChildren(element)
        .filter(childElement => fb2Name(childElement) === name)
    const safeText = element => {
        let value = ''
        const collect = node => {
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                value += node.data
                return
            }
            if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)
                || fb2Name(node) === 'style') return
            for (const childNode of node.childNodes) collect(childNode)
        }
        collect(element)
        return value.replace(/[\t\n\f\r ]+/g, ' ').trim()
    }
    const metadataText = (element, maximum = 4096) => {
        const value = safeText(element).slice(0, maximum)
        return /[\u0000-\u001f\u007f]/u.test(value) ? '' : value
    }
    const attributeText = (element, name, maximum) => {
        const value = element.getAttribute(name)?.trim() ?? ''
        if (!value || value.length > maximum || /[\u0000-\u001f\u007f]/u.test(value)) return ''
        return value
    }
    const readFragment = element => {
        const href = element.getAttributeNS(XLINK_NS, 'href') || element.getAttribute('href')
        if (typeof href !== 'string' || !href.startsWith('#')) return null
        let fragment
        try {
            fragment = decodeURIComponent(href.slice(1))
        } catch {
            return null
        }
        return safeFragment(fragment) ? fragment : null
    }

    const binaryByID = new Map()
    for (const binary of binaries) {
        const id = idWinners.get(binary)
        if (id && !binaryByID.has(id) && !elementChildren(binary).length)
            binaryByID.set(id, binary)
    }
    const imageSources = []
    const collectImageSources = node => {
        if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) return
        if (fb2Name(node) === 'image') imageSources.push(node)
        for (const childNode of node.childNodes) collectImageSources(childNode)
    }
    for (const container of [...bodies, ...descriptions]) collectImageSources(container)
    const referenced = new Set()
    for (const image of imageSources) {
        const id = readFragment(image)
        if (id && binaryByID.has(id)) referenced.add(id)
    }

    const decodeBinary = async binary => {
        const encoded = binary.textContent.replace(/[\t\n\f\r ]+/g, '')
        if (!encoded || encoded.length > Math.ceil(LIMITS.sourceBytes * 4 / 3) + 4
            || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) return null
        try {
            const raw = atob(encoded)
            return Uint8Array.from(raw, character => character.charCodeAt(0))
        } catch {
            return null
        }
    }
    const candidateBinaries = new Map()
    for (const id of referenced) {
        checkAbort(signal)
        const bytes = await decodeBinary(binaryByID.get(id))
        if (!bytes) continue
        try {
            candidateBinaries.set(id, await canonicalizeFB2RasterImage(
                bytes, binaryByID.get(id).getAttribute('content-type')))
        } catch (error) {
            if (!(error instanceof PublicationError)) throw error
        }
        checkAbort(signal)
    }

    const appendSafeText = (parent, name, sourceElement, maximum = 4096) => {
        const value = metadataText(sourceElement, maximum)
        if (!value) return null
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        result.textContent = value
        parent.append(result)
        return result
    }
    const firstSource = (elements, name) => elements
        .flatMap(element => direct(element, name)).find(element => metadataText(element))
    const appendFirstText = (parent, elements, name, maximum = 4096) => {
        const sourceElement = firstSource(elements, name)
        return sourceElement ? appendSafeText(parent, name, sourceElement, maximum) : null
    }
    const appendAllText = (parent, elements, name, maximum = 4096) => {
        for (const sourceElement of elements.flatMap(element => direct(element, name)))
            if (metadataText(sourceElement)) appendSafeText(parent, name, sourceElement, maximum)
    }
    const makePerson = (sourceElement, name) => {
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        const fields = ['first-name', 'middle-name', 'last-name', 'nickname']
        for (const field of fields) {
            const sourceField = direct(sourceElement, field).find(element => metadataText(element))
            if (sourceField) appendSafeText(result, field, sourceField)
        }
        for (const field of ['home-page', 'email'])
            for (const sourceField of direct(sourceElement, field))
                if (metadataText(sourceField)) appendSafeText(result, field, sourceField)
        const sourceID = direct(sourceElement, 'id').find(element => metadataText(element))
        if (sourceID) appendSafeText(result, 'id', sourceID)
        if (!result.children.length) {
            const value = metadataText(sourceElement)
            if (!value) return null
            const nickname = safe.createElementNS(namespace, 'nickname')
            nickname.textContent = value
            result.append(nickname)
        }
        return result
    }
    const appendPeople = (parent, elements, name) => {
        for (const sourceElement of elements.flatMap(element => direct(element, name))) {
            const person = makePerson(sourceElement, name)
            if (person) parent.append(person)
        }
    }
    const retainedBinaries = new Set()
    const retainedRasterBudget = createRasterBudget()
    const makeImage = sourceElement => {
        const id = readFragment(sourceElement)
        const decoded = id ? candidateBinaries.get(id) : null
        if (!decoded) return null
        if (!retainedBinaries.has(id)) {
            if (retainedRasterBudget.pixelFrames + decoded.pixelFrames
                    > LIMITS.publicationImagePixelFrames
                || retainedRasterBudget.frames + decoded.frames
                    > LIMITS.publicationImageFrames) return null
            retainedRasterBudget.pixelFrames += decoded.pixelFrames
            retainedRasterBudget.frames += decoded.frames
            retainedBinaries.add(id)
        }
        const image = safe.createElementNS(namespace, 'image')
        copyID(sourceElement, image)
        image.setAttributeNS(XLINK_NS, 'l:href', `#${id}`)
        for (const attribute of ['alt', 'title']) {
            const value = attributeText(sourceElement, attribute, 1024)
            if (value) image.setAttribute(attribute, value)
        }
        return image
    }
    function hasUsableContent(node) {
        if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
            return hasVisibleUnicodeText(node.data)
        if (node.nodeType !== Node.ELEMENT_NODE) return false
        if (fb2Name(node) === 'image') return true
        return [...node.childNodes].some(hasUsableContent)
    }
    function appendInlineNode(node, parent) {
        if (isDroppedElement(node)) return false
        const name = fb2Name(node)
        if (name === 'image') {
            const image = makeImage(node)
            if (!image) return false
            parent.append(image)
            return true
        }
        if (name === 'br') {
            parent.append(safe.createTextNode('\n'))
            return false
        }
        if (name !== 'a' && !FB2_INLINE_ELEMENTS.has(name))
            return appendInline(node, parent)
        const fragment = name === 'a' ? readFragment(node) : null
        const result = safe.createElementNS(namespace, name === 'a' && !fragment ? 'style' : name)
        copyID(node, result)
        if (name === 'a' && fragment) {
            result.setAttributeNS(XLINK_NS, 'l:href', `#${fragment}`)
            if (node.getAttribute('type') === 'note') result.setAttribute('type', 'note')
        }
        appendInline(node, result)
        if (!result.childNodes.length) return false
        parent.append(result)
        return hasUsableContent(result)
    }
    function appendInline(sourceElement, parent) {
        let added = false
        for (const node of sourceElement.childNodes) {
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                parent.append(safe.createTextNode(node.data))
                added ||= hasVisibleUnicodeText(node.data)
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                const childAdded = appendInlineNode(node, parent)
                added ||= childAdded
            }
        }
        return added
    }
    function makeParagraph(sourceElement) {
        const result = safe.createElementNS(namespace, 'p')
        copyID(sourceElement, result)
        appendInline(sourceElement, result)
        return hasUsableContent(result) ? result : null
    }
    function copyEmptyLine(sourceElement) {
        const result = safe.createElementNS(namespace, 'empty-line')
        copyID(sourceElement, result)
        return result
    }
    function copySimpleInlineBlock(name, sourceElement) {
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        if (name === 'date') {
            const value = attributeText(sourceElement, 'value', 128)
            if (value) result.setAttribute('value', value)
        }
        appendInline(sourceElement, result)
        return result
    }
    function copyTitle(sourceElement) {
        const result = safe.createElementNS(namespace, 'title')
        copyID(sourceElement, result)
        let paragraph = null
        const flush = () => {
            if (paragraph && hasUsableContent(paragraph)) result.append(paragraph)
            paragraph = null
        }
        const appendRun = node => {
            paragraph ??= safe.createElementNS(namespace, 'p')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                paragraph.append(safe.createTextNode(node.data))
            else appendInlineNode(node, paragraph)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (name === 'p') {
                    flush()
                    const copied = makeParagraph(node)
                    if (copied) result.append(copied)
                } else if (name === 'empty-line' || name === 'br') {
                    flush()
                    result.append(copyEmptyLine(node))
                } else if (name === 'image') {
                    flush()
                    const copied = makeParagraph(node)
                    if (copied) result.append(copied)
                } else if (name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flush()
        }
        consume(sourceElement)
        return result
    }
    function copySectionLike(name, sourceElement) {
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        appendFlowChildren(sourceElement, result)
        return result
    }
    function copySection(sourceElement) {
        return copySectionLike('section', sourceElement)
    }
    function copyEpigraph(sourceElement) {
        return copySectionLike('epigraph', sourceElement)
    }
    function copyCite(sourceElement) {
        return copySectionLike('cite', sourceElement)
    }
    function copyAnnotation(sourceElement) {
        return copySectionLike('annotation', sourceElement)
    }
    function copyV(sourceElement) {
        const result = safe.createElementNS(namespace, 'v')
        copyID(sourceElement, result)
        appendInline(sourceElement, result)
        return result
    }
    function copyStanzaTitle(sourceElement) {
        return copyTitle(sourceElement)
    }
    function copyStanza(sourceElement) {
        const result = safe.createElementNS(namespace, 'stanza')
        copyID(sourceElement, result)
        let repairedV = null
        const flush = () => {
            if (repairedV) result.append(repairedV)
            repairedV = null
        }
        const appendRun = node => {
            repairedV ??= safe.createElementNS(namespace, 'v')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                repairedV.append(safe.createTextNode(node.data))
            else appendInlineNode(node, repairedV)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (name === 'v') {
                    flush()
                    result.append(copyV(node))
                } else if (name === 'title') {
                    flush()
                    result.append(copyStanzaTitle(node))
                } else if (name === 'subtitle') {
                    flush()
                    result.append(copySimpleInlineBlock('subtitle', node))
                } else if (name === 'br' || name === 'empty-line') {
                    flush()
                    result.append(safe.createElementNS(namespace, 'v'))
                } else if (name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flush()
        }
        consume(sourceElement)
        return result
    }
    function copyPoem(sourceElement) {
        const result = safe.createElementNS(namespace, 'poem')
        copyID(sourceElement, result)
        let repairedStanza = null
        let repairedV = null
        const flushV = () => {
            if (repairedV) repairedStanza.append(repairedV)
            repairedV = null
        }
        const flushStanza = () => {
            if (repairedStanza) {
                flushV()
                result.append(repairedStanza)
            }
            repairedStanza = null
        }
        const appendRun = node => {
            repairedStanza ??= safe.createElementNS(namespace, 'stanza')
            repairedV ??= safe.createElementNS(namespace, 'v')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                repairedV.append(safe.createTextNode(node.data))
            else appendInlineNode(node, repairedV)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (name === 'stanza') {
                    flushStanza()
                    result.append(copyStanza(node))
                } else if (name === 'epigraph') {
                    flushStanza()
                    result.append(copyEpigraph(node))
                } else if (name === 'subtitle' || name === 'title') {
                    flushStanza()
                    result.append(copySimpleInlineBlock('subtitle', node))
                } else if (name === 'text-author' || name === 'date') {
                    flushStanza()
                    result.append(copySimpleInlineBlock(name, node))
                } else if (name === 'v') {
                    flushStanza()
                    const stanza = safe.createElementNS(namespace, 'stanza')
                    stanza.append(copyV(node))
                    result.append(stanza)
                } else if (name === 'br' || name === 'empty-line') {
                    flushV()
                    repairedStanza ??= safe.createElementNS(namespace, 'stanza')
                    repairedStanza.append(safe.createElementNS(namespace, 'v'))
                } else if (name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flushStanza()
        }
        consume(sourceElement)
        return result
    }
    function copyCell(sourceElement, name = fb2Name(sourceElement)) {
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        for (const attribute of ['colspan', 'rowspan']) {
            const value = attributeText(sourceElement, attribute, 3)
            const number = Number(value)
            if (Number.isInteger(number) && number >= 1 && number <= 100)
                result.setAttribute(attribute, String(number))
        }
        for (const attribute of ['align', 'valign']) {
            const value = attributeText(sourceElement, attribute, 32)
            if (value && safeToken(value)) result.setAttribute(attribute, value)
        }
        appendInline(sourceElement, result)
        return result
    }
    function copyTableRow(sourceElement) {
        const result = safe.createElementNS(namespace, 'tr')
        copyID(sourceElement, result)
        const align = attributeText(sourceElement, 'align', 32)
        if (align && safeToken(align)) result.setAttribute('align', align)
        let repairedCell = null
        const flush = () => {
            if (repairedCell) result.append(repairedCell)
            repairedCell = null
        }
        const appendRun = node => {
            repairedCell ??= safe.createElementNS(namespace, 'td')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                repairedCell.append(safe.createTextNode(node.data))
            else appendInlineNode(node, repairedCell)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (name === 'th' || name === 'td') {
                    flush()
                    result.append(copyCell(node, name))
                } else if (name === 'br') {
                    appendRun(node)
                } else if (name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flush()
        }
        consume(sourceElement)
        return result
    }
    function copyTable(sourceElement) {
        const result = safe.createElementNS(namespace, 'table')
        copyID(sourceElement, result)
        let repairedRow = null
        let repairedCell = null
        const flush = () => {
            if (repairedRow) {
                if (repairedCell) repairedRow.append(repairedCell)
                result.append(repairedRow)
            }
            repairedRow = null
            repairedCell = null
        }
        const appendRun = node => {
            repairedRow ??= safe.createElementNS(namespace, 'tr')
            repairedCell ??= safe.createElementNS(namespace, 'td')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                repairedCell.append(safe.createTextNode(node.data))
            else appendInlineNode(node, repairedCell)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (name === 'tr') {
                    flush()
                    result.append(copyTableRow(node))
                } else if (name === 'th' || name === 'td') {
                    flush()
                    const row = safe.createElementNS(namespace, 'tr')
                    row.append(copyCell(node, name))
                    result.append(row)
                } else if (name === 'br' || name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flush()
        }
        consume(sourceElement)
        return result
    }
    function makeRepairPoem(sourceElement) {
        const result = safe.createElementNS(namespace, 'poem')
        const stanza = fb2Name(sourceElement) === 'stanza'
            ? copyStanza(sourceElement) : safe.createElementNS(namespace, 'stanza')
        if (fb2Name(sourceElement) === 'v') stanza.append(copyV(sourceElement))
        result.append(stanza)
        return result
    }
    function makeRepairTable(sourceElement) {
        const result = safe.createElementNS(namespace, 'table')
        const name = fb2Name(sourceElement)
        if (name === 'tr') result.append(copyTableRow(sourceElement))
        else {
            const row = safe.createElementNS(namespace, 'tr')
            row.append(copyCell(sourceElement, name === 'th' ? 'th' : 'td'))
            result.append(row)
        }
        return result
    }
    function copyFlowBlock(sourceElement) {
        const name = fb2Name(sourceElement)
        if (name === 'section') return copySection(sourceElement)
        if (name === 'title') return copyTitle(sourceElement)
        if (name === 'epigraph') return copyEpigraph(sourceElement)
        if (name === 'image') return makeImage(sourceElement)
        if (name === 'annotation') return copyAnnotation(sourceElement)
        if (name === 'p') return makeParagraph(sourceElement)
        if (name === 'poem') return copyPoem(sourceElement)
        if (name === 'subtitle' || name === 'text-author')
            return copySimpleInlineBlock(name, sourceElement)
        if (name === 'cite') return copyCite(sourceElement)
        if (name === 'empty-line') return copyEmptyLine(sourceElement)
        if (name === 'table') return copyTable(sourceElement)
        if (name === 'date') return makeParagraph(sourceElement)
        if (name === 'stanza' || name === 'v') return makeRepairPoem(sourceElement)
        if (['tr', 'th', 'td'].includes(name)) return makeRepairTable(sourceElement)
        return null
    }
    function appendFlowChildren(sourceElement, parent, { bodyBoundaries = false } = {}) {
        let paragraph = null
        let repairSection = null
        const outputParent = () => {
            if (!bodyBoundaries) return parent
            repairSection ??= safe.createElementNS(namespace, 'section')
            return repairSection
        }
        const flushRepair = () => {
            if (!repairSection) return
            if (hasUsableContent(repairSection)) parent.append(repairSection)
            repairSection = null
        }
        const flushParagraph = () => {
            if (paragraph && hasUsableContent(paragraph)) outputParent().append(paragraph)
            paragraph = null
        }
        const appendRun = node => {
            paragraph ??= safe.createElementNS(namespace, 'p')
            if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                paragraph.append(safe.createTextNode(node.data))
            else appendInlineNode(node, paragraph)
        }
        const appendBlock = node => {
            if (node) outputParent().append(node)
        }
        const consume = container => {
            for (const node of container.childNodes) {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
                    appendRun(node)
                    continue
                }
                if (node.nodeType !== Node.ELEMENT_NODE || isDroppedElement(node)) continue
                const name = fb2Name(node)
                if (bodyBoundaries && FB2_BODY_BOUNDARIES.has(name)) {
                    flushParagraph()
                    flushRepair()
                    const copied = copyFlowBlock(node)
                    if (copied) parent.append(copied)
                } else if (name === 'br') {
                    flushParagraph()
                    appendBlock(copyEmptyLine(node))
                } else if (FB2_FLOW_BLOCKS.has(name)) {
                    flushParagraph()
                    appendBlock(copyFlowBlock(node))
                } else if (['date', 'stanza', 'v', 'tr', 'th', 'td'].includes(name)) {
                    flushParagraph()
                    appendBlock(copyFlowBlock(node))
                } else if (name === 'a' || FB2_INLINE_ELEMENTS.has(name)) {
                    appendRun(node)
                } else {
                    consume(node)
                }
            }
            flushParagraph()
        }
        consume(sourceElement)
        flushRepair()
    }
    function copyBody(sourceElement) {
        const result = safe.createElementNS(namespace, 'body')
        copyID(sourceElement, result)
        const bodyName = attributeText(sourceElement, 'name', 256)
        if (bodyName && safeToken(bodyName)) result.setAttribute('name', bodyName)
        appendFlowChildren(sourceElement, result, { bodyBoundaries: true })
        return result
    }

    const titleInfoSources = descriptions.flatMap(description => direct(description, 'title-info'))
    const sourceCover = elements => elements
        .flatMap(element => direct(element, 'coverpage'))
        .flatMap(coverpage => direct(coverpage, 'image'))
        .find(image => {
            const id = readFragment(image)
            return id && candidateBinaries.has(id)
        })
    const makeCoverpage = elements => {
        const sourceImage = sourceCover(elements)
        if (!sourceImage) return null
        const result = safe.createElementNS(namespace, 'coverpage')
        const sourcePage = elements.flatMap(element => direct(element, 'coverpage'))
            .find(coverpage => direct(coverpage, 'image').includes(sourceImage))
        if (sourcePage) copyID(sourcePage, result)
        const image = makeImage(sourceImage)
        if (image) result.append(image)
        return image ? result : null
    }
    const makeAnnotation = sourceElement => {
        const result = copyAnnotation(sourceElement)
        return hasUsableContent(result) ? result : null
    }
    const appendFirstAnnotation = (parent, elements) => {
        for (const sourceElement of elements.flatMap(element => direct(element, 'annotation'))) {
            const annotation = makeAnnotation(sourceElement)
            if (!annotation) continue
            parent.append(annotation)
            break
        }
    }
    const appendFirstDate = (parent, elements, name = 'date') => {
        const sourceElement = elements.flatMap(element => direct(element, name))
            .find(element => metadataText(element) || attributeText(element, 'value', 128))
        if (!sourceElement) return null
        const value = metadataText(sourceElement)
        const attribute = attributeText(sourceElement, 'value', 128)
        const result = safe.createElementNS(namespace, name)
        copyID(sourceElement, result)
        if (value) result.textContent = value
        if (attribute) result.setAttribute('value', attribute)
        parent.append(result)
        return result
    }
    const appendSequences = (parent, elements) => {
        for (const sourceElement of elements.flatMap(element => direct(element, 'sequence'))) {
            const name = attributeText(sourceElement, 'name', 256)
            const number = attributeText(sourceElement, 'number', 256)
            if (!name && !number) continue
            const result = safe.createElementNS(namespace, 'sequence')
            copyID(sourceElement, result)
            if (name) result.setAttribute('name', name)
            if (number) result.setAttribute('number', number)
            parent.append(result)
        }
    }
    const makeTitleInfo = (elements, name, force = false) => {
        if (!elements.length && !force) return null
        const result = safe.createElementNS(namespace, name)
        if (elements[0]) copyID(elements[0], result)
        appendAllText(result, elements, 'genre')
        appendPeople(result, elements, 'author')
        appendFirstText(result, elements, 'book-title')
        appendFirstAnnotation(result, elements)
        appendFirstText(result, elements, 'keywords')
        appendFirstDate(result, elements)
        const cover = makeCoverpage(elements)
        if (cover) result.append(cover)
        appendFirstText(result, elements, 'lang', 64)
        appendFirstText(result, elements, 'src-lang', 64)
        appendPeople(result, elements, 'translator')
        appendSequences(result, elements)
        return result
    }
    const makeDocumentInfo = elements => {
        const result = safe.createElementNS(namespace, 'document-info')
        if (elements[0]) copyID(elements[0], result)
        appendPeople(result, elements, 'author')
        appendFirstText(result, elements, 'program-used')
        appendAllText(result, elements, 'src-url')
        appendFirstDate(result, elements)
        appendFirstText(result, elements, 'src-ocr')
        const identifier = firstSource(elements, 'id')
        if (identifier) appendSafeText(result, 'id', identifier)
        appendFirstText(result, elements, 'version')
        const historySource = firstSource(elements, 'history')
        if (historySource) {
            const history = copySectionLike('history', historySource)
            if (hasUsableContent(history)) result.append(history)
        }
        appendAllText(result, elements, 'publisher')
        return result
    }
    const makePublishInfo = elements => {
        if (!elements.length) return null
        const result = safe.createElementNS(namespace, 'publish-info')
        copyID(elements[0], result)
        appendFirstText(result, elements, 'book-name')
        appendFirstText(result, elements, 'publisher')
        appendFirstText(result, elements, 'city')
        appendFirstText(result, elements, 'year')
        appendFirstText(result, elements, 'isbn')
        appendSequences(result, elements)
        return result.children.length ? result : null
    }

    const safeDescription = safe.createElementNS(namespace, 'description')
    if (descriptions[0]) copyID(descriptions[0], safeDescription)
    safeDescription.append(makeTitleInfo(titleInfoSources, 'title-info', true))
    const sourceTitleInfoSources = descriptions.flatMap(
        description => direct(description, 'src-title-info'))
    const sourceTitleInfo = makeTitleInfo(sourceTitleInfoSources, 'src-title-info')
    if (sourceTitleInfo?.children.length) safeDescription.append(sourceTitleInfo)
    const documentInfoSources = descriptions.flatMap(
        description => direct(description, 'document-info'))
    safeDescription.append(makeDocumentInfo(documentInfoSources))
    const publishInfoSources = descriptions.flatMap(
        description => direct(description, 'publish-info'))
    const publishInfo = makePublishInfo(publishInfoSources)
    if (publishInfo) safeDescription.append(publishInfo)
    safeRoot.append(safeDescription)

    const canonicalBodies = []
    for (const sourceBody of bodies) {
        const body = copyBody(sourceBody)
        if (!hasUsableContent(body)) continue
        canonicalBodies.push(body)
    }
    if (!canonicalBodies.length)
        fail('The FB2 publication has no usable body content.')
    safeRoot.append(...canonicalBodies)

    const encodeBase64 = bytes => {
        let encoded = ''
        for (let offset = 0; offset < bytes.length; offset += 0x8000)
            encoded += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
        return btoa(encoded)
    }
    for (const id of retainedBinaries) {
        checkAbort(signal)
        const decoded = candidateBinaries.get(id)
        const binary = safe.createElementNS(namespace, 'binary')
        binary.setAttribute('id', id)
        binary.setAttribute('content-type', decoded.type)
        binary.textContent = encodeBase64(decoded.bytes)
        safeRoot.append(binary)
    }

    const serialized = new XMLSerializer().serializeToString(safe)
    const blob = new Blob([serialized], { type: MEDIA_TYPES.fb2 })
    let book
    try {
        book = await makeFB2(blob)
        checkAbort(signal)
        book.getCover = () => null
        if (!book.sections?.length)
            fail('The FB2 publication has no readable sections.')
        return { book, zipReader: null, adapterURLs: [] }
    } catch (error) {
        book?.destroy?.()
        throw error
    }
}

const readSafeZip = async (file, signal) => {
    const { configure, ZipReader, BlobReader, BlobWriter } =
        await import('../vendor/foliate/vendor/zip.js')
    checkAbort(signal)
    configure({ useWebWorkers: false })
    const reader = new ZipReader(new BlobReader(file))
    const controller = new AbortController()
    const unlink = linkAbortSignal(controller, signal)
    try {
        const names = new Map()
        let declaredTotal = 0
        let entryCount = 0
        let firstEntry
        for await (const entry of reader.getEntriesGenerator({ signal: controller.signal })) {
            checkAbort(signal)
            entryCount++
            if (entryCount > LIMITS.zipEntries) {
                controller.abort()
                fail('The EPUB has an invalid number of ZIP entries.')
            }
            firstEntry ??= entry
            const rawName = entry.directory && entry.filename.endsWith('/')
                ? entry.filename.slice(0, -1) : entry.filename
            const name = canonicalArchivePath(rawName)
            if (names.has(name)) fail('The EPUB contains duplicate normalized paths.')
            names.set(name, entry)
            if (entry.encrypted) fail('Encrypted EPUB files are not supported.')
            if (![0, 8].includes(entry.compressionMethod))
                fail('The EPUB uses unsupported ZIP compression.')
            if (!Number.isSafeInteger(entry.uncompressedSize) || entry.uncompressedSize < 0)
                fail('The EPUB has an invalid expanded size.')
            declaredTotal += entry.uncompressedSize
            if (declaredTotal > LIMITS.expandedBytes)
                fail('The EPUB exceeds the 128 MiB expanded-size limit.')
        }
        if (!entryCount) fail('The EPUB has an invalid number of ZIP entries.')
        const firstName = firstEntry.directory ? '' : canonicalArchivePath(firstEntry.filename)
        if (firstName !== 'mimetype' || firstEntry.compressionMethod !== 0)
            fail('The EPUB mimetype entry is invalid.')

        const blobs = new Map()
        let actualTotal = 0
        for (const [name, entry] of names) {
            if (entry.directory) continue
            const completed = actualTotal
            const blob = await entry.getData(new BlobWriter(), {
                signal: controller.signal,
                onprogress: size => {
                    if (completed + size > LIMITS.expandedBytes) controller.abort()
                },
            })
            checkAbort(signal)
            actualTotal += blob.size
            if (actualTotal > LIMITS.expandedBytes) {
                controller.abort()
                fail('The EPUB exceeds the 128 MiB expanded-size limit.')
            }
            blobs.set(name, blob)
        }
        const mimetype = await blobs.get('mimetype')?.text()
        checkAbort(signal)
        if (mimetype !== MEDIA_TYPES.epub)
            fail('The EPUB mimetype entry is invalid.')
        return { reader, blobs }
    } catch (error) {
        controller.abort()
        try { await reader.close() } catch { /* Preserve the validation error. */ }
        if (error instanceof PublicationError) throw error
        if (error?.name === 'AbortError')
            throw new PublicationError('EPUB expansion exceeded a limit or was cancelled.', { cause: error })
        throw new PublicationError('The EPUB ZIP archive is invalid.', { cause: error })
    } finally {
        unlink()
    }
}

const XHTML_ELEMENTS = new Set([
    'address', 'article', 'aside', 'blockquote', 'body', 'br', 'caption', 'cite',
    'code', 'col', 'colgroup', 'dd', 'del', 'dfn', 'div', 'dl', 'dt', 'em',
    'figcaption', 'figure', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header',
    'hr', 'i', 'ins', 'kbd', 'li', 'main', 'mark', 'nav', 'ol', 'p', 'pre', 'q',
    'rp', 'rt', 'ruby', 's', 'samp', 'section', 'small', 'span', 'strong', 'sub',
    'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul', 'var',
    'wbr', 'a', 'img',
])
const DROP_CONTENT_ELEMENTS = new Set([
    'script', 'noscript', 'iframe', 'frame', 'frameset', 'object', 'embed', 'form',
    'input', 'button', 'select', 'option', 'textarea', 'audio', 'video', 'source',
    'track', 'canvas', 'svg', 'math', 'template', 'portal', 'base', 'meta', 'link',
    'style',
])
const GLOBAL_ARIA = new Set([
    'aria-label', 'aria-labelledby', 'aria-describedby', 'aria-hidden',
])
const SAFE_ROLES = new Set([
    'article', 'blockquote', 'caption', 'cell', 'columnheader', 'definition',
    'doc-abstract', 'doc-acknowledgments', 'doc-afterword', 'doc-appendix',
    'doc-backlink', 'doc-biblioentry', 'doc-bibliography', 'doc-chapter',
    'doc-conclusion', 'doc-cover', 'doc-dedication', 'doc-endnote', 'doc-endnotes',
    'doc-epigraph', 'doc-foreword', 'doc-glossary', 'doc-index', 'doc-introduction',
    'doc-noteref', 'doc-notice', 'doc-pagebreak', 'doc-part', 'doc-preface',
    'doc-prologue', 'doc-pullquote', 'doc-qna', 'doc-subtitle', 'document',
    'figure', 'heading', 'img', 'list', 'listitem', 'navigation', 'note', 'row',
    'rowgroup', 'rowheader', 'table',
])
const CSS_PROPERTIES = new Set([
    'background-color', 'background-image', 'border', 'border-bottom',
    'border-bottom-color', 'border-bottom-style', 'border-bottom-width',
    'border-collapse', 'border-color', 'border-left', 'border-left-color',
    'border-left-style', 'border-left-width', 'border-radius', 'border-right',
    'border-right-color', 'border-right-style', 'border-right-width', 'border-spacing',
    'border-style', 'border-top', 'border-top-color', 'border-top-style',
    'border-top-width', 'border-width', 'box-sizing', 'caption-side', 'clear', 'color',
    'display', 'empty-cells', 'float', 'font-family', 'font-style', 'font-variant',
    'font-weight', 'height', 'hyphens', 'letter-spacing', 'line-height',
    'list-style', 'list-style-position', 'list-style-type', 'margin', 'margin-bottom',
    'margin-left', 'margin-right', 'margin-top', 'max-height', 'max-width', 'min-height',
    'min-width', 'orphans', 'overflow-wrap', 'padding', 'padding-bottom', 'padding-left',
    'padding-right', 'padding-top', 'page-break-after', 'page-break-before',
    'page-break-inside', 'text-align', 'text-decoration', 'text-indent',
    'text-transform', 'vertical-align', 'white-space', 'widows', 'width', 'word-break',
    'word-spacing', 'writing-mode',
])

const containsCSSAtRule = source => {
    let quote = '', comment = false
    for (let index = 0; index < source.length; index++) {
        const current = source[index], next = source[index + 1]
        if (comment) {
            if (current === '*' && next === '/') { comment = false; index++ }
            continue
        }
        if (!quote && current === '/' && next === '*') { comment = true; index++; continue }
        if (quote) {
            if (current === '\\') index++
            else if (current === quote) quote = ''
            continue
        }
        if (current === '"' || current === "'") quote = current
        else if (current === '@') return true
    }
    return false
}

const safeSelector = selector => {
    if (!selector || selector.length > LIMITS.selectorLength || selector.includes('\\')
        || /[\[\](){}]/.test(selector)
        || !/^[A-Za-z0-9_.*#,:+>~\s-]+$/.test(selector)) return false
    const pseudos = selector.match(/:[A-Za-z-]+/g) ?? []
    return pseudos.every(value => [
        ':first-child', ':last-child', ':first-of-type', ':last-of-type',
        ':only-child', ':only-of-type', ':empty', ':root',
    ].includes(value))
}

const cssFunctionsAreSafe = value => {
    let quote = ''
    for (let index = 0; index < value.length; index++) {
        const current = value[index]
        if (quote) {
            if (current === '\\') index++
            else if (current === quote) quote = ''
            continue
        }
        if (current === '"' || current === "'") { quote = current; continue }
        if (!/[A-Za-z-]/.test(current)) continue
        let end = index + 1
        while (end < value.length && /[A-Za-z0-9-]/.test(value[end])) end++
        let next = end
        while (/\s/.test(value[next] ?? '')) next++
        if (value[next] === '(') {
            const name = value.slice(index, end).toLowerCase()
            if (!['rgb', 'rgba', 'hsl', 'hsla'].includes(name)) return false
        }
        index = end - 1
    }
    return !quote
}

const ZERO_DIMENSION_PROPERTIES = new Set([
    'line-height', 'height', 'width', 'max-height', 'max-width',
    'min-height', 'min-width',
])
const isZeroCSSDimension = value => /^[+-]?(?:0+(?:\.0*)?|\.0+)(?:[a-z%]+)?$/i.test(value)
const isTransparentCSSColor = value => {
    const lower = value.toLowerCase().replace(/\s+/g, ' ').trim()
    return lower === 'transparent'
        || /^#[0-9a-f]{4}(?:[0-9a-f]{4})?$/i.test(lower)
        || /^(?:rgba|hsla)\(/.test(lower)
        || /^(?:rgb|hsl)\([^)]*\/[^)]*\)$/.test(lower)
}
const cssDeclarationHidesContent = (property, value) =>
    (property === 'display' && value.trim().toLowerCase() === 'none')
    || (ZERO_DIMENSION_PROPERTIES.has(property) && isZeroCSSDimension(value.trim()))
    || (property === 'color' && isTransparentCSSColor(value))

const parseSingleCSSURL = value => {
    let index = 0
    const skip = () => { while (/\s/.test(value[index] ?? '')) index++ }
    skip()
    if (value.slice(index, index + 3).toLowerCase() !== 'url') return null
    index += 3; skip()
    if (value[index++] !== '(') return null
    skip()
    const quote = value[index]
    if (quote !== '"' && quote !== "'") return null
    index++
    let result = ''
    while (index < value.length && value[index] !== quote) {
        if (value[index] === '\\' || value[index] === '\n' || value[index] === '\r') return null
        result += value[index++]
    }
    if (value[index++] !== quote) return null
    skip()
    if (value[index++] !== ')') return null
    skip()
    return index === value.length ? result : null
}

const parseCSSRules = source => {
    try {
        const sheet = new CSSStyleSheet()
        sheet.replaceSync(source)
        return [...sheet.cssRules]
    } catch (error) {
        throw new PublicationError('A publication stylesheet could not be parsed.', { cause: error })
    }
}

const decodeStylesheetBlob = async (blob, budget, signal) => {
    if (blob.size > LIMITS.stylesheetBytes
        || budget.input + blob.size > LIMITS.totalStylesheetBytes)
        fail('A publication stylesheet is too large.')
    const source = await blob.text()
    checkAbort(signal)
    return source
}

const sanitizeCSS = async (
    source, basePath, resolveImage, budget, signal, declarationsOnly = false,
) => {
    checkAbort(signal)
    const bytes = new TextEncoder().encode(source).byteLength
    if (bytes > LIMITS.stylesheetBytes) fail('A publication stylesheet is too large.')
    budget.input += bytes
    if (budget.input > LIMITS.totalStylesheetBytes)
        fail('The publication contains too much stylesheet data.')
    if (containsCSSAtRule(source)) return ''
    const wrapped = declarationsOnly ? `.sopds-inline{${source}}` : source
    const rules = parseCSSRules(wrapped)
    const output = []
    for (const rule of rules) {
        budget.rules++
        if (budget.rules > LIMITS.cssRules) fail('The publication has too many CSS rules.')
        if (rule.type !== 1) continue
        const selector = declarationsOnly ? '.sopds-inline' : rule.selectorText
        if (!declarationsOnly && !safeSelector(selector)) continue
        if (rule.style.length > LIMITS.declarationsPerRule)
            fail('A publication CSS rule has too many declarations.')
        const declarations = []
        for (const property of rule.style) {
            const lower = property.toLowerCase()
            if (!CSS_PROPERTIES.has(lower) || lower.startsWith('--')) continue
            const value = rule.style.getPropertyValue(property).trim()
            if (cssDeclarationHidesContent(lower, value)) continue
            let safeValue = value
            if (lower === 'background-image') {
                const reference = parseSingleCSSURL(value)
                if (!reference) continue
                const url = await resolveImage(reference, basePath)
                checkAbort(signal)
                if (!url) continue
                safeValue = `url("${url}")`
            } else if (!cssFunctionsAreSafe(value)) continue
            const priority = rule.style.getPropertyPriority(property) === 'important'
                ? ' !important' : ''
            declarations.push(`${lower}: ${safeValue}${priority}`)
        }
        if (!declarations.length) continue
        output.push(declarationsOnly
            ? declarations.join('; ')
            : `${selector} { ${declarations.join('; ')} }`)
    }
    const result = declarationsOnly ? (output[0] ?? '') : output.join('\n')
    budget.output += new TextEncoder().encode(result).byteLength
    if (budget.output > LIMITS.cssOutputBytes)
        fail('The sanitized publication CSS is too large.')
    return result
}

const chargeCSSUse = (source, budget) => {
    budget.generated += new TextEncoder().encode(source).byteLength
    if (budget.generated > LIMITS.cssOutputBytes)
        fail('The generated publication CSS is too large.')
}

const validateEPUBPackage = async (blobs, signal) => {
    if ([...blobs.keys()].some(name => name.toLowerCase() === 'meta-inf/encryption.xml'))
        fail('Encrypted EPUB files are not supported.')
    const containerSource = blobs.has('META-INF/container.xml')
        ? await decodeXMLBlob(blobs.get('META-INF/container.xml'), 'EPUB container', signal) : null
    checkAbort(signal)
    if (!containerSource) fail('The EPUB container document is missing.')
    const container = parseXML(containerSource, 'EPUB container')
    if (container.documentElement.localName !== 'container'
        || container.documentElement.namespaceURI !== CONTAINER_NS)
        fail('The EPUB container document is invalid.')
    for (const element of container.getElementsByTagName('*'))
        if (['rootfiles', 'rootfile'].includes(element.localName)
            && element.namespaceURI !== CONTAINER_NS)
            fail('The EPUB container structure has an invalid namespace.')
    const rootfilesElement = child(container.documentElement, 'rootfiles', CONTAINER_NS)
    const rootfiles = rootfilesElement
        ? children(rootfilesElement, 'rootfile', CONTAINER_NS)
            .filter(item => item.getAttribute('media-type') === 'application/oebps-package+xml')
        : []
    if (!rootfiles.length) fail('The EPUB container has no package document.')
    const rootfile = resolvePackageReference(requiredAttribute(rootfiles[0], 'full-path'), '')
    if (!rootfile || rootfile.fragment)
        fail('The EPUB container has an invalid package path.')
    const opfPath = rootfile.path
    const opfSource = blobs.has(opfPath)
        ? await decodeXMLBlob(blobs.get(opfPath), 'EPUB package', signal) : null
    checkAbort(signal)
    if (!opfSource) fail('The EPUB package document is missing.')
    const opf = parseXML(opfSource, 'EPUB package')
    const packageElement = opf.documentElement
    const version = requiredAttribute(packageElement, 'version')
    if (packageElement.localName !== 'package'
        || packageElement.namespaceURI !== OPF_NS
        || !/^(?:2(?:\.\d+)?|3(?:\.\d+)?)$/.test(version))
        fail('Only EPUB 2 and EPUB 3 package documents are supported.')
    const structural = new Set(['package', 'metadata', 'manifest', 'spine', 'item', 'itemref', 'meta'])
    for (const element of [packageElement, ...packageElement.getElementsByTagName('*')])
        if (structural.has(element.localName) && element.namespaceURI !== OPF_NS)
            fail('The EPUB package contains a structural element in an invalid namespace.')
    const requiredChild = name => {
        const matches = elementChildren(packageElement).filter(item => item.localName === name)
        if (matches.length !== 1 || matches[0].namespaceURI !== OPF_NS)
            fail('The EPUB package structure is incomplete.')
        return matches[0]
    }
    const metadata = requiredChild('metadata')
    const manifestElement = requiredChild('manifest')
    const spineElement = requiredChild('spine')
    for (const element of metadata.getElementsByTagName('*'))
        if (['title', 'language'].includes(element.localName)
            && element.namespaceURI !== DC_NS)
            fail('The EPUB Dublin Core metadata has an invalid namespace.')

    for (const meta of [...metadata.getElementsByTagNameNS(OPF_NS, 'meta')]) {
        const property = (meta.getAttribute('property') ?? '').toLowerCase()
        const name = (meta.getAttribute('name') ?? '').toLowerCase()
        const value = (meta.getAttribute('content') || text(meta)).trim().toLowerCase()
        if (((property === 'rendition:layout' || property.endsWith(':layout')
            || name === 'rendition:layout' || name.endsWith(':layout'))
            && value === 'pre-paginated')
            || (name === 'fixed-layout' && value === 'true'))
            fail('Fixed-layout EPUB files are not supported.')
    }
    for (const path of [
        'META-INF/com.apple.ibooks.display-options.xml',
        'META-INF/com.kobobooks.display-options.xml',
    ]) {
        if (!blobs.has(path)) continue
        const optionSource = await decodeXMLBlob(blobs.get(path), 'EPUB display options', signal)
        checkAbort(signal)
        const options = parseXML(optionSource, 'EPUB display options')
        for (const option of [...options.getElementsByTagNameNS('*', 'option')])
            if (option.getAttribute('name') === 'fixed-layout' && text(option) === 'true')
                fail('Fixed-layout EPUB files are not supported.')
    }

    const manifest = new Map()
    const manifestByPath = new Map()
    for (const item of children(manifestElement, 'item')) {
        const id = requiredAttribute(item, 'id')
        if (!safeToken(id) || manifest.has(id)) fail('The EPUB manifest has invalid IDs.')
        const resolved = resolvePackageReference(requiredAttribute(item, 'href'), opfPath)
        if (!resolved || resolved.fragment || !blobs.has(resolved.path)
            || manifestByPath.has(resolved.path))
            fail('The EPUB manifest contains an invalid resource path.')
        const record = {
            id,
            path: resolved.path,
            mediaType: normalizedMediaType(requiredAttribute(item, 'media-type')),
            properties: new Set((item.getAttribute('properties') ?? '').split(/\s+/).filter(Boolean)),
        }
        if (record.properties.has('remote-resources'))
            fail('EPUB remote resources are not supported.')
        if ([...record.properties].some(value => value.endsWith('layout-pre-paginated')))
            fail('Fixed-layout EPUB files are not supported.')
        manifest.set(id, record)
        manifestByPath.set(record.path, record)
    }

    const spine = []
    const spinePaths = new Set()
    for (const itemref of children(spineElement, 'itemref')) {
        const item = manifest.get(requiredAttribute(itemref, 'idref'))
        const properties = (itemref.getAttribute('properties') ?? '').split(/\s+/)
        const declaredLinear = (itemref.getAttribute('linear') ?? '').toLowerCase()
        if (!item || item.mediaType !== XHTML
            || (declaredLinear && !['yes', 'no'].includes(declaredLinear))
            || properties.some(value => value.endsWith('layout-pre-paginated')))
            fail('The EPUB spine is invalid or is not reflowable XHTML.')
        if (!spinePaths.has(item.path)) {
            spinePaths.add(item.path)
            spine.push({ item, linear: declaredLinear === 'no' ? 'no' : 'yes' })
        }
    }
    if (!spine.length) fail('The EPUB has no readable spine.')
    if (!spine.some(record => record.linear === 'yes'))
        fail('The EPUB spine has no linear reading order.')
    const documents = [...manifest.values()].filter(item => item.mediaType === XHTML)
    const navItems = [...manifest.values()].filter(item => item.properties.has('nav'))
    if (navItems.length > 1 || navItems.some(item => item.mediaType !== XHTML))
        fail('The EPUB navigation document is invalid.')
    const declaredNCX = manifest.get(spineElement.getAttribute('toc') ?? '')
    const ncxItem = declaredNCX
        ?? [...manifest.values()].find(item => item.mediaType === 'application/x-dtbncx+xml')
    if (declaredNCX && declaredNCX.mediaType !== 'application/x-dtbncx+xml')
        fail('The EPUB NCX document is invalid.')
    const pageProgression = spineElement.getAttribute('page-progression-direction') ?? ''
    return {
        opfPath,
        manifestByPath,
        spine,
        documents,
        navItem: navItems[0],
        ncxItem,
        metadata: {
            title: text([...metadata.getElementsByTagNameNS(DC_NS, 'title')][0]),
            language: text([...metadata.getElementsByTagNameNS(DC_NS, 'language')][0]),
        },
        dir: ['ltr', 'rtl'].includes(pageProgression) ? pageProgression : undefined,
    }
}

const NAV_ELEMENTS = new Set([
    'nav', 'ol', 'li', 'a', 'span', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'em',
    'strong', 'i', 'b', 'small', 'code', 'sub', 'sup', 'br',
])
const NAV_INLINE_ELEMENTS = new Set([
    'span', 'em', 'strong', 'i', 'b', 'small', 'code', 'sub', 'sup', 'br',
])
const NCX_ELEMENTS = new Set(['navMap', 'navPoint', 'navLabel', 'text', 'content'])

const validateTOCMarkup = (root, namespace, allowed, budget, depth = 1) => {
    if (depth > LIMITS.tocDepth) fail('The EPUB contents hierarchy is too deep.')
    if (root.namespaceURI !== namespace || !allowed.has(root.localName))
        fail('The EPUB contents document contains invalid markup.')
    budget.nodes++
    if (budget.nodes > LIMITS.tocNodes) fail('The EPUB contents document has too many nodes.')
    for (const node of root.childNodes) {
        if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE) {
            budget.text += new TextEncoder().encode(node.data).byteLength
            if (budget.text > LIMITS.tocTextBytes)
                fail('The EPUB contents document contains too much text.')
        } else if (node.nodeType === Node.ELEMENT_NODE) {
            validateTOCMarkup(node, namespace, allowed, budget, depth + 1)
        }
    }
}

const tocLabel = element => {
    const label = text(element)
    const bytes = new TextEncoder().encode(label).byteLength
    if (!label || bytes > LIMITS.tocLabelBytes)
        fail('The EPUB contents document has an invalid label.')
    return label
}

const validateTOCTarget = (reference, basePath, idsByPath) => {
    if (!reference || isExternalReference(reference))
        fail('The EPUB contents document contains an external or invalid reference.')
    const resolved = resolvePackageReference(reference, basePath)
    if (!resolved || !idsByPath.has(resolved.path)
        || (resolved.fragment && !idsByPath.get(resolved.path).has(resolved.fragment)))
        fail('The EPUB contents document refers to missing publication content.')
    return packageHref(resolved)
}

const parseNavTOC = async (item, blobs, idsByPath, sources, signal) => {
    const blob = blobs.get(item.path)
    if (!blob || blob.size > LIMITS.tocMarkupBytes)
        fail('The EPUB navigation document is too large.')
    const source = sources.get(item.path)
        ?? await decodeXMLBlob(blob, 'EPUB navigation document', signal)
    checkAbort(signal)
    const navigation = parseXML(
        source, 'EPUB navigation document', { allowXHTMLDoctype: true })
    const html = navigation.documentElement
    if (html.localName !== 'html' || html.namespaceURI !== XHTML_NS)
        fail('The EPUB navigation document is not XHTML.')
    const tocNavs = [...html.getElementsByTagNameNS(XHTML_NS, 'nav')].filter(nav =>
        (nav.getAttributeNS(EPUB_NS, 'type') ?? '').split(/\s+/).includes('toc'))
    if (tocNavs.length !== 1) fail('The EPUB navigation document has an invalid contents list.')
    const nav = tocNavs[0]
    const budget = { nodes: 0, text: 0 }
    validateTOCMarkup(nav, XHTML_NS, NAV_ELEMENTS, budget)
    const roots = children(nav, 'ol', XHTML_NS)
    const headings = elementChildren(nav).filter(element =>
        element.namespaceURI === XHTML_NS && /^h[1-6]$/.test(element.localName))
    if (roots.length !== 1 || headings.length > 1
        || elementChildren(nav).some(element => !roots.includes(element)
            && !headings.includes(element)))
        fail('The EPUB navigation document has an invalid contents list.')
    for (const heading of headings)
        for (const descendant of heading.getElementsByTagName('*'))
            if (descendant.namespaceURI !== XHTML_NS
                || !NAV_INLINE_ELEMENTS.has(descendant.localName))
                fail('The EPUB navigation heading contains invalid markup.')

    const parseList = (list, hierarchyDepth) => {
        if (hierarchyDepth > LIMITS.tocDepth)
            fail('The EPUB contents hierarchy is too deep.')
        const direct = elementChildren(list)
        if (!direct.length || direct.some(element =>
            element.localName !== 'li' || element.namespaceURI !== XHTML_NS))
            fail('The EPUB navigation document has an invalid contents list.')
        return direct.map(li => {
            const labels = elementChildren(li).filter(element =>
                element.namespaceURI === XHTML_NS && ['a', 'span'].includes(element.localName))
            const sublists = children(li, 'ol', XHTML_NS)
            if (labels.length !== 1 || sublists.length > 1
                || elementChildren(li).some(element => !labels.includes(element)
                    && !sublists.includes(element)))
                fail('The EPUB navigation document has an invalid contents item.')
            for (const descendant of labels[0].getElementsByTagName('*'))
                if (descendant.namespaceURI !== XHTML_NS
                    || !NAV_INLINE_ELEMENTS.has(descendant.localName))
                    fail('The EPUB navigation label contains invalid markup.')
            const subitems = sublists.length ? parseList(sublists[0], hierarchyDepth + 1) : []
            const href = labels[0].localName === 'a'
                ? validateTOCTarget(
                    requiredAttribute(labels[0], 'href'), item.path, idsByPath)
                : null
            if (!href && !subitems.length)
                fail('The EPUB navigation item has no publication target.')
            const result = { label: tocLabel(labels[0]), href }
            if (subitems.length) result.subitems = subitems
            return result
        })
    }
    const toc = parseList(roots[0], 1)
    checkAbort(signal)
    return toc
}

const parseNCXTOC = async (item, blobs, idsByPath, signal) => {
    const blob = blobs.get(item.path)
    if (!blob || blob.size > LIMITS.tocMarkupBytes)
        fail('The EPUB NCX document is too large.')
    const source = await decodeXMLBlob(blob, 'EPUB NCX document', signal)
    checkAbort(signal)
    const ncx = parseXML(source, 'EPUB NCX document')
    const root = ncx.documentElement
    if (root.localName !== 'ncx' || root.namespaceURI !== NCX_NS)
        fail('The EPUB NCX document has an invalid namespace.')
    const maps = children(root, 'navMap', NCX_NS)
    if (maps.length !== 1) fail('The EPUB NCX document has an invalid contents list.')
    const budget = { nodes: 0, text: 0 }
    validateTOCMarkup(maps[0], NCX_NS, NCX_ELEMENTS, budget)

    const parsePoint = (point, hierarchyDepth) => {
        if (hierarchyDepth > LIMITS.tocDepth)
            fail('The EPUB contents hierarchy is too deep.')
        const labels = children(point, 'navLabel', NCX_NS)
        const contents = children(point, 'content', NCX_NS)
        const subpoints = children(point, 'navPoint', NCX_NS)
        if (labels.length !== 1 || contents.length !== 1
            || elementChildren(point).some(element => !labels.includes(element)
                && !contents.includes(element) && !subpoints.includes(element)))
            fail('The EPUB NCX document has an invalid contents item.')
        const labelTexts = children(labels[0], 'text', NCX_NS)
        if (labelTexts.length !== 1 || elementChildren(labels[0]).length !== 1
            || elementChildren(labelTexts[0]).length
            || elementChildren(contents[0]).length)
            fail('The EPUB NCX document has an invalid label.')
        const href = validateTOCTarget(
            requiredAttribute(contents[0], 'src'), item.path, idsByPath)
        const subitems = subpoints.map(childPoint => parsePoint(childPoint, hierarchyDepth + 1))
        const result = { label: tocLabel(labelTexts[0]), href }
        if (subitems.length) result.subitems = subitems
        return result
    }
    const points = children(maps[0], 'navPoint', NCX_NS)
    if (!points.length || elementChildren(maps[0]).some(element => !points.includes(element)))
        fail('The EPUB NCX document has an invalid contents list.')
    const toc = points.map(point => parsePoint(point, 1))
    checkAbort(signal)
    return toc
}

const parsePublicationTOC = async (publication, blobs, idsByPath, sources, signal) => {
    if (publication.navItem)
        return parseNavTOC(publication.navItem, blobs, idsByPath, sources, signal)
    if (publication.ncxItem)
        return parseNCXTOC(publication.ncxItem, blobs, idsByPath, signal)
    fail('The EPUB publication has no supported contents document.')
}

const makeSafeEPUB = async (file, signal) => {
    const signature = new Uint8Array(await file.slice(0, 4).arrayBuffer())
    checkAbort(signal)
    if (signature[0] !== 0x50 || signature[1] !== 0x4b
        || signature[2] !== 0x03 || signature[3] !== 0x04)
        fail('The EPUB source is not a ZIP archive.')
    const { reader, blobs } = await readSafeZip(file, signal)
    const adapterURLs = []
    try {
        const publication = await validateEPUBPackage(blobs, signal)
        checkAbort(signal)
        for (const item of [publication.navItem, publication.ncxItem].filter(Boolean)) {
            const blob = blobs.get(item.path)
            if (!blob || blob.size > LIMITS.tocMarkupBytes)
                fail('The EPUB contents document is too large.')
        }
        const rasterURLs = new Map()
        const rasterBudget = createRasterBudget()
        const resolveImage = async (reference, basePath) => {
            if (isExternalReference(reference)) return null
            const resolved = resolvePackageReference(reference, basePath)
            if (!resolved || resolved.fragment) return null
            const item = publication.manifestByPath.get(resolved.path)
            const blob = blobs.get(resolved.path)
            if (!item || !blob || !isRasterMediaType(item.mediaType))
                fail('EPUB content refers to a missing or unsupported image.')
            if (!rasterURLs.has(resolved.path)) {
                await validateRasterImage(blob, item.mediaType, rasterBudget)
                checkAbort(signal)
                const url = URL.createObjectURL(new Blob([blob], { type: item.mediaType }))
                rasterURLs.set(resolved.path, url)
                adapterURLs.push(url)
            }
            checkAbort(signal)
            return rasterURLs.get(resolved.path)
        }

        const documentPaths = new Set(publication.documents.map(item => item.path))
        const spineByPath = new Map(publication.spine.map(record => [record.item.path, record]))
        const sanitized = new Map()
        const idsByPath = new Map()
        const documentSources = new Map()
        const stylesheetCache = new Map()
        const cssBudget = { input: 0, rules: 0, output: 0, generated: 0 }
        const getSanitizedStylesheet = async path => {
            if (stylesheetCache.has(path)) return stylesheetCache.get(path)
            const source = await decodeStylesheetBlob(blobs.get(path), cssBudget, signal)
            checkAbort(signal)
            const clean = await sanitizeCSS(
                source, path, resolveImage, cssBudget, signal)
            checkAbort(signal)
            stylesheetCache.set(path, clean)
            return clean
        }
        let linearReadingOrderHasContent = false
        for (const item of publication.documents) {
            checkAbort(signal)
            const source = await decodeXMLBlob(blobs.get(item.path), `EPUB document ${item.path}`, signal)
            checkAbort(signal)
            documentSources.set(item.path, source)
            checkAbort(signal)
            const original = parseXML(
                source, `EPUB document ${item.path}`, { allowXHTMLDoctype: true })
            const html = original.documentElement
            if (html.localName !== 'html' || html.namespaceURI !== XHTML_NS)
                fail('An EPUB spine document is not XHTML.')
            for (const element of [html, ...html.getElementsByTagName('*')])
                if (['html', 'head', 'body', 'style', 'link'].includes(element.localName)
                    && element.namespaceURI !== XHTML_NS)
                    fail('An EPUB document has structural markup in an invalid namespace.')
            const bodyElements = elementChildren(html).filter(element => element.localName === 'body')
            const headElements = elementChildren(html).filter(element => element.localName === 'head')
            if (bodyElements.length !== 1 || bodyElements[0].namespaceURI !== XHTML_NS
                || headElements.length > 1
                || headElements.some(element => element.namespaceURI !== XHTML_NS))
                fail('An EPUB spine document has an invalid XHTML structure.')
            const originalBody = bodyElements[0]

            const safeDocument = document.implementation.createDocument(XHTML_NS, 'html')
            const safeHTML = safeDocument.documentElement
            const ids = new Set()
            const claimID = id => {
                if (!safeFragment(id) || ids.has(id))
                    fail('An EPUB document has invalid or duplicate fragment identifiers.')
                ids.add(id)
            }
            const copyCommonAttributes = (sourceElement, targetElement) => {
                const id = sourceElement.getAttribute('id')
                if (id) {
                    claimID(id)
                    targetElement.setAttribute('id', id)
                }
                const classNames = (sourceElement.getAttribute('class') ?? '')
                    .split(/\s+/).filter(Boolean)
                if (classNames.length && classNames.every(safeToken))
                    targetElement.setAttribute('class', classNames.join(' '))
                const language = sourceElement.getAttribute('lang')
                    || sourceElement.getAttribute('xml:lang')
                if (safeLanguage(language)) targetElement.setAttribute('lang', language)
                const direction = sourceElement.getAttribute('dir')
                if (safeDirection(direction)) targetElement.setAttribute('dir', direction)
            }
            copyCommonAttributes(html, safeHTML)
            const head = safeDocument.createElementNS(XHTML_NS, 'head')
            const body = safeDocument.createElementNS(XHTML_NS, 'body')
            copyCommonAttributes(originalBody, body)
            safeHTML.append(head, body)
            const styles = []
            const originalHead = headElements[0]
            if (originalHead) for (const node of elementChildren(originalHead)) {
                if (node.namespaceURI !== XHTML_NS) continue
                if (node.localName === 'style') {
                    styles.push(await sanitizeCSS(
                        node.textContent, item.path, resolveImage, cssBudget, signal))
                    checkAbort(signal)
                } else if (node.localName === 'link'
                    && (node.getAttribute('rel') ?? '').toLowerCase()
                        .split(/\s+/).includes('stylesheet')) {
                    const resolved = resolvePackageReference(
                        requiredAttribute(node, 'href'), item.path)
                    if (!resolved || resolved.fragment) continue
                    const resource = publication.manifestByPath.get(resolved.path)
                    if (!resource || resource.mediaType !== 'text/css' || !blobs.has(resolved.path))
                        fail('An EPUB stylesheet is missing or has an invalid type.')
                    const stylesheet = await getSanitizedStylesheet(resolved.path)
                    checkAbort(signal)
                    styles.push(stylesheet)
                }
            }

            const copy = async node => {
                if (node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
                    return safeDocument.createTextNode(node.data)
                if (node.nodeType !== Node.ELEMENT_NODE) return null
                const name = node.localName.toLowerCase()
                if (node.namespaceURI !== XHTML_NS) return null
                if (DROP_CONTENT_ELEMENTS.has(name)) return null
                if (!XHTML_ELEMENTS.has(name)) {
                    const fragment = safeDocument.createDocumentFragment()
                    for (const childNode of node.childNodes) {
                        const copied = await copy(childNode)
                        checkAbort(signal)
                        if (copied) fragment.append(copied)
                    }
                    return fragment
                }
                let inertAnchor = false
                if (name === 'a') {
                    const href = node.getAttribute('href')
                    if (href) {
                        const resolved = isExternalReference(href)
                            ? null : resolvePackageReference(href, item.path)
                        inertAnchor = !resolved || !documentPaths.has(resolved.path)
                    }
                }
                if (name === 'img') {
                    const src = node.getAttribute('src')
                    if (!src || isExternalReference(src)) return null
                    const url = await resolveImage(src, item.path)
                    checkAbort(signal)
                    if (!url) return null
                    const image = safeDocument.createElementNS(XHTML_NS, 'img')
                    image.setAttribute('src', url)
                    copyCommonAttributes(node, image)
                    for (const attribute of ['alt', 'title']) {
                        const value = node.getAttribute(attribute)
                        if (value) image.setAttribute(attribute, value.slice(0, 1024))
                    }
                    return image
                }
                const result = safeDocument.createElementNS(
                    XHTML_NS, inertAnchor ? 'span' : name)
                copyCommonAttributes(node, result)
                if (name === 'a') {
                    const legacyName = node.getAttribute('name')
                    if (legacyName && safeFragment(legacyName)) {
                        const id = node.getAttribute('id')
                        if (!id) {
                            claimID(legacyName)
                            result.setAttribute('id', legacyName)
                        } else if (legacyName !== id) {
                            claimID(legacyName)
                            const marker = safeDocument.createElementNS(XHTML_NS, 'span')
                            marker.setAttribute('id', legacyName)
                            result.append(marker)
                        }
                    }
                }
                for (const attribute of GLOBAL_ARIA) {
                    const value = node.getAttribute(attribute)
                    if (value && value.length <= 1024) result.setAttribute(attribute, value)
                }
                const role = node.getAttribute('role')
                if (SAFE_ROLES.has(role)) result.setAttribute('role', role)
                const epubType = node.getAttributeNS(EPUB_NS, 'type') || node.getAttribute('epub:type')
                if (epubType) {
                    const tokens = epubType.split(/\s+/).filter(Boolean)
                    if (tokens.length && tokens.every(safeToken))
                        result.setAttributeNS(EPUB_NS, 'epub:type', tokens.join(' '))
                }
                if (name === 'a' && !inertAnchor) {
                    const href = node.getAttribute('href')
                    if (href) {
                        const resolved = resolvePackageReference(href, item.path)
                        const relative = resolved.path === item.path
                            ? '' : relativePackagePath(item.path, resolved.path)
                        const fragment = resolved.fragment
                            ? `#${encodePackageFragment(resolved.fragment)}` : ''
                        result.setAttribute('href', `${relative}${fragment}` || '#')
                    }
                }
                for (const attribute of ['title', 'abbr']) {
                    const value = node.getAttribute(attribute)
                    if (value) result.setAttribute(attribute, value.slice(0, 1024))
                }
                for (const attribute of ['colspan', 'rowspan', 'span', 'start']) {
                    const value = Number(node.getAttribute(attribute))
                    if (Number.isInteger(value) && value >= 1 && value <= 10000)
                        result.setAttribute(attribute, String(value))
                }
                if (node.hasAttribute('reversed') && name === 'ol') result.setAttribute('reversed', '')
                const inlineStyle = node.getAttribute('style')
                if (inlineStyle) {
                    const clean = await sanitizeCSS(
                        inlineStyle, item.path, resolveImage, cssBudget, signal, true)
                    checkAbort(signal)
                    if (clean) {
                        chargeCSSUse(clean, cssBudget)
                        result.setAttribute('style', clean)
                    }
                }
                for (const childNode of node.childNodes) {
                    const copied = await copy(childNode)
                    checkAbort(signal)
                    if (copied) result.append(copied)
                }
                return result
            }
            for (const node of originalBody.childNodes) {
                const copied = await copy(node)
                checkAbort(signal)
                if (copied) body.append(copied)
            }
            if (spineByPath.get(item.path)?.linear === 'yes') {
                const walker = safeDocument.createTreeWalker(body, NodeFilter.SHOW_TEXT)
                let hasText = false
                while (walker.nextNode()) if (hasVisibleUnicodeText(walker.currentNode.data)) {
                    hasText = true
                    break
                }
                if (hasText || body.querySelector('img')) linearReadingOrderHasContent = true
            }
            const css = styles.filter(Boolean).join('\n')
            if (css) {
                chargeCSSUse(css, cssBudget)
                const style = safeDocument.createElementNS(XHTML_NS, 'style')
                style.textContent = css
                head.append(style)
            }
            sanitized.set(item.path, {
                string: new XMLSerializer().serializeToString(safeDocument),
                title: text(body.querySelector('h1, h2, title, p')).slice(0, 512),
                linear: spineByPath.get(item.path)?.linear ?? 'no',
            })
            idsByPath.set(item.path, ids)
        }
        if (!linearReadingOrderHasContent)
            fail('The EPUB linear reading order has no readable text or raster content.')

        const toc = await parsePublicationTOC(
            publication, blobs, idsByPath, documentSources, signal)
        checkAbort(signal)
        const ordered = [
            ...publication.spine.map(record => record.item),
            ...publication.documents.filter(item => !spineByPath.has(item.path)),
        ]
        const indexByPath = new Map(ordered.map((item, index) => [item.path, index]))
        const sectionURLs = []
        const parser = new DOMParser()
        const sections = ordered.map((item, index) => {
            const data = sanitized.get(item.path)
            const blob = new Blob([data.string], { type: XHTML })
            const url = URL.createObjectURL(blob)
            sectionURLs.push(url)
            adapterURLs.push(url)
            return {
                id: encodePackagePath(item.path),
                size: blob.size,
                linear: data.linear === 'yes' ? undefined : 'no',
                load: () => url,
                unload: () => {},
                createDocument: () => parser.parseFromString(data.string, XHTML),
                resolveHref: href => {
                    if (isExternalReference(href)) return null
                    const resolved = resolvePackageReference(href, item.path)
                    if (!resolved || !indexByPath.has(resolved.path)) return null
                    return packageHref(resolved)
                },
            }
        })
        let destroyed = false
        const book = {
            metadata: publication.metadata,
            dir: publication.dir,
            sections,
            toc,
            resolveHref: href => {
                if (typeof href !== 'string' || isExternalReference(href)) return null
                const resolved = splitEncodedPackageHref(href)
                const index = indexByPath.get(resolved.path)
                if (index === undefined) return null
                return {
                    index,
                    anchor: resolved.fragment
                        ? document => document.getElementById(resolved.fragment)
                        : () => 0,
                }
            },
            splitTOCHref: href => {
                if (!href) return []
                const resolved = splitEncodedPackageHref(href)
                return [encodePackagePath(resolved.path), resolved.fragment]
            },
            getTOCFragment: (document, id) => id ? document.getElementById(id) : document.body,
            isExternal: isExternalReference,
            getCover: () => null,
            destroy: () => {
                if (destroyed) return
                destroyed = true
                for (const url of sectionURLs) URL.revokeObjectURL(url)
            },
        }
        checkAbort(signal)
        return { book, zipReader: reader, adapterURLs }
    } catch (error) {
        try { await reader.close() } catch { /* Preserve the validation error. */ }
        for (const url of adapterURLs) URL.revokeObjectURL(url)
        throw error
    }
}

export const openPublication = async ({ sourceUrl, format, signal }) => {
    if (!Object.hasOwn(MEDIA_TYPES, format)) fail('The source format is unsupported.')
    const source = await boundedFetch({ sourceUrl, format, signal })
    let publication
    try {
        publication = format === 'fb2'
            ? await makeSafeFB2(source.file, signal)
            : await makeSafeEPUB(source.file, signal)
    } catch (error) {
        source.controller.abort()
        if (error instanceof PublicationError) throw error
        throw new PublicationError('The publication failed security validation.', { cause: error })
    }

    let destroyed = false
    const destroy = async () => {
        if (destroyed) return
        destroyed = true
        source.controller.abort()
        try { publication.book.destroy?.() } catch { /* Continue releasing owned resources. */ }
        if (publication.zipReader) {
            try { await publication.zipReader.close() } catch { /* Cleanup is best effort. */ }
        }
        for (const url of publication.adapterURLs) URL.revokeObjectURL(url)
    }
    try {
        checkAbort(signal)
    } catch (error) {
        await destroy()
        throw error
    }
    return {
        book: publication.book,
        revision: source.revision,
        format,
        abort: () => source.controller.abort(),
        destroy,
    }
}
