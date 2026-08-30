import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

const scriptPath = new URL("../src/sopds/web/static/reader/i18n.js", import.meta.url);
const source = readFileSync(scriptPath, "utf8");
const behavior = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const russianMessages = {
  pages: "Страницы",
  scroll: "Прокрутка",
  switchToPagesView: "Переключиться на постраничный режим",
  switchToScrollView: "Переключиться на прокрутку",
  previousPage: "Предыдущая страница",
  nextPage: "Следующая страница",
  genericOpenError: "Не удалось открыть книгу в веб-читалке.",
};

function configured(locale, messages = russianMessages) {
  return behavior.readReaderI18n({
    dataset: {
      readerLocale: locale,
      readerMessages: JSON.stringify(messages),
    },
  });
}

test("dynamic mode and directional controls use validated English and Russian labels", () => {
  const english = behavior.readReaderI18n(null);
  assert.deepEqual(behavior.readerModeControl(english, "scroll"), {
    text: "Pages",
    ariaLabel: "Switch to pages view",
  });
  assert.deepEqual(behavior.readerModeControl(english, "pages"), {
    text: "Scroll",
    ariaLabel: "Switch to scroll view",
  });
  assert.deepEqual(behavior.readerEdgeLabels(english, false), {
    left: "Previous page",
    right: "Next page",
  });

  const russian = configured("ru");
  assert.deepEqual(behavior.readerModeControl(russian, "scroll"), {
    text: "Страницы",
    ariaLabel: "Переключиться на постраничный режим",
  });
  assert.deepEqual(behavior.readerEdgeLabels(russian, true), {
    left: "Следующая страница",
    right: "Предыдущая страница",
  });
});

test("percentages use the selected locale without changing the bounded fraction", () => {
  for (const locale of ["en", "ru"]) {
    const i18n = configured(locale, locale === "ru" ? russianMessages : {});
    const expected = new Intl.NumberFormat(locale, {
      style: "percent",
      maximumFractionDigits: 0,
    });
    assert.equal(behavior.formatReaderPercent(i18n, 0.456), expected.format(0.456));
    assert.equal(behavior.formatReaderPercent(i18n, -1), expected.format(0));
    assert.equal(behavior.formatReaderPercent(i18n, 2), expected.format(1));
  }
});

test("malformed locale and message payloads fall back to bounded English", () => {
  const invalidLocale = configured("ru-RU");
  assert.equal(invalidLocale.locale, "en");
  assert.equal(behavior.readerMessage(invalidLocale, "pages"), "Pages");

  const malformedJson = behavior.readReaderI18n({
    dataset: {readerLocale: "ru", readerMessages: "{"},
  });
  assert.equal(behavior.readerMessage(malformedJson, "nextPage"), "Next page");

  const malformedMessages = configured("ru", {
    ...russianMessages,
    pages: "",
    scroll: "x".repeat(301),
    switchToPagesView: "Некорректно {title}",
  });
  assert.equal(behavior.readerMessage(malformedMessages, "pages"), "Pages");
  assert.equal(behavior.readerMessage(malformedMessages, "scroll"), "Scroll");
  assert.equal(
    behavior.readerMessage(malformedMessages, "switchToPagesView"),
    "Switch to pages view",
  );
  assert.equal(behavior.readerMessage(malformedMessages, "nextPage"), "Следующая страница");
});

test("unexpected failures use the localized generic recovery message", () => {
  const russian = configured("ru");
  assert.equal(
    behavior.safeReaderErrorMessage(russian, new TypeError("private implementation detail")),
    "Не удалось открыть книгу в веб-читалке.",
  );
  assert.equal(behavior.safeReaderErrorMessage(russian, null), russianMessages.genericOpenError);
});

test("PublicationError diagnostics remain exact and are never translated", () => {
  const diagnostic = "EPUB entry ../private/content.xhtml is unsafe.";
  const publicationError = {name: "PublicationError", message: diagnostic};
  assert.equal(
    behavior.safeReaderErrorMessage(configured("ru"), publicationError),
    diagnostic,
  );
  assert.equal(
    behavior.safeReaderErrorMessage(behavior.readReaderI18n(null), publicationError),
    diagnostic,
  );
});
