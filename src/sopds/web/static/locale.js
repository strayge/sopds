(() => {
  "use strict";

  const LOCALES = new Set(["en", "ru"]);
  const COOKIE_NAME = "sopds_ui_language";
  const HTMX_HISTORY_CACHE_KEY = "htmx-history-cache";
  let initialized = false;

  function selectedLocale(root = document.body) {
    const value = root?.dataset?.uiLocale;
    return LOCALES.has(value) ? value : "en";
  }

  function languageCookie(value, protocol = window.location.protocol) {
    if (!LOCALES.has(value)) return null;
    const secure = protocol === "https:" ? "; Secure" : "";
    return `${COOKIE_NAME}=${value}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;
  }

  function clearHtmxHistoryCache() {
    try {
      window.sessionStorage?.removeItem(HTMX_HISTORY_CACHE_KEY);
    } catch {
      // Storage may be disabled or unavailable; reloading still applies the locale.
    }
  }

  function chooseLocale(value) {
    const cookie = languageCookie(value);
    if (!cookie) return false;
    document.cookie = cookie;
    clearHtmxHistoryCache();
    window.location.reload();
    return true;
  }

  function preventLocaleMismatch(event) {
    const responseLocale = event.detail?.xhr?.getResponseHeader?.("Content-Language");
    if (!LOCALES.has(responseLocale) || responseLocale === selectedLocale()) return false;
    event.detail.shouldSwap = false;
    event.preventDefault();
    clearHtmxHistoryCache();
    window.location.reload();
    return true;
  }

  function historyItemLocale(content) {
    if (typeof content !== "string") return null;
    try {
      const cachedDocument = new DOMParser().parseFromString(content, "text/html");
      const markers = cachedDocument.querySelectorAll("[data-history-locale]");
      if (markers.length !== 1) return null;
      const locale = markers[0].getAttribute("data-history-locale");
      return LOCALES.has(locale) ? locale : null;
    } catch {
      return null;
    }
  }

  function preventHistoryLocaleMismatch(event) {
    const cachedLocale = historyItemLocale(event.detail?.item?.content);
    const currentLocale = document.body?.dataset?.uiLocale;
    if (LOCALES.has(currentLocale) && cachedLocale === currentLocale) return false;
    event.preventDefault();
    clearHtmxHistoryCache();
    window.location.reload();
    return true;
  }

  function handleLocaleChoice(event) {
    const choice = event.target?.closest?.("[data-locale-choice]");
    if (!choice) return false;
    return chooseLocale(choice.dataset.localeChoice);
  }

  function localizeCatalogTimes(root, locale = selectedLocale()) {
    if (!root?.querySelectorAll) return;
    const formatter = new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "medium",
      hourCycle: "h23",
    });
    root.querySelectorAll("time.local-datetime").forEach((element) => {
      const value = new Date(element.dateTime);
      if (Number.isNaN(value.getTime())) return;
      element.textContent = formatter.format(value);
      element.title = element.dateTime;
    });
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    if (window.htmx?.config) window.htmx.config.refreshOnHistoryMiss = true;
    document.addEventListener("click", handleLocaleChoice);
    document.addEventListener("htmx:historyCacheHit", preventHistoryLocaleMismatch);
    localizeCatalogTimes(document);
    document.body?.addEventListener("htmx:beforeSwap", preventLocaleMismatch);
    document.body?.addEventListener("htmx:afterSwap", (event) => {
      localizeCatalogTimes(event.detail?.elt || event.target);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
