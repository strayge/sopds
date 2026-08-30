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
    chooseLocale,
    localizeCatalogTimes,
    initialize,
  };
`;
const instrumented = source.replace(
  '  if (document.readyState === "loading")',
  `${exportHook}\n  if (document.readyState === "loading")`,
);

function harness({protocol = "http:", locale = "en"} = {}) {
  const listeners = new Map();
  const bodyListeners = new Map();
  const choices = [];
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
    addEventListener(name, listener) { bodyListeners.set(name, listener); },
  };
  const document = {
    readyState: "loading",
    body,
    addEventListener(name, listener) { listeners.set(name, listener); },
    querySelectorAll(selector) {
      return selector === "[data-locale-choice]" ? choices : [];
    },
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
  const context = vm.createContext({
    console,
    Date,
    document,
    globalThis: null,
    Intl: {DateTimeFormat},
    Number,
    Set,
    window: {location},
  });
  context.globalThis = context;
  vm.runInContext(instrumented, context, {filename: scriptPath.pathname});
  return {
    behavior: context.localeBehavior,
    bodyListeners,
    choices,
    document,
    formatterCalls,
    listeners,
    location,
    cookie: () => cookie,
    hrefWrites: () => hrefWrites,
    reloads: () => reloads,
  };
}

function choice(value) {
  let listener;
  return {
    dataset: {localeChoice: value},
    addEventListener(name, callback) {
      assert.equal(name, "click");
      listener = callback;
    },
    click() { listener(); },
  };
}

test("explicit choices write the exact host-only cookie and reload without assigning a URL", () => {
  for (const [protocol, secure] of [["http:", ""], ["https:", "; Secure"]]) {
    const page = harness({protocol});
    page.choices.push(choice("ru"));
    page.listeners.get("DOMContentLoaded")();
    assert.equal(page.cookie(), "", "initialization never writes automatically");
    page.choices[0].click();
    assert.equal(
      page.cookie(),
      `sopds_ui_language=ru; Max-Age=31536000; Path=/; SameSite=Lax${secure}`,
    );
    assert.equal(page.reloads(), 1);
    assert.equal(page.hrefWrites(), 0);
    assert.deepEqual(
      [page.location.pathname, page.location.search, page.location.hash],
      ["/catalog", "?q=book", "#view=tree"],
    );
  }
});

test("invalid, regional, and case-changed choices are rejected without side effects", () => {
  const page = harness({protocol: "https:"});
  for (const value of ["RU", "ru-RU", "de", "", undefined]) {
    assert.equal(page.behavior.chooseLocale(value), false);
  }
  assert.equal(page.cookie(), "");
  assert.equal(page.reloads(), 0);
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
