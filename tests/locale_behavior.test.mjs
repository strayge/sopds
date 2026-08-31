import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const scriptPath = new URL("../src/sopds/web/static/locale.js", import.meta.url);
const source = readFileSync(scriptPath, "utf8");
const exportHook = `
  globalThis.localeBehavior = {
    selectedLocale,
    languageCookie,
    clearHtmxHistoryCache,
    chooseLocale,
    preventLocaleMismatch,
    historyItemLocale,
    preventHistoryLocaleMismatch,
    handleLocaleChoice,
    localizeCatalogTimes,
    initialize,
  };
`;
const instrumented = source.replace(
  '  if (document.readyState === "loading")',
  `${exportHook}\n  if (document.readyState === "loading")`,
);

function harness({
  protocol = "http:",
  locale = "en",
  htmx = true,
  storageAccessThrows = false,
  storageRemoveThrows = false,
} = {}) {
  const listeners = new Map();
  const bodyListeners = new Map();
  const listenerRegistrations = new Map();
  const bodyListenerRegistrations = new Map();
  const removedStorageKeys = [];
  let cookie = "";
  let reloads = 0;
  let hrefWrites = 0;
  const location = {
    protocol,
    pathname: "/catalog",
    search: "?q=book",
    hash: "#view=tree",
    reload() { reloads += 1; },
    set href(_value) { hrefWrites += 1; },
  };
  const body = {
    dataset: {uiLocale: locale},
    addEventListener(name, listener) {
      bodyListeners.set(name, listener);
      bodyListenerRegistrations.set(name, (bodyListenerRegistrations.get(name) || 0) + 1);
    },
  };
  const document = {
    readyState: "loading",
    body,
    addEventListener(name, listener) {
      listeners.set(name, listener);
      listenerRegistrations.set(name, (listenerRegistrations.get(name) || 0) + 1);
    },
    querySelectorAll() { return []; },
    get cookie() { return cookie; },
    set cookie(value) { cookie = value; },
  };
  const formatterCalls = [];
  class DateTimeFormat {
    constructor(selected, options) {
      formatterCalls.push({locale: selected, options});
    }
    format(value) { return `localized:${value.toISOString()}`; }
  }
  class DOMParser {
    parseFromString(content, type) {
      assert.equal(type, "text/html");
      const values = [];
      const pattern = /\sdata-history-locale=(?:"([^"]*)"|'([^']*)'|([^\s>]+))/g;
      for (const match of content.matchAll(pattern)) {
        values.push(match[1] ?? match[2] ?? match[3]);
      }
      return {
        querySelectorAll(selector) {
          assert.equal(selector, "[data-history-locale]");
          return values.map((value) => ({
            getAttribute(name) {
              assert.equal(name, "data-history-locale");
              return value;
            },
          }));
        },
      };
    }
  }
  const sessionStorage = {
    removeItem(key) {
      if (storageRemoveThrows) throw new Error("storage removal failed");
      removedStorageKeys.push(key);
    },
  };
  const window = {location};
  if (htmx) window.htmx = {config: {refreshOnHistoryMiss: false}};
  Object.defineProperty(window, "sessionStorage", {
    get() {
      if (storageAccessThrows) throw new Error("storage unavailable");
      return sessionStorage;
    },
  });
  const context = vm.createContext({
    console,
    Date,
    document,
    DOMParser,
    globalThis: null,
    Intl: {DateTimeFormat},
    Number,
    Set,
    window,
  });
  context.globalThis = context;
  vm.runInContext(instrumented, context, {filename: scriptPath.pathname});
  return {
    behavior: context.localeBehavior,
    bodyListeners,
    bodyListenerRegistrations,
    document,
    htmx: window.htmx,
    listenerRegistrations,
    formatterCalls,
    listeners,
    location,
    cookie: () => cookie,
    hrefWrites: () => hrefWrites,
    reloads: () => reloads,
    removedStorageKeys,
  };
}

function choice(value) {
  return {
    dataset: {localeChoice: value},
    closest(selector) {
      assert.equal(selector, "[data-locale-choice]");
      return this;
    },
  };
}

test("explicit choices write the exact host-only cookie and reload without assigning a URL", () => {
  for (const [protocol, secure] of [["http:", ""], ["https:", "; Secure"]]) {
    const page = harness({protocol});
    const control = choice("ru");
    page.listeners.get("DOMContentLoaded")();
    assert.equal(page.cookie(), "", "initialization never writes automatically");
    page.listeners.get("click")({target: control});
    assert.equal(
      page.cookie(),
      `sopds_ui_language=ru; Max-Age=31536000; Path=/; SameSite=Lax${secure}`,
    );
    assert.equal(page.reloads(), 1);
    assert.equal(page.hrefWrites(), 0);
    assert.deepEqual(page.removedStorageKeys, ["htmx-history-cache"]);
    assert.deepEqual(
      [page.location.pathname, page.location.search, page.location.hash],
      ["/catalog", "?q=book", "#view=tree"],
    );
  }
});

test("initialization configures full reloads for HTMX history misses without duplicate bindings", () => {
  const page = harness();
  page.listeners.get("DOMContentLoaded")();
  page.behavior.initialize();

  assert.equal(page.htmx.config.refreshOnHistoryMiss, true);
  assert.equal(page.listenerRegistrations.get("click"), 1);
  assert.equal(page.listenerRegistrations.get("htmx:historyCacheHit"), 1);
  assert.equal(page.bodyListenerRegistrations.get("htmx:beforeSwap"), 1);
  assert.equal(page.bodyListenerRegistrations.get("htmx:afterSwap"), 1);

  const reader = harness({htmx: false});
  assert.doesNotThrow(() => reader.listeners.get("DOMContentLoaded")());
});

test("delegated locale choices support controls introduced and replaced after initialization", () => {
  const page = harness();
  page.listeners.get("DOMContentLoaded")();
  const click = page.listeners.get("click");

  assert.equal(click({target: {closest: () => null}}), false);
  const introduced = choice("ru");
  assert.equal(click({target: introduced}), true);
  assert.equal(page.cookie(), "sopds_ui_language=ru; Max-Age=31536000; Path=/; SameSite=Lax");

  const restored = choice("en");
  const restoredChild = {closest: () => restored};
  assert.equal(click({target: restoredChild}), true);
  assert.equal(page.cookie(), "sopds_ui_language=en; Max-Age=31536000; Path=/; SameSite=Lax");
  assert.equal(page.reloads(), 2);
  assert.deepEqual(page.removedStorageKeys, ["htmx-history-cache", "htmx-history-cache"]);
});

test("locale changes still reload when session storage is unavailable", () => {
  for (const failure of [
    {storageAccessThrows: true},
    {storageRemoveThrows: true},
  ]) {
    const page = harness(failure);
    assert.equal(page.behavior.chooseLocale("ru"), true);
    assert.equal(page.cookie(), "sopds_ui_language=ru; Max-Age=31536000; Path=/; SameSite=Lax");
    assert.equal(page.reloads(), 1);
    assert.equal(page.hrefWrites(), 0);
  }
});

test("invalid, regional, and case-changed choices are rejected without side effects", () => {
  const page = harness({protocol: "https:"});
  for (const value of ["RU", "ru-RU", "de", "", undefined]) {
    assert.equal(page.behavior.chooseLocale(value), false);
  }
  assert.equal(page.cookie(), "");
  assert.equal(page.reloads(), 0);
  assert.deepEqual(page.removedStorageKeys, []);
});

test("HTMX locale mismatches prevent fragment swaps and reload the exact current URL", () => {
  const page = harness({locale: "en"});
  page.listeners.get("DOMContentLoaded")();
  let prevented = 0;
  const detail = {
    shouldSwap: true,
    xhr: {getResponseHeader: (name) => name === "Content-Language" ? "ru" : null},
  };

  const handled = page.bodyListeners.get("htmx:beforeSwap")({
    detail,
    preventDefault() { prevented += 1; },
  });

  assert.equal(handled, true);
  assert.equal(detail.shouldSwap, false);
  assert.equal(prevented, 1);
  assert.equal(page.reloads(), 1);
  assert.equal(page.hrefWrites(), 0);
  assert.deepEqual(page.removedStorageKeys, ["htmx-history-cache"]);
  assert.deepEqual(
    [page.location.pathname, page.location.search, page.location.hash],
    ["/catalog", "?q=book", "#view=tree"],
  );
});

test("HTMX responses without a conflicting supported locale continue swapping", () => {
  const page = harness({locale: "ru"});
  page.listeners.get("DOMContentLoaded")();
  const beforeSwap = page.bodyListeners.get("htmx:beforeSwap");

  for (const responseLocale of ["ru", "en-US", null]) {
    let prevented = 0;
    const detail = {
      shouldSwap: true,
      xhr: {getResponseHeader: () => responseLocale},
    };
    assert.equal(beforeSwap({detail, preventDefault() { prevented += 1; }}), false);
    assert.equal(detail.shouldSwap, true);
    assert.equal(prevented, 0);
  }
  assert.equal(page.reloads(), 0);
  assert.deepEqual(page.removedStorageKeys, []);
});

test("same-locale HTMX history cache hits retain normal exact-context restoration", () => {
  const page = harness({locale: "ru"});
  page.listeners.get("DOMContentLoaded")();
  let prevented = 0;
  const handled = page.listeners.get("htmx:historyCacheHit")({
    detail: {
      path: "/catalog?q=book",
      item: {
        content: '<a class="skip-link"></a><div class="app-shell" data-history-locale="ru"><main>cached</main></div>',
      },
    },
    preventDefault() { prevented += 1; },
  });

  assert.equal(handled, false);
  assert.equal(prevented, 0);
  assert.equal(page.reloads(), 0);
  assert.equal(page.hrefWrites(), 0);
  assert.deepEqual(page.removedStorageKeys, []);
  assert.deepEqual(
    [page.location.pathname, page.location.search, page.location.hash],
    ["/catalog", "?q=book", "#view=tree"],
  );
});

test("stale, missing, and invalid HTMX history locales reload the exact URL", () => {
  const cachedBodies = [
    '<div class="app-shell" data-history-locale="en"><main>stale</main></div>',
    '<div class="app-shell"><main>pre-deployment</main></div>',
    '<div class="app-shell" data-history-locale="de"><main>invalid</main></div>',
  ];

  for (const content of cachedBodies) {
    const page = harness({locale: "ru"});
    page.listeners.get("DOMContentLoaded")();
    let prevented = 0;
    const handled = page.listeners.get("htmx:historyCacheHit")({
      detail: {path: "/catalog?q=book", item: {content}},
      preventDefault() { prevented += 1; },
    });

    assert.equal(handled, true);
    assert.equal(prevented, 1);
    assert.equal(page.reloads(), 1);
    assert.equal(page.hrefWrites(), 0);
    assert.deepEqual(page.removedStorageKeys, ["htmx-history-cache"]);
    assert.deepEqual(
      [page.location.pathname, page.location.search, page.location.hash],
      ["/catalog", "?q=book", "#view=tree"],
    );
  }
});

test("timestamp formatting uses the selected locale and preserves datetime and title", () => {
  const page = harness({locale: "ru"});
  const time = {
    dateTime: "2025-01-02T03:04:05+00:00",
    textContent: "original",
    title: "",
  };
  page.behavior.localizeCatalogTimes({querySelectorAll: () => [time]}, "ru");
  assert.equal(page.formatterCalls[0].locale, "ru");
  assert.deepEqual(
    JSON.parse(JSON.stringify(page.formatterCalls[0].options)),
    {dateStyle: "medium", timeStyle: "medium", hourCycle: "h23"},
  );
  assert.equal(time.dateTime, "2025-01-02T03:04:05+00:00");
  assert.equal(time.title, time.dateTime);
  assert.match(time.textContent, /^localized:/);
});

test("initial and HTMX-swapped timestamps use the safe DOM locale", () => {
  const page = harness({locale: "ru"});
  const initial = {dateTime: "2025-01-01T00:00:00Z", textContent: "", title: ""};
  page.document.querySelectorAll = (selector) => selector === "time.local-datetime" ? [initial] : [];
  page.listeners.get("DOMContentLoaded")();
  const swapped = {dateTime: "2025-01-02T00:00:00Z", textContent: "", title: ""};
  page.bodyListeners.get("htmx:afterSwap")({
    detail: {elt: {querySelectorAll: () => [swapped]}},
    target: null,
  });
  assert.deepEqual(page.formatterCalls.map((call) => call.locale), ["ru", "ru"]);
  assert.match(initial.textContent, /^localized:/);
  assert.match(swapped.textContent, /^localized:/);
});
