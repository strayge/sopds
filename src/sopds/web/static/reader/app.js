import '../vendor/foliate/view.js'
import { openPublication } from './book.js'
import {
    FONT_SCALE_RANGE,
    discardLocation,
    getReaderMode,
    getFontScale,
    loadLocation,
    saveLocation,
    setFontScale,
    setReaderMode,
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
const modeToggle = document.querySelector('[data-reader-mode-toggle]')
const previousButton = document.querySelector('[data-reader-previous]')
const nextButton = document.querySelector('[data-reader-next]')
const previousEdgeButton = document.querySelector('[data-reader-edge-left]')
const nextEdgeButton = document.querySelector('[data-reader-edge-right]')
const pageControls = document.querySelectorAll('[data-reader-page-control]')
const progressOutput = document.querySelector('[data-reader-progress]')
const dockProgressOutput = document.querySelector('[data-reader-dock-progress]')
const decreaseButton = document.querySelector('[data-reader-font-decrease]')
const increaseButton = document.querySelector('[data-reader-font-increase]')
const retryLink = document.querySelector('[data-reader-retry]')

const publicId = root?.dataset.publicId ?? ''
const format = root?.dataset.sourceFormat ?? ''
const sourceUrl = root?.dataset.sourceUrl ?? ''
const FONT_STEP = 0.1

let fontScale = getFontScale()
let readerMode = getReaderMode()
let attemptNumber = 0
let activeController = null
let activePublication = null
let activeView = null
let viewListeners = []
let loadedDocumentListener = null
let currentPageRTL = false
let unloading = false
let lifecycle = Promise.resolve()
let modeSwitch = Promise.resolve()
let modeSwitching = false

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

const applyPageControlState = (visible, disabled = false) => {
    for (const control of pageControls) {
        control.hidden = !visible
        control.setAttribute('aria-hidden', String(!visible))
        if (control.matches('button')) {
            control.tabIndex = visible ? 0 : -1
            control.disabled = disabled
        }
    }
}

const applyModeUI = mode => {
    const pages = mode === 'pages'
    readerState.dataset.readerMode = mode
    modeToggle.dataset.readerMode = mode
    modeToggle.textContent = pages ? 'Scroll' : 'Pages'
    modeToggle.setAttribute('aria-label', pages
        ? 'Switch to scroll view' : 'Switch to pages view')
    progressOutput.hidden = pages
    dockProgressOutput.hidden = !pages
    applyPageControlState(pages)
}

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
    dockProgressOutput.value = progressOutput.value
    dockProgressOutput.textContent = progressOutput.value
    modeToggle.disabled = true
    applyPageControlState(false, true)
    if (contentsDialog.open) contentsDialog.close()
    contentsButton.setAttribute('aria-expanded', 'false')
}

const updateFontControls = () => {
    decreaseButton.disabled = fontScale <= FONT_SCALE_RANGE.min
    increaseButton.disabled = fontScale >= FONT_SCALE_RANGE.max
}

const updateNavigationControls = (view, switchingComplete = false) => {
    if (modeSwitching && !switchingComplete) {
        previousButton.disabled = true
        nextButton.disabled = true
        previousEdgeButton.disabled = true
        nextEdgeButton.disabled = true
        return
    }
    const atStart = Boolean(view.renderer?.atStart)
    const atEnd = Boolean(view.renderer?.atEnd)
    previousButton.disabled = atStart
    nextButton.disabled = atEnd
    previousEdgeButton.disabled = currentPageRTL ? atEnd : atStart
    nextEdgeButton.disabled = currentPageRTL ? atStart : atEnd
    previousEdgeButton.setAttribute('aria-label', currentPageRTL ? 'Next page' : 'Previous page')
    nextEdgeButton.setAttribute('aria-label', currentPageRTL ? 'Previous page' : 'Next page')
}

const turnLeft = () => currentPageRTL ? activeView?.next() : activeView?.prev()
const turnRight = () => currentPageRTL ? activeView?.prev() : activeView?.next()

const updateProgress = detail => {
    const fraction = Number(detail?.fraction)
    const progress = Number.isFinite(fraction)
        ? Math.min(1, Math.max(0, fraction)) : 0
    progressOutput.value = `${Math.round(progress * 100)}%`
    progressOutput.textContent = progressOutput.value
    dockProgressOutput.value = progressOutput.value
    dockProgressOutput.textContent = progressOutput.value
}

const addViewListener = (target, type, listener, options) => {
    target.addEventListener(type, listener, options)
    viewListeners.push(() => target.removeEventListener(type, listener, options))
}

const keyboardNavigation = event => {
    if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey
        || event.shiftKey || contentsDialog.open || modeSwitching) return
    const target = event.target
    if (target?.closest?.('a, button, input, select, textarea, [contenteditable="true"]')) return
    if (!activeView) return
    let navigation
    if (event.key === 'ArrowLeft') navigation = turnLeft
    else if (event.key === 'ArrowRight') navigation = turnRight
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
        if (!modeSwitching && typeof event.detail?.cfi === 'string' && event.detail.cfi)
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
        const direction = doc.defaultView?.getComputedStyle(doc.body).direction
        currentPageRTL = doc.body.dir === 'rtl' || doc.documentElement.dir === 'rtl'
            || direction === 'rtl'
        updateNavigationControls(view)
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
    if (readerMode === 'scroll') view.renderer.setAttribute('flow', 'scrolled')
    else view.renderer.removeAttribute('flow')
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
        modeToggle.disabled = false
        applyModeUI(readerMode)
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

const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve))
const wait = delay => new Promise(resolve => setTimeout(resolve, delay))

const applyReaderFlow = (view, mode) => {
    if (mode === 'scroll') view.renderer.setAttribute('flow', 'scrolled')
    else view.renderer.removeAttribute('flow')
}

const ensureActiveView = view => {
    if (view !== activeView || unloading)
        throw new DOMException('Reader mode switch replaced', 'AbortError')
}

const waitForRendererIdle = async view => {
    ensureActiveView(view)
    while (view.renderer?.locked) {
        await nextFrame()
        ensureActiveView(view)
    }
    // Scroll relocation is debounced by the renderer; let that final location
    // settle before taking the CFI used to restore the other flow.
    await wait(260)
    ensureActiveView(view)
    while (view.renderer?.locked) {
        await nextFrame()
        ensureActiveView(view)
    }
}

const goToRendererLocation = async (view, cfi) => {
    if (!cfi) return
    const resolved = view.resolveNavigation(cfi)
    if (!resolved) throw new Error('Could not resolve the current reader location')
    await view.renderer.goTo(resolved)
}

const switchReaderMode = async requestedMode => {
    const view = activeView
    if (!view || requestedMode === readerMode || unloading) return
    const previousMode = readerMode
    const publication = activePublication
    modeSwitching = true
    modeToggle.disabled = true
    contentsButton.disabled = true
    previousButton.disabled = true
    nextButton.disabled = true
    decreaseButton.disabled = true
    increaseButton.disabled = true
    applyPageControlState(readerMode === 'pages', true)
    readerState.dataset.readerSwitching = 'true'
    view.renderer.inert = true
    view.renderer.setAttribute('inert', '')
    let cfi
    try {
        await waitForRendererIdle(view)
        cfi = view.lastLocation?.cfi
        applyReaderFlow(view, requestedMode)
        await nextFrame()
        await nextFrame()
        await waitForRendererIdle(view)
        await goToRendererLocation(view, cfi)
        await waitForRendererIdle(view)
        ensureActiveView(view)
        readerMode = setReaderMode(requestedMode)
        if (cfi && publication) saveLocation({
            publicId,
            revision: publication.revision,
            format: publication.format,
            location: cfi,
        })
    } catch (error) {
        if (view === activeView && !unloading) {
            try {
                applyReaderFlow(view, previousMode)
                await nextFrame()
                await nextFrame()
                await waitForRendererIdle(view)
                await goToRendererLocation(view, cfi)
                await waitForRendererIdle(view)
            } catch (recoveryError) {
                console.warn('Reader mode recovery failed', recoveryError)
            }
        }
        readerMode = previousMode
        throw error
    } finally {
        if (view === activeView && !unloading) {
            view.renderer.inert = false
            view.renderer.removeAttribute('inert')
            applyModeUI(readerMode)
            contentsButton.disabled = !contentsNavigation.querySelector('button')
            updateNavigationControls(view, true)
            updateFontControls()
            modeToggle.disabled = false
        }
        modeSwitching = false
        delete readerState.dataset.readerSwitching
    }
}

contentsButton.addEventListener('click', () => {
    if (contentsButton.disabled || modeSwitching) return
    contentsDialog.showModal()
    contentsButton.setAttribute('aria-expanded', 'true')
})
contentsDialog.addEventListener('close', () =>
    contentsButton.setAttribute('aria-expanded', 'false'))
contentsDialog.addEventListener('click', event => {
    if (event.target === contentsDialog) contentsDialog.close()
})
modeToggle.addEventListener('click', () => {
    if (modeToggle.disabled || modeSwitching) return
    const requestedMode = readerMode === 'scroll' ? 'pages' : 'scroll'
    modeSwitch = modeSwitch.then(
        () => switchReaderMode(requestedMode),
        () => switchReaderMode(requestedMode),
    ).catch(error => {
        if (error?.name !== 'AbortError') console.warn('Reader mode switch failed', error)
    })
})
previousButton.addEventListener('click', () => {
    if (!modeSwitching) void activeView?.prev()
})
nextButton.addEventListener('click', () => {
    if (!modeSwitching) void activeView?.next()
})
previousEdgeButton.addEventListener('click', () => {
    if (!modeSwitching) void turnLeft()
})
nextEdgeButton.addEventListener('click', () => {
    if (!modeSwitching) void turnRight()
})

decreaseButton.addEventListener('click', () => {
    if (modeSwitching) return
    fontScale = setFontScale(Math.round((fontScale - FONT_STEP) * 10) / 10)
    activeView?.renderer.setStyles(publicationStyles(fontScale))
    updateFontControls()
})
increaseButton.addEventListener('click', () => {
    if (modeSwitching) return
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

applyModeUI(readerMode)
restart()
