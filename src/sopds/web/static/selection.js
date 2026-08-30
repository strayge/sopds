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
        showSelectionStatus("Saved selection was repaired.");
      }
      return ids;
    } catch (_error) {
      storageReady = false;
      showSelectionStatus("Book selection is unavailable in this browser.", true);
      return [];
    }
  }

  function syncNavigationCount() {
    document.querySelectorAll("[data-selection-count]").forEach((count) => {
      count.textContent = String(selectedIds.length);
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
      original.textContent = sourceFormats.size === 1 ? [...sourceFormats][0] : "Original";
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

  function saveSelection(nextIds, previewFocus = null, preserveEntries = false) {
    const normalized = normalizeIds(nextIds);
    if (!storageReady) {
      showSelectionStatus("Book selection is unavailable in this browser.", true);
      syncInterface();
      return false;
    }
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    } catch (_error) {
      showSelectionStatus("Could not save the book selection.", true);
      syncInterface();
      return false;
    }
    selectedIds = normalized;
    if (previewFocus) {
      pendingPreviewFocus = {publicId: previewFocus.publicId, ids: [...selectedIds]};
    } else if (pendingPreviewFocus && !sameIds(pendingPreviewFocus.ids, selectedIds)) {
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
      showSelectionStatus("The selection is limited to 10,000 books.", true);
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

  function removeSelectedId(publicId, entry) {
    const index = selectedIds.indexOf(publicId);
    if (!isValidId(publicId)) {
      syncInterface();
      return;
    }
    if (index < 0) {
      entry?.remove();
      syncInterface();
      if (!document.querySelector("[data-selected-entry]")) {
        refreshSelectedPreview();
      }
      return;
    }
    const publicIdToFocus = selectedIds[index + 1] || selectedIds[index - 1] || null;
    const saved = saveSelection(
      selectedIds.filter((selectedId) => selectedId !== publicId),
      {publicId: publicIdToFocus},
      true,
    );
    if (saved) {
      entry?.remove();
    }
  }

  function clearSelection() {
    saveSelection([], {publicId: null}, true);
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
      element.textContent = selectedFormat === "original" ? "Size" : "Source size";
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
      element.textContent = String(usableCount);
    });
    page.querySelectorAll("[data-selected-total-size]").forEach((element) => {
      element.textContent = formatSize(totalSize);
    });
    page.querySelectorAll("[data-selected-total-label]").forEach((element) => {
      element.textContent = content.dataset.archiveFormat === "original" ? "Size" : "Source size";
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
    if (!pendingPreviewFocus || !sameIds(pendingPreviewFocus.ids, requestIds)) {
      return;
    }
    let focusTarget = null;
    const preferredId = pendingPreviewFocus.publicId;
    if (preferredId) {
      for (const button of target.querySelectorAll("[data-selection-remove]")) {
        if (button.dataset.publicId === preferredId) {
          focusTarget = button;
          break;
        }
      }
    }
    focusTarget =
      focusTarget ||
      target.querySelector("[data-selected-empty]") ||
      target.querySelector("[data-selected-summary]") ||
      target.querySelector("[data-selected-preview-error]");
    pendingPreviewFocus = null;
    if (focusTarget) {
      focusTarget.focus();
    }
  }

  function showPreviewError(target) {
    target.innerHTML = '<div class="selected-preview-error" data-selected-preview-error role="alert" tabindex="-1"><p>Could not load the selection preview.</p></div>';
  }

  function createSelectedEmptyState() {
    const emptyState = document.createElement("div");
    emptyState.className = "catalog-results__message selected-empty";
    emptyState.dataset.selectedEmpty = "";
    emptyState.setAttribute("tabindex", "-1");
    emptyState.innerHTML = "<h2>No books selected</h2><p>Select downloadable books from the catalog to build a ZIP.</p><p><a href=\"/\">Browse the catalog</a></p>";
    return emptyState;
  }

  function selectedElement(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function selectedTextCompare(left, right) {
    const normalizedLeft = String(left).normalize("NFKC").toLowerCase();
    const normalizedRight = String(right).normalize("NFKC").toLowerCase();
    if (normalizedLeft === normalizedRight) return left < right ? -1 : left > right ? 1 : 0;
    return normalizedLeft < normalizedRight ? -1 : 1;
  }

  function selectedOptionalTextCompare(left, right) {
    if (!left || !right) {
      if (!left && !right) return 0;
      return left ? -1 : 1;
    }
    return selectedTextCompare(left, right);
  }

  function compareSelectedMetadata(left, right, sort, direction = "asc") {
    const leftAuthor = left.authors[0]?.label || null;
    const rightAuthor = right.authors[0]?.label || null;
    const leftSeries = left.series?.label || null;
    const rightSeries = right.series?.label || null;
    let compared;
    if (sort === "title") {
      compared = selectedTextCompare(left.title, right.title);
    } else if (sort === "series") {
      compared = selectedOptionalTextCompare(leftSeries, rightSeries)
        || selectedTextCompare(left.title, right.title)
        || selectedOptionalTextCompare(leftAuthor, rightAuthor);
    } else {
      compared = selectedOptionalTextCompare(leftAuthor, rightAuthor)
        || selectedOptionalTextCompare(leftSeries, rightSeries)
        || selectedTextCompare(left.title, right.title);
    }
    if (!compared) compared = selectedTextCompare(left.publicId, right.publicId);
    return direction === "desc" ? -compared : compared;
  }

  function selectedEntryComparator(sort, direction) {
    return (left, right) => compareSelectedMetadata(
      selectedEntryMetadata(left),
      selectedEntryMetadata(right),
      sort,
      direction,
    );
  }

  function selectedGroupKey(type, label) {
    return `${type}:${String(label).normalize("NFKC").toLowerCase()}`;
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

  function selectedEntryMetadata(entry) {
    const authorLinks = [...entry.querySelectorAll(".result-row__authors a")];
    let authors = authorLinks.map((link) => ({
      key: selectedGroupKey("author", link.textContent),
      label: link.textContent,
      searchUrl: link.getAttribute("href"),
    }));
    if (authors.length === 0) {
      authors = [{key: "synthetic-author:unknown", label: "Unknown author", searchUrl: null}];
    } else if (authors.length >= 6) {
      authors = [{key: "synthetic-author:many", label: "Many authors (6+)", searchUrl: null}];
    }
    const seriesLink = entry.querySelector(".result-row__series a");
    return {
      publicId: entry.dataset.publicId,
      title: entry.querySelector(".result-row__title")?.textContent.trim() || "Unknown selection",
      authors,
      series: seriesLink ? {
        key: selectedGroupKey("series", seriesLink.textContent),
        label: seriesLink.textContent,
        searchUrl: seriesLink.getAttribute("href"),
      } : null,
    };
  }

  function cloneSelectedEntry(entry) {
    const clone = entry.cloneNode(true);
    clone.removeAttribute("data-selected-entry");
    clone.dataset.selectedViewEntry = "";
    return clone;
  }

  function selectedGroupCheckbox(entries, label) {
    const publicIds = [...new Set(entries.map((entry) => entry.dataset.publicId).filter(isValidId))];
    const checkbox = selectedElement("input", "catalog-tree-select");
    checkbox.type = "checkbox";
    checkbox.dataset.selectionGroup = "";
    checkbox.dataset.publicIds = JSON.stringify(publicIds);
    checkbox.setAttribute("aria-label", `Select all books in ${label}`);
    return checkbox;
  }

  function renderSelectedFlat(entries) {
    const list = selectedElement("div", "result-list selected-result-list selected-flat-view");
    entries.forEach((entry) => list.append(cloneSelectedEntry(entry)));
    return list;
  }

  function renderSelectedTree(entries) {
    const groups = new Map();
    for (const entry of entries) {
      const metadata = selectedEntryMetadata(entry);
      for (const author of metadata.authors) {
        let branch = groups.get(author.key);
        if (!branch) {
          branch = {label: author.label, entries: [], searchUrls: [], series: new Map()};
          groups.set(author.key, branch);
        }
        branch.entries.push(entry);
        if (author.searchUrl) branch.searchUrls.push(author.searchUrl);
        const seriesKey = metadata.series?.key || "synthetic-series:without-series";
        let leaf = branch.series.get(seriesKey);
        if (!leaf) {
          leaf = {
            label: metadata.series?.label || "Books without series",
            entries: [],
            searchUrls: [],
          };
          branch.series.set(seriesKey, leaf);
        }
        leaf.entries.push(entry);
        if (metadata.series?.searchUrl) leaf.searchUrls.push(metadata.series.searchUrl);
      }
    }
    const tree = selectedElement("div", "catalog-tree-view selected-tree-view");
    const branches = [...groups.values()].sort((left, right) => selectedTextCompare(left.label, right.label));
    for (const branch of branches) {
      const author = selectedElement("details", "catalog-tree-author");
      const authorSummary = selectedElement("summary", "catalog-tree-author__summary");
      authorSummary.append(selectedGroupCheckbox(branch.entries, `author ${branch.label}`));
      authorSummary.append(selectedMetadataLink(branch.label, branch.searchUrls));
      authorSummary.append(selectedElement("span", "catalog-tree-count", ` (${new Set(branch.entries.map((entry) => entry.dataset.publicId)).size})`));
      author.append(authorSummary);
      const leaves = [...branch.series.values()].sort((left, right) => selectedTextCompare(left.label, right.label));
      for (const leaf of leaves) {
        const series = selectedElement("details", "catalog-tree-series");
        const summary = selectedElement("summary", "catalog-tree-series__summary");
        summary.append(selectedGroupCheckbox(leaf.entries, `series ${leaf.label}`));
        summary.append(selectedMetadataLink(leaf.label, leaf.searchUrls));
        summary.append(selectedElement("span", "catalog-tree-count", ` (${new Set(leaf.entries.map((entry) => entry.dataset.publicId)).size})`));
        const books = selectedElement("div", "catalog-tree-books");
        [...leaf.entries]
          .sort((left, right) => selectedTextCompare(selectedEntryMetadata(left).title, selectedEntryMetadata(right).title))
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
    table.append(selectedElement("caption", "visually-hidden", "Selected books"));
    const head = selectedElement("thead");
    const header = selectedElement("tr");
    for (const {key, label} of [
      {key: null, label: "Select"},
      {key: "author", label: "Author"},
      {key: "title", label: "Title"},
      {key: "series", label: "Series"},
      {key: null, label: "Status"},
      {key: null, label: "Actions"},
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
          button.append(selectedElement("span", "visually-hidden", `, sorted ${selectedTableDirection === "asc" ? "ascending" : "descending"}`));
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
      selectedTableCell(row, entry.querySelector(".result-row__authors")?.cloneNode(true));
      selectedTableCell(row, entry.querySelector(".result-row__heading")?.cloneNode(true));
      selectedTableCell(row, entry.querySelector(".result-row__series")?.cloneNode(true));
      const status = selectedElement("div", "selected-table__status");
      const statusValue = entry.dataset.status || "unknown";
      status.append(selectedElement(
        "strong",
        "selected-table__status-label",
        statusValue[0].toUpperCase() + statusValue.slice(1),
      ));
      status.append(entry.querySelector(".result-metadata")?.cloneNode(true) || document.createTextNode("Unknown"));
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
      errorContent.innerHTML = "<p>Could not load the selection preview.</p>";
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
      target.innerHTML = '<p class="selected-loading">Loading selection…</p>';
    }
    resetPreviewState(page);
    setPreviewStatus(page, "Loading preview…");

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
        setPreviewStatus(page, "Preview needs attention.", true);
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
      setPreviewStatus(page, "Could not refresh the selection preview.", true);
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
          showSelectionStatus("The selection is limited to 10,000 books.", true);
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
    const remove = event.target.closest("[data-selection-remove]");
    if (remove) {
      let entry = remove.closest("[data-selected-entry]");
      if (!entry && remove.closest("[data-selected-view-entry]")) {
        for (const candidate of document.querySelectorAll("[data-selected-entry]")) {
          if (candidate.dataset.publicId === remove.dataset.publicId) {
            entry = candidate;
            break;
          }
        }
      }
      removeSelectedId(remove.dataset.publicId, entry || remove.closest("[data-selected-view-entry]"));
      renderSelectedView();
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
    if (pendingPreviewFocus && !sameIds(pendingPreviewFocus.ids, selectedIds)) {
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
