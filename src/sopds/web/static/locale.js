(() => {
  "use strict";

  const LOCALES = new Set(["en", "ru"]);
  const COOKIE_NAME = "sopds_ui_language";

  function selectedLocale(root = document.body) {
    const value = root?.dataset?.uiLocale;
    return LOCALES.has(value) ? value : "en";
  }

  function languageCookie(value, protocol = window.location.protocol) {
    if (!LOCALES.has(value)) return null;
    const secure = protocol === "https:" ? "; Secure" : "";
    return `${COOKIE_NAME}=${value}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;
  }

  function chooseLocale(value) {
    const cookie = languageCookie(value);
    if (!cookie) return false;
    document.cookie = cookie;
    window.location.reload();
    return true;
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
    document.querySelectorAll("[data-locale-choice]").forEach((choice) => {
      choice.addEventListener("click", () => chooseLocale(choice.dataset.localeChoice));
    });
    localizeCatalogTimes(document);
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
