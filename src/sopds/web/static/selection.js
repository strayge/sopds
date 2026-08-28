(() => {
  "use strict";

  const STORAGE_KEY = "sopds.selected-books.v1";
  const MAX_SELECTED = 10_000;
  const INVALID_ID = /[\u0000\uD800-\uDFFF]/;

  let selectedIds = [];
  let storageReady = false;
  let previewController = null;
  let previewGeneration = 0;
  let pendingPreviewFocus = null;

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
    root.querySelectorAll("[data-selection-checkbox]").forEach((checkbox) => {
      const publicId = checkbox.dataset.publicId;
      checkbox.checked = isValidId(publicId) && selectedIds.includes(publicId);
      checkbox.disabled = !storageReady;
      const control = checkbox.closest("[data-selection-control]");
      if (control) {
        control.hidden = !storageReady;
      }
    });
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
  }

  function syncInterface(root = document) {
    syncNavigationCount();
    syncCheckboxes(root);
    syncSelectedPageForm();
  }

  function saveSelection(nextIds, previewFocus = null) {
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
    refreshSelectedPreview();
    return true;
  }

  function appendId(publicId) {
    if (!isValidId(publicId) || selectedIds.includes(publicId)) {
      syncInterface();
      return;
    }
    if (selectedIds.length >= MAX_SELECTED) {
      showSelectionStatus("The selection is limited to 10,000 books.", true);
      syncInterface();
      return;
    }
    saveSelection([...selectedIds, publicId]);
  }

  function removeId(publicId) {
    if (!isValidId(publicId)) {
      syncInterface();
      return;
    }
    saveSelection(selectedIds.filter((selectedId) => selectedId !== publicId));
  }

  function removeSelectedId(publicId) {
    const index = selectedIds.indexOf(publicId);
    if (!isValidId(publicId) || index < 0) {
      syncInterface();
      return;
    }
    const publicIdToFocus = selectedIds[index + 1] || selectedIds[index - 1] || null;
    saveSelection(
      selectedIds.filter((selectedId) => selectedId !== publicId),
      {publicId: publicIdToFocus},
    );
  }

  function clearSelection() {
    saveSelection([], {publicId: null});
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

  async function refreshSelectedPreview() {
    const page = document.querySelector("[data-selection-page]");
    if (!page) {
      return;
    }
    syncSelectedPageForm();
    const target = page.querySelector("[data-selected-preview-target]");
    const preset = page.querySelector("[data-selected-preset]");
    if (!target || !preset) {
      return;
    }

    previewGeneration += 1;
    const requestGeneration = previewGeneration;
    if (previewController) {
      previewController.abort();
    }
    previewController = new AbortController();
    const requestIds = [...selectedIds];
    target.setAttribute("aria-busy", "true");
    target.innerHTML = '<p class="selected-loading">Loading selection…</p>';
    resetPreviewState(page);
    setPreviewStatus(page, "Loading preview…");

    try {
      const response = await window.fetch("/selected/preview", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "text/html"},
        body: JSON.stringify({ids: requestIds, preset: preset.value}),
        signal: previewController.signal,
      });
      const markup = await response.text();
      if (requestGeneration !== previewGeneration) {
        return;
      }
      target.innerHTML = markup;
      const content = target.querySelector("#selected-preview-content");
      if (!content) {
        throw new Error("Invalid preview response");
      }
      const successful = response.ok && !content.hasAttribute("data-selected-preview-error");
      if (!successful) {
        resetPreviewState(page);
        setPreviewStatus(page, "Preview needs attention.", true);
        restorePreviewFocus(target, requestIds);
        return;
      }
      applySuccessfulPreviewState(page, content);
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
      showPreviewError(target);
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
    if (checkbox.checked) {
      appendId(checkbox.dataset.publicId);
    } else {
      removeId(checkbox.dataset.publicId);
    }
  }

  function handleClick(event) {
    const remove = event.target.closest("[data-selection-remove]");
    if (remove) {
      removeSelectedId(remove.dataset.publicId);
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
    refreshSelectedPreview();
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
      preset.addEventListener("change", refreshSelectedPreview);
    }
    refreshSelectedPreview();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
