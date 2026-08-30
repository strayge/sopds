# Foliate-js vendor snapshot

Upstream: <https://github.com/johnfactotum/foliate-js>

Commit: `78914aef4466eb960965702401634c2cb348e9b1`

The snapshot contains only the direct FB2/reflowable-EPUB reader path:

- `view.js` provides the reader custom element and navigation/events.
- `paginator.js` provides reflowable pagination.
- `fb2.js` and `epub.js` provide the two supported publication adapters.
- `epubcfi.js` and `progress.js` provide locations and progress.
- `overlayer.js` and `text-walker.js` satisfy `view.js`'s static imports.
- `vendor/zip.js` provides the ZIP primitives required for EPUB. It is Foliate's bundled copy of `@zip.js/zip.js` 2.8.22; the supported path disables its workers.
- `LICENSE` preserves Foliate-js's MIT license.
- `vendor/zip.js.LICENSE` preserves zip.js's BSD-3-Clause license and copyright notice.

All copied files are byte-for-byte identical to that commit except for the local
changes documented below.

Security patches:

1. `paginator.js` removes `allow-scripts` from publication iframe sandboxes while
   retaining `allow-same-origin`. SOPDS supports Chromium and Firefox rather than
   Foliate's WebKit event workaround.
2. `view.js` still emits the cancelable `external-link` custom event but removes
   Foliate's default call to `globalThis.open`, leaving link handling to the
   embedding adapter.
3. `paginator.js` disconnects its resize observers during destruction and makes
   queued iframe-load, resize, font, and style callbacks no-op, preventing observer
   reactivation and detached-document access during reader teardown.
4. `paginator.js` gives every publication iframe the stable accessible title
   `Book content` before attaching it to the paginator.
Reader behavior patch:

- `paginator.js` supports switching between paginated and `flow=scrolled` layouts,
  forwards wheel input from publication documents to the section scroller,
  hides that section-local native scrollbar when the application provides its
  whole-book control, changes sections only for thresholded outward boundary
  swipes, and aligns keyboard screen advances to a visible text line with a
  small overlap.

Compatibility patches:

- `fb2.js` folds a primary body's title, leading epigraphs, bibliographic cover,
  and non-duplicated annotation into its first real section without changing that
  section's contents label, while retaining a leading body cover image and
  additional note bodies. It omits empty contents entries and preserves supported
  inline markup in verse lines.

Formatting-only normalization:

- `overlayer.js` removes the upstream trailing blank line at end of file. This
  does not change runtime behavior.

`view.js` retains upstream optional dynamic imports. The SOPDS adapter passes an
already-constructed FB2 or EPUB book to `View.open()`, so type-dispatch imports for
CBZ, PDF, MOBI, FB2-in-ZIP, and their helpers are not reached. EPUB preflight
rejects fixed-layout books, and SOPDS does not call the optional search or TTS
methods, so those omitted modules are not loaded. The reflowable branch loads the
included `paginator.js`.
