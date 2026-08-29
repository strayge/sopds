(() => {
  "use strict";

  const STORAGE_KEY = "sopds.selected-books.v1";
  const MAX_SELECTED = 10_000;
  const INVALID_ID = /[\u0000\uD800-\uDFFF]/;
  const CONVERTED_FORMATS = [
    {value: "epub", label: "EPUB"},
    {value: "azw3", label: "AZW3"},
  ];

  let selectedIds = [];
  let storageReady = false;
  let previewController = null;
  let previewGeneration = 0;
  let pendingPreviewFocus = null;
  let authoritativePreviewIds = null;

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
      const selectedEntry = checkbox.closest("[data-selected-entry]");
      if (selectedEntry) {
        selectedEntry.dataset.included = String(included);
        if (!included) {
          clearCollisionState(selectedEntry);
        }
      }
    });
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
    const preserveEntries = Boolean(checkbox.closest("[data-selected-entry]"));
    if (checkbox.checked) {
      appendId(checkbox.dataset.publicId, preserveEntries);
    } else {
      removeId(checkbox.dataset.publicId, preserveEntries);
    }
  }

  function handleClick(event) {
    const remove = event.target.closest("[data-selection-remove]");
    if (remove) {
      removeSelectedId(remove.dataset.publicId, remove.closest("[data-selected-entry]"));
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

  function initialize() {
    selectedIds = readSelection();
    syncInterface();
    document.addEventListener("change", handleChange);
    document.addEventListener("click", handleClick);
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
    refreshSelectedPreview();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
