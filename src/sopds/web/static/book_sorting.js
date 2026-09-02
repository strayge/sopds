(() => {
  "use strict";

  /*
   * Sortable book expectations:
   *
   * {
   *   publicId: string,
   *   titleSortKey: string,
   *   authors: Array<{sortKey: string}>,
   *   series: {sortKey: string, number: string | null} | null,
   * }
   *
   * Page-specific fields may be carried alongside these fields. Comparators do
   * not mutate books and use only the fields above.
   */

  const NATURAL_CHUNKS = /[0-9]+|[^0-9]+/g;

  function normalizeSortKey(value) {
    return String(value ?? "").normalize("NFKC").toLowerCase().replaceAll("ё", "е");
  }

  function normalizePhrase(value) {
    if (typeof value !== "string") return "";
    return (normalizeSortKey(value).match(/[\p{L}\p{N}]+/gu) || []).join(" ");
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
    const normalizedLeft = normalizeSortKey(left);
    const normalizedRight = normalizeSortKey(right);
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

  function compareNaturalText(left, right) {
    return naturalTextCompare(left, right) || unicodeScalarCompare(left, right);
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
      if (!compared) compared = naturalTextCompare(leftNumber.suffix, rightNumber.suffix);
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

  function compareTitleKeys(left, right) {
    return compareNaturalText(left.titleSortKey, right.titleSortKey);
  }

  function compareTitle(left, right) {
    return compareTitleKeys(left, right) || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function compareAuthorChain(left, right) {
    return compareNullableText(firstAuthorKey(left), firstAuthorKey(right))
      || compareNullableText(seriesKey(left), seriesKey(right))
      || compareSeriesNumberValues(left.series?.number, right.series?.number)
      || compareTitleKeys(left, right)
      || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function compareSeriesChain(left, right) {
    return compareNullableText(seriesKey(left), seriesKey(right))
      || compareSeriesNumberValues(left.series?.number, right.series?.number)
      || compareNullableText(firstAuthorKey(left), firstAuthorKey(right))
      || compareTitleKeys(left, right)
      || unicodeScalarCompare(left.publicId, right.publicId);
  }

  function flatComparator(sort, direction = "asc") {
    const base = sort === "author" ? compareAuthorChain : sort === "series" ? compareSeriesChain : compareTitle;
    const multiplier = direction === "desc" ? -1 : 1;
    return (left, right) => multiplier * base(left, right);
  }

  function tableComparator(sort, direction = "asc") {
    const base = sort === "author" ? compareAuthorChain : sort === "series" ? compareSeriesChain : compareTitle;
    const multiplier = direction === "desc" ? -1 : 1;
    return (left, right) => multiplier * base(left, right);
  }

  function treeComparator(left, right) {
    return compareSeriesNumberValues(left.series?.number, right.series?.number)
      || compareTitle(left, right);
  }

  function groupKey(type, label) {
    return identityKey(type, label);
  }

  function identityKey(type, ...parts) {
    return JSON.stringify([type, ...parts.map((part) => String(part))]);
  }

  function compareGroups(left, right) {
    return unicodeScalarCompare(left.sortKey, right.sortKey)
      || unicodeScalarCompare(left.stableLabel ?? left.label, right.stableLabel ?? right.label)
      || unicodeScalarCompare(left.key, right.key);
  }

  const namespace = Object.freeze({
    normalizeSortKey,
    normalizePhrase,
    unicodeScalarCompare,
    compareIntegerValues,
    compareDigitRuns,
    naturalTextCompare,
    compareNullableText,
    parseSeriesNumber,
    compareSeriesNumberValues,
    compareTitle,
    compareAuthorChain,
    compareSeriesChain,
    flatComparator,
    tableComparator,
    treeComparator,
    groupKey,
    identityKey,
    compareGroups,
  });

  const root = typeof globalThis === "object" && globalThis !== null ? globalThis : window;
  Object.defineProperty(root, "SOPDSBookSorting", {
    value: namespace,
    configurable: false,
    enumerable: false,
    writable: false,
  });
  if (root.window && root.window !== root) {
    Object.defineProperty(root.window, "SOPDSBookSorting", {
      value: namespace,
      configurable: false,
      enumerable: false,
      writable: false,
    });
  }
})();
