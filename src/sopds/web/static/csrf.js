(() => {
  "use strict";

  document.addEventListener("htmx:responseError", (event) => {
    const detail = event.detail;
    const xhr = detail && detail.xhr;
    const target = detail && detail.target;
    if (
      !xhr ||
      xhr.status !== 403 ||
      xhr.getResponseHeader("X-SOPDS-CSRF-Expired") !== "true" ||
      !(target instanceof Element)
    ) {
      return;
    }
    target.innerHTML = xhr.responseText;
  });
})();
