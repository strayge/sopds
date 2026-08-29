# Project guidance

## Product and UX intent

- Optimize the web UI primarily for readers finding and downloading books. Administration is deliberately secondary and should not compete with the catalog.
- The intended visual character is compact utility software with a warm editorial tone—not a spacious magazine or card-based design.
- Treat an active book as the normal state and leave it implicit. Surface availability labels only for exceptional states such as hidden or missed.
- Keep missed records discoverable for their metadata and details, but never present them as downloadable.
- Returning from a book detail page must restore the reader's exact catalog context, including search, filters, and pagination.
- Keep the management page deliberately terse. Operational explanations and security guidance belong in documentation unless explicitly requested in the UI.

## Documentation audience

- Treat `README.md` primarily as documentation for catalog users and operators.
- Describe features through user goals, visible actions, benefits, limits, and recovery steps.
- Use the exact labels shown in the UI.
- Do not document internal routes, handler names, request formats, storage mechanisms, control flow, or cleanup implementation in user-facing sections.
- Technical detail is appropriate in `README.md` only when needed for deployment, configuration, local execution, backup, or connecting an external client.
- Put developer architecture and implementation details in `docs/ARCHITECTURE.md`.
- Document only user-observable behavior; do not add technical notes merely because code changed.

## Browser state and privacy

- Do not introduce cookies, sessions, or server-persisted reader state without explicit approval.
- Keep book selection local to the browser and preserve the backend's stateless selection model.
- Before adding browser state for security or UX, explain the threat model and persistence trade-offs.
