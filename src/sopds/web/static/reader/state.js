const PREFIX = 'sopds.reader.v1'
const FONT_KEY = `${PREFIX}.font-scale`
const LOCATION_PREFIX = `${PREFIX}.location.`
const DEFAULT_FONT_SCALE = 1
const MIN_FONT_SCALE = 0.75
const MAX_FONT_SCALE = 1.5
const MAX_LOCATION_LENGTH = 16384

const storage = () => {
    try {
        return globalThis.localStorage
    } catch {
        return null
    }
}

const locationKey = publicId => `${LOCATION_PREFIX}${encodeURIComponent(publicId)}`

const remove = key => {
    try {
        storage()?.removeItem(key)
    } catch {
        // Persistence is optional; reading must continue when storage is denied.
    }
}

export const getFontScale = () => {
    try {
        const value = Number(storage()?.getItem(FONT_KEY))
        return Number.isFinite(value) && value >= MIN_FONT_SCALE && value <= MAX_FONT_SCALE
            ? value : DEFAULT_FONT_SCALE
    } catch {
        return DEFAULT_FONT_SCALE
    }
}

export const setFontScale = value => {
    const number = Number(value)
    const bounded = Number.isFinite(number)
        ? Math.min(MAX_FONT_SCALE, Math.max(MIN_FONT_SCALE, number))
        : DEFAULT_FONT_SCALE
    try {
        storage()?.setItem(FONT_KEY, String(bounded))
    } catch {
        // Persistence is optional; return the usable in-memory value.
    }
    return bounded
}

export const loadLocation = ({ publicId, revision, format }) => {
    const key = locationKey(publicId)
    try {
        const raw = storage()?.getItem(key)
        if (!raw) return null
        const value = JSON.parse(raw)
        if (value?.version !== 1 || value.revision !== revision || value.format !== format
            || typeof value.location !== 'string' || !value.location
            || value.location.length > MAX_LOCATION_LENGTH) {
            remove(key)
            return null
        }
        return value.location
    } catch {
        remove(key)
        return null
    }
}

export const saveLocation = ({ publicId, revision, format, location }) => {
    if (typeof location !== 'string' || !location
        || location.length > MAX_LOCATION_LENGTH) return false
    try {
        const target = storage()
        if (!target) return false
        target.setItem(locationKey(publicId), JSON.stringify({
            version: 1,
            revision,
            format,
            location,
        }))
        return true
    } catch {
        return false
    }
}

export const discardLocation = publicId => remove(locationKey(publicId))

export const FONT_SCALE_RANGE = Object.freeze({
    default: DEFAULT_FONT_SCALE,
    min: MIN_FONT_SCALE,
    max: MAX_FONT_SCALE,
})
