# AI project notes

`tickytickertextual` is a Python Textual application that provides a read-only, ranger-style filesystem navigator in a terminal or browser. The browser server uses `textual-serve`.

## Structure

- `src/tickytickertextual/app.py`: filesystem model, Textual interface, direct tickyticker fit/TIC workers, settings, locking, ASCII plots, and SVG generator.
- `src/tickytickertextual/web.py`: self-hosted browser entry point and non-indexed temporary SVG route.
- `src/tickytickertextual/templates/app_index.html`: textual-serve template with the browser `Ctrl+.` bridge.
- `tests/`: filesystem safety and keyboard-navigation tests.

## Invariants

- Read directories on demand with `os.scandir`; do not index or recursively scan.
- Confine navigation to the configured root and never follow symlinks.
- Keep the navigator read-only. Files show metadata only; file contents and filesystem images are not previewed.
- Folder-only mode defaults on across filesystem panes. A `.d` containing both `analysis.tdf` and `analysis.tdf_bin` previews its `SampleInfo.xml` Description, gradient length from `MAX(Frames.Time) - MIN(Frames.Time)`, and human-readable file sizes instead of an internal listing. Cache this metadata. Show gradient length in aligned current-pane rows. Sort ordinary folders, then `.d` folders, then files.
- Name filtering is a non-recursive, current-directory shell glob and must not build an index.
- Preserve the browser template bridge: legacy terminal transport cannot distinguish `Ctrl+.` from `.`, so the template sends Textual's extended key sequence.
- The optional CLI directory defaults to the process current working directory; Make targets are project-configured to `/mnt/bigssd/tickyticker/data`.
- Choosing a HeLa dataset must confirm before analysis. Keep loading and review in one large modal. Call `tickyticker.charge_regions.analyse()` directly in a Textual worker thread, omit `output_dir`, stream its progress callback into the modal, and consume its `ChargeRegionResult` without analysis-file round trips.
- Render a large terminal-native dominant-charge map with the fitted alpha separator, a compact vertical raw-event histogram, and only the fitted intercept/slope. Do not display intensity maps or general run-data dumps. A uniquely named SVG of the dominant-charge view may be generated in the configured temporary plot directory and served without directory indexing.
- Rejecting a fit clears the HeLa choice but preserves `:selected:`. Accepting it calls `tickyticker.charge_regions.analyse_line_tic()` sequentially for every selected dataset. Use the same fit line, user comparison m/z range, minimum intensity, and frame stride for all datasets. Report integer TIC below and on/above the line plus both percentages relative to HeLa. Mark a failed row `ERROR`, exclude it, and continue.
- Persist validated algorithm settings atomically as TOML. Hold the configured Linux `flock` for the full UI process lifetime so a second UI session cannot open concurrently.
- Catch every analysis-worker exception. Require acknowledgement in a red modal with a syntax-coloured traceback and context-specific recovery guidance; insufficient dominant-charge evidence should suggest another dataset or greater sampling coverage.
- `.d` directories can be collected in the interactive `:selected:` pane. Read Description from `SampleInfo.xml` with the standard library and the `Frames.Time` span through one read-only built-in `sqlite3` connection; reuse the cached object during selection and analysis. User-editable `mz_min`/`mz_max` are the primary comparison window passed to both fit and TIC APIs. Selected rows use aligned columns ordered as root-relative path, Description, below TIC, above TIC, and optional HeLa fit; omit gradient length there. One path may be marked as `:HELA CHOSEN:`, and removal must not touch the filesystem.

## Development

Dependencies live in `pyproject.toml` and are locked by `uv.lock`; tickyticker is pinned as a direct Git dependency. Use `make venv`, `make sync`, and `make test`. The Make targets default to the project data, settings, and lock paths; override with `DIRECTORY`, `SETTINGS`, or `LOCK_FILE` as needed.
