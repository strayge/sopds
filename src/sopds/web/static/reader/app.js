import '../vendor/foliate/view.js'
import { openPublication } from './book.js'
import {
    formatReaderPercent,
    readReaderI18n,
    readerEdgeLabels,
    readerModeControl,
    safeReaderErrorMessage,
} from './i18n.js'
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
const contentsCloseButton = document.querySelector('[data-reader-contents-close]')
const contentsNavigation = document.querySelector('[data-reader-contents-navigation]')
const modeToggle = document.querySelector('[data-reader-mode-toggle]')
const modeToggleLabel = modeToggle.querySelector('.reader-mode-label')
const previousButton = document.querySelector('[data-reader-previous]')
const nextButton = document.querySelector('[data-reader-next]')
const previousEdgeButton = document.querySelector('[data-reader-edge-left]')
const nextEdgeButton = document.querySelector('[data-reader-edge-right]')
const pageControls = document.querySelectorAll('[data-reader-page-control]')
const progressOutput = document.querySelector('[data-reader-progress]')
const dockProgressOutput = document.querySelector('[data-reader-dock-progress]')
const bookScrollbar = document.querySelector('[data-reader-book-scrollbar]')
const bookPosition = document.querySelector('[data-reader-book-position]')
const seekPreview = document.querySelector('[data-reader-seek-preview]')
const decreaseButton = document.querySelector('[data-reader-font-decrease]')
const increaseButton = document.querySelector('[data-reader-font-increase]')
const retryLink = document.querySelector('[data-reader-retry]')
const toolbarMenus = [...document.querySelectorAll('[data-reader-toolbar-menu]')]

const publicId = root?.dataset.publicId ?? ''
const format = root?.dataset.sourceFormat ?? ''
const sourceUrl = root?.dataset.sourceUrl ?? ''
const readerI18n = readReaderI18n(root)
const FONT_STEP = 0.1
const BOOK_POSITION_MAX = 10_000

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
let bookSeeking = false
let bookPositionPointerSeeking = false
let lastProgress = 0
let lastChapter = ''
let bookPositionMarkers = []
let contentsEntries = new Map()
let activeContentsEntry = null
let currentContentsHref = ''

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

const getBookPositionChapter = progress => {
    let chapter = ''
    for (const marker of bookPositionMarkers) {
        if (marker.fraction > progress + Number.EPSILON) break
        chapter = marker.label
    }
    return chapter
}

const updateBookPosition = (progress, preview = false, chapter = '') => {
    const bounded = Number.isFinite(progress) ? Math.min(1, Math.max(0, progress)) : 0
    const percent = formatReaderPercent(readerI18n, bounded)
    const label = chapter || getBookPositionChapter(bounded)
    const valueText = label ? `${percent}, ${label}` : percent
    bookPosition.value = String(Math.round(bounded * BOOK_POSITION_MAX))
    bookPosition.setAttribute('aria-valuetext', valueText)
    bookScrollbar.style.setProperty('--reader-seek-position', `${bounded * 100}%`)
    if (preview) {
        const previewText = label ? `${percent} · ${label}` : percent
        seekPreview.value = previewText
        seekPreview.textContent = previewText
        seekPreview.hidden = false
    }
}

const applyModeUI = mode => {
    const pages = mode === 'pages'
    readerState.dataset.readerMode = mode
    modeToggle.dataset.readerMode = mode
    const control = readerModeControl(readerI18n, mode)
    modeToggleLabel.textContent = control.text
    modeToggle.setAttribute('aria-label', control.ariaLabel)
    progressOutput.hidden = pages
    dockProgressOutput.hidden = !pages
    bookScrollbar.hidden = pages
    bookPosition.disabled = pages || !activeView || modeSwitching || bookSeeking
    bookPosition.tabIndex = pages ? -1 : 0
    if (pages) seekPreview.hidden = true
    applyPageControlState(pages)
}

const setToolbarMenuOpen = (menu, open) => {
    menu.toggleAttribute('data-open', open)
    menu.querySelector('[data-reader-toolbar-menu-toggle]')
        ?.setAttribute('aria-expanded', String(open))
}

const closeToolbarMenus = (except = null) => {
    for (const menu of toolbarMenus) {
        if (menu !== except) setToolbarMenuOpen(menu, false)
    }
}

const showState = state => {
    loadingState.hidden = state !== 'loading'
    readerState.hidden = state !== 'reader'
    errorState.hidden = state !== 'error'
    root.dataset.readerVisibleState = state
    if (state !== 'reader') closeToolbarMenus()
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
    clearContentsHighlight()
    contentsEntries = new Map()
    currentContentsHref = ''
    contentsButton.disabled = true
    previousButton.disabled = true
    nextButton.disabled = true
    decreaseButton.disabled = true
    increaseButton.disabled = true
    progressOutput.value = formatReaderPercent(readerI18n, 0)
    progressOutput.textContent = progressOutput.value
    dockProgressOutput.value = progressOutput.value
    dockProgressOutput.textContent = progressOutput.value
    bookSeeking = false
    bookPositionPointerSeeking = false
    lastProgress = 0
    lastChapter = ''
    bookPositionMarkers = []
    updateBookPosition(0)
    bookPosition.disabled = true
    bookScrollbar.hidden = true
    seekPreview.hidden = true
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
    const edgeLabels = readerEdgeLabels(readerI18n, currentPageRTL)
    previousEdgeButton.setAttribute('aria-label', edgeLabels.left)
    nextEdgeButton.setAttribute('aria-label', edgeLabels.right)
}

const turnLeft = () => currentPageRTL ? activeView?.next() : activeView?.prev()
const turnRight = () => currentPageRTL ? activeView?.prev() : activeView?.next()

const clearContentsHighlight = () => {
    if (!activeContentsEntry) return
    activeContentsEntry.element.removeAttribute('aria-current')
    for (const ancestor of activeContentsEntry.ancestors)
        ancestor.removeAttribute('data-reader-current-parent')
    activeContentsEntry = null
}

const updateContentsHighlight = href => {
    currentContentsHref = typeof href === 'string' ? href : ''
    clearContentsHighlight()
    const entry = contentsEntries.get(currentContentsHref)
    if (!entry) return
    entry.element.setAttribute('aria-current', 'location')
    for (const ancestor of entry.ancestors)
        ancestor.setAttribute('data-reader-current-parent', '')
    activeContentsEntry = entry
}

const centerCurrentContentsEntry = () => {
    const element = activeContentsEntry?.element
    if (!element || !contentsDialog.open) return
    element.focus({ preventScroll: true })
    const navigationRect = contentsNavigation.getBoundingClientRect()
    const elementRect = element.getBoundingClientRect()
    contentsNavigation.scrollTop += elementRect.top - navigationRect.top
        - (contentsNavigation.clientHeight - elementRect.height) / 2
}

const updateProgress = detail => {
    const fraction = Number(detail?.fraction)
    const progress = Number.isFinite(fraction)
        ? Math.min(1, Math.max(0, fraction)) : 0
    lastProgress = progress
    lastChapter = typeof detail?.tocItem?.label === 'string'
        ? detail.tocItem.label.trim() : ''
    if (typeof detail?.tocItem?.href === 'string')
        updateContentsHighlight(detail.tocItem.href)
    if (!bookSeeking) updateBookPosition(progress, false, lastChapter)
    progressOutput.value = formatReaderPercent(readerI18n, progress)
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
        || event.shiftKey || contentsDialog.open || modeSwitching || bookSeeking
        || toolbarMenus.some(menu => menu.hasAttribute('data-open'))) return
    const target = event.target
    if (target?.closest?.('a, button, input, select, textarea, [contenteditable="true"]')) return
    if (!activeView) return
    let navigation
    if (event.key === 'ArrowLeft') navigation = turnLeft
    else if (event.key === 'ArrowRight') navigation = turnRight
    else if (event.key === 'PageUp') navigation = () => activeView.prev()
    else if (event.key === 'PageDown') navigation = () => activeView.next()
    else if (event.code === 'Space' || [' ', 'Space', 'Spacebar'].includes(event.key))
        navigation = () => activeView.renderer.nextScreen()
    if (!navigation) return
    event.preventDefault()
    void navigation()
}

const renderContents = (items, view) => {
    clearContentsHighlight()
    contentsEntries = new Map()
    const buildList = (entries, ancestors = []) => {
        const list = document.createElement('ul')
        for (const item of entries) {
            if (typeof item?.label !== 'string') continue
            const listItem = document.createElement('li')
            let element
            if (typeof item.href === 'string') {
                element = document.createElement('button')
                element.type = 'button'
                element.textContent = item.label
                element.addEventListener('click', async () => {
                    await view.goTo(item.href)
                    contentsDialog.close()
                    surface.focus()
                })
                contentsEntries.set(item.href, { element, ancestors })
            } else {
                element = document.createElement('span')
                element.className = 'reader-contents-group'
                element.textContent = item.label
            }
            listItem.append(element)
            if (Array.isArray(item.subitems) && item.subitems.length)
                listItem.append(buildList(item.subitems, [...ancestors, element]))
            list.append(listItem)
        }
        return list
    }
    contentsNavigation.replaceChildren(buildList(Array.isArray(items) ? items : []))
    updateContentsHighlight(currentContentsHref)
    const available = Boolean(contentsNavigation.querySelector('button'))
    contentsButton.disabled = !available
}

const configureBookPositionMarkers = async book => {
    bookPositionMarkers = []
    const sections = Array.isArray(book.sections) ? book.sections : []
    const sizes = sections.map(section => section.linear !== 'no' && section.size > 0
        ? section.size : 0)
    const total = sizes.reduce((sum, size) => sum + size, 0)
    if (!total || typeof book.splitTOCHref !== 'function') return

    const starts = []
    let sizeBefore = 0
    for (const size of sizes) {
        starts.push(sizeBefore / total)
        sizeBefore += size
    }
    const sectionIndexes = new Map(sections.map((section, index) =>
        [String(section.id), index]))
    const entriesBySection = new Map()
    const firstLabels = new Map()

    const visit = async (items, parentLabel = '') => {
        for (const item of Array.isArray(items) ? items : []) {
            const label = typeof item?.label === 'string' ? item.label.trim() : ''
            const markerLabel = parentLabel && label
                ? `${parentLabel} — ${label}` : label
            if (markerLabel && typeof item.href === 'string') {
                try {
                    const [id, fragment] = await book.splitTOCHref(item.href) ?? []
                    const index = sectionIndexes.get(String(id))
                    if (index !== undefined && sizes[index]) {
                        firstLabels.set(index, firstLabels.get(index) ?? markerLabel)
                        const entries = entriesBySection.get(index) ?? []
                        entries.push({ fragment, label: markerLabel })
                        entriesBySection.set(index, entries)
                    }
                } catch { /* A usable section label is optional seek metadata. */ }
            }
            await visit(item?.subitems, label || parentLabel)
        }
    }
    await visit(book.toc)

    const addMarker = (index, localFraction, label) => bookPositionMarkers.push({
        fraction: starts[index] + localFraction * sizes[index] / total,
        label,
    })
    for (const [index, entries] of entriesBySection) {
        const anchored = []
        for (const entry of entries) {
            const { fragment } = entry
            if (fragment === undefined || fragment === null || fragment === '')
                addMarker(index, 0, entry.label)
            else anchored.push(entry)
        }
        const section = sections[index]
        if (!anchored.length || typeof section.createDocument !== 'function'
            || typeof book.getTOCFragment !== 'function') continue
        try {
            const document = section.createDocument()
            const body = document.body
            if (!body) continue
            const targets = new Map()
            for (const entry of anchored) {
                const node = book.getTOCFragment(document, entry.fragment)
                if (!node || !body.contains(node)) continue
                const entriesAtNode = targets.get(node) ?? []
                entriesAtNode.push(entry)
                targets.set(node, entriesAtNode)
            }
            const offsets = new Map(targets.has(body) ? [[body, 0]] : [])
            let textLength = 0
            const walker = document.createTreeWalker(body,
                NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT
                | NodeFilter.SHOW_CDATA_SECTION)
            while (walker.nextNode()) {
                const node = walker.currentNode
                if (node.nodeType === Node.TEXT_NODE
                    || node.nodeType === Node.CDATA_SECTION_NODE)
                    textLength += node.data.length
                else if (targets.has(node)) offsets.set(node, textLength)
            }
            for (const [node, nodeEntries] of targets) {
                const offset = offsets.get(node)
                if (offset === undefined) continue
                const localFraction = textLength ? offset / textLength : 0
                for (const entry of nodeEntries)
                    addMarker(index, localFraction, entry.label)
            }
        } catch { /* Fall back to the section-level contents label. */ }
    }
    for (const [index, label] of firstLabels)
        addMarker(index, 0, label)
    bookPositionMarkers.sort((a, b) => a.fraction - b.fraction)
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
    await configureBookPositionMarkers(publication.book)
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

const safeErrorMessage = error => safeReaderErrorMessage(readerI18n, error)

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
    if (!view || requestedMode === readerMode || unloading || bookSeeking) return
    const previousMode = readerMode
    const publication = activePublication
    modeSwitching = true
    modeToggle.disabled = true
    bookPosition.disabled = true
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
        if (view === activeView && !unloading)
            bookPosition.disabled = readerMode !== 'scroll' || bookSeeking
        delete readerState.dataset.readerSwitching
    }
}

const cancelBookSeek = () => {
    if (!bookSeeking || bookPosition.disabled) return
    bookSeeking = false
    seekPreview.hidden = true
    updateBookPosition(lastProgress, false, lastChapter)
    if (activeView && !unloading) modeToggle.disabled = false
}

const seekToBookPosition = async restoreFocus => {
    const view = activeView
    if (!view || readerMode !== 'scroll' || unloading) return cancelBookSeek()
    const requested = Number(bookPosition.value) / BOOK_POSITION_MAX
    const maximum = 1 - 1 / BOOK_POSITION_MAX
    const fraction = Math.min(maximum, Math.max(0, requested))
    bookSeeking = true
    modeToggle.disabled = true
    contentsButton.disabled = true
    decreaseButton.disabled = true
    increaseButton.disabled = true
    bookPosition.disabled = true
    readerState.dataset.readerSeeking = 'true'
    view.renderer.inert = true
    view.renderer.setAttribute('inert', '')
    try {
        await waitForRendererIdle(view)
        await view.goToFraction(fraction)
    } catch (error) {
        if (view === activeView && !unloading)
            console.warn('Book position seek failed', error)
    } finally {
        if (view === activeView && !unloading) {
            view.renderer.inert = false
            view.renderer.removeAttribute('inert')
            bookSeeking = false
            seekPreview.hidden = true
            const actual = Number(view.lastLocation?.fraction)
            updateBookPosition(
                Number.isFinite(actual) ? actual : lastProgress, false, lastChapter)
            contentsButton.disabled = !contentsNavigation.querySelector('button')
            updateFontControls()
            modeToggle.disabled = false
            bookPosition.disabled = false
            if (restoreFocus) bookPosition.focus()
        }
        delete readerState.dataset.readerSeeking
    }
}

bookPosition.addEventListener('input', () => {
    if (!activeView || readerMode !== 'scroll' || modeSwitching) return
    bookSeeking = true
    modeToggle.disabled = true
    updateBookPosition(Number(bookPosition.value) / BOOK_POSITION_MAX, true)
})
bookPosition.addEventListener('pointerdown', () => {
    bookPositionPointerSeeking = true
})
bookPosition.addEventListener('keydown', () => {
    bookPositionPointerSeeking = false
})
bookPosition.addEventListener('change', () => {
    const restoreFocus = !bookPositionPointerSeeking
        && bookPosition.matches(':focus-visible')
    bookPositionPointerSeeking = false
    void seekToBookPosition(restoreFocus)
})
bookPosition.addEventListener('pointercancel', () => {
    bookPositionPointerSeeking = false
    cancelBookSeek()
})
const finishBookPositionPointerInteraction = () => {
    setTimeout(() => {
        bookPositionPointerSeeking = false
        cancelBookSeek()
    }, 0)
}
bookPosition.addEventListener('pointerup', finishBookPositionPointerInteraction)
bookPosition.addEventListener('lostpointercapture', finishBookPositionPointerInteraction)
bookPosition.addEventListener('blur', () => {
    bookPositionPointerSeeking = false
    cancelBookSeek()
})

for (const menu of toolbarMenus) {
    const toggle = menu.querySelector('[data-reader-toolbar-menu-toggle]')
    toggle.addEventListener('click', () => {
        const open = !menu.hasAttribute('data-open')
        closeToolbarMenus(menu)
        setToolbarMenuOpen(menu, open)
    })
}
document.addEventListener('click', event => {
    if (!event.target.closest?.('[data-reader-toolbar-menu]')) closeToolbarMenus()
})
document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return
    const openMenu = toolbarMenus.find(menu => menu.hasAttribute('data-open'))
    if (!openMenu) return
    setToolbarMenuOpen(openMenu, false)
    openMenu.querySelector('[data-reader-toolbar-menu-toggle]')?.focus()
})

contentsButton.addEventListener('click', () => {
    if (contentsButton.disabled || modeSwitching || bookSeeking) return
    contentsDialog.showModal()
    contentsButton.setAttribute('aria-expanded', 'true')
    requestAnimationFrame(centerCurrentContentsEntry)
})
contentsDialog.addEventListener('close', () =>
    contentsButton.setAttribute('aria-expanded', 'false'))
contentsCloseButton.addEventListener('click', () => contentsDialog.close())
contentsDialog.addEventListener('click', event => {
    if (event.target === contentsDialog) contentsDialog.close()
})
modeToggle.addEventListener('click', () => {
    if (modeToggle.disabled || modeSwitching) return
    closeToolbarMenus()
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
    if (modeSwitching || bookSeeking) return
    fontScale = setFontScale(Math.round((fontScale - FONT_STEP) * 10) / 10)
    activeView?.renderer.setStyles(publicationStyles(fontScale))
    updateFontControls()
})
increaseButton.addEventListener('click', () => {
    if (modeSwitching || bookSeeking) return
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
