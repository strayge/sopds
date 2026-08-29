import { makeFB2 } from '../vendor/foliate/fb2.js'
import {
    LIMITS,
    MEDIA_TYPES,
    PublicationError,
    canonicalArchivePath,
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
const hasVisibleElementText = element => hasVisibleUnicodeText(element?.textContent ?? '')
const decodeFB2Fragment = href => {
    if (typeof href !== 'string' || !href.startsWith('#')) return null
    let fragment
    try {
        fragment = decodeURIComponent(href.slice(1))
    } catch {
        fail('FB2 contains an invalid encoded fragment.')
    }
    if (!safeFragment(fragment)) fail('FB2 contains an unsafe fragment.')
    return fragment
}

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
    'sub', 'sup', 'code',
])
const FB2_METADATA_ELEMENTS = new Set([
    'description', 'title-info', 'src-title-info', 'document-info', 'publish-info',
    'custom-info', 'genre', 'author', 'translator', 'book-title', 'book-name',
    'annotation', 'keywords', 'date', 'coverpage', 'image', 'lang', 'src-lang',
    'sequence', 'first-name', 'middle-name', 'last-name', 'nickname', 'email',
    'home-page', 'id', 'version', 'program-used', 'src-url', 'src-ocr', 'publisher',
    'city', 'year', 'isbn', 'history', 'output', 'part', 'output-document-class',
])
const FB2_TITLE_INFO_CHILDREN = new Set([
    'genre', 'author', 'book-title', 'annotation', 'keywords', 'date', 'coverpage',
    'lang', 'src-lang', 'translator', 'sequence',
])
const FB2_PERSON_CHILDREN = new Set([
    'first-name', 'middle-name', 'last-name', 'nickname', 'home-page', 'email', 'id',
])
const FB2_DESCRIPTION_CHILDREN = Object.freeze({
    description: new Set([
        'title-info', 'src-title-info', 'document-info', 'publish-info', 'custom-info',
        'output',
    ]),
    'title-info': FB2_TITLE_INFO_CHILDREN,
    'src-title-info': FB2_TITLE_INFO_CHILDREN,
    'document-info': new Set([
        'author', 'program-used', 'date', 'src-url', 'src-ocr', 'id', 'version',
        'history', 'publisher',
    ]),
    'publish-info': new Set([
        'book-name', 'publisher', 'city', 'year', 'isbn', 'sequence',
    ]),
    author: FB2_PERSON_CHILDREN,
    translator: FB2_PERSON_CHILDREN,
    coverpage: new Set(['image']),
    sequence: new Set(['sequence']),
    genre: new Set(),
    'book-title': new Set(),
    'book-name': new Set(),
    keywords: new Set(),
    date: new Set(),
    lang: new Set(),
    'src-lang': new Set(),
    'first-name': new Set(),
    'middle-name': new Set(),
    'last-name': new Set(),
    nickname: new Set(),
    email: new Set(),
    'home-page': new Set(),
    id: new Set(),
    version: new Set(),
    'program-used': new Set(),
    'src-url': new Set(),
    'src-ocr': new Set(),
    publisher: new Set(),
    city: new Set(),
    year: new Set(),
    isbn: new Set(),
    'custom-info': new Set(),
    output: new Set(['part', 'output-document-class']),
    'output-document-class': new Set(['part']),
    part: new Set(),
})
const FB2_TEXT_ELEMENTS = new Set([
    'genre', 'book-title', 'book-name', 'keywords', 'lang', 'src-lang', 'first-name',
    'middle-name', 'last-name', 'nickname', 'email', 'home-page', 'id', 'version',
    'program-used', 'src-url', 'src-ocr', 'publisher', 'city', 'year', 'isbn', 'v',
])
const FB2_METADATA_ORDER = Object.freeze({
    description: [
        ['title-info', 1, 1], ['src-title-info', 0, 1], ['document-info', 1, 1],
        ['publish-info', 0, 1], ['custom-info', 0, Infinity], ['output', 0, 2],
    ],
    'title-info': [
        ['genre', 1, Infinity], ['author', 1, Infinity], ['book-title', 1, 1],
        ['annotation', 0, 1], ['keywords', 0, 1], ['date', 0, 1],
        ['coverpage', 0, 1], ['lang', 1, 1], ['src-lang', 0, 1],
        ['translator', 0, Infinity], ['sequence', 0, Infinity],
    ],
    'src-title-info': [
        ['genre', 1, Infinity], ['author', 1, Infinity], ['book-title', 1, 1],
        ['annotation', 0, 1], ['keywords', 0, 1], ['date', 0, 1],
        ['coverpage', 0, 1], ['lang', 1, 1], ['src-lang', 0, 1],
        ['translator', 0, Infinity], ['sequence', 0, Infinity],
    ],
    'document-info': [
        ['author', 1, Infinity], ['program-used', 0, 1], ['date', 1, 1],
        ['src-url', 0, Infinity], ['src-ocr', 0, 1], ['id', 1, 1],
        ['version', 1, 1], ['history', 0, 1], ['publisher', 0, Infinity],
    ],
    'publish-info': [
        ['book-name', 0, 1], ['publisher', 0, 1], ['city', 0, 1],
        ['year', 0, 1], ['isbn', 0, 1], ['sequence', 0, Infinity],
    ],
    person: [
        ['first-name', 0, 1], ['middle-name', 0, 1], ['last-name', 0, 1],
        ['nickname', 0, 1], ['home-page', 0, Infinity], ['email', 0, Infinity],
        ['id', 0, 1],
    ],
    coverpage: [['image', 1, Infinity]],
})
const FB2_INLINE_ELEMENTS = new Set([
    'strong', 'emphasis', 'style', 'a', 'strikethrough', 'sub', 'sup', 'code',
    'image',
])
const FB2_LINK_INLINE_ELEMENTS = new Set([...FB2_INLINE_ELEMENTS].filter(name => name !== 'a'))
const FB2_SECTION_CONTENT = new Set([
    'p', 'poem', 'subtitle', 'cite', 'empty-line', 'table',
])
const FB2_ANNOTATION_CONTENT = new Set([
    'p', 'poem', 'cite', 'subtitle', 'table', 'empty-line',
])

const validateFB2ElementChildren = (element, allowed) => {
    for (const item of elementChildren(element))
        if (item.namespaceURI !== FB2_NS || !allowed.has(item.localName))
            fail(`FB2 contains ${item.localName} in an invalid structural context.`)
}

const validateFB2NoBlockText = element => {
    for (const node of element.childNodes)
        if ((node.nodeType === Node.TEXT_NODE || node.nodeType === Node.CDATA_SECTION_NODE)
            && node.data.trim())
            fail(`FB2 contains text outside a supported block in ${element.localName}.`)
}

const validateFB2Inline = (element, insideAnchor = false) => {
    const allowed = insideAnchor ? FB2_LINK_INLINE_ELEMENTS : FB2_INLINE_ELEMENTS
    validateFB2ElementChildren(element, allowed)
    for (const item of elementChildren(element)) {
        if (item.localName === 'image') {
            validateFB2ElementChildren(item, new Set())
            continue
        }
        validateFB2Inline(item, insideAnchor || item.localName === 'a')
    }
}

const validateFB2Block = element => {
    const items = elementChildren(element)
    const names = items.map(item => item.localName)
    switch (element.localName) {
    case 'body': {
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set(['image', 'title', 'epigraph', 'section']))
        let index = names[0] === 'image' ? 1 : 0
        if (names[index] === 'title') index++
        while (names[index] === 'epigraph') index++
        if (index === names.length || names.slice(index).some(name => name !== 'section'))
            fail('An FB2 body must end with one or more sections.')
        break
    }
    case 'section': {
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set([
            'title', 'epigraph', 'image', 'annotation', 'section',
            ...FB2_SECTION_CONTENT,
        ]))
        let index = names[0] === 'title' ? 1 : 0
        while (names[index] === 'epigraph') index++
        if (names[index] === 'image') index++
        if (names[index] === 'annotation') index++
        const content = names.slice(index)
        if (content.length) {
            if (content[0] === 'section') {
                if (content.some(name => name !== 'section'))
                    fail('An FB2 section cannot mix child sections with text blocks.')
            } else if (!FB2_SECTION_CONTENT.has(content[0])
                || content.slice(1).some(name => name !== 'image'
                    && !FB2_SECTION_CONTENT.has(name)))
                fail('An FB2 section has invalid text-block ordering.')
        }
        break
    }
    case 'annotation':
    case 'history':
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, FB2_ANNOTATION_CONTENT)
        break
    case 'title':
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set(['p', 'empty-line']))
        break
    case 'epigraph':
    case 'cite': {
        validateFB2NoBlockText(element)
        const content = element.localName === 'epigraph'
            ? new Set(['p', 'poem', 'cite', 'empty-line'])
            : new Set(['p', 'poem', 'empty-line', 'subtitle', 'table'])
        validateFB2ElementChildren(element, new Set([...content, 'text-author']))
        const firstAuthor = names.indexOf('text-author')
        if ((firstAuthor >= 0
            && names.slice(firstAuthor).some(name => name !== 'text-author'))
            || names.slice(0, firstAuthor < 0 ? names.length : firstAuthor)
                .some(name => !content.has(name)))
            fail(`An FB2 ${element.localName} has invalid block ordering.`)
        break
    }
    case 'poem': {
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set([
            'title', 'epigraph', 'subtitle', 'stanza', 'text-author', 'date',
        ]))
        let index = names[0] === 'title' ? 1 : 0
        while (names[index] === 'epigraph') index++
        const contentStart = index
        while (['subtitle', 'stanza'].includes(names[index])) index++
        if (index === contentStart) fail('An FB2 poem must contain a stanza or subtitle.')
        while (names[index] === 'text-author') index++
        if (names[index] === 'date') index++
        if (index !== names.length) fail('An FB2 poem has invalid block ordering.')
        break
    }
    case 'stanza': {
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set(['title', 'subtitle', 'v']))
        let index = names[0] === 'title' ? 1 : 0
        if (names[index] === 'subtitle') index++
        if (index === names.length || names.slice(index).some(name => name !== 'v'))
            fail('An FB2 stanza must end with one or more verse lines.')
        break
    }
    case 'table':
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set(['tr']))
        if (!items.length) fail('An FB2 table must contain a row.')
        break
    case 'tr':
        validateFB2NoBlockText(element)
        validateFB2ElementChildren(element, new Set(['th', 'td']))
        if (!items.length) fail('An FB2 table row must contain a cell.')
        break
    case 'image':
    case 'empty-line':
        validateFB2ElementChildren(element, new Set())
        break
    case 'date':
        validateFB2ElementChildren(element, new Set())
        break
    default:
        validateFB2Inline(element)
        break
    }
    for (const item of items) validateFB2Block(item)
}

const isFB2Person = element => ['author', 'translator'].includes(element.localName)
    || (element.localName === 'publisher' && element.parentElement?.localName === 'document-info')

const validateFB2MetadataOrder = element => {
    const rules = FB2_METADATA_ORDER[isFB2Person(element) ? 'person' : element.localName]
    if (!rules) return
    const orderByName = new Map(rules.map(([name], index) => [name, index]))
    const counts = new Map()
    let lastIndex = 0
    for (const item of elementChildren(element)) {
        const index = orderByName.get(item.localName)
        if (index === undefined || index < lastIndex)
            fail(`FB2 contains ${item.localName} in invalid metadata order.`)
        lastIndex = index
        counts.set(item.localName, (counts.get(item.localName) ?? 0) + 1)
    }
    for (const [name, minimum, maximum] of rules) {
        const count = counts.get(name) ?? 0
        if (count < minimum || count > maximum)
            fail(`FB2 metadata has invalid ${name} cardinality.`)
    }
}

const validateFB2Person = element => {
    if (!isFB2Person(element)) return
    const first = children(element, 'first-name')
    const middle = children(element, 'middle-name')
    const last = children(element, 'last-name')
    const nickname = children(element, 'nickname')
    const usesStructuredName = first.length || middle.length || last.length
    if (usesStructuredName) {
        if (first.length !== 1 || last.length !== 1
            || !hasVisibleElementText(first[0]) || !hasVisibleElementText(last[0]))
            fail('FB2 person names require both a readable first and last name.')
    } else if (nickname.length !== 1 || !hasVisibleElementText(nickname[0]))
        fail('FB2 person names require a readable nickname or first and last name.')
}

const validateFB2DescriptionChildren = element => {
    if (['annotation', 'history'].includes(element.localName)) {
        validateFB2Block(element)
        return
    }
    if (['output', 'output-document-class', 'part'].includes(element.localName))
        validateFB2NoBlockText(element)
    const allowed = isFB2Person(element)
        ? FB2_PERSON_CHILDREN : FB2_DESCRIPTION_CHILDREN[element.localName]
    if (!allowed) fail(`FB2 contains unsupported metadata: ${element.localName}.`)
    validateFB2MetadataOrder(element)
    validateFB2Person(element)
    for (const item of elementChildren(element)) {
        if (item.namespaceURI !== FB2_NS || !allowed.has(item.localName))
            fail(`FB2 contains ${item.localName} in invalid metadata context.`)
        if (item.localName === 'image') validateFB2Block(item)
        else validateFB2DescriptionChildren(item)
    }
}

const decodeFB2Binary = async (element, budget, signal) => {
    const type = normalizedMediaType(requiredAttribute(element, 'content-type'))
    if (!isRasterMediaType(type)) fail('FB2 contains an unsupported image type.')
    const encoded = element.textContent.replace(/[\t\n\f\r ]+/g, '')
    if (!encoded || encoded.length > Math.ceil(LIMITS.sourceBytes * 4 / 3) + 4
        || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded))
        fail('FB2 contains invalid image data.')
    let decoded
    try {
        const raw = atob(encoded)
        decoded = Uint8Array.from(raw, character => character.charCodeAt(0))
    } catch (error) {
        throw new PublicationError('FB2 contains invalid base64 image data.', { cause: error })
    }
    await validateRasterImage(decoded, type, budget)
    checkAbort(signal)
    return { type, encoded }
}

const makeSafeFB2 = async (file, signal) => {
    checkAbort(signal)
    const source = await decodeXMLBlob(file, 'FB2', signal)
    checkAbort(signal)
    const original = parseXML(source, 'FB2', { allowXHTMLDoctype: false })
    const root = original.documentElement
    if (root.localName !== 'FictionBook' || root.namespaceURI !== FB2_NS)
        fail('The FB2 root element or namespace is invalid.')
    const publicationIDs = new Set()
    for (const element of [root, ...root.getElementsByTagName('*')]) {
        if (element.namespaceURI !== FB2_NS)
            fail('FB2 contains content in an unexpected namespace.')
        if (!element.hasAttribute('id')) continue
        const id = element.getAttribute('id')
        if (!safeFragment(id) || publicationIDs.has(id))
            fail('FB2 contains invalid or duplicate IDs.')
        publicationIDs.add(id)
    }

    const descriptions = children(root, 'description')
    const bodies = children(root, 'body')
    const rootChildren = elementChildren(root)
    const rootOrder = rootChildren.map(item => item.localName)
    const firstBody = rootOrder.indexOf('body')
    const firstBinary = rootOrder.indexOf('binary')
    if (descriptions.length !== 1 || rootChildren[0] !== descriptions[0]
        || firstBody !== 1 || (firstBinary >= 0
            && rootOrder.slice(firstBinary).some(name => name !== 'binary'))
        || bodies.length < 1
        || (!hasVisibleElementText(bodies[0])
            && !bodies[0].getElementsByTagNameNS(FB2_NS, 'image').length))
        fail('The FB2 publication has no readable body or required structure.')
    for (const item of rootChildren)
        if (item.namespaceURI !== FB2_NS
            || !['description', 'body', 'binary'].includes(item.localName))
            fail('FB2 contains unsupported top-level content.')
    const topLevelOnly = new Set(['description', 'body', 'binary'])
    for (const item of root.getElementsByTagName('*'))
        if (topLevelOnly.has(item.localName) && item.parentElement !== root)
            fail(`FB2 contains ${item.localName} outside the publication root.`)
    validateFB2DescriptionChildren(descriptions[0])
    const titleInfo = child(descriptions[0], 'title-info')
    const documentInfo = child(descriptions[0], 'document-info')
    if (!children(titleInfo, 'genre').every(hasVisibleElementText)
        || !hasVisibleElementText(child(titleInfo, 'book-title'))
        || !hasVisibleElementText(child(titleInfo, 'lang'))
        || !hasVisibleElementText(child(documentInfo, 'id')))
        fail('The FB2 publication has empty required metadata.')
    for (const body of bodies) {
        if (body.hasAttribute('name') && !safeToken(body.getAttribute('name')))
            fail('The FB2 publication has an invalid body name.')
        validateFB2Block(body)
    }

    const binaries = new Map()
    for (const binary of children(root, 'binary')) {
        checkAbort(signal)
        const id = binary.getAttribute('id')
        if (!safeFragment(id) || binaries.has(id)) fail('FB2 contains an invalid binary ID.')
        if (elementChildren(binary).length)
            fail('FB2 binary data contains invalid child markup.')
        binaries.set(id, binary)
    }

    const namespace = FB2_NS
    const safe = document.implementation.createDocument(namespace, 'FictionBook')
    const safeRoot = safe.documentElement
    safeRoot.setAttributeNS('http://www.w3.org/2000/xmlns/', 'xmlns:l', XLINK_NS)
    const referenced = new Set()

    const copyNode = (node, inBody) => {
        if (node.nodeType === Node.TEXT_NODE) return safe.createTextNode(node.data)
        if (node.nodeType === Node.CDATA_SECTION_NODE) return safe.createTextNode(node.data)
        if (node.nodeType !== Node.ELEMENT_NODE) return null
        const name = node.localName
        if (node.namespaceURI !== FB2_NS)
            fail('FB2 contains content in an unexpected namespace.')
        const allowed = inBody ? FB2_BODY_ELEMENTS : FB2_METADATA_ELEMENTS
        if (!allowed.has(name))
            fail(`FB2 contains unsupported content: ${name}.`)
        if (!inBody && name === 'output') return null
        let linkFragment
        if (name === 'a') {
            const href = node.getAttributeNS(XLINK_NS, 'href') || node.getAttribute('href')
            linkFragment = decodeFB2Fragment(href)
            if (linkFragment === null) {
                const inert = safe.createElementNS(namespace, 'style')
                for (const item of node.childNodes) {
                    const copied = copyNode(item, inBody)
                    if (copied) inert.append(copied)
                }
                return inert
            }
        }
        const isPoemTitle = inBody && name === 'title'
            && node.parentElement?.localName === 'poem'
        const result = safe.createElementNS(namespace, isPoemTitle ? 'subtitle' : name)
        const id = node.getAttribute('id')
        if (id && safeFragment(id)) result.setAttribute('id', id)
        if (name === 'body' && node.hasAttribute('name'))
            result.setAttribute('name', node.getAttribute('name'))
        if (name === 'image') {
            const href = node.getAttributeNS(XLINK_NS, 'href') || node.getAttribute('href')
            const fragment = decodeFB2Fragment(href)
            if (fragment === null || !binaries.has(fragment))
                fail('FB2 contains an invalid image reference.')
            referenced.add(fragment)
            result.setAttributeNS(XLINK_NS, 'l:href', `#${fragment}`)
            for (const attribute of ['alt', 'title']) {
                const value = node.getAttribute(attribute)
                if (value) result.setAttribute(attribute, value.slice(0, 1024))
            }
        } else if (name === 'a') {
            result.setAttributeNS(XLINK_NS, 'l:href', `#${linkFragment}`)
            if (node.getAttribute('type') === 'note') result.setAttribute('type', 'note')
        } else if (name === 'date') {
            const value = node.getAttribute('value')
            if (value) result.setAttribute('value', value.slice(0, 128))
        } else if (name === 'sequence') {
            for (const attribute of ['name', 'number']) {
                const value = node.getAttribute(attribute)
                if (value) result.setAttribute(attribute, value.slice(0, 256))
            }
        } else if (['td', 'th'].includes(name)) {
            for (const attribute of ['colspan', 'rowspan']) {
                const value = Number(node.getAttribute(attribute))
                if (Number.isInteger(value) && value >= 1 && value <= 100)
                    result.setAttribute(attribute, String(value))
            }
        }
        if (inBody && (name === 'annotation' || isPoemTitle)) {
            result.textContent = elementChildren(node)
                .map(item => text(item)).filter(Boolean).join('\n\n')
        } else {
            const childInBody = inBody || ['annotation', 'history'].includes(name)
            for (const item of node.childNodes) {
                const copied = copyNode(item, childInBody)
                if (copied) result.append(copied)
            }
        }
        const textOnlyMetadata = FB2_TEXT_ELEMENTS.has(name)
            && !(name === 'publisher' && node.parentElement?.localName === 'document-info')
        if (textOnlyMetadata && result.children.length)
            fail(`FB2 contains malformed text metadata: ${name}.`)
        return result
    }

    safeRoot.append(copyNode(descriptions[0], false))
    for (const body of bodies) safeRoot.append(copyNode(body, true))
    const rasterBudget = createRasterBudget()
    for (const id of referenced) {
        checkAbort(signal)
        const decoded = await decodeFB2Binary(binaries.get(id), rasterBudget, signal)
        checkAbort(signal)
        const binary = safe.createElementNS(namespace, 'binary')
        binary.setAttribute('id', id)
        binary.setAttribute('content-type', decoded.type)
        binary.textContent = decoded.encoded
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
