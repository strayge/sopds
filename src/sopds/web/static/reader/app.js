import '../vendor/foliate/view.js'
import { openPublication } from './book.js'
import {
    FONT_SCALE_RANGE,
    discardLocation,
    getFontScale,
    loadLocation,
    saveLocation,
    setFontScale,
} from './state.js'

const root = document.querySelector('[data-reader-root]')
const loadingState = document.querySelector('[data-reader-state="loading"]')
const readerState = document.querySelector('[data-reader-state="reader"]')
const errorState = document.querySelector('[data-reader-state="error"]')
const errorMessage = document.querySelector('[data-reader-error-message]')
const surface = document.querySelector('[data-reader-surface]')
const contentsButton = document.querySelector('[data-reader-contents-button]')
const contentsDialog = document.querySelector('[data-reader-contents]')
const contentsNavigation = document.querySelector('[data-reader-contents-navigation]')
const previousButton = document.querySelector('[data-reader-previous]')
const nextButton = document.querySelector('[data-reader-next]')
const progressOutput = document.querySelector('[data-reader-progress]')
const decreaseButton = document.querySelector('[data-reader-font-decrease]')
const increaseButton = document.querySelector('[data-reader-font-increase]')
const retryLink = document.querySelector('[data-reader-retry]')

const publicId = root?.dataset.publicId ?? ''
const format = root?.dataset.sourceFormat ?? ''
const sourceUrl = root?.dataset.sourceUrl ?? ''
const FONT_STEP = 0.1

let fontScale = getFontScale()
let attemptNumber = 0
let activeController = null
let activePublication = null
let activeView = null
let viewListeners = []
let loadedDocumentListener = null
let unloading = false
let lifecycle = Promise.resolve()

const publicationStyles = scale => `
@font-face {
    font-family: "SOPDS Editorial";
    src: url("/static/fonts/Literata-SemiBold.woff2") format("woff2");
    font-style: normal;
    font-weight: 600;
    font-display: swap;
}
:root {
    color-scheme: light dark;
    font-family: Georgia, "Times New Roman", serif !important;
    font-size: ${scale}em !important;
    line-height: 1.58 !important;
    background: #fffdf8 !important;
    color: #26251f !important;
}
body {
    padding: 0 !important;
    background: #fffdf8 !important;
    color: #26251f !important;
}
h1, h2, h3, h4, h5, h6 {
    font-family: "SOPDS Editorial", Georgia, "Times New Roman", serif !important;
    font-weight: 600 !important;
}
a { color: #275b49 !important; }
img { max-width: 100% !important; }
@media (prefers-color-scheme: dark) {
    :root, body {
        background: #22251e !important;
        color: #eeeadf !important;
    }
    a { color: #8fc9ad !important; }
}
`

const showState = state => {
    loadingState.hidden = state !== 'loading'
    readerState.hidden = state !== 'reader'
    errorState.hidden = state !== 'error'
}

const removeViewListeners = () => {
    for (const remove of viewListeners.splice(0)) remove()
}

const removeLoadedDocumentListener = () => {
    loadedDocumentListener?.()
    loadedDocumentListener = null
}

const closeView = view => {
    if (!view) return
    try { view.close() } catch { /* Continue publication cleanup. */ }
    view.remove()
}

const cleanup = async () => {
    attemptNumber += 1
    const controller = activeController
    const view = activeView
    const publication = activePublication
    activeController = null
    activeView = null
    activePublication = null

    controller?.abort()
    publication?.abort()
    try { view?.close() } catch { /* Continue publication cleanup. */ }
    if (publication) await publication.destroy()
    removeViewListeners()
    removeLoadedDocumentListener()
    view?.remove()
    surface.replaceChildren()
    contentsNavigation.replaceChildren()
    contentsButton.disabled = true
    previousButton.disabled = true
    nextButton.disabled = true
    decreaseButton.disabled = true
    increaseButton.disabled = true
    progressOutput.value = '0%'
    progressOutput.textContent = progressOutput.value
    if (contentsDialog.open) contentsDialog.close()
    contentsButton.setAttribute('aria-expanded', 'false')
}

const updateFontControls = () => {
    decreaseButton.disabled = fontScale <= FONT_SCALE_RANGE.min
    increaseButton.disabled = fontScale >= FONT_SCALE_RANGE.max
}

const updateNavigationControls = view => {
    previousButton.disabled = Boolean(view.renderer?.atStart)
    nextButton.disabled = Boolean(view.renderer?.atEnd)
}

const updateProgress = detail => {
    const fraction = Number(detail?.fraction)
    const progress = Number.isFinite(fraction)
        ? Math.min(1, Math.max(0, fraction)) : 0
    progressOutput.value = `${Math.round(progress * 100)}%`
    progressOutput.textContent = progressOutput.value
}

const addViewListener = (target, type, listener, options) => {
    target.addEventListener(type, listener, options)
    viewListeners.push(() => target.removeEventListener(type, listener, options))
}

const keyboardNavigation = event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
        || event.shiftKey || contentsDialog.open) return
    const target = event.target
    if (target?.closest?.('a, button, input, select, textarea, [contenteditable="true"]')) return
    if (!activeView) return
    let navigation
    if (event.key === 'ArrowLeft') navigation = () => activeView.goLeft()
    else if (event.key === 'ArrowRight') navigation = () => activeView.goRight()
    else if (event.key === 'PageUp') navigation = () => activeView.prev()
    else if (event.key === 'PageDown') navigation = () => activeView.next()
    if (!navigation) return
    event.preventDefault()
    void navigation()
}

const renderContents = (items, view) => {
    const buildList = entries => {
        const list = document.createElement('ul')
        for (const item of entries) {
            if (typeof item?.label !== 'string') continue
            const listItem = document.createElement('li')
            if (typeof item.href === 'string') {
                const button = document.createElement('button')
                button.type = 'button'
                button.textContent = item.label
                button.addEventListener('click', async () => {
                    await view.goTo(item.href)
                    contentsDialog.close()
                    surface.focus()
                })
                listItem.append(button)
            } else {
                const label = document.createElement('span')
                label.className = 'reader-contents-group'
                label.textContent = item.label
                listItem.append(label)
            }
            if (Array.isArray(item.subitems) && item.subitems.length)
                listItem.append(buildList(item.subitems))
            list.append(listItem)
        }
        return list
    }
    contentsNavigation.replaceChildren(buildList(Array.isArray(items) ? items : []))
    const available = Boolean(contentsNavigation.querySelector('button'))
    contentsButton.disabled = !available
}

const configureView = async (publication, attempt) => {
    const view = document.createElement('foliate-view')
    activeView = view
    surface.replaceChildren(view)

    const relocation = event => {
        if (attempt !== attemptNumber) return
        updateProgress(event.detail)
        updateNavigationControls(view)
        if (typeof event.detail?.cfi === 'string' && event.detail.cfi)
            saveLocation({
                publicId,
                revision: publication.revision,
                format: publication.format,
                location: event.detail.cfi,
            })
    }
    const inertExternalLink = event => {
        event.preventDefault()
        event.stopImmediatePropagation()
    }
    const loadedDocument = event => {
        removeLoadedDocumentListener()
        const doc = event.detail.doc
        doc.addEventListener('keydown', keyboardNavigation)
        loadedDocumentListener = () =>
            doc.removeEventListener('keydown', keyboardNavigation)
    }

    addViewListener(view, 'relocate', relocation)
    addViewListener(view, 'external-link', inertExternalLink, true)
    addViewListener(view, 'load', loadedDocument)
    addViewListener(document, 'keydown', keyboardNavigation)

    await view.open(publication.book)
    if (attempt !== attemptNumber) throw new DOMException('Reader attempt replaced', 'AbortError')
    view.renderer.setAttribute('max-column-count', '1')
    view.renderer.setAttribute('max-inline-size', '720px')
    view.renderer.setAttribute('max-block-size', '1440px')
    view.renderer.setAttribute('gap', '5%')
    view.renderer.setAttribute('margin', innerWidth < 600 ? '16px' : '32px')
    view.renderer.setStyles(publicationStyles(fontScale))
    renderContents(publication.book.toc, view)
    return view
}

const discardView = view => {
    removeViewListeners()
    removeLoadedDocumentListener()
    closeView(view)
    if (activeView === view) activeView = null
}

const initializeView = async (publication, attempt) => {
    const savedLocation = loadLocation({
        publicId,
        revision: publication.revision,
        format: publication.format,
    })
    let view = await configureView(publication, attempt)
    if (savedLocation) {
        const resolved = view.resolveNavigation(savedLocation)
        const validIndex = Number.isInteger(resolved?.index)
            && resolved.index >= 0
            && resolved.index < publication.book.sections.length
        if (validIndex) {
            try {
                await view.init({ lastLocation: savedLocation, showTextStart: true })
                return view
            } catch {
                discardLocation(publicId)
                discardView(view)
                view = await configureView(publication, attempt)
            }
        } else discardLocation(publicId)
    }
    await view.init({ lastLocation: null, showTextStart: true })
    return view
}

const safeErrorMessage = error => error?.name === 'PublicationError'
    ? error.message : 'The book could not be opened in the web reader.'

const startFresh = async () => {
    await cleanup()
    if (unloading) return
    showState('loading')
    const attempt = ++attemptNumber
    const controller = new AbortController()
    activeController = controller
    try {
        const publication = await openPublication({
            sourceUrl,
            format,
            signal: controller.signal,
        })
        if (attempt !== attemptNumber) {
            await publication.destroy()
            return
        }
        activePublication = publication
        const view = await initializeView(publication, attempt)
        if (attempt !== attemptNumber) return
        activeView = view
        activeController = null
        previousButton.disabled = false
        nextButton.disabled = false
        decreaseButton.disabled = false
        increaseButton.disabled = false
        updateFontControls()
        updateNavigationControls(view)
        showState('reader')
        surface.focus()
    } catch (error) {
        if (attempt !== attemptNumber || unloading) return
        const message = safeErrorMessage(error)
        await cleanup()
        errorMessage.textContent = message
        showState('error')
        retryLink.focus()
    }
}

const restart = () => {
    lifecycle = lifecycle.then(startFresh, startFresh)
}

contentsButton.addEventListener('click', () => {
    if (contentsButton.disabled) return
    contentsDialog.showModal()
    contentsButton.setAttribute('aria-expanded', 'true')
})
contentsDialog.addEventListener('close', () =>
    contentsButton.setAttribute('aria-expanded', 'false'))
contentsDialog.addEventListener('click', event => {
    if (event.target === contentsDialog) contentsDialog.close()
})
previousButton.addEventListener('click', () => void activeView?.prev())
nextButton.addEventListener('click', () => void activeView?.next())

decreaseButton.addEventListener('click', () => {
    fontScale = setFontScale(Math.round((fontScale - FONT_STEP) * 10) / 10)
    activeView?.renderer.setStyles(publicationStyles(fontScale))
    updateFontControls()
})
increaseButton.addEventListener('click', () => {
    fontScale = setFontScale(Math.round((fontScale + FONT_STEP) * 10) / 10)
    activeView?.renderer.setStyles(publicationStyles(fontScale))
    updateFontControls()
})
retryLink.addEventListener('click', event => {
    event.preventDefault()
    restart()
})

const teardown = () => {
    if (unloading) return
    unloading = true
    void cleanup()
}
addEventListener('pagehide', teardown, { once: true })
addEventListener('beforeunload', teardown, { once: true })

restart()
