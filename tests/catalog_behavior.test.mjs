import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const scriptPath = new URL("../src/sopds/web/static/catalog.js", import.meta.url);
const source = readFileSync(scriptPath, "utf8");
const exportHook = `
  globalThis.catalogBehavior = {
    DEFAULT_STATE,
    RENDER_EVENT,
    parseFragment,
    serializeFragment,
    presentationState,
    freshCriteriaState,
    appendPresentationFragment,
    synchronizeCriteriaLinks,
    normalizePhrase,
    unicodeScalarCompare,
    naturalTextCompare,
    parseSeriesNumber,
    compareSeriesNumberValues,
    flatComparator,
    tableComparator,
    matchesFilters,
    filterBooks,
    validatePayload,
    buildTreeModel,
    filterExpansionTransition,
    buildBookRow,
    buildActions,
    detailHistoryContext,
    emitRendered,
    CatalogController,
    initializeCatalog,
    initialize,
    getActiveController: () => activeController,
  };
`;
const instrumented = source.replace(
  '  if (document.readyState === "loading")',
  `${exportHook}\n  if (document.readyState === "loading")`,
);

function dataName(attribute) {
  return attribute.replace(/^data-/, "").replace(/-([a-z])/g, (_match, value) => value.toUpperCase());
}

class FakeNode {
  constructor(tagName = "#text", text = "") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = "";
    this._text = text;
    this.hidden = false;
    this.open = false;
    this.value = "";
  }

  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === "string" ? new FakeNode("#text", node) : node;
      child.parentNode = this;
      this.children.push(child);
    }
  }

  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }

  set textContent(value) {
    this.children = [];
    this._text = String(value ?? "");
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "id") this.id = String(value);
    if (name.startsWith("data-")) this.dataset[dataName(name)] = String(value);
  }

  getAttribute(name) {
    if (name === "href") return this.href || null;
    if (name === "id") return this.id || null;
    if (name.startsWith("data-")) {
      const value = this.dataset[dataName(name)];
      return value === undefined ? null : value;
    }
    return this.attributes.get(name) ?? null;
  }

  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }

  dispatch(name, event = {target: this}) {
    for (const listener of this.listeners.get(name) || []) listener(event);
  }

  matches(selector) {
    if (selector.startsWith("#")) return this.id === selector.slice(1);
    if (selector.startsWith(".")) return this.className.split(/\s+/).includes(selector.slice(1));
    const data = selector.match(/^\[data-([a-z0-9-]+)\]$/);
    if (data) return dataName(`data-${data[1]}`) in this.dataset;
    const tagData = selector.match(/^([a-z]+)\[data-([a-z0-9-]+)\]$/i);
    if (tagData) return this.tagName === tagData[1].toUpperCase() && dataName(`data-${tagData[2]}`) in this.dataset;
    return this.tagName === selector.toUpperCase();
  }

  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (child.matches(selector)) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches(selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
}

class FakeCustomEvent {
  constructor(type, options) {
    this.type = type;
    this.detail = options?.detail;
  }
}

const dispatched = [];
const documentListeners = new Map();
const documentStub = {
  readyState: "loading",
  currentRoot: null,
  addEventListener(name, listener, options = {}) {
    if (!documentListeners.has(name)) documentListeners.set(name, []);
    documentListeners.get(name).push({listener, once: options.once === true});
  },
  dispatchEvent(event) {
    dispatched.push(event);
    if (!event.target) event.target = this;
    const registrations = [...(documentListeners.get(event.type) || [])];
    for (const registration of registrations) registration.listener(event);
    documentListeners.set(event.type, (documentListeners.get(event.type) || []).filter(
      (registration) => !registration.once || !registrations.includes(registration),
    ));
    return true;
  },
  createElement(tag) {
    return new FakeNode(tag);
  },
  createTextNode(text) {
    return new FakeNode("#text", text);
  },
  querySelector(selector) {
    if (this.currentRoot?.matches(selector)) return this.currentRoot;
    return this.currentRoot?.querySelector(selector) || null;
  },
  querySelectorAll(selector) {
    const found = [];
    if (this.currentRoot?.matches(selector)) found.push(this.currentRoot);
    return found.concat(this.currentRoot?.querySelectorAll(selector) || []);
  },
  referrer: "",
};
const location = new URL("https://catalog.test/?q=book");
const windowStub = {
  location,
  history: {
    length: 1,
    state: null,
    replaceState() {},
    back() {},
  },
};
const context = vm.createContext({
  AbortController,
  console,
  CustomEvent: FakeCustomEvent,
  document: documentStub,
  globalThis: null,
  Map,
  Set,
  URL,
  URLSearchParams,
  window: windowStub,
});
context.globalThis = context;
vm.runInContext(instrumented, context, {filename: scriptPath.pathname});
const behavior = context.catalogBehavior;

function rawBook(id, overrides = {}) {
  const authorName = Object.hasOwn(overrides, "authorName") ? overrides.authorName : "Alpha Author";
  const seriesName = overrides.seriesName === undefined ? "Series" : overrides.seriesName;
  const seriesNumber = overrides.seriesNumber === undefined ? "1" : overrides.seriesNumber;
  return {
    publicId: id,
    title: overrides.title ?? `Title ${id}`,
    titleSortKey: overrides.titleSortKey ?? (overrides.title ?? `title ${id}`).toLowerCase(),
    authors: overrides.authors ?? (authorName === null ? [] : [{
      raw: authorName,
      display: authorName,
      sortKey: authorName.toLowerCase(),
      scopeUrl: `/?author=${encodeURIComponent(authorName)}`,
    }]),
    series: seriesName === null ? null : {
      name: seriesName,
      sortKey: seriesName.toLowerCase(),
      number: seriesNumber,
      scopeUrl: `/?series=${encodeURIComponent(seriesName)}`,
    },
    language: "en",
    sourceFormat: {key: "fb2", label: "FB2"},
    size: Object.hasOwn(overrides, "size") ? overrides.size : 1024,
    sizeLabel: "1 KB",
    publishedDate: "2024-01-02",
    availability: overrides.availability ?? "active",
    selectable: overrides.selectable ?? true,
    downloadable: overrides.downloadable ?? true,
    detailUrl: `/books/${id}`,
    readUrl: overrides.readUrl === undefined ? `/books/${id}/read` : overrides.readUrl,
    originalDownload: overrides.originalDownload === undefined
      ? {url: `/books/${id}/download`, label: "FB2"}
      : overrides.originalDownload,
    conversions: overrides.conversions ?? [{url: `/books/${id}/download/epub`, label: "EPUB"}],
  };
}

function books(...values) {
  return behavior.validatePayload({books: values, truncated: false}).books;
}

function ids(values) {
  return values.map((book) => book.publicId);
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

test("fragment state is bounded, allowlisted, canonical, and fresh criteria clear only quick filters", () => {
  const long = "x".repeat(220);
  const parsed = behavior.parseFragment(`#view=tree&flatSort=author&flatDir=desc&tableSort=number&tableDir=desc&title=${long}&author=A&series=S&unknown=yes`);
  assert.equal(parsed.view, "tree");
  assert.equal(parsed.flatSort, "author");
  assert.equal(parsed.tableSort, "number");
  assert.equal(parsed.title.length, 200);
  assert.equal(parsed.author, "A");
  assert.equal(behavior.parseFragment("#view=cards&flatSort=nope&flatDir=sideways").view, "flat");
  assert.equal(behavior.parseFragment("#view=cards&flatSort=nope&flatDir=sideways").flatSort, "title");

  const serialized = behavior.serializeFragment(parsed);
  const restored = behavior.parseFragment(serialized);
  assert.deepEqual(plain(restored), plain(parsed));
  const fresh = behavior.freshCriteriaState(parsed);
  assert.equal(fresh.view, "tree");
  assert.equal(fresh.flatDir, "desc");
  assert.equal(fresh.tableSort, "number");
  assert.equal(fresh.title, "");
  assert.equal(fresh.author, "");
  assert.equal(fresh.series, "");
});

test("criteria and HTMX history paths receive only validated view and sort state", () => {
  const state = behavior.parseFragment("#view=table&flatSort=series&flatDir=desc&tableSort=number&tableDir=desc&title=typed");
  const path = behavior.appendPresentationFragment("/?author=A%26B", state, "https://catalog.test/?q=old");
  assert.match(path, /^\/\?author=A%26B#view=table/);
  const restored = behavior.parseFragment(new URL(path, "https://catalog.test").hash);
  assert.equal(restored.view, "table");
  assert.equal(restored.tableSort, "number");
  assert.equal(restored.title, "");
  assert.equal(behavior.appendPresentationFragment("https://evil.test/", state, "https://catalog.test/"), "https://evil.test/");
});

test("phrase normalization is NFKC, case-insensitive, ё/е equivalent, and punctuation-collapsing", () => {
  assert.equal(behavior.normalizePhrase("  Ａ—Ёж, Том_2!! "), "a еж том 2");
  assert.equal(behavior.normalizePhrase("Еж"), "е ж");
  const [book] = books(rawBook("one", {
    title: "Ёж и Ａльфа",
    authorName: "Иванов, Иван",
    seriesName: "Том: 2",
  }));
  assert.equal(behavior.matchesFilters(book, {title: "еж и a", author: "иванов иван", series: "том 2"}), true);
  assert.equal(behavior.matchesFilters(book, {title: "еж", author: "петров", series: "том"}), false);
  assert.equal(behavior.matchesFilters(book, {title: "альфа еж"}), false, "each field is one ordered phrase");
});

test("author filtering checks all authors rather than only displayed overflow authors", () => {
  const authors = ["A", "B", "C", "D", "E", "Hidden Sixth"].map((name) => ({
    raw: name, display: name, sortKey: name.toLowerCase(), scopeUrl: `/?author=${name}`,
  }));
  const [book] = books(rawBook("many", {authors}));
  assert.equal(behavior.matchesFilters(book, {author: "hidden sixth"}), true);
});

test("Unicode scalar and natural text helpers do not depend on ambient locale or Number precision", () => {
  assert.equal(behavior.unicodeScalarCompare("\u{10000}", "\uE000"), 1);
  assert.ok(behavior.naturalTextCompare("Part 2", "Part 10") < 0);
  assert.ok(behavior.compareSeriesNumberValues("9007199254740993", "9007199254740992") > 0);
});

test("natural series numbers follow every ascending bucket and deterministic suffix rule", () => {
  const values = [null, " ", "0.6", "0", "Part 2", "10", "3.5", "2", "1-A", "01a", "1"];
  values.sort((left, right) => behavior.compareSeriesNumberValues(left, right, "asc"));
  assert.deepEqual(values, ["1", "1-A", "01a", "2", "3.5", "10", "Part 2", "0", "0.6", " ", null]);
});

test("compatibility digits remain in the text series-number bucket in both directions", () => {
  assert.equal(behavior.parseSeriesNumber("１").bucket, "text");
  assert.equal(behavior.parseSeriesNumber("²").bucket, "text");
  const values = ["１", "2", "²"];
  assert.deepEqual(
    [...values].sort((left, right) => behavior.compareSeriesNumberValues(left, right, "asc")),
    ["2", "１", "²"],
  );
  assert.deepEqual(
    [...values].sort((left, right) => behavior.compareSeriesNumberValues(left, right, "desc")),
    ["²", "１", "2"],
  );
});

test("natural series suffix comparison preserves punctuation and spacing", () => {
  const values = ["1-B10", "1-B2", "1 B10"];
  values.sort((left, right) => behavior.compareSeriesNumberValues(left, right, "asc"));
  assert.deepEqual(values, ["1 B10", "1-B2", "1-B10"]);
});

test("descending Number keeps text first and zero/missing last", () => {
  const values = ["1", "01a", "1-A", "2", "3.5", "10", "Part 2", "0", "0.6", "", null];
  values.sort((left, right) => behavior.compareSeriesNumberValues(left, right, "desc"));
  assert.deepEqual(values, ["Part 2", "10", "3.5", "2", "01a", "1-A", "1", "0.6", "0", "", null]);
});

test("Flat comparator chains cover Title, Author, Series, directions, missing values, and public-ID ties", () => {
  const values = books(
    rawBook("b", {title: "Same", titleSortKey: "same", authorName: "A", seriesName: "S", seriesNumber: "2"}),
    rawBook("a", {title: "Same", titleSortKey: "same", authorName: "A", seriesName: "S", seriesNumber: "2"}),
    rawBook("author-b", {title: "A", titleSortKey: "a", authorName: "B", seriesName: "A", seriesNumber: "1"}),
    rawBook("series-b", {title: "B", titleSortKey: "b", authorName: "A", seriesName: "Z", seriesNumber: "1"}),
    rawBook("missing", {title: "Z", titleSortKey: "z", authorName: null, seriesName: null}),
  );
  for (const sort of ["title", "author", "series"]) {
    const ascending = [...values].sort(behavior.flatComparator(sort, "asc"));
    const descending = [...values].sort(behavior.flatComparator(sort, "desc"));
    assert.deepEqual(ids(descending), ids(ascending).reverse(), `${sort} direction reverses its full ordinary chain`);
  }
  assert.ok(ids([...values].sort(behavior.flatComparator("title", "asc"))).indexOf("a") < ids([...values].sort(behavior.flatComparator("title", "asc"))).indexOf("b"));
  assert.equal([...values].sort(behavior.flatComparator("author", "asc")).at(-1).publicId, "missing");
  assert.equal([...values].sort(behavior.flatComparator("author", "desc"))[0].publicId, "missing");
  assert.equal([...values].sort(behavior.flatComparator("series", "asc")).at(-1).publicId, "missing");
});

test("Author and Series chains use natural number and subsequent metadata tie-breakers", () => {
  const values = books(
    rawBook("two", {authorName: "A", seriesName: "S", seriesNumber: "10", titleSortKey: "a"}),
    rawBook("one", {authorName: "A", seriesName: "S", seriesNumber: "2", titleSortKey: "z"}),
    rawBook("other-series", {authorName: "A", seriesName: "T", seriesNumber: "1", titleSortKey: "a"}),
    rawBook("other-author", {authorName: "B", seriesName: "A", seriesNumber: "1", titleSortKey: "a"}),
  );
  assert.deepEqual(ids([...values].sort(behavior.flatComparator("author", "asc"))), ["one", "two", "other-series", "other-author"]);
  assert.deepEqual(ids([...values].sort(behavior.flatComparator("series", "asc"))), ["other-author", "one", "two", "other-series"]);
});

test("Table ordinary comparator chains work in both directions and Number uses series/title/ID ties", () => {
  const ordinary = books(
    rawBook("b", {titleSortKey: "same", authorName: "B", seriesName: "B"}),
    rawBook("a", {titleSortKey: "same", authorName: "A", seriesName: "A"}),
    rawBook("missing", {titleSortKey: "z", authorName: null, seriesName: null}),
  );
  for (const sort of ["author", "title", "series"]) {
    const asc = [...ordinary].sort(behavior.tableComparator(sort, "asc"));
    const desc = [...ordinary].sort(behavior.tableComparator(sort, "desc"));
    assert.deepEqual(ids(desc), ids(asc).reverse());
  }
  const numbered = books(
    rawBook("z", {titleSortKey: "a", seriesName: "B", seriesNumber: "2"}),
    rawBook("b", {titleSortKey: "same", seriesName: "A", seriesNumber: "2"}),
    rawBook("a", {titleSortKey: "same", seriesName: "A", seriesNumber: "2"}),
    rawBook("text", {seriesName: "Z", seriesNumber: "Part 3"}),
    rawBook("zero", {seriesName: "Z", seriesNumber: "0"}),
    rawBook("none", {seriesName: null}),
  );
  assert.deepEqual(ids([...numbered].sort(behavior.tableComparator("number", "asc"))), ["a", "b", "z", "text", "zero", "none"]);
  assert.deepEqual(ids([...numbered].sort(behavior.tableComparator("number", "desc"))), ["text", "z", "b", "a", "zero", "none"]);
});

test("Tree grouping applies no-author, one, duplicate 2–5, and Many authors 6+ rules", () => {
  const makeAuthors = (count, prefix) => Array.from({length: count}, (_value, index) => ({
    raw: `${prefix}${index}`,
    display: `${prefix}${index}`,
    sortKey: `${prefix.toLowerCase()}${index}`,
    scopeUrl: `/?author=${prefix}${index}`,
  }));
  const values = books(
    rawBook("unknown", {authors: [], seriesName: null}),
    rawBook("one", {authors: makeAuthors(1, "Solo"), seriesName: null}),
    rawBook("two", {authors: makeAuthors(2, "Duo"), seriesName: "Named", seriesNumber: "2"}),
    rawBook("five", {authors: makeAuthors(5, "Five"), seriesName: "Named", seriesNumber: "1"}),
    rawBook("six", {authors: makeAuthors(6, "Six"), seriesName: "Named", seriesNumber: "3"}),
  );
  const tree = behavior.buildTreeModel(values);
  const labels = tree.map((branch) => branch.label);
  assert.deepEqual(plain(labels), [...plain(labels)].sort(), "synthetic and ordinary displayed labels share strict order");
  assert.equal(tree.filter((branch) => branch.books.some((book) => book.publicId === "two")).length, 2);
  assert.equal(tree.filter((branch) => branch.books.some((book) => book.publicId === "five")).length, 5);
  assert.equal(tree.filter((branch) => branch.books.some((book) => book.publicId === "six")).length, 1);
  assert.equal(tree.find((branch) => branch.books.some((book) => book.publicId === "six")).label, "Many authors (6+)");
  assert.equal(tree.find((branch) => branch.books.some((book) => book.publicId === "unknown")).label, "Unknown author");
});

test("Tree branch counts are unique, named series sort naturally by name, and no-series is last", () => {
  const duplicateAuthor = [{raw: "A", display: "A", sortKey: "a", scopeUrl: "/?author=A"}];
  const tree = behavior.buildTreeModel(books(
    rawBook("two", {authors: duplicateAuthor, seriesName: "B", seriesNumber: "10"}),
    rawBook("one", {authors: duplicateAuthor, seriesName: "A", seriesNumber: "2"}),
    rawBook("none", {authors: duplicateAuthor, seriesName: null}),
  ));
  assert.equal(tree[0].count, 3);
  assert.deepEqual(plain(tree[0].series.map((leaf) => leaf.label)), ["A", "B", "Books without series"]);
  assert.equal(tree[0].series.at(-1).withoutSeries, true);
});

test("a named none series and Books without series retain independent expansion identities", () => {
  const values = books(
    rawBook("named-none", {authorName: "A", seriesName: "none"}),
    rawBook("without-series", {authorName: "A", seriesName: null}),
  );
  const branch = behavior.buildTreeModel(values)[0];
  const named = branch.series.find((leaf) => leaf.label === "none");
  const without = branch.series.find((leaf) => leaf.withoutSeries);
  assert.notEqual(named.key, without.key);

  const fixture = controllerRoot();
  const controller = new behavior.CatalogController(
    fixture.root,
    {books: values, truncated: false},
    behavior.parseFragment("#view=tree"),
  );
  controller.expansion = new Map([[named.key, true], [without.key, false]]);
  controller.render();
  const leaves = fixture.mount.querySelectorAll(".catalog-tree-series");
  assert.equal(leaves[0].open, true);
  assert.equal(leaves[1].open, false);
  controller.destroy();
});

test("filter expansion snapshots once and restores the user's state", () => {
  const expansion = new Map([["author:A", false], ["author:A|series:S", true]]);
  const started = behavior.filterExpansionTransition(expansion, null, false, true);
  expansion.set("author:A", true);
  const continued = behavior.filterExpansionTransition(expansion, started.snapshot, true, true);
  assert.equal(continued.snapshot.get("author:A"), false);
  const cleared = behavior.filterExpansionTransition(expansion, continued.snapshot, true, false);
  assert.equal(cleared.expansion.get("author:A"), false);
  assert.equal(cleared.expansion.get("author:A|series:S"), true);
  assert.equal(cleared.snapshot, null);
});

test("payload validation deduplicates IDs and rejects unsafe actions without trusting markup", () => {
  const hostile = rawBook("safe", {title: '<img src=x onerror="bad">'});
  hostile.readUrl = "https://evil.test/read";
  hostile.conversions.push({url: "//evil.test/file", label: "EVIL"});
  const payload = behavior.validatePayload({books: [hostile, rawBook("safe"), {publicId: "broken"}], truncated: true});
  assert.equal(payload.books.length, 1);
  assert.equal(payload.truncated, true);
  assert.equal(payload.books[0].readUrl, null);
  assert.deepEqual(plain(payload.books[0].conversions), [{url: "/books/safe/download/epub", label: "EPUB"}]);
  const row = behavior.buildBookRow(payload.books[0]);
  assert.match(row.textContent, /<img src=x onerror="bad">/);
  assert.equal(row.querySelectorAll("img").length, 0, "metadata remains text rather than parsed markup");
});

test("payload validation preserves supplementary-plane text and rejects only unpaired surrogates", () => {
  const supplementary = rawBook("unicode", {
    title: "Emoji 😀 and CJK 𠀀",
    authorName: "Author 𠀀",
  });
  supplementary.originalDownload.label = "FB2 😀";
  const malformedTitle = rawBook("bad-title", {title: "Broken \ud800 title"});
  const malformedMetadata = rawBook("partial");
  malformedMetadata.authors[0].display = "Broken \udc00 author";
  malformedMetadata.originalDownload.label = "Broken \ud800 action";

  const payload = behavior.validatePayload({books: [supplementary, malformedTitle, malformedMetadata]});
  assert.deepEqual(plain(ids(payload.books)), ["unicode", "partial"]);
  assert.equal(payload.books[0].authors[0].display, "Author 𠀀");
  assert.equal(payload.books[0].originalDownload.label, "FB2 😀");
  assert.equal(payload.books[1].authors.length, 0);
  assert.equal(payload.books[1].originalDownload, null);
  assert.match(behavior.buildBookRow(payload.books[0]).textContent, /Emoji 😀 and CJK 𠀀/);
});

test("malformed availability or size fails closed for all downloadable actions", () => {
  const values = books(
    rawBook("bad-availability", {availability: "unknown"}),
    rawBook("bad-size", {size: "1024"}),
  );
  assert.equal(values.length, 2, "metadata and Details remain discoverable");
  for (const book of values) {
    assert.equal(book.downloadable, false);
    assert.equal(book.selectable, false);
    assert.equal(book.readUrl, null);
    assert.equal(book.originalDownload, null);
    assert.deepEqual(plain(book.conversions), []);
    const actions = behavior.buildActions(book);
    assert.equal(actions.querySelectorAll("[data-selection-checkbox]").length, 0);
    assert.doesNotMatch(actions.textContent, /Read|FB2|EPUB/);
    assert.match(actions.textContent, /Details/);
  }
});

test("shared actions expose selection/read/download/conversions/details only when supplied", () => {
  const [eligible, missed] = books(
    rawBook("eligible"),
    rawBook("missed", {availability: "missed", selectable: false, downloadable: false, readUrl: null, originalDownload: null, conversions: []}),
  );
  const eligibleActions = behavior.buildActions(eligible);
  assert.equal(eligibleActions.querySelectorAll("[data-selection-checkbox]").length, 1);
  assert.match(eligibleActions.textContent, /Read/);
  assert.match(eligibleActions.textContent, /FB2/);
  assert.match(eligibleActions.textContent, /EPUB/);
  assert.match(eligibleActions.textContent, /Details/);
  const missedActions = behavior.buildActions(missed);
  assert.equal(missedActions.querySelectorAll("[data-selection-checkbox]").length, 0);
  assert.doesNotMatch(missedActions.textContent, /Read|FB2|EPUB/);
  assert.match(missedActions.textContent, /Details/);
});

function controllerRoot() {
  const root = new FakeNode("section");
  root.dataset.catalogRoot = "";
  const summary = new FakeNode("p", "Loaded 2 books in title order.");
  summary.id = "catalog-loaded-summary";
  const views = ["flat", "tree", "table"].map((name) => {
    const button = new FakeNode("button", name);
    button.dataset.catalogView = name;
    return button;
  });
  const filters = ["title", "author", "series"].map((name) => {
    const input = new FakeNode("input");
    input.dataset.catalogFilter = name;
    return input;
  });
  const clear = new FakeNode("button", "Clear local filters");
  clear.dataset.catalogClearFilters = "";
  const sort = new FakeNode("div");
  sort.dataset.catalogSortControls = "";
  const mount = new FakeNode("div");
  mount.dataset.catalogResultView = "";
  root.append(summary, ...views, ...filters, clear, sort, mount);
  return {root, mount, summary};
}

test("controller keeps exactly one active renderer and emits once per complete render", () => {
  dispatched.length = 0;
  const fixture = controllerRoot();
  const values = books(rawBook("one"), rawBook("two", {seriesName: null}));
  const controller = new behavior.CatalogController(fixture.root, {books: values, truncated: false}, behavior.parseFragment("#view=flat"));
  assert.equal(fixture.mount.children.length, 1);
  assert.match(fixture.mount.children[0].className, /catalog-flat-view/);
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].type, "sopds:catalog-rendered");
  assert.equal(dispatched[0].detail.root, fixture.mount);

  controller.state.view = "table";
  controller.render();
  assert.equal(fixture.mount.children.length, 1);
  assert.match(fixture.mount.children[0].className, /catalog-table-scroll/);
  assert.equal(fixture.mount.querySelectorAll("table").length, 1);
  assert.equal(dispatched.length, 2);
});

test("Tree leaves start lazy, authors start open, and opening one leaf emits one lazy event", () => {
  dispatched.length = 0;
  const fixture = controllerRoot();
  const values = books(rawBook("one"));
  const controller = new behavior.CatalogController(fixture.root, {books: values, truncated: false}, behavior.parseFragment("#view=tree"));
  const author = fixture.mount.querySelector(".catalog-tree-author");
  const leaf = fixture.mount.querySelector(".catalog-tree-series");
  assert.equal(author.open, true);
  assert.equal(leaf.open, false);
  assert.equal(fixture.mount.querySelectorAll(".catalog-tree-books").length, 0);
  assert.equal(dispatched.length, 1);
  leaf.open = true;
  leaf.dispatch("toggle");
  const lazyRows = fixture.mount.querySelector(".catalog-tree-books");
  assert.ok(lazyRows);
  const lazyCriteriaLink = lazyRows.querySelector("a[data-catalog-criteria-link]");
  const lazyState = behavior.parseFragment(new URL(lazyCriteriaLink.href, location.href).hash);
  assert.equal(lazyState.view, "tree");
  assert.equal(lazyState.title, "");
  assert.equal(dispatched.length, 2);
  assert.equal(dispatched[1].detail.root, fixture.mount);
  controller.destroy();
});

test("local count copy is truthful for loaded and truncated zero results and restores on clear", () => {
  const fixture = controllerRoot();
  const values = books(rawBook("one"), rawBook("two"));
  const controller = new behavior.CatalogController(fixture.root, {books: values, truncated: true}, behavior.parseFragment("#title=missing"));
  assert.match(fixture.summary.textContent, /^0 of 2 loaded books/);
  assert.match(fixture.summary.textContent, /Additional catalog matches were not loaded/);
  assert.match(fixture.mount.textContent, /No loaded books match/);
  controller.state.title = "title";
  controller.render();
  assert.equal(fixture.summary.textContent, "2 of 2 loaded books.");
  controller.state.title = "";
  controller.render();
  assert.equal(fixture.summary.textContent, "Loaded 2 books in title order.");
});

test("detail history enhancement accepts only same-origin catalog or selected referrers with prior history", () => {
  assert.deepEqual(plain(behavior.detailHistoryContext("https://catalog.test/?q=x#view=tree", "https://catalog.test/books/1", 2)), {fallback: "/", canGoBack: true});
  assert.deepEqual(plain(behavior.detailHistoryContext("https://catalog.test/selected", "https://catalog.test/books/1", 2)), {fallback: "/selected", canGoBack: true});
  assert.deepEqual(plain(behavior.detailHistoryContext("https://catalog.test/selected", "https://catalog.test/books/1", 1)), {fallback: "/selected", canGoBack: false});
  assert.deepEqual(plain(behavior.detailHistoryContext("https://catalog.test/manage", "https://catalog.test/books/1", 4)), {fallback: "/", canGoBack: false});
  assert.deepEqual(plain(behavior.detailHistoryContext("https://evil.test/", "https://catalog.test/books/1", 4)), {fallback: "/", canGoBack: false});
  assert.deepEqual(plain(behavior.detailHistoryContext("", "https://catalog.test/books/1", 4)), {fallback: "/", canGoBack: false});
});

function explorerRoot(rawBooks) {
  const fixture = controllerRoot();
  const payload = new FakeNode("script");
  payload.dataset.catalogPayload = "";
  payload.textContent = JSON.stringify({books: rawBooks, truncated: false});
  fixture.root.append(payload);
  return fixture;
}

test("persistent and rendered criteria hrefs are ready for middle-click and context navigation", () => {
  const page = new FakeNode("main");
  const scopeRemoval = new FakeNode("a", "Remove");
  scopeRemoval.dataset.catalogCriteriaLink = "";
  scopeRemoval.href = "/?q=old";
  const clearAll = new FakeNode("a", "Clear all");
  clearAll.dataset.catalogCriteriaLink = "";
  clearAll.href = "/";
  const fixture = explorerRoot([rawBook("one")]);
  page.append(scopeRemoval, clearAll, fixture.root);
  documentStub.currentRoot = page;
  location.href = "https://catalog.test/?q=book#view=table&flatSort=series&flatDir=desc&tableSort=number&tableDir=desc&title=title";
  windowStub.history.replaceState = (_state, _title, value) => {
    location.href = new URL(value, location.href).href;
  };

  const controller = behavior.initializeCatalog(fixture.root);
  const renderedLinks = fixture.mount.querySelectorAll("a[data-catalog-criteria-link]");
  assert.ok(renderedLinks.length >= 2, "rendered Author and Series links are present");
  for (const link of [scopeRemoval, clearAll, ...renderedLinks]) {
    const state = behavior.parseFragment(new URL(link.href, location.href).hash);
    assert.equal(state.view, "table");
    assert.equal(state.tableSort, "number");
    assert.equal(state.title, "", "quick filters are absent from criteria destinations");
  }

  controller.state.view = "flat";
  controller.state.flatSort = "author";
  controller.state.flatDir = "asc";
  controller.commit();
  controller.render();
  const currentRenderedLink = fixture.mount.querySelector("a[data-catalog-criteria-link]");
  for (const link of [scopeRemoval, clearAll, currentRenderedLink]) {
    const state = behavior.parseFragment(new URL(link.href, location.href).hash);
    assert.equal(state.view, "flat");
    assert.equal(state.flatSort, "author");
    assert.equal(state.title, "");
  }

  const readyHref = currentRenderedLink.href;
  documentStub.dispatchEvent({type: "auxclick", target: currentRenderedLink, button: 1, defaultPrevented: false});
  documentStub.dispatchEvent({type: "contextmenu", target: currentRenderedLink, defaultPrevented: false});
  assert.equal(currentRenderedLink.href, readyHref, "non-primary navigation needs no click-time mutation");
  behavior.initializeCatalog(null);
});

test("real HTMX event sequence preserves presentation state and separates fresh swaps from history restoration", () => {
  const initial = explorerRoot([rawBook("initial")]);
  documentStub.currentRoot = initial.root;
  location.href = "https://catalog.test/?q=initial#view=table&flatSort=series&flatDir=desc&tableSort=number&tableDir=desc&title=typed&author=writer&series=saga";
  windowStub.history.replaceState = (_state, _title, value) => {
    location.href = new URL(value, location.href).href;
  };

  documentStub.dispatchEvent({type: "DOMContentLoaded"});
  const initialController = behavior.getActiveController();
  assert.equal(initialController.root, initial.root);
  assert.equal(initialController.state.title, "typed");

  const criteriaLink = new FakeNode("a", "Author");
  criteriaLink.dataset.catalogCriteriaLink = "";
  criteriaLink.href = "/?author=Author";
  documentStub.dispatchEvent({type: "click", target: criteriaLink, defaultPrevented: false});
  const criteriaState = behavior.parseFragment(new URL(criteriaLink.href, location.href).hash);
  assert.equal(criteriaState.view, "table");
  assert.equal(criteriaState.tableSort, "number");
  assert.equal(criteriaState.title, "");
  assert.equal(criteriaState.author, "");
  assert.equal(criteriaState.series, "");

  const freshHistory = {path: "/?q=fresh"};
  documentStub.dispatchEvent({
    type: "htmx:beforeHistoryUpdate",
    detail: {history: freshHistory},
  });
  const freshPathState = behavior.parseFragment(new URL(freshHistory.path, location.href).hash);
  assert.equal(freshPathState.view, "table");
  assert.equal(freshPathState.tableDir, "desc");
  assert.equal(freshPathState.title, "");

  const fresh = explorerRoot([rawBook("fresh")]);
  documentStub.currentRoot = fresh.root;
  location.href = new URL(freshHistory.path, location.href).href;
  documentStub.dispatchEvent({
    type: "htmx:afterSwap",
    detail: {target: fresh.root},
    target: fresh.root,
  });
  const freshController = behavior.getActiveController();
  assert.notEqual(freshController, initialController);
  assert.equal(initialController.abort.signal.aborted, true);
  assert.deepEqual(plain(ids(freshController.books)), ["fresh"]);
  assert.equal(freshController.state.view, "table");
  assert.equal(freshController.state.tableSort, "number");
  assert.equal(freshController.state.title, "");

  const swappedForm = new FakeNode("form");
  const swappedClear = new FakeNode("a", "Clear all");
  swappedClear.dataset.catalogCriteriaLink = "";
  swappedClear.href = "/";
  swappedForm.append(swappedClear);
  documentStub.dispatchEvent({
    type: "htmx:afterSwap",
    detail: {target: swappedForm},
    target: swappedForm,
  });
  const swappedClearState = behavior.parseFragment(new URL(swappedClear.href, location.href).hash);
  assert.equal(swappedClearState.view, "table");
  assert.equal(swappedClearState.tableSort, "number");
  assert.equal(swappedClearState.title, "");

  const cached = explorerRoot([rawBook("cached")]);
  documentStub.currentRoot = cached.root;
  location.href = "https://catalog.test/?q=cached#view=tree&flatSort=author&flatDir=asc&tableSort=title&tableDir=asc&title=restored";
  documentStub.dispatchEvent({type: "htmx:historyCacheHit"});
  documentStub.dispatchEvent({
    type: "htmx:afterSwap",
    detail: {target: cached.root},
    target: cached.root,
  });
  assert.equal(behavior.getActiveController(), freshController, "cache restoration waits for its final history event");
  documentStub.dispatchEvent({type: "htmx:historyRestore"});
  const cachedController = behavior.getActiveController();
  assert.equal(freshController.abort.signal.aborted, true);
  assert.deepEqual(plain(ids(cachedController.books)), ["cached"]);
  assert.equal(cachedController.state.view, "tree");
  assert.equal(cachedController.state.title, "restored");

  const uncached = explorerRoot([rawBook("uncached")]);
  documentStub.currentRoot = uncached.root;
  location.href = "https://catalog.test/?q=uncached#view=flat&flatSort=title&flatDir=asc&tableSort=author&tableDir=asc&author=history";
  documentStub.dispatchEvent({type: "htmx:historyCacheMissLoad"});
  documentStub.dispatchEvent({
    type: "htmx:afterSwap",
    detail: {target: uncached.root},
    target: uncached.root,
  });
  assert.equal(behavior.getActiveController(), cachedController, "cache-miss swaps also wait for restoration completion");
  documentStub.dispatchEvent({type: "htmx:historyRestore"});
  const uncachedController = behavior.getActiveController();
  assert.equal(cachedController.abort.signal.aborted, true);
  assert.deepEqual(plain(ids(uncachedController.books)), ["uncached"]);
  assert.equal(uncachedController.state.author, "history");
});
