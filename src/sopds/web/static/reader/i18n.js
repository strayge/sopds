const SUPPORTED_LOCALES = new Set(['en', 'ru'])
const MAX_MESSAGE_LENGTH = 300

const MESSAGE_FALLBACKS = Object.freeze({
    pages: 'Pages',
    scroll: 'Scroll',
    switchToPagesView: 'Switch to pages view',
    switchToScrollView: 'Switch to scroll view',
    previousPage: 'Previous page',
    nextPage: 'Next page',
    genericOpenError: 'The book could not be opened in the web reader.',
})

const templatePlaceholders = value => {
    const placeholders = []
    let position = 0
    for (const match of value.matchAll(/\{([a-z][a-zA-Z0-9]*)\}/g)) {
        if (/[{}]/.test(value.slice(position, match.index))) return null
        placeholders.push(match[1])
        position = match.index + match[0].length
    }
    return /[{}]/.test(value.slice(position)) ? null : placeholders
}

const validMessage = (value, fallback) => {
    if (typeof value !== 'string' || !value.trim() || value.length > MAX_MESSAGE_LENGTH)
        return false
    const placeholders = templatePlaceholders(value)
    const expected = templatePlaceholders(fallback)
    if (placeholders === null || expected === null
        || placeholders.length !== expected.length) return false
    placeholders.sort()
    expected.sort()
    return expected.every((name, index) => placeholders[index] === name)
}

export const readReaderI18n = root => {
    const configuredLocale = root?.dataset?.readerLocale
    const locale = SUPPORTED_LOCALES.has(configuredLocale) ? configuredLocale : 'en'
    let configured = {}
    if (configuredLocale === locale) {
        try {
            configured = JSON.parse(root?.dataset?.readerMessages || '{}')
            if (!configured || typeof configured !== 'object' || Array.isArray(configured))
                configured = {}
        } catch {
            configured = {}
        }
    }
    const messages = {}
    for (const [key, fallback] of Object.entries(MESSAGE_FALLBACKS))
        messages[key] = validMessage(configured[key], fallback) ? configured[key] : fallback
    return {
        locale,
        messages,
        percent: new Intl.NumberFormat(locale, {
            style: 'percent',
            maximumFractionDigits: 0,
        }),
    }
}

export const readerMessage = (i18n, key) =>
    i18n?.messages?.[key] || MESSAGE_FALLBACKS[key] || ''

export const readerModeControl = (i18n, mode) => mode === 'pages'
    ? {
        text: readerMessage(i18n, 'scroll'),
        ariaLabel: readerMessage(i18n, 'switchToScrollView'),
    }
    : {
        text: readerMessage(i18n, 'pages'),
        ariaLabel: readerMessage(i18n, 'switchToPagesView'),
    }

export const readerEdgeLabels = (i18n, rightToLeft) => rightToLeft
    ? {
        left: readerMessage(i18n, 'nextPage'),
        right: readerMessage(i18n, 'previousPage'),
    }
    : {
        left: readerMessage(i18n, 'previousPage'),
        right: readerMessage(i18n, 'nextPage'),
    }

export const formatReaderPercent = (i18n, fraction) => {
    const numeric = Number(fraction)
    const bounded = Number.isFinite(numeric) ? Math.min(1, Math.max(0, numeric)) : 0
    return i18n.percent.format(bounded)
}

export const safeReaderErrorMessage = (i18n, error) =>
    error?.name === 'PublicationError' && typeof error.message === 'string'
        ? error.message
        : readerMessage(i18n, 'genericOpenError')
