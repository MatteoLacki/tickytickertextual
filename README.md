# tickytickertextual

<p align="center">
  <img src="tickytickerlogo.png" alt="tickytickertextual logo" width="320">
</p>

A small, read-only filesystem navigator with a ranger-like three-pane layout.
Directories are read only when they are visited or selected; there is no generated
file index, recursive scan, application database, upload, rename, or delete operation.

Folder-only mode is enabled initially. Ordinary folders are listed before `.d`
datasets, while files can be revealed with `Ctrl+.`. The right pane follows the
same mode for ordinary folders. When a `.d` contains both `analysis.tdf` and
`analysis.tdf_bin`, its preview shows the `SampleInfo.xml` Description, the
gradient length calculated from the span of `Frames.Time`, and the
human-readable sizes of those two files instead of exposing its contents. The
metadata is cached and reused if the dataset is added to `:selected:`. Once
loaded, gradient length also appears in the aligned current-pane row. The app does not
decode or preview filesystem images.
Tickyticker results are rendered separately as responsive terminal-native
ASCII views, with a high-resolution SVG download for each plot.

## Charge-area analysis

Folders whose Description contains `hela` (case-insensitive) automatically join
the blue HeLa normalization group when added to `:selected:`. Space toggles
membership without launching analysis; manual choices survive table refreshes.
Enter on a blue HeLa opens parameter selection and marks that prospective fitting
reference orange. **Calculate** in that parameter panel starts the fit immediately
in a background thread, without another confirmation; **Reject** cancels.
In the subsequent results review, **Accept fit**
starts TIC calculations for every selected dataset. Only one HeLa supplies the
separator. All enabled, successfully processed HeLas supply normalization means.

Absolute QQ (below-line multicharge) and Q (on/above-line single-charge) values
appear as each dataset completes. Relative values appear after the batch finishes:
`QQ / mean(HeLa QQ)` and `Q / mean(HeLa Q)`. The UI shows percentages; CSV/TSV
store numeric ratios, with `1.0` meaning 100%. Failed HeLas are excluded visibly;
missing references or zero means leave the corresponding ratios unavailable.
No volume or frame-stride rescaling is applied.

**Rerun** allows repeated analysis with all parameters editable, including an
unchanged parameter set. Cancelling/rejecting a proposed rerun preserves prior
completed results. After adding more folders, **TIC new folders** uses the accepted
line and parameter snapshot only for those additions. New HeLas update every
relative value at completion. Removing datasets is disabled/grey during TIC work.

Left/right arrows switch review panels. **Help**, beside **Restart app**, lists
all keyboard shortcuts; keyboard footers are hidden.
Both dominant-charge and event-histogram views resize vertically. Download
SVG acts on the active plot. **See Chromatograms** opens a single scalable,
vertically stacked figure labelled with each folder name and Description.

If analysis fails, the app opens a red error dialog with a colourized,
scrollable traceback and a concrete recovery suggestion. It must be
acknowledged with **OK**. Insufficient charge evidence, for example, suggests
choosing another `.d` dataset or increasing frame/scan coverage in settings.

The UI imports `tickyticker.charge_regions.analyse()` and
`analyse_line_tic()` as package APIs. Their in-memory results avoid NPZ, JSON,
PNG, and log-file round trips. Plots are published to the web server in memory and served directly over HTTP,
so downloads do not depend on the terminal connection. The latest document for
each plot/format is retained until replacement or session restart. Viewer tabs have their own Download SVG button; no
server plot files, expiry timers, or persistent browser storage are required.
The regular
tickyticker CLIs remain available for deliberate archival runs.

Enter on a HeLa or press **Rerun** to edit parameters, then **Calculate** to run.
Edits are validated and apply only to the current session. Each new session reads
`src/tickytickertextual/defaults.toml` (or the file supplied with `SETTINGS` / `--settings`).
The app never writes to that file. **Defaults** restores the startup values;
**Restart app** discards all edits and reloads the defaults file. A Linux `flock` held at
`/tmp/tickyticker/tickytickertextual.lock` permits only one active UI session;
the kernel releases it automatically when that process exits.

Only `mz_min` and `mz_max` are exposed, defaulting to 350–1200. This range applies
to fitting and all TICs, including unthresholded raw TIC. Legacy TOML files use
their former border limits as the new range in memory; the file stays unchanged. `rt_min`, `rt_max`, and `frame_stride` are shared by fit and TIC passes.
Frame stride samples MS1 frames after retention-time filtering; TICs are not
scaled to estimate skipped frames.

Volume and its unit come from XML `Sample.Volume` and `AutoSamplerVolumeUnit`.
They appear below Gradient length in the preview and after Gradient in `:current:`.
Gradient length is the retention-time span from read-only `analysis.tdf` Frames.Time.
Unread metadata shows `...`; missing or invalid Gradient or Volume shows red `NA`.
A dataset cannot be selected unless both fields are available.
Full paths remain available in the preview; selected rows show only folder names.

The browser's **Restart app** button is always available, including during a
calculation or after a session ends. It stops the old app process and starts a
fresh session, clearing selections, fits, TIC results and exports. Session parameter edits are discarded and startup defaults are reloaded. `r` only refreshes the folder listing.

The browser's **Copy table**, **Save table**, and **Raw TIC CSV** buttons sit beside
**See Chromatograms** and **Rerun** in one row below the selection. They are grey before results/during calculation and red when ready.
Copy and Save use the same table values plus the export date, never terminal text.
Table exports are held in memory, not `/tmp`. `--production` suppresses toast
notifications; progress and error panels remain available.

## Install

```bash
make venv
make sync
```

Run it in a terminal:

```bash
make run DIRECTORY=/path/to/browse
```

Run the same interface in a browser:

```bash
make web DIRECTORY=/path/to/browse
```

Then open <http://127.0.0.1:8000> on the VM, or `http://<VM-IP>:8000`
from another computer. `make web` listens on all interfaces (`HOST=0.0.0.0`)
by default; use `make web HOST=127.0.0.1` for local access only. Browser assets
and the app connection use the address you opened. For a reverse proxy, set
`PUBLIC_URL=https://your-app.example`. Use a trusted network or VPN, or put
the service behind authentication and HTTPS.

The Make targets default to `/mnt/bigssd/tickyticker/data`. Override the root,
settings file or lock file with `DIRECTORY`, `SETTINGS`, or `LOCK_FILE`.
Use `PRODUCTION=1` with Make to suppress toast notifications. The `./webui`
launcher enables this by default; use `./webui PRODUCTION=0` for development. The directory argument of the
underlying `tickytickertextual` and
`tickytickertextual-web` commands remains optional and defaults to the process
current working directory. In this paired checkout, uv resolves `tickyticker`
from the editable core repository two directories above the app, ensuring the
RT/per-frame API matches the UI. Install this checkout layout before `make sync`.

The status row reports state only. Use **Help** for the keyboard reference.
**Estimate charge split**, beside Help, opens the fit parameters for the
highlighted eligible HeLa in `:selected:`. It is disabled for other rows or
while calculation is running.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move selection |
| `l`, right arrow, Enter | Enter an ordinary directory |
| Space in middle pane | Add and mark a `.d`, reuse its cached Description, then move down |
| `Ctrl+Down` / `Ctrl+Up` | Move focus between filesystem and `:selected:` |
| Space in `:selected:` | Toggle HeLa normalization membership |
| Enter in `:selected:` | Choose an enabled HeLa for fitting and edit parameters |
| Calculate / Reject in configuration | Start the fit immediately / cancel without changing previous results |
| `y` / `n` in review | Accept / reject the fitted separator |
| `x` or click `×` | Remove a path from `:selected:` |
| `h`, left arrow, Backspace | Return to parent |
| `g` / `G` | First / last entry |
| `.` | Toggle hidden entries |
| `Ctrl+.` | Toggle folder-only mode (on initially) |
| `/` | Filter current names with a shell glob; empty input clears it |
| Left/right in review | Switch between plot/parameter panels |
| `r` | Refresh current directory |
| Shift+H or Help | Open the overall usage popup |
| `q` | Quit the session |

Navigation is confined to the root passed on the command line. Symlinks are
displayed but never followed.

The ten selected-table columns are `Path`, `Description`, `Gradient`, `Volume`,
`QQ`, `QQ / QQ-HeLa`, `Q`, `Q / Q-HeLa`, `Fit Parameters`, and `Injection amount (µL)`. Rows never wrap;
wide tables scroll horizontally. A path ending in `/e/f/g/folder.d` displays
as `folder.d`. The separate raw CSV has per-frame RT, raw TIC, QQ and Q values.

## Injection amount

After TIC calculation, **Injection amount (µL)** is calculated as
`ROUND((mean HeLa QQ / sample QQ) × (MM target ng / PM QC ng) × original injection volume in µL, 2)`.
The reference is the same enabled, successfully processed HeLa group used for
QQ normalization. The **MM target amount (ng)** field defaults to 200 and updates
all injection amounts immediately without repeating the analysis. Changes last
only for this session; restart reloads `[injection].target_amount_ng` from the
defaults file. **PM QC amount (ng)** is also editable, defaults to 100, and
updates the column immediately. It reloads from `[injection].pm_qc_amount_ng`
on restart.

The original volume comes from each dataset's XML metadata. µL/μL/uL, nL, mL
and L are converted to µL. Results use spreadsheet-style rounding to two decimal
places in both the table and CSV/TSV. Values remain blank while calculation is
incomplete, or if QQ, HeLa reference, target, or volume is missing/invalid/zero,
or the volume unit is unknown. Changing the enabled HeLa group or completing
TIC for newly selected folders recomputes the column.

Copied tables, selected-table CSV and raw TIC CSV include a leading `Date` column
using the server-local export date in `dd.mm.yyyy` format. The date is added when
the export is requested, including when a session remains open past midnight.

Download buttons (table exports, review SVGs, and the chromatogram viewer)
are disabled for at least one second after a click. Review SVG downloads stay
disabled until background preparation and publication finish; only one plot is
prepared at a time. The review shows progress or a retryable error. SVG generation
and HTTP publication run outside the UI event loop. Copy table uses the same cooldown.
Chromatogram legends label QQ as `multicharge` and Q as `singly charge`.

The browser controls share one full-width row of equally sized buttons. Button
availability is pushed over the existing connection when selection or app state
changes; clicks immediately show feedback. Moving the selection reuses cached
exports instead of rebuilding or uploading the CSV data.

The `:current:` and `:selected:` regions share the available height equally.
During TIC calculation, each queued row shows `Queued` in QQ, and the active
row shows its processed frame count. Its final QQ replaces that progress on
completion. Progress text is never included in exported tables.

Each browser start and app restart opens a welcome window with the ASCII logo.
Click `oh my tickyticker` to enter the file browser. The artwork is read from
`logoascii.txt` in the app repository, or the existing `logoacsii.txt` spelling;
installed packages also include a copy of the logo.
