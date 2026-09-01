# AI project notes

`tickytickertextual` is a Python Textual application that provides a read-only, ranger-style filesystem navigator in a terminal or browser. The browser server uses `textual-serve`.

## Structure

- `src/tickytickertextual/app.py`: filesystem model and Textual interface.
- `src/tickytickertextual/web.py`: self-hosted browser entry point.
- `src/tickytickertextual/templates/app_index.html`: textual-serve template with the browser `Ctrl+.` bridge.
- `tests/`: filesystem safety and keyboard-navigation tests.

## Invariants

- Read directories on demand with `os.scandir`; do not index or recursively scan.
- Confine navigation to the configured root and never follow symlinks.
- Keep the navigator read-only. Files show metadata only; file contents and filesystem images are not previewed.
- Folder-only mode defaults on across filesystem panes; `.d` previews may show all contents. Sort ordinary folders, then `.d` folders, then files.
- Name filtering is a non-recursive, current-directory shell glob and must not build an index.
- Preserve the browser template bridge: legacy terminal transport cannot distinguish `Ctrl+.` from `.`, so the template sends Textual's extended key sequence.
- The optional CLI directory defaults to the process current working directory.
- `.d` directories can be collected in the interactive `:selected:` pane; selection reads only `GlobalMetadata.Description` from `analysis.tdf` via built-in `sqlite3`, one path may be marked as `:HELA CHOSEN:`, and removal must not touch the filesystem.

## Development

Dependencies live in `pyproject.toml` and are locked by `uv.lock`. Use `make venv`, `make sync`, and `make test`. Run locally with `make run DIRECTORY=/path` or `make web DIRECTORY=/path`.
