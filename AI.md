# AI project notes

`tickytickertextual` is a Python Textual application that provides a read-only, ranger-style filesystem navigator in a terminal or browser. The browser server uses `textual-serve`.

## Structure

- `src/tickytickertextual/app.py`: filesystem model, Textual interface, direct tickyticker fit/TIC workers, settings, locking, ASCII plots, and SVG generator.
- `src/tickytickertextual/workflow.py`: multi-HeLa membership, reference selection, normalization, incremental batches and browser delivery.
- `src/tickytickertextual/plots.py`: file-free histogram and combined chromatogram SVGs and browser viewer.
- `src/tickytickertextual/web.py`: self-hosted browser entry point and in-memory table export bridge. SVGs use Textual's native delivery protocol.
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
- Render resize-aware dominant-charge and histogram plots, plus fit parameters. Left/right switches review tabs. View/download acts on the active plot. Generate SVGs only in memory and use `deliver_text`; never write server plot files. Combine chromatograms for all completed datasets with folder and Description headings.
- Description containing `hela` after lowercasing automatically enables blue normalization membership. Space toggles membership; Enter on an enabled HeLa opens settings and selects an orange prospective fitting reference. There is no `s` settings shortcut. Preserve manual membership overrides.
- Cancelling/rejecting a proposed fit preserves the previous completed analysis. Accepting a new fit sequentially recalculates every selected dataset. Use the same accepted line, m/z range, RT range, intensity threshold and frame stride for every dataset. Fill absolute QQ (below) and Q (on/above) first; after the batch, divide by their separate arithmetic means across successful enabled HeLas. Failed rows show ERROR; absent/zero denominators give unavailable ratios.
- Rerun accepts unchanged settings and remains usable repeatedly. Newly added folders expose TIC new folders, which uses the accepted fit/settings without recomputing existing absolute TICs. New HeLas update all relative values. Disable/grey removal and freeze membership/reference changes while calculations run.
- Persist validated algorithm settings atomically as TOML. Hold the configured Linux `flock` for the full UI process lifetime so a second UI session cannot open concurrently.
- Catch every analysis-worker exception. Require acknowledgement in a red modal with a syntax-coloured traceback and context-specific recovery guidance; insufficient dominant-charge evidence should suggest another dataset or greater sampling coverage.
- Read Description and Volume/unit from SampleInfo.xml and gradient from read-only Frames.Time. Display Volume after Gradient, retain one-line rows, and show only the final folder in selected Path. Keep full paths internally and in the preview. The nine table/export columns are Path, Description, Gradient, Volume, QQ, QQ / QQ-HeLa, Q, Q / Q-HeLa, Fit Parameters. Display ratios as percentages but export numeric ratios. Clipboard/CSV share full-precision structured values, not rendered text.
- Pending metadata displays `...`; missing/invalid Gradient or Volume displays red `NA` and blocks selection. Configuration for a reference provides Calculate and Reject directly; Calculate starts fitting without an intermediate confirmation.
- Expose only mz_min/mz_max (350–1200 defaults), and pass them as both analysis and fitting bounds. Migrate old TOML border limits into the sole range. Unthresholded raw TIC is also filtered to that range. Do not multiply sampled TICs by frame stride.
- Browser export controls sit below the selection, grey/disabled until the batch finishes and red/enabled afterward. Production mode suppresses toast notifications, not error/progress panels.

## Development

Dependencies live in `pyproject.toml` and are locked by `uv.lock`; in this paired checkout tickyticker resolves from the editable core repository at `../..`. Use `make venv`, `make sync`, and `make test`. Override `DIRECTORY`, `SETTINGS`, or `LOCK_FILE` as needed. `PRODUCTION=1` suppresses toast notifications.
