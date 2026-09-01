# AI project notes

`tickytickertextual` is a Python Textual application that provides a read-only, ranger-style filesystem navigator in a terminal or browser. The browser server uses `textual-serve`.

## Structure

- `src/tickytickertextual/app.py`: filesystem model and Textual interface.
- `src/tickytickertextual/web.py`: self-hosted browser entry point.
- `tests/`: filesystem safety and keyboard-navigation tests.

## Invariants

- Read directories on demand with `os.scandir`; do not index or recursively scan.
- Confine navigation to the configured root and never follow symlinks.
- Keep the navigator read-only. Files show metadata only; file contents and filesystem images are not previewed.
- The optional CLI directory defaults to the process current working directory.
- `.d` directories can be collected in the interactive `:selected:` pane; one path may be marked as `:HELA CHOSEN:`, and removal must not touch the filesystem.

## Development

Dependencies live in `pyproject.toml` and are locked by `uv.lock`. Use `make venv`, `make sync`, and `make test`. Run locally with `make run DIRECTORY=/path` or `make web DIRECTORY=/path`.
