(() => {
  "use strict";

  const STORAGE_KEY = "sopds.selected-books.v1";
  const MAX_SELECTED = 10_000;
  const INVALID_ID = /[\u0000\uD800-\uDFFF]/;
  const CONVERTED_FORMATS = [
    {value: "epub", label: "EPUB"},
    {value: "azw3", label: "AZW3"},
  ];
  const SELECTED_VIEWS = new Set(["flat", "tree", "table"]);
  const SELECTED_TABLE_SORTS = new Set(["author", "title", "series"]);
  const SORT_DIRECTIONS = new Set(["asc", "desc"]);
  const BOOK_SORTING = globalThis.SOPDSBookSorting;
  const SELECTION_MESSAGE_FALLBACKS = Object.freeze({
    savedSelectionRepaired: "Saved selection was repaired.",
    selectionUnavailable: "Book selection is unavailable in this browser.",
    couldNotSaveSelection: "Could not save the book selection.",
    selectionLimit: "The selection is limited to 10 000 books.",
    original: "Original",
    size: "Size",
    sourceSize: "Source size",
    couldNotLoadPreview: "Could not load the selection preview.",
    noBooksSelected: "No books selected",
    selectBooksForZip: "Select downloadable books from the catalog to build a ZIP.",
    browseCatalog: "Browse the catalog",
    unknownAuthor: "Unknown author",
    manyAuthors: "Many authors (6+)",
    moreAuthors: "+{count} more",
    booksWithoutSeries: "Books without series",
    unknownSelection: "Unknown selection",
    selectAllAuthor: "Select all books by {label}",
    selectAllSeries: "Select all books in series {label}",
    selectedBooks: "Selected books",
    select: "Select",
    author: "Author",
    title: "Title",
    series: "Series",
    status: "Status",
    actions: "Actions",
    sortedAscending: ", sorted ascending",
    sortedDescending: ", sorted descending",
    downloadable: "Downloadable",
    unsupported: "Unsupported",
    unavailable: "Unavailable",
    unknown: "Unknown",
    loadingSelection: "Loading selection…",
    loadingPreview: "Loading preview…",
    previewNeedsAttention: "Preview needs attention.",
    couldNotRefreshPreview: "Could not refresh the selection preview.",
  });

  function templatePlaceholders(value) {
    const placeholders = [];
    let position = 0;
    for (const match of value.matchAll(/\{([a-z][a-zA-Z0-9]*)\}/g)) {
      if (/[{}]/.test(value.slice(position, match.index))) return null;
      placeholders.push(match[1]);
      position = match.index + match[0].length;
    }
    return /[{}]/.test(value.slice(position)) ? null : placeholders;
  }

  function validSelectionMessage(value, fallback) {
    if (typeof value !== "string" || value.trim().length === 0 || value.length > 500) return false;
    const placeholders = templatePlaceholders(value);
    const expected = templatePlaceholders(fallback);
    if (placeholders === null || expected === null || placeholders.length !== expected.length) return false;
    placeholders.sort();
    expected.sort();
    return expected.every((name, index) => placeholders[index] === name);
  }

  function readSelectionI18n(root = document.body) {
    const configuredLocale = root?.dataset?.uiLocale;
    const locale = ["en", "ru"].includes(configuredLocale) ? configuredLocale : "en";
    let configured = {};
    if (configuredLocale === locale) {
      try {
        configured = JSON.parse(root?.dataset?.selectionMessages || "{}");
        if (!configured || typeof configured !== "object" || Array.isArray(configured)) configured = {};
      } catch (_error) {
        configured = {};
      }
    }
    const messages = {};
    for (const [key, fallback] of Object.entries(SELECTION_MESSAGE_FALLBACKS)) {
      messages[key] = validSelectionMessage(configured[key], fallback) ? configured[key] : fallback;
    }
    return {locale, messages, plurals: new Intl.PluralRules(locale)};
  }

  let selectionI18n = readSelectionI18n();

  function selectionMessage(key, values = {}) {
    let text = selectionI18n.messages[key] || SELECTION_MESSAGE_FALLBACKS[key] || "";
    for (const [name, value] of Object.entries(values)) text = text.replaceAll(`{${name}}`, String(value));
    return text;
  }

  function selectedTableStatusKey(status) {
    if (status === "downloadable") return null;
    return Object.hasOwn(SELECTION_MESSAGE_FALLBACKS, status) ? status : "unknown";
  }

  function formatInteger(value) {
    const digits = String(Math.max(0, Math.trunc(Number(value) || 0)));
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  let selectedIds = [];
  let storageReady = false;
  let previewController = null;
  let previewGeneration = 0;
  let pendingPreviewFocus = null;
  let authoritativePreviewIds = null;
  let selectedView = "flat";
  let selectedTableSort = "author";
  let selectedTableDirection = "asc";

  function isValidId(value) {
    return typeof value === "string" && value.length > 0 && value.length <= 64 && !INVALID_ID.test(value);
  }

  function normalizeIds(value) {
    if (!Array.isArray(value)) {
      return [];
    }
    const unique = new Set();
    const normalized = [];
    for (const valueId of value) {
      if (!isValidId(valueId) || unique.has(valueId)) {
        continue;
      }
      unique.add(valueId);
      normalized.push(valueId);
      if (normalized.length === MAX_SELECTED) {
        break;
      }
    }
    return normalized;
  }

  function parseIds(value) {
    if (value === null) {
      return [];
    }
    try {
      return normalizeIds(JSON.parse(value));
    } catch (_error) {
      return [];
    }
  }

  function showSelectionStatus(message, isError = false) {
    let statuses = document.querySelectorAll("[data-selection-status]");
    if (message && statuses.length === 0) {
      const status = document.createElement("p");
      status.className = "selection-status";
      status.dataset.selectionStatus = "";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      (document.querySelector(".app-sidebar") || document.body).append(status);
      statuses = document.querySelectorAll("[data-selection-status]");
    }
    statuses.forEach((status) => {
      status.textContent = message;
      status.hidden = !message;
      status.classList.toggle("selection-status--error", isError);
    });
  }

  function readSelection() {
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY);
      const ids = parseIds(raw);
      storageReady = true;
      if (raw !== null && raw !== JSON.stringify(ids)) {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
        showSelectionStatus(selectionMessage("savedSelectionRepaired"));
      }
      return ids;
    } catch (_error) {
      storageReady = false;
      showSelectionStatus(selectionMessage("selectionUnavailable"), true);
      return [];
    }
  }

  function syncNavigationCount() {
    document.querySelectorAll("[data-selection-count]").forEach((count) => {
      count.textContent = formatInteger(selectedIds.length);
      count.hidden = !storageReady;
    });
  }

  function selectionGroupIds(checkbox) {
    try {
      return normalizeIds(JSON.parse(checkbox.dataset.publicIds || "[]"));
    } catch (_error) {
      return [];
    }
  }

  function syncGroupCheckboxes(root = document) {
    const includedIds = new Set(selectedIds);
    root.querySelectorAll("[data-selection-group]").forEach((checkbox) => {
      const publicIds = selectionGroupIds(checkbox);
      const includedCount = publicIds.filter((publicId) => includedIds.has(publicId)).length;
      checkbox.checked = publicIds.length > 0 && includedCount === publicIds.length;
      checkbox.indeterminate = includedCount > 0 && includedCount < publicIds.length;
      checkbox.disabled = !storageReady || publicIds.length === 0;
    });
  }

  function syncCheckboxes(root = document) {
    const includedIds = new Set(selectedIds);
    root.querySelectorAll("[data-selection-checkbox]").forEach((checkbox) => {
      const publicId = checkbox.dataset.publicId;
      const included = isValidId(publicId) && includedIds.has(publicId);
      checkbox.checked = included;
      checkbox.disabled = !storageReady;
      const control = checkbox.closest("[data-selection-control]");
      if (control) {
        control.hidden = !storageReady;
      }
      const selectedEntry = checkbox.closest("[data-selected-entry]") || checkbox.closest("[data-selected-view-entry]");
      if (selectedEntry) {
        selectedEntry.dataset.included = String(included);
        if (!included && selectedEntry.matches("[data-selected-entry]")) {
          clearCollisionState(selectedEntry);
        }
      }
    });
    syncGroupCheckboxes(root);
  }

  function hasAuthoritativePreviewRows(page) {
    if (selectedIds.length === 0) {
      return true;
    }
    if (!authoritativePreviewIds) {
      return false;
    }
    const displayedIds = new Set();
    page.querySelectorAll("[data-selected-entry]").forEach((entry) => {
      displayedIds.add(entry.dataset.publicId);
    });
    return selectedIds.every(
      (publicId) => authoritativePreviewIds.has(publicId) && displayedIds.has(publicId),
    );
  }

  function syncFormatSelector(page) {
    const selector = page.querySelector("[data-selected-format]");
    if (!selector || !hasAuthoritativePreviewRows(page)) {
      return false;
    }
    const selectedValue = selector.value;
    const sourceFormats = new Set();
    const availableTargets = new Set();
    page
      .querySelectorAll(
        '[data-selected-entry][data-included="true"][data-source-downloadable="true"]',
      )
      .forEach((entry) => {
        if (entry.dataset.sourceFormat) {
          sourceFormats.add(entry.dataset.sourceFormat);
        }
        (entry.dataset.outputFormats || "")
          .split(",")
          .filter(Boolean)
          .forEach((target) => {
            availableTargets.add(target);
          });
      });

    const original = selector.querySelector('option[value="original"]');
    if (original) {
      original.textContent = sourceFormats.size === 1 ? [...sourceFormats][0] : selectionMessage("original");
    }
    CONVERTED_FORMATS.forEach(({value}) => {
      selector.querySelector(`option[value="${value}"]`)?.remove();
    });
    CONVERTED_FORMATS.forEach(({value, label}) => {
      if (!availableTargets.has(value)) {
        return;
      }
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      selector.append(option);
    });
    if (selector.querySelector(`option[value="${selectedValue}"]`)) {
      selector.value = selectedValue;
      return false;
    }
    selector.value = "original";
    return selectedValue !== "original";
  }

  function syncSelectedPageForm() {
    const page = document.querySelector("[data-selection-page]");
    if (!page) {
      return;
    }
    const idsField = page.querySelector("[data-selected-ids]");
    const clearButton = page.querySelector("[data-selection-clear]");
    if (idsField) {
      idsField.value = JSON.stringify(selectedIds);
    }
    if (clearButton) {
      clearButton.disabled = !storageReady || selectedIds.length === 0;
    }
    syncFormatSelector(page);
  }

  function syncInterface(root = document) {
    syncNavigationCount();
    syncCheckboxes(root);
    syncSelectedPageForm();
  }

  function saveSelection(nextIds, restoreFocus = false, preserveEntries = false) {
    const normalized = normalizeIds(nextIds);
    if (!storageReady) {
      showSelectionStatus(selectionMessage("selectionUnavailable"), true);
      syncInterface();
      return false;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    } catch (_error) {
      showSelectionStatus(selectionMessage("couldNotSaveSelection"), true);
      syncInterface();
      return false;
    }
    selectedIds = normalized;
    if (restoreFocus) {
      pendingPreviewFocus = [...selectedIds];
    } else if (pendingPreviewFocus && !sameIds(pendingPreviewFocus, selectedIds)) {
      pendingPreviewFocus = null;
    }
    showSelectionStatus("");
    syncInterface();
    refreshSelectedPreview({preserveEntries});
    return true;
  }

  function appendId(publicId, preserveEntries = false) {
    if (!isValidId(publicId) || selectedIds.includes(publicId)) {
      syncInterface();
      return;
    }
    if (selectedIds.length >= MAX_SELECTED) {
      showSelectionStatus(selectionMessage("selectionLimit"), true);
      syncInterface();
      return;
    }
    saveSelection([...selectedIds, publicId], null, preserveEntries);
  }

  function removeId(publicId, preserveEntries = false) {
    if (!isValidId(publicId)) {
      syncInterface();
      return;
    }
    saveSelection(
      selectedIds.filter((selectedId) => selectedId !== publicId),
      null,
      preserveEntries,
    );
  }

  function clearSelection() {
    saveSelection([], true, true);
  }

  function formatSize(size) {
    const units = ["bytes", "KiB", "MiB", "GiB", "TiB"];
    let value = Number.isFinite(size) && size >= 0 ? size : 0;
    for (const unit of units) {
      if (value < 1024 || unit === units[units.length - 1]) {
        return unit === "bytes" ? `${Math.trunc(value)} bytes` : `${value.toFixed(1)} ${unit}`;
      }
      value /= 1024;
    }
    return "0 bytes";
  }

  function setPreviewStatus(page, message, isError = false) {
    const status = page.querySelector("[data-selected-request-status]");
    if (!status) {
      return;
    }
    status.textContent = message;
    status.classList.toggle("selected-request-status--error", isError);
  }

  function resetPreviewState(page) {
    page.querySelectorAll("[data-selected-downloadable-count]").forEach((element) => {
      element.textContent = "0";
    });
    page.querySelectorAll("[data-selected-total-size]").forEach((element) => {
      element.textContent = "0 bytes";
    });
    const selectedFormat = page.querySelector("[data-selected-format]")?.value || "original";
    page.querySelectorAll("[data-selected-total-label]").forEach((element) => {
      element.textContent = selectionMessage(selectedFormat === "original" ? "size" : "sourceSize");
    });
    const download = page.querySelector("[data-selected-download]");
    if (download) {
      download.disabled = true;
    }
  }

  function applySuccessfulPreviewState(page, content) {
    const downloadable = Number.parseInt(content.dataset.downloadableCount || "0", 10);
    const totalSize = Number.parseInt(content.dataset.totalSize || "0", 10);
    const usableCount = Number.isFinite(downloadable) && downloadable > 0 ? downloadable : 0;
    page.querySelectorAll("[data-selected-downloadable-count]").forEach((element) => {
      element.textContent = formatInteger(usableCount);
    });
    page.querySelectorAll("[data-selected-total-size]").forEach((element) => {
      element.textContent = formatSize(totalSize);
    });
    page.querySelectorAll("[data-selected-total-label]").forEach((element) => {
      element.textContent = selectionMessage(content.dataset.archiveFormat === "original" ? "size" : "sourceSize");
    });
    const download = page.querySelector("[data-selected-download]");
    if (download) {
      download.disabled = usableCount === 0;
    }
  }

  function sameIds(left, right) {
    return left.length === right.length && left.every((publicId, index) => publicId === right[index]);
  }

  function restorePreviewFocus(target, requestIds) {
    if (!pendingPreviewFocus || !sameIds(pendingPreviewFocus, requestIds)) {
      return;
    }
    const focusTarget =
      target.querySelector("[data-selected-empty]") ||
      target.querySelector("[data-selected-summary]") ||
      target.querySelector("[data-selected-preview-error]");
    pendingPreviewFocus = null;
    if (focusTarget) {
      focusTarget.focus();
    }
  }

  function showPreviewError(target) {
    const error = selectedElement("div", "selected-preview-error");
    error.dataset.selectedPreviewError = "";
    error.setAttribute("role", "alert");
    error.setAttribute("tabindex", "-1");
    error.append(selectedElement("p", "", selectionMessage("couldNotLoadPreview")));
    target.replaceChildren(error);
  }

  function createSelectedEmptyState() {
    const emptyState = selectedElement("div", "catalog-results__message selected-empty");
    emptyState.dataset.selectedEmpty = "";
    emptyState.setAttribute("tabindex", "-1");
    emptyState.append(selectedElement("h2", "", selectionMessage("noBooksSelected")));
    emptyState.append(selectedElement("p", "", selectionMessage("selectBooksForZip")));
    const navigation = selectedElement("p");
    const link = selectedElement("a", "", selectionMessage("browseCatalog"));
    link.href = "/";
    navigation.append(link);
    emptyState.append(navigation);
    return emptyState;
  }

  function selectedElement(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function compareSelectedSeriesNumbers(left, right, direction = "asc") {
    return BOOK_SORTING.compareSeriesNumberValues(left, right, direction);
  }

  function compareSelectedMetadata(left, right, sort, direction = "asc") {
    return BOOK_SORTING.tableComparator(sort, direction)(left, right);
  }

  function selectedEntryComparator(sort, direction) {
    const comparator = BOOK_SORTING.tableComparator(sort, direction);
    return (left, right) => comparator(selectedEntryMetadata(left), selectedEntryMetadata(right));
  }

  function selectedGroupKey(type, label) {
    return BOOK_SORTING.groupKey(type, label);
  }

  function mergeSelectedSearchUrls(urls) {
    if (!urls.length) return null;
    const merged = new URL(urls[0], "http://localhost/");
    for (const value of urls.slice(1)) {
      const candidate = new URL(value, "http://localhost/");
      for (const name of ["include_hidden", "include_missed"]) {
        if (candidate.searchParams.get(name) === "true") merged.searchParams.set(name, "true");
      }
    }
    return `${merged.pathname}${merged.search}`;
  }

  function selectedMetadataLink(label, urls) {
    const href = mergeSelectedSearchUrls(urls);
    if (!href) return document.createTextNode(label);
    const link = selectedElement("a", "", label);
    link.href = href;
    return link;
  }

  function selectedEntryAuthors(entry) {
    return [...entry.querySelectorAll(".result-row__authors a")].map((link) => {
      const label = link.textContent;
      const sortKey = link.dataset?.sortKey
        || link.getAttribute("data-sort-key")
        || BOOK_SORTING.normalizeSortKey(label);
      const identityValue = link.dataset?.identityKey
        || link.getAttribute("data-identity-key")
        || label;
      return {
        key: selectedGroupKey("author", identityValue),
        label,
        sortKey,
        identityValue,
        searchUrl: link.getAttribute("href"),
      };
    });
  }

  function selectedTableAuthorModel(entry, limit = 2) {
    const authors = selectedEntryAuthors(entry);
    return {visible: authors.slice(0, limit), overflow: authors.slice(limit)};
  }

  function selectedAuthorToken(author, trailingComma) {
    const token = selectedElement("span", "result-row__author-token");
    if (author.searchUrl) {
      const link = selectedElement("a", "", author.label);
      link.href = author.searchUrl;
      token.append(link);
    } else token.append(document.createTextNode(author.label));
    if (trailingComma) {
      const separator = selectedElement("span", "", ",");
      separator.setAttribute("aria-hidden", "true");
      token.append(separator);
    }
    return token;
  }

  function selectedTableAuthors(entry) {
    const source = entry.querySelector(".result-row__authors");
    if (!source) return null;
    const authors = selectedElement("div", "result-row__authors");
    const accessible = source.querySelector(".visually-hidden")?.cloneNode(true);
    if (accessible) authors.append(accessible);
    const model = selectedTableAuthorModel(entry);
    model.visible.forEach((author, index) => {
      authors.append(selectedAuthorToken(author, index < model.visible.length - 1));
    });
    if (model.overflow.length) {
      const disclosure = selectedElement("details", "author-overflow");
      disclosure.append(selectedElement(
        "summary",
        "",
        selectionMessage("moreAuthors", {count: formatInteger(model.overflow.length)}),
      ));
      const links = selectedElement("span", "author-overflow__links");
      model.overflow.forEach((author, index) => {
        links.append(selectedAuthorToken(author, index < model.overflow.length - 1));
      });
      disclosure.append(links);
      authors.append(disclosure);
    }
    return authors;
  }

  function selectedEntryMetadata(entry) {
    const authors = selectedEntryAuthors(entry);
    const groupAuthors = authors.length === 0
      ? [{key: BOOK_SORTING.identityKey("synthetic-author", "unknown"), label: selectionMessage("unknownAuthor"), sortKey: BOOK_SORTING.normalizePhrase("Unknown author"), searchUrl: null, stableLabel: "Unknown author"}]
      : authors.length >= 6
        ? [{key: BOOK_SORTING.identityKey("synthetic-author", "many"), label: selectionMessage("manyAuthors"), sortKey: BOOK_SORTING.normalizePhrase("Many authors (6+)"), searchUrl: null, stableLabel: "Many authors (6+)"}]
        : authors;
    const seriesLink = entry.querySelector(".result-row__series a");
    const seriesLabel = seriesLink?.textContent;
    const seriesIdentityValue = seriesLink?.dataset?.identityKey
      || seriesLink?.getAttribute("data-identity-key")
      || seriesLabel;
    const seriesSortKey = seriesLink?.dataset?.sortKey
      || seriesLink?.getAttribute("data-sort-key")
      || BOOK_SORTING.normalizeSortKey(seriesLabel);
    const seriesNumber = entry.querySelector("[data-series-number]")?.textContent.trim().replace(/^#/, "") || null;
    const renderedTitle = entry.querySelector(".result-row__title")?.textContent.trim();
    const unknownEntry = entry.dataset.status === "unknown";
    const title = renderedTitle || selectionMessage("unknownSelection");
    return {
      publicId: entry.dataset.publicId,
      title,
      titleSortKey: unknownEntry
        ? "unknown selection"
        : entry.dataset.titleSortKey || BOOK_SORTING.normalizeSortKey(title),
      authors,
      groupAuthors,
      series: seriesLink ? {
        key: selectedGroupKey("series", seriesIdentityValue),
        label: seriesLabel,
        sortKey: seriesSortKey,
        identityValue: seriesIdentityValue,
        number: seriesNumber,
        searchUrl: seriesLink.getAttribute("href"),
      } : null,
    };
  }

  function compareSelectedTreeEntries(left, right) {
    return BOOK_SORTING.treeComparator(selectedEntryMetadata(left), selectedEntryMetadata(right));
  }

  function cloneSelectedEntry(entry) {
    const clone = entry.cloneNode(true);
    clone.removeAttribute("data-selected-entry");
    clone.dataset.selectedViewEntry = "";
    return clone;
  }

  function selectedGroupCheckbox(entries, label, type) {
    const publicIds = [...new Set(entries.map((entry) => entry.dataset.publicId).filter(isValidId))];
    const checkbox = selectedElement("input", "catalog-tree-select");
    checkbox.type = "checkbox";
    checkbox.dataset.selectionGroup = "";
    checkbox.dataset.publicIds = JSON.stringify(publicIds);
    checkbox.setAttribute("aria-label", selectionMessage(type === "author" ? "selectAllAuthor" : "selectAllSeries", {label}));
    return checkbox;
  }

  function renderSelectedFlat(entries) {
    const list = selectedElement("div", "result-list selected-result-list selected-flat-view catalog-flat-view");
    entries.forEach((entry) => list.append(cloneSelectedEntry(entry)));
    return list;
  }

  function renderSelectedTree(entries) {
    const groups = new Map();
    for (const entry of entries) {
      const metadata = selectedEntryMetadata(entry);
      for (const groupAuthor of metadata.groupAuthors) {
        let branch = groups.get(groupAuthor.key);
        if (!branch) {
          branch = {
            label: groupAuthor.label,
            stableLabel: groupAuthor.stableLabel || groupAuthor.label,
            sortKey: BOOK_SORTING.normalizePhrase(groupAuthor.label),
            key: groupAuthor.key,
            entries: [],
            searchUrls: [],
            series: new Map(),
          };
          groups.set(groupAuthor.key, branch);
        }
        branch.entries.push(entry);
        if (groupAuthor.searchUrl) branch.searchUrls.push(groupAuthor.searchUrl);
        const seriesKey = metadata.series
          ? BOOK_SORTING.identityKey("series", groupAuthor.key, "named", metadata.series.identityValue)
          : BOOK_SORTING.identityKey("series", groupAuthor.key, "without-series");
        let leaf = branch.series.get(seriesKey);
        if (!leaf) {
          leaf = {
            key: seriesKey,
            label: metadata.series?.label || selectionMessage("booksWithoutSeries"),
            stableLabel: metadata.series?.label || "Books without series",
            sortKey: metadata.series?.sortKey || null,
            entries: [],
            searchUrls: [],
            withoutSeries: !metadata.series,
          };
          branch.series.set(seriesKey, leaf);
        }
        leaf.entries.push(entry);
        if (metadata.series?.searchUrl) leaf.searchUrls.push(metadata.series.searchUrl);
      }
    }
    const tree = selectedElement("div", "catalog-tree-view selected-tree-view");
    const branches = [...groups.values()].sort(BOOK_SORTING.compareGroups);
    for (const branch of branches) {
      const author = selectedElement("details", "catalog-tree-author");
      const authorSummary = selectedElement("summary", "catalog-tree-author__summary");
      authorSummary.append(selectedGroupCheckbox(branch.entries, branch.label, "author"));
      authorSummary.append(selectedMetadataLink(branch.label, branch.searchUrls));
      authorSummary.append(selectedElement("span", "catalog-tree-count", ` (${formatInteger(new Set(branch.entries.map((entry) => entry.dataset.publicId)).size)})`));
      author.append(authorSummary);
      const leaves = [...branch.series.values()]
        .filter((leaf) => !leaf.withoutSeries)
        .sort(BOOK_SORTING.compareGroups);
      const withoutSeries = [...branch.series.values()].find((leaf) => leaf.withoutSeries);
      if (withoutSeries) leaves.push(withoutSeries);
      for (const leaf of leaves) {
        const series = selectedElement("details", "catalog-tree-series");
        const summary = selectedElement("summary", "catalog-tree-series__summary");
        summary.append(selectedGroupCheckbox(leaf.entries, leaf.label, "series"));
        summary.append(selectedMetadataLink(leaf.label, leaf.searchUrls));
        summary.append(selectedElement("span", "catalog-tree-count", ` (${formatInteger(new Set(leaf.entries.map((entry) => entry.dataset.publicId)).size)})`));
        const books = selectedElement("div", "catalog-tree-books");
        [...leaf.entries]
          .sort(compareSelectedTreeEntries)
          .forEach((entry) => books.append(cloneSelectedEntry(entry)));
        series.append(summary, books);
        author.append(series);
      }
      tree.append(author);
    }
    return tree;
  }

  function selectedTableCell(row, content, className = "") {
    const cell = selectedElement("td", className);
    if (content) cell.append(content);
    row.append(cell);
  }

  function renderSelectedTable(entries) {
    const wrapper = selectedElement("div", "catalog-table-scroll");
    const table = selectedElement("table", "catalog-table selected-table");
    table.append(selectedElement("caption", "visually-hidden", selectionMessage("selectedBooks")));
    const head = selectedElement("thead");
    const header = selectedElement("tr");
    for (const {key, label} of [
      {key: null, label: selectionMessage("select")},
      {key: "author", label: selectionMessage("author")},
      {key: "title", label: selectionMessage("title")},
      {key: "series", label: selectionMessage("series")},
      {key: null, label: selectionMessage("status")},
      {key: null, label: selectionMessage("actions")},
    ]) {
      const cell = selectedElement("th");
      cell.scope = "col";
      if (!key) cell.textContent = label;
      else {
        const button = selectedElement("button", "catalog-table__sort", label);
        button.type = "button";
        button.dataset.selectedTableSort = key;
        button.addEventListener("click", () => {
          if (selectedTableSort === key) {
            selectedTableDirection = selectedTableDirection === "asc" ? "desc" : "asc";
          } else {
            selectedTableSort = key;
            selectedTableDirection = "asc";
          }
          commitSelectedViewState();
          renderSelectedView();
          const replacement = [...document.querySelectorAll("[data-selected-table-sort]")]
            .find((candidate) => candidate.dataset.selectedTableSort === key);
          replacement?.focus();
        });
        cell.append(button);
        if (selectedTableSort === key) {
          cell.setAttribute("aria-sort", selectedTableDirection === "asc" ? "ascending" : "descending");
          button.append(selectedElement("span", "visually-hidden", selectionMessage(selectedTableDirection === "asc" ? "sortedAscending" : "sortedDescending")));
        }
      }
      header.append(cell);
    }
    head.append(header);
    const body = selectedElement("tbody");
    const sortedEntries = [...entries].sort(selectedEntryComparator(selectedTableSort, selectedTableDirection));
    for (const entry of sortedEntries) {
      const row = selectedElement(
        "tr",
        `selected-table__row selected-table__row--${entry.dataset.status || "unknown"}${entry.dataset.collision === "true" ? " selected-table__row--collision" : ""}`,
      );
      row.dataset.selectedViewEntry = "";
      row.dataset.publicId = entry.dataset.publicId;
      row.dataset.included = entry.dataset.included || "true";
      selectedTableCell(row, entry.querySelector("[data-selection-control]")?.cloneNode(true), "catalog-table__selection");
      selectedTableCell(row, selectedTableAuthors(entry));
      selectedTableCell(row, entry.querySelector(".result-row__heading")?.cloneNode(true));
      selectedTableCell(row, entry.querySelector(".result-row__series")?.cloneNode(true));
      const status = selectedElement("div", "selected-table__status");
      const statusKey = selectedTableStatusKey(entry.dataset.status || "unknown");
      if (statusKey) {
        status.append(selectedElement(
          "strong",
          "selected-table__status-label",
          selectionMessage(statusKey),
        ));
      }
      status.append(entry.querySelector(".result-metadata")?.cloneNode(true) || document.createTextNode(selectionMessage("unknown")));
      const note = entry.querySelector(".selected-entry-note")?.cloneNode(true);
      if (note) status.append(note);
      selectedTableCell(row, status);
      selectedTableCell(row, entry.querySelector(".result-row__actions")?.cloneNode(true));
      body.append(row);
    }
    table.append(head, body);
    wrapper.append(table);
    return wrapper;
  }

  function syncSelectedViewButtons() {
    document.querySelectorAll("[data-selected-view]").forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.selectedView === selectedView));
    });
  }

  function commitSelectedViewState() {
    const values = new URLSearchParams((window.location?.hash || "").replace(/^#/, ""));
    values.set("view", selectedView);
    values.set("selectedSort", selectedTableSort);
    values.set("selectedDir", selectedTableDirection);
    window.history.replaceState(
      window.history.state,
      "",
      `${window.location.pathname}${window.location.search}#${values.toString()}`,
    );
  }

  function renderSelectedView(target = document.querySelector("[data-selected-preview-target]")) {
    const content = target?.querySelector("#selected-preview-content");
    const entriesContainer = content?.querySelector("[data-selected-entries]");
    content?.querySelector("[data-selected-view-mount]")?.remove();
    if (!content || !entriesContainer) return;
    const entries = [...entriesContainer.querySelectorAll("[data-selected-entry]")];
    entriesContainer.hidden = true;
    const mount = selectedElement("div", "selected-view-mount");
    mount.dataset.selectedViewMount = "";
    if (selectedView === "tree") mount.append(renderSelectedTree(entries));
    else if (selectedView === "table") mount.append(renderSelectedTable(entries));
    else mount.append(renderSelectedFlat(entries));
    content.append(mount);
    syncCheckboxes(mount);
  }

  function initializeSelectedViewSwitcher() {
    const values = new URLSearchParams((window.location?.hash || "").replace(/^#/, ""));
    selectedView = SELECTED_VIEWS.has(values.get("view")) ? values.get("view") : "flat";
    selectedTableSort = SELECTED_TABLE_SORTS.has(values.get("selectedSort"))
      ? values.get("selectedSort") : "author";
    selectedTableDirection = SORT_DIRECTIONS.has(values.get("selectedDir"))
      ? values.get("selectedDir") : "asc";
    syncSelectedViewButtons();
    document.querySelectorAll("[data-selected-view]").forEach((button) => {
      button.addEventListener("click", () => {
        if (!SELECTED_VIEWS.has(button.dataset.selectedView)) return;
        selectedView = button.dataset.selectedView;
        commitSelectedViewState();
        syncSelectedViewButtons();
        renderSelectedView();
      });
    });
  }

  function replacePreviewHeader(currentContent, currentEntries, incomingContent) {
    for (const child of [...currentContent.children]) {
      if (child !== currentEntries) {
        child.remove();
      }
    }
    for (const child of incomingContent.children) {
      if (!child.matches("[data-selected-entries], [data-selected-empty]")) {
        currentContent.insertBefore(child.cloneNode(true), currentEntries);
      }
    }
  }

  function clearCollisionState(entry) {
    entry.dataset.collision = "false";
    entry.classList.remove("result-row--collision");
    entry.querySelector("[data-collision-notice]")?.remove();
  }

  function updateSelectedEntry(entry, incoming) {
    const control = entry.querySelector(":scope > [data-selection-control]");
    entry.className = incoming.className;
    entry.dataset.status = incoming.dataset.status;
    entry.dataset.collision = incoming.dataset.collision;
    entry.dataset.sourceDownloadable = incoming.dataset.sourceDownloadable;
    entry.dataset.sourceFormat = incoming.dataset.sourceFormat;
    entry.dataset.outputFormats = incoming.dataset.outputFormats;
    for (const child of [...entry.children]) {
      if (child !== control) {
        child.remove();
      }
    }
    for (const child of incoming.children) {
      if (!child.matches("[data-selection-control]")) {
        entry.append(child.cloneNode(true));
      }
    }
  }

  function showPreservedPreviewError(target, incomingContent = null) {
    const currentContent = target.querySelector("#selected-preview-content");
    const currentEntries = currentContent && currentContent.querySelector("[data-selected-entries]");
    if (!currentContent || !currentEntries) {
      showPreviewError(target);
      return;
    }
    currentContent.dataset.selectedCount = "0";
    currentContent.dataset.downloadableCount = "0";
    currentContent.dataset.totalSize = "0";
    for (const child of [...currentContent.children]) {
      if (child !== currentEntries) {
        child.remove();
      }
    }
    let errorContent;
    if (incomingContent) {
      errorContent = incomingContent.cloneNode(true);
      errorContent.removeAttribute("id");
    } else {
      errorContent = document.createElement("div");
      errorContent.className = "selected-preview-error";
      errorContent.dataset.selectedPreviewError = "";
      errorContent.setAttribute("role", "alert");
      errorContent.setAttribute("tabindex", "-1");
      errorContent.append(selectedElement("p", "", selectionMessage("couldNotLoadPreview")));
    }
    currentContent.insertBefore(errorContent, currentEntries);
    currentContent.querySelectorAll("[data-selected-entry]").forEach(clearCollisionState);
    if (!currentEntries.querySelector("[data-selected-entry]")) {
      currentEntries.replaceWith(createSelectedEmptyState());
    }
  }

  function mergeSelectedPreview(target, incomingContent) {
    const currentContent = target.querySelector("#selected-preview-content");
    const currentEntries = currentContent && currentContent.querySelector("[data-selected-entries]");
    if (!currentContent || !currentEntries) {
      return false;
    }

    currentContent.dataset.selectedCount = incomingContent.dataset.selectedCount || "0";
    currentContent.dataset.downloadableCount = incomingContent.dataset.downloadableCount || "0";
    currentContent.dataset.totalSize = incomingContent.dataset.totalSize || "0";
    currentContent.dataset.archiveFormat = incomingContent.dataset.archiveFormat || "original";
    currentContent.dataset.catalogGeneration = incomingContent.dataset.catalogGeneration || "";
    replacePreviewHeader(currentContent, currentEntries, incomingContent);

    const incomingEntries = new Map();
    incomingContent.querySelectorAll("[data-selected-entry]").forEach((entry) => {
      incomingEntries.set(entry.dataset.publicId, entry);
    });
    const includedIds = new Set(selectedIds);
    currentEntries.querySelectorAll("[data-selected-entry]").forEach((entry) => {
      const publicId = entry.dataset.publicId;
      const incoming = incomingEntries.get(publicId);
      clearCollisionState(entry);
      if (!includedIds.has(publicId) || !incoming) {
        return;
      }
      updateSelectedEntry(entry, incoming);
      incomingEntries.delete(publicId);
    });
    incomingEntries.forEach((entry, publicId) => {
      if (includedIds.has(publicId)) {
        currentEntries.append(entry.cloneNode(true));
      }
    });
    if (!currentEntries.querySelector("[data-selected-entry]")) {
      const emptyState = incomingContent.querySelector("[data-selected-empty]");
      currentEntries.replaceWith(
        emptyState ? emptyState.cloneNode(true) : createSelectedEmptyState(),
      );
    }
    syncCheckboxes(target);
    return true;
  }

  function hasExcludedDisplayedEntries() {
    return Boolean(document.querySelector('[data-selected-entry][data-included="false"]'));
  }

  async function refreshSelectedPreview({preserveEntries = false} = {}) {
    const page = document.querySelector("[data-selection-page]");
    if (!page) {
      return;
    }
    syncSelectedPageForm();
    const target = page.querySelector("[data-selected-preview-target]");
    const preset = page.querySelector("[data-selected-preset]");
    const selectedFormat = page.querySelector("[data-selected-format]");
    if (!target || !preset || !selectedFormat) {
      return;
    }

    const keepEntries = preserveEntries && Boolean(target.querySelector("[data-selected-entries]"));
    previewGeneration += 1;
    const requestGeneration = previewGeneration;
    if (previewController) {
      previewController.abort();
    }
    previewController = new AbortController();
    const requestIds = [...selectedIds];
    target.setAttribute("aria-busy", "true");
    if (!keepEntries) {
      target.replaceChildren(selectedElement("p", "selected-loading", selectionMessage("loadingSelection")));
    }
    resetPreviewState(page);
    setPreviewStatus(page, selectionMessage("loadingPreview"));

    try {
      const response = await window.fetch("/selected/preview", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "text/html"},
        body: JSON.stringify({
          ids: requestIds,
          preset: preset.value,
          format: selectedFormat.value,
        }),
        signal: previewController.signal,
      });
      const markup = await response.text();
      if (requestGeneration !== previewGeneration) {
        return;
      }
      const template = document.createElement("template");
      template.innerHTML = markup;
      const incomingContent = template.content.querySelector("#selected-preview-content");
      if (!incomingContent) {
        throw new Error("Invalid preview response");
      }
      const successful =
        response.ok && !incomingContent.hasAttribute("data-selected-preview-error");
      if (!successful) {
        if (keepEntries) {
          showPreservedPreviewError(target, incomingContent);
          renderSelectedView(target);
        } else {
          target.innerHTML = markup;
        }
        resetPreviewState(page);
        setPreviewStatus(page, selectionMessage("previewNeedsAttention"), true);
        restorePreviewFocus(target, requestIds);
        return;
      }

      let content = incomingContent;
      if (keepEntries) {
        if (!mergeSelectedPreview(target, incomingContent)) {
          throw new Error("Could not preserve selected entries");
        }
      } else {
        target.innerHTML = markup;
        content = target.querySelector("#selected-preview-content");
        syncCheckboxes(target);
      }
      authoritativePreviewIds = new Set(requestIds);
      renderSelectedView(target);
      applySuccessfulPreviewState(page, content);
      if (syncFormatSelector(page)) {
        refreshSelectedPreview({preserveEntries: hasExcludedDisplayedEntries()});
        return;
      }
      setPreviewStatus(page, "");
      restorePreviewFocus(target, requestIds);
    } catch (error) {
      if (error && error.name === "AbortError") {
        return;
      }
      if (requestGeneration !== previewGeneration) {
        return;
      }
      resetPreviewState(page);
      if (keepEntries) {
        showPreservedPreviewError(target);
        renderSelectedView(target);
      } else {
        showPreviewError(target);
      }
      setPreviewStatus(page, selectionMessage("couldNotRefreshPreview"), true);
      restorePreviewFocus(target, requestIds);
    } finally {
      if (requestGeneration === previewGeneration) {
        target.removeAttribute("aria-busy");
      }
    }
  }

  function handleChange(event) {
    const checkbox = event.target.closest("[data-selection-checkbox]");
    if (!checkbox) {
      return;
    }
    const preserveEntries = Boolean(
      checkbox.closest("[data-selected-entry]") || checkbox.closest("[data-selected-view-entry]"),
    );
    if (checkbox.checked) {
      appendId(checkbox.dataset.publicId, preserveEntries);
    } else {
      removeId(checkbox.dataset.publicId, preserveEntries);
    }
  }

  function handleClick(event) {
    const group = event.target.closest("[data-selection-group]");
    if (group) {
      event.stopPropagation();
      const publicIds = selectionGroupIds(group);
      const selected = new Set(selectedIds);
      const include = publicIds.some((publicId) => !selected.has(publicId));
      const preserveEntries = Boolean(group.closest(".selected-tree-view"));
      if (include) {
        const nextIds = [...new Set([...selectedIds, ...publicIds])];
        if (nextIds.length > MAX_SELECTED) {
          showSelectionStatus(selectionMessage("selectionLimit"), true);
          syncInterface();
          return;
        }
        saveSelection(nextIds, null, preserveEntries);
      } else {
        const removed = new Set(publicIds);
        saveSelection(
          selectedIds.filter((publicId) => !removed.has(publicId)),
          null,
          preserveEntries,
        );
      }
      return;
    }
    if (event.target.closest("[data-selection-clear]")) {
      clearSelection();
    }
  }

  function handleStorage(event) {
    if (event.key !== STORAGE_KEY && event.key !== null) {
      return;
    }
    selectedIds = readSelection();
    if (pendingPreviewFocus && !sameIds(pendingPreviewFocus, selectedIds)) {
      pendingPreviewFocus = null;
    }
    syncInterface();
    refreshSelectedPreview({preserveEntries: hasExcludedDisplayedEntries()});
  }

  function handleCatalogRendered(event) {
    const root = event.detail?.root;
    if (root?.querySelectorAll) {
      syncCheckboxes(root);
    }
  }

  function initialize() {
    selectionI18n = readSelectionI18n();
    selectedIds = readSelection();
    syncInterface();
    document.addEventListener("change", handleChange);
    document.addEventListener("click", handleClick);
    document.addEventListener("sopds:catalog-rendered", handleCatalogRendered);
    document.addEventListener("htmx:afterSwap", (event) => {
      syncCheckboxes(event.detail && event.detail.elt ? event.detail.elt : document);
      syncNavigationCount();
    });
    window.addEventListener("storage", handleStorage);
    const preset = document.querySelector("[data-selected-preset]");
    if (preset) {
      preset.addEventListener("change", () => {
        refreshSelectedPreview({preserveEntries: hasExcludedDisplayedEntries()});
      });
    }
    const selectedFormat = document.querySelector("[data-selected-format]");
    if (selectedFormat) {
      selectedFormat.addEventListener("change", () => {
        refreshSelectedPreview({preserveEntries: hasExcludedDisplayedEntries()});
      });
    }
    initializeSelectedViewSwitcher();
    refreshSelectedPreview();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
