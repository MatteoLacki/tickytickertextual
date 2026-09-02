# AI project notes

`tickytickertextual` is a Python Textual application that provides a read-only, ranger-style filesystem navigator in a terminal or browser. The browser server uses `textual-serve`.

## Structure

- `src/tickytickertextual/app.py`: filesystem model, Textual interface, direct tickyticker analysis worker, settings, locking, and ASCII result renderer.
- `src/tickytickertextual/web.py`: self-hosted browser entry point.
- `src/tickytickertextual/templates/app_index.html`: textual-serve template with the browser `Ctrl+.` bridge.
- `tests/`: filesystem safety and keyboard-navigation tests.

## Invariants

- Read directories on demand with `os.scandir`; do not index or recursively scan.
- Confine navigation to the configured root and never follow symlinks.
- Keep the navigator read-only. Files show metadata only; file contents and filesystem images are not previewed.
- Folder-only mode defaults on across filesystem panes. A `.d` containing both `analysis.tdf` and `analysis.tdf_bin` previews as Description plus human-readable file sizes instead of an internal listing. Sort ordinary folders, then `.d` folders, then files.
- Name filtering is a non-recursive, current-directory shell glob and must not build an index.
- Preserve the browser template bridge: legacy terminal transport cannot distinguish `Ctrl+.` from `.`, so the template sends Textual's extended key sequence.
- The optional CLI directory defaults to the process current working directory; Make targets are project-configured to `/mnt/bigssd/tickyticker/data`.
- Choosing a HeLa dataset must confirm before analysis. Call `tickyticker.charge_regions.analyse()` directly in a Textual worker thread, omit `output_dir`, stream its progress callback into the loading panel, and consume its `ChargeRegionResult` without analysis-file round trips.
- Render only the terminal-native dominant-charge map with the fitted alpha separator overlaid, the raw-event histogram, and numerical run metadata. Do not display intensity maps or generate UI-specific PNGs.
- Persist validated algorithm settings atomically as TOML. Hold the configured Linux `flock` for the full UI process lifetime so a second UI session cannot open concurrently.
- Catch every analysis-worker exception. Require acknowledgement in a red modal with a syntax-coloured traceback and context-specific recovery guidance; insufficient dominant-charge evidence should suggest another dataset or greater sampling coverage.
- `.d` directories can be collected in the interactive `:selected:` pane. Cache `GlobalMetadata.Description` from the read-only built-in `sqlite3` query during preview and reuse it during selection; do not query it twice. Selected rows show root-relative paths and their Description on one line. One path may be marked as `:HELA CHOSEN:`, and removal must not touch the filesystem.

## Development

Dependencies live in `pyproject.toml` and are locked by `uv.lock`; tickyticker is pinned as a direct Git dependency. Use `make venv`, `make sync`, and `make test`. The Make targets default to the project data, settings, and lock paths; override with `DIRECTORY`, `SETTINGS`, or `LOCK_FILE` as needed.
