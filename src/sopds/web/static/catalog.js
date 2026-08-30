(() => {
  "use strict";

  const MAX_FILTER_LENGTH = 200;
  const MAX_BOOKS = 1_000;
  const VIEWS = new Set(["flat", "tree", "table"]);
  const FLAT_SORTS = new Set(["title", "author", "series"]);
  const TABLE_SORTS = new Set(["author", "title", "series", "number"]);
  const DIRECTIONS = new Set(["asc", "desc"]);
  const DEFAULT_STATE = Object.freeze({
    view: "flat",
    flatSort: "title",
    flatDir: "asc",
    tableSort: "author",
    tableDir: "asc",
    title: "",
    author: "",
    series: "",
  });
  const RENDER_EVENT = "sopds:catalog-rendered";
  const INVALID_URL_TEXT = /[\u0000-\u001f\u007f]/;
  const NATURAL_CHUNKS = /[0-9]+|[^0-9]+/g;

  let activeController = null;
  let pendingFreshState = null;
  let historyRestoring = false;

  function boundedText(value) {
    return typeof value === "string" ? Array.from(value).slice(0, MAX_FILTER_LENGTH).join("") : "";
  }

  function parseFragment(fragment) {
    const raw = typeof fragment === "string" ? fragment.replace(/^#/, "") : "";
    const values = new URLSearchParams(raw);
    return {
      view: VIEWS.has(values.get("view")) ? values.get("view") : DEFAULT_STATE.view,
      flatSort: FLAT_SORTS.has(values.get("flatSort"))
        ? values.get("flatSort")
        : DEFAULT_STATE.flatSort,
      flatDir: DIRECTIONS.has(values.get("flatDir"))
        ? values.get("flatDir")
        : DEFAULT_STATE.flatDir,
      tableSort: TABLE_SORTS.has(values.get("tableSort"))
        ? values.get("tableSort")
        : DEFAULT_STATE.tableSort,
      tableDir: DIRECTIONS.has(values.get("tableDir"))
        ? values.get("tableDir")
        : DEFAULT_STATE.tableDir,
      title: boundedText(values.get("title") || ""),
      author: boundedText(values.get("author") || ""),
      series: boundedText(values.get("series") || ""),
    };
  }

  function presentationState(state) {
    return {
      ...parseFragment(""),
      view: VIEWS.has(state?.view) ? state.view : DEFAULT_STATE.view,
      flatSort: FLAT_SORTS.has(state?.flatSort) ? state.flatSort : DEFAULT_STATE.flatSort,
      flatDir: DIRECTIONS.has(state?.flatDir) ? state.flatDir : DEFAULT_STATE.flatDir,
      tableSort: TABLE_SORTS.has(state?.tableSort)
        ? state.tableSort
        : DEFAULT_STATE.tableSort,
      tableDir: DIRECTIONS.has(state?.tableDir) ? state.tableDir : DEFAULT_STATE.tableDir,
    };
  }

  function serializeFragment(state, includeFilters = true) {
    const safe = {...presentationState(state)};
    if (includeFilters) {
      safe.title = boundedText(state?.title);
      safe.author = boundedText(state?.author);
      safe.series = boundedText(state?.series);
    }
    const values = new URLSearchParams();
    values.set("view", safe.view);
    values.set("flatSort", safe.flatSort);
    values.set("flatDir", safe.flatDir);
    values.set("tableSort", safe.tableSort);
    values.set("tableDir", safe.tableDir);
    if (includeFilters) {
      for (const field of ["title", "author", "series"]) {
        if (safe[field]) values.set(field, safe[field]);
      }
    }
    return `#${values.toString()}`;
  }

  function freshCriteriaState(state) {
    return presentationState(state);
  }

  function appendPresentationFragment(value, state, baseHref = "http://localhost/") {
    if (typeof value !== "string" || !value) return value;
    try {
      const base = new URL(baseHref);
      const url = new URL(value, base);
      if (url.origin !== base.origin || !["http:", "https:"].includes(url.protocol)) return value;
      url.hash = serializeFragment(state, false).slice(1);
      return `${url.pathname}${url.search}${url.hash}`;
    } catch (_error) {
      return value;
    }
  }

  function synchronizeCriteriaLinks(root, state, baseHref = window.location.href) {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll("a[data-catalog-criteria-link]").forEach((link) => {
      const next = appendPresentationFragment(link.getAttribute("href"), state, baseHref);
      if (next) link.href = next;
    });
  }

  function normalizePhrase(value) {
    if (typeof value !== "string") return "";
    return (value.normalize("NFKC").toLowerCase().replaceAll("ё", "е").match(/[\p{L}\p{N}]+/gu) || []).join(" ");
  }

  function unicodeScalarCompare(left, right) {
    const leftPoints = Array.from(String(left), (value) => value.codePointAt(0));
    const rightPoints = Array.from(String(right), (value) => value.codePointAt(0));
    const length = Math.min(leftPoints.length, rightPoints.length);
    for (let index = 0; index < length; index += 1) {
      if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] < rightPoints[index] ? -1 : 1;
    }
    return Math.sign(leftPoints.length - rightPoints.length);
  }

  function compareIntegerValues(left, right) {
    const normalizedLeft = left.replace(/^0+/, "") || "0";
    const normalizedRight = right.replace(/^0+/, "") || "0";
    if (normalizedLeft.length !== normalizedRight.length) {
      return normalizedLeft.length < normalizedRight.length ? -1 : 1;
    }
    return unicodeScalarCompare(normalizedLeft, normalizedRight);
  }

  function compareDigitRuns(left, right) {
    return compareIntegerValues(left, right) || unicodeScalarCompare(left, right);
  }

  function naturalTextCompare(left, right) {
    const normalizedLeft = String(left ?? "").normalize("NFKC").toLowerCase().replaceAll("ё", "е");
    const normalizedRight = String(right ?? "").normalize("NFKC").toLowerCase().replaceAll("ё", "е");
    const leftChunks = normalizedLeft.match(NATURAL_CHUNKS) || [];
    const rightChunks = normalizedRight.match(NATURAL_CHUNKS) || [];
    const length = Math.min(leftChunks.length, rightChunks.length);
    for (let index = 0; index < length; index += 1) {
      const leftChunk = leftChunks[index];
      const rightChunk = rightChunks[index];
      const leftDigits = /^[0-9]+$/.test(leftChunk);
      const rightDigits = /^[0-9]+$/.test(rightChunk);
      let compared;
      if (leftDigits && rightDigits) compared = compareDigitRuns(leftChunk, rightChunk);
      else if (leftDigits !== rightDigits) compared = leftDigits ? -1 : 1;
      else compared = unicodeScalarCompare(leftChunk, rightChunk);
      if (compared) return compared;
    }
    return Math.sign(leftChunks.length - rightChunks.length);
  }

  function compareNullableText(left, right) {
    const leftMissing = left === null || left === undefined || left === "";
    const rightMissing = right === null || right === undefined || right === "";
    if (leftMissing || rightMissing) {
      if (leftMissing === rightMissing) return 0;
      return leftMissing ? 1 : -1;
    }
    return unicodeScalarCompare(left, right);
  }

  function parseSeriesNumber(value) {
    if (value === null || value === undefined || String(value).trim() === "") {
      return {bucket: "missing", raw: "", digits: "", suffix: ""};
    }
    const raw = String(value).trim();
    const match = raw.match(/^([0-9]+)(.*)$/s);
    if (!match) return {bucket: "text", raw, digits: "", suffix: raw};
    const digits = match[1];
    const positive = /[1-9]/.test(digits);
    return {bucket: positive ? "positive" : "zero", raw, digits, suffix: match[2]};
  }

  function compareSeriesNumberValues(left, right, direction = "asc") {
    const leftNumber = parseSeriesNumber(left);
    const rightNumber = parseSeriesNumber(right);
    const ranks = direction === "desc"
      ? {text: 0, positive: 1, zero: 2, missing: 3}
      : {positive: 0, text: 1, zero: 2, missing: 3};
    if (ranks[leftNumber.bucket] !== ranks[rightNumber.bucket]) {
      return ranks[leftNumber.bucket] - ranks[rightNumber.bucket];
    }
    if (leftNumber.bucket === "missing") return 0;
    let compared = 0;
    if (leftNumber.bucket === "positive") {
      compared = compareIntegerValues(leftNumber.digits, rightNumber.digits);
      if (!compared) {
        compared = naturalTextCompare(leftNumber.suffix, rightNumber.suffix);
      }
    } else if (leftNumber.bucket === "text") {
      compared = naturalTextCompare(leftNumber.raw, rightNumber.raw);
    } else {
      compared = naturalTextCompare(leftNumber.raw, rightNumber.raw);
    }
    if (!compared) compared = unicodeScalarCompare(leftNumber.raw, rightNumber.raw);
    return direction === "desc" ? -compared : compared;
  }

  function firstAuthorKey(book) {
    return book.authors[0]?.sortKey || null;
  }

  function seriesKey(book) {
    return book.series?.sortKey || null;
  }

  function compareTitle(left, right) {
    return unicodeScalarCompare(left.titleSortKey, right.titleSortKey)
      || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function compareAuthorChain(left, right) {
    return compareNullableText(firstAuthorKey(left), firstAuthorKey(right))
      || compareNullableText(seriesKey(left), seriesKey(right))
      || compareSeriesNumberValues(left.series?.number, right.series?.number)
      || unicodeScalarCompare(left.titleSortKey, right.titleSortKey)
      || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function compareSeriesChain(left, right) {
    return compareNullableText(seriesKey(left), seriesKey(right))
      || compareSeriesNumberValues(left.series?.number, right.series?.number)
      || compareNullableText(firstAuthorKey(left), firstAuthorKey(right))
      || unicodeScalarCompare(left.titleSortKey, right.titleSortKey)
      || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function flatComparator(sort, direction = "asc") {
    const base = sort === "author" ? compareAuthorChain : sort === "series" ? compareSeriesChain : compareTitle;
    const multiplier = direction === "desc" ? -1 : 1;
    return (left, right) => multiplier * base(left, right);
  }

  function tableNumberCompare(left, right, direction) {
    return compareSeriesNumberValues(left.series?.number, right.series?.number, direction)
      || (direction === "desc" ? -1 : 1) * (
        compareNullableText(seriesKey(left), seriesKey(right))
        || unicodeScalarCompare(left.titleSortKey, right.titleSortKey)
        || unicodeScalarCompare(left.publicId, right.publicId)
      );
  }

  function tableComparator(sort, direction = "asc") {
    if (sort === "number") return (left, right) => tableNumberCompare(left, right, direction);
    return flatComparator(sort, direction);
  }

  function matchesFilters(book, filters) {
    const title = normalizePhrase(filters?.title || "");
    const author = normalizePhrase(filters?.author || "");
    const series = normalizePhrase(filters?.series || "");
    return (!title || normalizePhrase(book.title).includes(title))
      && (!author || book.authors.some((value) => normalizePhrase(value.display).includes(author)))
      && (!series || normalizePhrase(book.series?.name || "").includes(series));
  }

  function filterBooks(books, filters) {
    return books.filter((book) => matchesFilters(book, filters));
  }

  function hasInvalidText(value) {
    for (let index = 0; index < value.length; index += 1) {
      const unit = value.charCodeAt(index);
      if (unit === 0) return true;
      if (unit >= 0xd800 && unit <= 0xdbff) {
        const next = value.charCodeAt(index + 1);
        if (next < 0xdc00 || next > 0xdfff) return true;
        index += 1;
      } else if (unit >= 0xdc00 && unit <= 0xdfff) return true;
    }
    return false;
  }

  function isCleanText(value, {empty = true, length = 10_000} = {}) {
    return typeof value === "string" && value.length <= length && (empty || value.length > 0) && !hasInvalidText(value);
  }

  function safeServerPath(value) {
    return isCleanText(value, {empty: false, length: 4_096}) && !INVALID_URL_TEXT.test(value) && value.startsWith("/") && !value.startsWith("//") && !value.includes("\\");
  }

  function validLink(value) {
    return value && typeof value === "object" && safeServerPath(value.url) && isCleanText(value.label, {empty: false, length: 100});
  }

  function validateBook(value) {
    if (!value || typeof value !== "object"
      || !isCleanText(value.publicId, {empty: false, length: 256})
      || !isCleanText(value.title, {empty: false})
      || !isCleanText(value.titleSortKey)
      || !Array.isArray(value.authors)
      || !safeServerPath(value.detailUrl)) return null;
    const authors = [];
    const authorKeys = new Set();
    for (const author of value.authors) {
      if (!author || typeof author !== "object"
        || !isCleanText(author.raw, {empty: false})
        || !isCleanText(author.display, {empty: false})
        || !isCleanText(author.sortKey)
        || !safeServerPath(author.scopeUrl)
        || authorKeys.has(author.raw)) continue;
      authorKeys.add(author.raw);
      authors.push({raw: author.raw, display: author.display, sortKey: author.sortKey, scopeUrl: author.scopeUrl});
    }
    let series = null;
    if (value.series && typeof value.series === "object"
      && isCleanText(value.series.name, {empty: false})
      && isCleanText(value.series.sortKey)
      && (value.series.number === null || isCleanText(value.series.number))
      && safeServerPath(value.series.scopeUrl)) {
      series = {name: value.series.name, sortKey: value.series.sortKey, number: value.series.number, scopeUrl: value.series.scopeUrl};
    }
    const sourceFormat = value.sourceFormat && typeof value.sourceFormat === "object"
      && (value.sourceFormat.key === null || isCleanText(value.sourceFormat.key, {length: 30}))
      && isCleanText(value.sourceFormat.label, {empty: false, length: 100})
      ? {key: value.sourceFormat.key, label: value.sourceFormat.label} : {key: null, label: "Original"};
    const validAvailability = ["active", "hidden", "missed"].includes(value.availability);
    const availability = validAvailability ? value.availability : "active";
    const validSize = Number.isSafeInteger(value.size) && value.size >= 0;
    const size = validSize ? value.size : null;
    const actionsSafe = validAvailability && validSize;
    const downloadable = actionsSafe && value.downloadable === true && availability !== "missed";
    const readEligible = downloadable
      && ["fb2", "epub"].includes(sourceFormat.key)
      && size <= 64 * 1024 * 1024;
    return {
      publicId: value.publicId,
      title: value.title,
      titleSortKey: value.titleSortKey,
      authors,
      series,
      language: isCleanText(value.language, {length: 100}) ? value.language : "",
      sourceFormat,
      size,
      sizeLabel: isCleanText(value.sizeLabel, {length: 100}) ? value.sizeLabel : "",
      publishedDate: value.publishedDate === null || isCleanText(value.publishedDate, {length: 100}) ? value.publishedDate : null,
      availability,
      selectable: value.selectable === true && downloadable,
      downloadable,
      detailUrl: value.detailUrl,
      readUrl: readEligible && safeServerPath(value.readUrl) ? value.readUrl : null,
      originalDownload: downloadable && validLink(value.originalDownload) ? {url: value.originalDownload.url, label: value.originalDownload.label} : null,
      conversions: downloadable && Array.isArray(value.conversions)
        ? value.conversions.filter(validLink).map((link) => ({url: link.url, label: link.label})) : [],
    };
  }

  function validatePayload(value) {
    const books = [];
    const ids = new Set();
    if (value && typeof value === "object" && Array.isArray(value.books)) {
      for (const candidate of value.books) {
        const book = validateBook(candidate);
        if (book && !ids.has(book.publicId)) {
          ids.add(book.publicId);
          books.push(book);
          if (books.length === MAX_BOOKS) break;
        }
      }
    }
    return {books, truncated: value?.truncated === true};
  }

  function treeIdentity(type, ...parts) {
    return JSON.stringify([type, ...parts]);
  }

  function treeAuthorTargets(book) {
    if (book.authors.length === 0) return [{key: treeIdentity("synthetic-author", "unknown"), label: "Unknown author", sortKey: normalizePhrase("Unknown author")}];
    if (book.authors.length >= 6) return [{key: treeIdentity("synthetic-author", "many"), label: "Many authors (6+)", sortKey: normalizePhrase("Many authors (6+)")}];
    return book.authors.map((author) => ({key: treeIdentity("author", author.raw), label: author.display, sortKey: normalizePhrase(author.display), author}));
  }

  function buildTreeModel(books) {
    const authorGroups = new Map();
    for (const book of books) {
      for (const target of treeAuthorTargets(book)) {
        let branch = authorGroups.get(target.key);
        if (!branch) {
          branch = {...target, books: [], named: new Map(), withoutSeries: []};
          authorGroups.set(target.key, branch);
        }
        branch.books.push(book);
        if (book.series) {
          let leaf = branch.named.get(book.series.name);
          if (!leaf) {
            leaf = {key: treeIdentity("series", target.key, "named", book.series.name), label: book.series.name, sortKey: book.series.sortKey, scopeUrl: book.series.scopeUrl, books: []};
            branch.named.set(book.series.name, leaf);
          }
          leaf.books.push(book);
        } else branch.withoutSeries.push(book);
      }
    }
    const branches = [...authorGroups.values()];
    branches.sort((left, right) => unicodeScalarCompare(left.sortKey, right.sortKey) || unicodeScalarCompare(left.label, right.label) || unicodeScalarCompare(left.key, right.key));
    for (const branch of branches) {
      branch.series = [...branch.named.values()].sort((left, right) => unicodeScalarCompare(left.sortKey, right.sortKey) || unicodeScalarCompare(left.label, right.label));
      for (const leaf of branch.series) leaf.books.sort((left, right) => compareSeriesNumberValues(left.series?.number, right.series?.number) || compareTitle(left, right));
      branch.withoutSeries.sort(compareTitle);
      if (branch.withoutSeries.length) {
        branch.series.push({key: treeIdentity("series", branch.key, "without-series"), label: "Books without series", sortKey: null, books: branch.withoutSeries, withoutSeries: true});
      }
      branch.count = new Set(branch.books.map((book) => book.publicId)).size;
      delete branch.named;
      delete branch.withoutSeries;
    }
    return branches;
  }

  function filterExpansionTransition(expansion, snapshot, wasFiltering, isFiltering) {
    if (!wasFiltering && isFiltering) {
      return {expansion, snapshot: new Map(expansion)};
    }
    if (wasFiltering && !isFiltering) {
      return {expansion: snapshot ? new Map(snapshot) : expansion, snapshot: null};
    }
    return {expansion, snapshot};
  }

  function element(tag, className = "", text = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function safeAnchor(label, href, className = "") {
    const anchor = element("a", className, label);
    anchor.href = safeServerPath(href) ? href : "/";
    return anchor;
  }

  function metadataLink(label, href) {
    const anchor = safeAnchor(label, href);
    anchor.dataset.catalogCriteriaLink = "";
    return anchor;
  }

  function appendAuthors(container, book, limit) {
    const visible = book.authors.slice(0, limit);
    visible.forEach((author, index) => {
      container.append(metadataLink(author.display, author.scopeUrl));
      if (index < visible.length - 1) container.append(document.createTextNode(", "));
    });
    if (book.authors.length > limit) {
      const disclosure = element("details", "author-overflow");
      const summary = element("summary", "", `+${book.authors.length - limit} more`);
      disclosure.append(summary);
      const links = element("span", "author-overflow__links");
      book.authors.slice(limit).forEach((author, index, values) => {
        links.append(metadataLink(author.display, author.scopeUrl));
        if (index < values.length - 1) links.append(document.createTextNode(", "));
      });
      disclosure.append(links);
      container.append(disclosure);
    }
  }

  function buildSelectionControl(book) {
    if (!book.selectable) return null;
    const label = element("label", "selection-control");
    label.dataset.selectionControl = "";
    label.hidden = true;
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.dataset.selectionCheckbox = "";
    checkbox.dataset.publicId = book.publicId;
    const accessible = element("span", "visually-hidden", `Select ${book.title}`);
    label.append(checkbox, accessible);
    return label;
  }

  function buildActions(book, includeSelection = true) {
    const actions = element("div", "result-row__actions");
    const selection = includeSelection ? buildSelectionControl(book) : null;
    if (selection) actions.append(selection);
    if (book.readUrl) {
      const read = safeAnchor("Read", book.readUrl, "result-row__action result-row__read");
      read.target = "_blank";
      read.rel = "noopener noreferrer";
      actions.append(read);
    }
    if (book.originalDownload) {
      const split = element("div", "download-split");
      const original = safeAnchor(book.originalDownload.label, book.originalDownload.url, "result-row__download");
      original.setAttribute("aria-label", `Download original ${book.originalDownload.label} file for ${book.title}`);
      split.append(original);
      if (book.conversions.length) {
        const menu = element("details", "download-menu");
        const summary = element("summary", "", "Formats");
        summary.setAttribute("aria-label", `More download formats for ${book.title}`);
        const items = element("div", "download-menu__items");
        for (const conversion of book.conversions) {
          const link = safeAnchor(conversion.label, conversion.url);
          link.setAttribute("aria-label", `Download ${conversion.label} conversion for ${book.title}`);
          items.append(link);
        }
        menu.append(summary, items);
        split.append(menu);
      }
      actions.append(split);
    }
    actions.append(safeAnchor("Details", book.detailUrl, "result-row__action"));
    return actions;
  }

  function buildBookRow(book, index = 0, compact = false) {
    const row = element("article", compact ? "result-row result-row--catalog tree-book-row" : "result-row result-row--catalog");
    row.dataset.publicId = book.publicId;
    const tile = element("div", `book-tile book-tile--${(index % 4) + 1}`, Array.from(book.title.trim())[0]?.toUpperCase() || "?");
    tile.setAttribute("aria-hidden", "true");
    const body = element("div", "result-row__body");
    const heading = element("div", "result-row__heading");
    const title = element("h2", "result-row__title");
    title.append(safeAnchor(book.title, book.detailUrl));
    heading.append(title);
    if (book.availability !== "active") heading.append(element("span", `availability-badge availability-badge--${book.availability}`, book.availability === "missed" ? "Missed" : "Hidden"));
    body.append(heading);
    if (book.authors.length) {
      const authors = element("div", "result-row__authors");
      authors.append(element("span", "visually-hidden", "Authors: "));
      appendAuthors(authors, book, 3);
      body.append(authors);
    }
    if (book.series) {
      const series = element("p", "result-row__series");
      series.append(element("span", "visually-hidden", "Series: "), metadataLink(book.series.name, book.series.scopeUrl));
      if (book.series.number) series.append(document.createTextNode(` #${book.series.number}`));
      body.append(series);
    }
    const metadata = element("ul", "result-metadata");
    metadata.setAttribute("aria-label", "Book metadata");
    const first = element("li", "result-metadata__line");
    first.append(element("span", "", book.sourceFormat.label));
    if (book.language) first.append(document.createTextNode(` · ${book.language.toUpperCase()}`));
    const second = element("li", "result-metadata__line");
    if (book.publishedDate) second.append(document.createTextNode(`${book.publishedDate} · `));
    second.append(document.createTextNode(book.sizeLabel));
    metadata.append(first, second);
    const selection = buildSelectionControl(book);
    if (selection) row.append(selection);
    row.append(tile, body, metadata, buildActions(book, false));
    return row;
  }

  function emitRendered(root) {
    document.dispatchEvent(new CustomEvent(RENDER_EVENT, {detail: {root}}));
  }

  class CatalogController {
    constructor(root, payload, state) {
      this.root = root;
      this.books = payload.books;
      this.truncated = payload.truncated;
      this.state = state;
      this.mount = root.querySelector("[data-catalog-result-view]");
      this.sortMount = root.querySelector("[data-catalog-sort-controls]");
      this.summary = root.querySelector("#catalog-loaded-summary");
      this.loadedSummary = this.summary?.textContent || "";
      this.expansion = new Map();
      this.expansionSnapshot = null;
      this.wasFiltering = false;
      this.treeScroll = 0;
      this.bind();
      this.applyStateToControls();
      this.render();
    }

    destroy() {
      this.abort?.abort();
    }

    bind() {
      this.abort = new AbortController();
      const options = {signal: this.abort.signal};
      this.root.addEventListener("click", (event) => {
        const view = event.target.closest("[data-catalog-view]");
        if (view) {
          if (this.state.view === "tree") this.treeScroll = this.mount.scrollTop;
          this.state.view = VIEWS.has(view.dataset.catalogView) ? view.dataset.catalogView : "flat";
          this.commit();
          this.applyStateToControls();
          this.render();
          return;
        }
        if (event.target.closest("[data-catalog-clear-filters]")) {
          this.state.title = "";
          this.state.author = "";
          this.state.series = "";
          this.applyStateToControls();
          this.commit();
          this.render();
        }
      }, options);
      this.root.addEventListener("input", (event) => {
        const field = event.target.closest("[data-catalog-filter]");
        if (!field || !["title", "author", "series"].includes(field.dataset.catalogFilter)) return;
        const name = field.dataset.catalogFilter;
        field.value = boundedText(field.value);
        this.state[name] = field.value;
        this.commit();
        this.render();
      }, options);
      this.sortMount.addEventListener("change", (event) => {
        const select = event.target.closest("[data-catalog-sort]");
        if (!select) return;
        if (this.state.view === "flat" && FLAT_SORTS.has(select.value)) this.state.flatSort = select.value;
        if (this.state.view === "table" && TABLE_SORTS.has(select.value)) this.state.tableSort = select.value;
        this.commit();
        this.render();
        this.sortMount.querySelector("[data-catalog-sort]")?.focus();
      }, options);
      this.sortMount.addEventListener("click", (event) => {
        if (!event.target.closest("[data-catalog-direction]")) return;
        const key = this.state.view === "flat" ? "flatDir" : "tableDir";
        this.state[key] = this.state[key] === "asc" ? "desc" : "asc";
        this.commit();
        this.render();
        this.sortMount.querySelector("[data-catalog-direction]")?.focus();
      }, options);
    }

    commit() {
      window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}${serializeFragment(this.state)}`);
      synchronizeCriteriaLinks(document, this.state);
    }

    applyStateToControls() {
      this.root.querySelectorAll("[data-catalog-view]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.catalogView === this.state.view));
      });
      this.root.querySelectorAll("[data-catalog-filter]").forEach((input) => {
        input.value = this.state[input.dataset.catalogFilter];
      });
    }

    filteredBooks() {
      return filterBooks(this.books, this.state);
    }

    updateSummary(count) {
      if (!this.summary) return;
      const filtered = this.state.title || this.state.author || this.state.series;
      if (!filtered) {
        this.summary.textContent = this.loadedSummary;
        return;
      }
      if (count === 0 && this.truncated) {
        this.summary.textContent = `0 of ${this.books.length} loaded books. Additional catalog matches were not loaded; refine the catalog search to search beyond this loaded set.`;
      } else {
        this.summary.textContent = `${count} of ${this.books.length} loaded ${this.books.length === 1 ? "book" : "books"}.`;
      }
    }

    renderSortControls() {
      this.sortMount.replaceChildren();
      if (this.state.view === "tree") return;
      const table = this.state.view === "table";
      const sort = table ? this.state.tableSort : this.state.flatSort;
      const direction = table ? this.state.tableDir : this.state.flatDir;
      const label = element("label", "catalog-sort-control", "Sort by ");
      const select = element("select");
      select.dataset.catalogSort = "";
      const choices = table ? ["author", "title", "series", "number"] : ["title", "author", "series"];
      for (const choice of choices) {
        const option = element("option", "", choice[0].toUpperCase() + choice.slice(1));
        option.value = choice;
        option.selected = choice === sort;
        select.append(option);
      }
      label.append(select);
      const button = element("button", "", direction === "asc" ? "Ascending" : "Descending");
      button.type = "button";
      button.dataset.catalogDirection = "";
      button.setAttribute("aria-label", `Sort ${direction === "asc" ? "descending" : "ascending"}`);
      this.sortMount.append(label, button);
    }

    render() {
      const books = this.filteredBooks();
      this.updateSummary(books.length);
      this.renderSortControls();
      this.mount.replaceChildren();
      if (!books.length) {
        const empty = element("div", "catalog-results__message catalog-local-empty");
        empty.setAttribute("role", "status");
        empty.append(element("h2", "", "No loaded books match"));
        empty.append(element("p", "", this.truncated
          ? "Additional catalog matches were not loaded. Refine the catalog search to search beyond this loaded set."
          : "Clear a local filter or try a broader phrase."));
        this.mount.append(empty);
      } else if (this.state.view === "tree") this.renderTree(books);
      else if (this.state.view === "table") this.renderTable(books);
      else this.renderFlat(books);
      synchronizeCriteriaLinks(this.root, this.state);
      emitRendered(this.mount);
    }

    renderFlat(books) {
      const sorted = [...books].sort(flatComparator(this.state.flatSort, this.state.flatDir));
      const list = element("div", "catalog-flat-view");
      sorted.forEach((book, index) => list.append(buildBookRow(book, index)));
      this.mount.append(list);
    }

    renderTree(books) {
      const filtering = Boolean(this.state.title || this.state.author || this.state.series);
      const transitioned = filterExpansionTransition(this.expansion, this.expansionSnapshot, this.wasFiltering, filtering);
      this.expansion = transitioned.expansion;
      this.expansionSnapshot = transitioned.snapshot;
      this.wasFiltering = filtering;
      const tree = element("div", "catalog-tree-view");
      const branches = buildTreeModel(books);
      for (const branch of branches) {
        const author = element("details", "catalog-tree-author");
        author.open = filtering || (this.expansion.has(branch.key) ? this.expansion.get(branch.key) : true);
        const authorSummary = element("summary", "catalog-tree-author__summary");
        if (branch.author) authorSummary.append(metadataLink(branch.label, branch.author.scopeUrl));
        else authorSummary.append(document.createTextNode(branch.label));
        authorSummary.append(element("span", "catalog-tree-count", ` (${branch.count})`));
        author.append(authorSummary);
        author.addEventListener("toggle", () => {
          if (!filtering) this.expansion.set(branch.key, author.open);
        });
        for (const leaf of branch.series) {
          const details = element("details", "catalog-tree-series");
          details.open = filtering || (this.expansion.has(leaf.key) ? this.expansion.get(leaf.key) : false);
          const summary = element("summary", "catalog-tree-series__summary");
          if (leaf.scopeUrl) summary.append(metadataLink(leaf.label, leaf.scopeUrl));
          else summary.append(document.createTextNode(leaf.label));
          summary.append(element("span", "catalog-tree-count", ` (${new Set(leaf.books.map((book) => book.publicId)).size})`));
          details.append(summary);
          let rendered = false;
          const renderLeaf = (lazy) => {
            if (rendered || !details.open) return;
            const rows = element("div", "catalog-tree-books");
            leaf.books.forEach((book, index) => rows.append(buildBookRow(book, index, true)));
            details.append(rows);
            synchronizeCriteriaLinks(rows, this.state);
            rendered = true;
            if (lazy) emitRendered(this.mount);
          };
          if (details.open) renderLeaf(false);
          details.addEventListener("toggle", () => {
            if (!filtering) this.expansion.set(leaf.key, details.open);
            renderLeaf(true);
          });
          author.append(details);
        }
        tree.append(author);
      }
      this.mount.append(tree);
      this.mount.scrollTop = this.treeScroll;
    }

    renderTable(books) {
      const sorted = [...books].sort(tableComparator(this.state.tableSort, this.state.tableDir));
      const wrapper = element("div", "catalog-table-scroll");
      wrapper.dataset.catalogTableScroll = "";
      const table = element("table", "catalog-table");
      const caption = element("caption", "visually-hidden", "Loaded catalog books");
      const head = element("thead");
      const headerRow = element("tr");
      for (const name of ["author", "title", "series", "number", "actions"]) {
        const cell = element("th");
        cell.scope = "col";
        if (name === "actions") cell.textContent = "Actions";
        else {
          const button = element("button", "catalog-table__sort", name[0].toUpperCase() + name.slice(1));
          button.type = "button";
          button.dataset.catalogTableSort = name;
          button.addEventListener("click", () => {
            if (this.state.tableSort === name) this.state.tableDir = this.state.tableDir === "asc" ? "desc" : "asc";
            else {
              this.state.tableSort = name;
              this.state.tableDir = "asc";
            }
            this.commit();
            this.render();
            const replacement = [...this.mount.querySelectorAll("[data-catalog-table-sort]")]
              .find((candidate) => candidate.dataset.catalogTableSort === name);
            replacement?.focus();
          });
          cell.append(button);
          if (this.state.tableSort === name) {
            cell.setAttribute("aria-sort", this.state.tableDir === "asc" ? "ascending" : "descending");
            button.append(element("span", "visually-hidden", `, sorted ${this.state.tableDir === "asc" ? "ascending" : "descending"}`));
          }
        }
        headerRow.append(cell);
      }
      head.append(headerRow);
      const body = element("tbody");
      for (const book of sorted) {
        const row = element("tr");
        row.dataset.publicId = book.publicId;
        const authors = element("td");
        appendAuthors(authors, book, 2);
        const title = element("td");
        title.append(safeAnchor(book.title, book.detailUrl));
        if (book.availability !== "active") title.append(document.createTextNode(` — ${book.availability === "missed" ? "Missed" : "Hidden"}`));
        const series = element("td");
        if (book.series) series.append(metadataLink(book.series.name, book.series.scopeUrl));
        const number = element("td", "", book.series?.number || "");
        const actions = element("td");
        actions.append(buildActions(book));
        row.append(authors, title, series, number, actions);
        body.append(row);
      }
      table.append(caption, head, body);
      wrapper.append(table);
      this.mount.append(wrapper);
    }
  }

  function readPayload(root) {
    const script = root.querySelector("[data-catalog-payload]");
    if (!script) return validatePayload(null);
    try {
      return validatePayload(JSON.parse(script.textContent));
    } catch (_error) {
      return validatePayload(null);
    }
  }

  function canonicalizeState(state) {
    const expected = serializeFragment(state);
    if (window.location.hash !== expected) {
      window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}${expected}`);
    }
  }

  function initializeCatalog(root = document.querySelector("[data-catalog-root]"), stateOverride = null) {
    const state = stateOverride ? {...stateOverride} : parseFragment(window.location.hash);
    if (!root) {
      activeController?.destroy();
      activeController = null;
      synchronizeCriteriaLinks(document, state);
      return null;
    }
    if (activeController?.root === root) activeController.destroy();
    else activeController?.destroy();
    canonicalizeState(state);
    activeController = new CatalogController(root, readPayload(root), state);
    synchronizeCriteriaLinks(document, state);
    return activeController;
  }

  function detailHistoryContext(referrer, currentHref, historyLength) {
    try {
      if (!referrer) return {fallback: "/", canGoBack: false};
      const current = new URL(currentHref);
      const previous = new URL(referrer);
      if (previous.origin !== current.origin || !["/", "/selected"].includes(previous.pathname)) {
        return {fallback: "/", canGoBack: false};
      }
      return {fallback: previous.pathname === "/selected" ? "/selected" : "/", canGoBack: historyLength > 1};
    } catch (_error) {
      return {fallback: "/", canGoBack: false};
    }
  }

  function initializeDetailBack() {
    const context = detailHistoryContext(document.referrer, window.location.href, window.history.length);
    document.querySelectorAll("[data-detail-back]").forEach((link) => {
      link.href = context.fallback;
      if (!context.canGoBack) return;
      link.addEventListener("click", (event) => {
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        window.history.back();
      });
    });
  }

  function currentPresentationState() {
    return activeController?.state || parseFragment(window.location.hash);
  }

  function handleCriteriaClick(event) {
    const link = event.target.closest("a[data-catalog-criteria-link]");
    if (!link || event.defaultPrevented) return;
    const next = appendPresentationFragment(link.getAttribute("href"), currentPresentationState(), window.location.href);
    if (next) link.href = next;
  }

  function handleBeforeHistoryUpdate(event) {
    const history = event.detail?.history;
    if (!history || typeof history.path !== "string") return;
    pendingFreshState = freshCriteriaState(currentPresentationState());
    history.path = appendPresentationFragment(history.path, pendingFreshState, window.location.href);
  }

  function initialize() {
    initializeDetailBack();
    initializeCatalog();
    document.addEventListener("click", handleCriteriaClick);
    document.addEventListener("htmx:beforeHistoryUpdate", handleBeforeHistoryUpdate);
    document.addEventListener("htmx:historyCacheHit", () => {
      historyRestoring = true;
    });
    document.addEventListener("htmx:historyCacheMissLoad", () => {
      historyRestoring = true;
    });
    document.addEventListener("htmx:afterSwap", (event) => {
      if (historyRestoring) return;
      const target = event.detail?.target || event.detail?.elt || event.target;
      synchronizeCriteriaLinks(target, pendingFreshState || currentPresentationState());
      const root = target?.matches?.("[data-catalog-root]") ? target : target?.querySelector?.("[data-catalog-root]");
      if (root || !document.querySelector("[data-catalog-root]")) {
        initializeCatalog(root || null, root && pendingFreshState ? pendingFreshState : null);
        pendingFreshState = null;
      }
    });
    document.addEventListener("htmx:historyRestore", () => {
      historyRestoring = false;
      pendingFreshState = null;
      initializeCatalog(document.querySelector("[data-catalog-root]"), parseFragment(window.location.hash));
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once: true});
  else initialize();
})();
