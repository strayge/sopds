import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import vm from "node:vm";

const scriptPath = new URL("../src/sopds/web/static/selection.js", import.meta.url);
const source = readFileSync(scriptPath, "utf8");
const exportHook = `
  globalThis.selectionBehavior = {
    mergeSelectedPreview,
    syncFormatSelector,
    compareSelectedMetadata,
    selectedGroupKey,
    mergeSelectedSearchUrls,
    selectionGroupIds,
    syncGroupCheckboxes,
    initialize,
    setState(ids, previewIds) {
      selectedIds = ids;
      authoritativePreviewIds = previewIds === null ? null : new Set(previewIds);
    },
  };
`;
const instrumented = source.replace(
  '  if (document.readyState === "loading") {',
  `${exportHook}\n  if (document.readyState === "loading") {`,
);

class ClassList {
  constructor(values = []) {
    this.values = new Set(values);
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  toggle(value, enabled) {
    enabled ? this.add(value) : this.remove(value);
  }
}

function makeChild(kind, text = "") {
  return {
    kind,
    text,
    parent: null,
    matches(selector) {
      return selector === "[data-selection-control]" && this.kind === "control";
    },
    cloneNode() {
      return makeChild(this.kind, this.text);
    },
    remove() {
      const index = this.parent?.children.indexOf(this) ?? -1;
      if (index >= 0) {
        this.parent.children.splice(index, 1);
      }
    },
  };
}

function makeEntry(publicId, {status, outputFormats, bodyText, included = true}) {
  const control = makeChild("control");
  control.hidden = false;
  const body = makeChild("body", bodyText);
  const entry = {
    className: `result-row result-row--${status}`,
    classList: new ClassList(),
    dataset: {
      publicId,
      status,
      collision: "false",
      sourceDownloadable: "true",
      sourceFormat: "FB2",
      outputFormats,
      included: String(included),
    },
    children: [control, body],
    querySelector(selector) {
      if (selector === ":scope > [data-selection-control]") return control;
      if (selector === "[data-collision-notice]") return null;
      if (selector === ".result-row__body") return this.children.find((child) => child.kind === "body");
      return null;
    },
    append(child) {
      child.parent = this;
      this.children.push(child);
    },
    cloneNode() {
      return makeEntry(publicId, {status, outputFormats, bodyText, included});
    },
  };
  for (const child of entry.children) child.parent = entry;
  return {entry, control};
}

function makeEntries(entries) {
  return {
    entries,
    matches(selector) {
      return selector.includes("[data-selected-entries]");
    },
    querySelectorAll(selector) {
      return selector === "[data-selected-entry]" ? this.entries : [];
    },
    querySelector(selector) {
      return selector === "[data-selected-entry]" ? this.entries[0] || null : null;
    },
    append(entry) {
      this.entries.push(entry);
    },
  };
}

function makeContent(entries, archiveFormat = "epub") {
  const entryContainer = makeEntries(entries);
  return {
    dataset: {
      selectedCount: String(entries.length),
      downloadableCount: String(entries.length),
      totalSize: "10",
      archiveFormat,
      catalogGeneration: "1",
    },
    children: [entryContainer],
    querySelector(selector) {
      if (selector === "[data-selected-entries]") return entryContainer;
      if (selector === "[data-selected-empty]") return null;
      return null;
    },
    querySelectorAll(selector) {
      return selector === "[data-selected-entry]" ? entryContainer.entries : [];
    },
    insertBefore() {},
  };
}

const documentListeners = new Map();
const windowListeners = new Map();
let documentCheckboxes = [];
let documentGroups = [];
let storedSelection = null;
const documentStub = {
  readyState: "loading",
  addEventListener(name, listener) {
    if (!documentListeners.has(name)) documentListeners.set(name, []);
    documentListeners.get(name).push(listener);
  },
  dispatch(name, event = {}) {
    for (const listener of documentListeners.get(name) || []) listener(event);
  },
  querySelectorAll(selector) {
    if (selector === "[data-selection-checkbox]") return documentCheckboxes;
    if (selector === "[data-selection-group]") return documentGroups;
    return [];
  },
  querySelector() {
    return null;
  },
  createElement(tag) {
    assert.equal(tag, "option");
    return makeOption("", "");
  },
};
const windowStub = {
  addEventListener(name, listener) {
    if (!windowListeners.has(name)) windowListeners.set(name, []);
    windowListeners.get(name).push(listener);
  },
  dispatch(name, event = {}) {
    for (const listener of windowListeners.get(name) || []) listener(event);
  },
  localStorage: {
    getItem() {
      return storedSelection;
    },
    setItem(_key, value) {
      storedSelection = value;
    },
  },
};
const context = vm.createContext({
  AbortController,
  console,
  document: documentStub,
  globalThis: null,
  Set,
  URL,
  URLSearchParams,
  window: windowStub,
});
context.globalThis = context;
vm.runInContext(instrumented, context, {filename: scriptPath.pathname});
const behavior = context.selectionBehavior;

function makeOption(value, textContent) {
  return {
    value,
    textContent,
    owner: null,
    remove() {
      const index = this.owner?.options.indexOf(this) ?? -1;
      if (index >= 0) this.owner.options.splice(index, 1);
    },
  };
}

function makeSelector(value, targets) {
  const selector = {
    value,
    options: [makeOption("original", "Original"), ...targets.map((target) => makeOption(target, target.toUpperCase()))],
    querySelector(query) {
      const match = query.match(/^option\[value="([^"]+)"\]$/);
      return match ? this.options.find((option) => option.value === match[1]) || null : null;
    },
    append(option) {
      option.owner = this;
      this.options.push(option);
    },
  };
  for (const option of selector.options) option.owner = selector;
  return selector;
}

function makePage(selector, rows) {
  return {
    rows,
    querySelector(query) {
      return query === "[data-selected-format]" ? selector : null;
    },
    querySelectorAll(query) {
      if (query === "[data-selected-entry]") return this.rows;
      if (query.includes('[data-included="true"]')) {
        return this.rows.filter(
          (row) => row.dataset.included === "true" && row.dataset.sourceDownloadable === "true",
        );
      }
      return [];
    },
  };
}

function makeSelectionGroup(publicIds) {
  return {
    checked: false,
    indeterminate: false,
    disabled: true,
    dataset: {publicIds: JSON.stringify(publicIds), selectionGroup: ""},
    closest(selector) {
      return selector === "[data-selection-group]" ? this : null;
    },
  };
}

function makeSelectionCheckbox(publicId, checked = false) {
  const control = {hidden: true};
  return {
    checked,
    disabled: true,
    dataset: {publicId},
    closest(selector) {
      if (selector === "[data-selection-checkbox]") return this;
      return selector === "[data-selection-control]" ? control : null;
    },
    control,
  };
}

test("selected Tree metadata groups visible names and merges availability searches", () => {
  assert.equal(behavior.selectedGroupKey("author", "Ａuthor"), "author:author");
  assert.equal(behavior.selectedGroupKey("series", "Saga"), "series:saga");
  assert.equal(
    behavior.mergeSelectedSearchUrls([
      "/?q=Writer&search_field=author",
      "/?q=Writer&search_field=author&include_hidden=true",
      "/?q=Writer&search_field=author&include_missed=true",
    ]),
    "/?q=Writer&search_field=author&include_hidden=true&include_missed=true",
  );
  assert.equal(behavior.mergeSelectedSearchUrls([]), null);
});

test("selected Table metadata sorting supports three columns and both directions", () => {
  const values = [
    {publicId: "b", title: "Beta", authors: [{label: "Alpha"}], series: {label: "Series 2"}},
    {publicId: "a", title: "Alpha", authors: [{label: "Beta"}], series: null},
    {publicId: "c", title: "Gamma", authors: [{label: "Alpha"}], series: {label: "Series 1"}},
  ];
  const sorted = (sort, direction) => [...values]
    .sort((left, right) => behavior.compareSelectedMetadata(left, right, sort, direction))
    .map((value) => value.publicId);

  assert.deepEqual(sorted("author", "asc"), ["c", "b", "a"]);
  assert.deepEqual(sorted("author", "desc"), ["a", "b", "c"]);
  assert.deepEqual(sorted("title", "asc"), ["a", "b", "c"]);
  assert.deepEqual(sorted("title", "desc"), ["c", "b", "a"]);
  assert.deepEqual(sorted("series", "asc"), ["c", "b", "a"]);
  assert.deepEqual(sorted("series", "desc"), ["a", "b", "c"]);
});

test("a re-included preserved row takes the authoritative format state", () => {
  const stale = makeEntry("book-1", {
    status: "downloadable",
    outputFormats: "epub,azw3",
    bodyText: "supported",
    included: false,
  });
  const incoming = makeEntry("book-1", {
    status: "unsupported",
    outputFormats: "azw3",
    bodyText: "unsupported",
  });
  const currentContent = makeContent([stale.entry]);
  const incomingContent = makeContent([incoming.entry]);
  const checkbox = {
    dataset: {publicId: "book-1"},
    closest(selector) {
      if (selector === "[data-selection-control]") return stale.control;
      if (selector === "[data-selected-entry]") return stale.entry;
      return null;
    },
  };
  const target = {
    querySelector(selector) {
      return selector === "#selected-preview-content" ? currentContent : null;
    },
    querySelectorAll(selector) {
      return selector === "[data-selection-checkbox]" ? [checkbox] : [];
    },
  };

  behavior.setState(["book-1"], ["book-1"]);
  assert.equal(behavior.mergeSelectedPreview(target, incomingContent), true);
  assert.equal(stale.entry.dataset.status, "unsupported");
  assert.equal(stale.entry.dataset.outputFormats, "azw3");
  assert.equal(stale.entry.children[0], stale.control, "the existing checkbox control is retained");
  assert.equal(stale.entry.children[1].text, "unsupported");
  assert.equal(checkbox.checked, true);
});

test("a delayed replacement preview does not prematurely reset the chosen target", async () => {
  const selector = makeSelector("epub", ["epub"]);
  const oldRow = makeEntry("old", {
    status: "downloadable",
    outputFormats: "epub",
    bodyText: "old",
  }).entry;
  const page = makePage(selector, [oldRow]);
  behavior.setState(["old"], ["old"]);
  assert.equal(behavior.syncFormatSelector(page), false);

  behavior.setState(["old", "replacement"], ["old"]);
  assert.equal(behavior.syncFormatSelector(page), false);
  behavior.setState(["replacement"], ["old"]);
  oldRow.dataset.included = "false";
  assert.equal(behavior.syncFormatSelector(page), false);
  assert.equal(selector.value, "epub", "pending storage updates retain the target");

  await new Promise((resolve) => setTimeout(resolve, 5));
  const replacement = makeEntry("replacement", {
    status: "downloadable",
    outputFormats: "epub,azw3",
    bodyText: "replacement",
  }).entry;
  page.rows = [replacement];
  behavior.setState(["replacement"], ["replacement"]);
  assert.equal(behavior.syncFormatSelector(page), false);
  assert.equal(selector.value, "epub", "the authoritative replacement supports the target");

  replacement.dataset.outputFormats = "azw3";
  assert.equal(behavior.syncFormatSelector(page), true);
  assert.equal(selector.value, "original", "an authoritative preview can still reset the target");
});

test("Tree group selection uses the native checked and indeterminate states", () => {
  storedSelection = '["one"]';
  documentCheckboxes = [];
  const group = makeSelectionGroup(["one", "two"]);
  documentGroups = [group];
  behavior.initialize();

  assert.equal(group.checked, false);
  assert.equal(group.indeterminate, true);
  let stopped = false;
  documentStub.dispatch("click", {
    target: group,
    stopPropagation() { stopped = true; },
  });
  assert.equal(stopped, true);
  assert.equal(storedSelection, '["one","two"]');
  assert.equal(group.checked, true);
  assert.equal(group.indeterminate, false);

  documentStub.dispatch("click", {
    target: group,
    stopPropagation() {},
  });
  assert.equal(storedSelection, "[]");
  assert.equal(group.checked, false);
  assert.equal(group.indeterminate, false);
  documentGroups = [];
});

test("dynamic catalog renders stay root-scoped while duplicate and cross-tab selection stays authoritative", () => {
  storedSelection = "[]";
  documentCheckboxes = [];
  behavior.initialize();

  const first = makeSelectionCheckbox("repeat");
  const duplicate = makeSelectionCheckbox("repeat");
  const other = makeSelectionCheckbox("other");
  const outside = makeSelectionCheckbox("outside", true);
  const renderRoot = {
    querySelectorAll(selector) {
      return selector === "[data-selection-checkbox]" ? [first, duplicate, other] : [];
    },
  };
  documentCheckboxes = [first, duplicate, other, outside];

  documentStub.dispatch("sopds:catalog-rendered", {detail: {root: renderRoot}});
  for (const checkbox of [first, duplicate, other]) {
    assert.equal(checkbox.checked, false);
    assert.equal(checkbox.disabled, false);
    assert.equal(checkbox.control.hidden, false, "newly rendered controls become available");
  }
  assert.equal(outside.checked, true, "render synchronization does not escape event.detail.root");
  assert.equal(outside.disabled, true);

  first.checked = true;
  documentStub.dispatch("change", {target: first});
  assert.equal(storedSelection, '["repeat"]');
  assert.equal(first.checked, true);
  assert.equal(duplicate.checked, true, "selecting one occurrence updates every duplicate");
  assert.equal(other.checked, false);

  storedSelection = '["other"]';
  windowStub.dispatch("storage", {key: "sopds.selected-books.v1"});
  assert.equal(first.checked, false);
  assert.equal(duplicate.checked, false);
  assert.equal(other.checked, true, "cross-tab state remains authoritative");

  const lazy = makeSelectionCheckbox("other");
  documentStub.dispatch("sopds:catalog-rendered", {
    detail: {root: {querySelectorAll: (selector) => selector === "[data-selection-checkbox]" ? [lazy] : []}},
  });
  assert.equal(lazy.checked, true, "lazy and rerendered controls receive current state");
  assert.equal(lazy.disabled, false);
  assert.equal(lazy.control.hidden, false);
});
