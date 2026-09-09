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
ASCII views, with an optional high-resolution SVG for the dominant-charge map.

## Charge-area analysis

In `:selected:`, Space marks or unmarks a dataset as HeLa. Marking it opens a
large confirmation popup. **Calculate** calls tickyticker’s `analyse()`
function directly in a background worker; the popup stays open and shows
blinking progress. It then becomes a review window with a large colour-coded
ASCII dominant-charge map and fitted separator, a compact vertical raw-event
histogram, and the fitted intercept and slope. Intensity maps and general data
dumps are not shown. **Open hi-res SVG** opens a scalable version of the
dominant-charge separation plot in a separate browser tab.

**Reject** closes the popup, clears the HeLa choice, and preserves every path
in `:selected:`. **Accept fit** closes it and sequentially revisits every
selected dataset with the accepted separator. Each row reports integer TIC
below the line, integer TIC on/above the line, and both values relative to the
HeLa reference. Both sides use the configured raw-event intensity threshold,
comparison m/z window, and MS1 frame stride. A failed dataset is marked
`ERROR`, excluded from comparisons, and does not stop the remaining datasets.

If analysis fails, the app opens a red error dialog with a colourized,
scrollable traceback and a concrete recovery suggestion. It must be
acknowledged with **OK**. Insufficient charge evidence, for example, suggests
choosing another `.d` dataset or increasing frame/scan coverage in settings.

The UI imports `tickyticker.charge_regions.analyse()` and
`analyse_line_tic()` as package APIs. Their in-memory results avoid NPZ, JSON,
PNG, and log-file round trips. Only the explicitly requested, uniquely named
SVG is written to the configured temporary plot directory. The regular
tickyticker CLIs remain available for deliberate archival runs.

Press `s` to edit the analysis parameters. They are validated and atomically
stored in `/tmp/tickyticker/settings.toml` by default. A Linux `flock` held at
`/tmp/tickyticker/tickytickertextual.lock` permits only one active UI session;
the kernel releases it automatically when that process exits.

The comparison minimum and maximum m/z are the first and most important
settings: the same user-selected interval is applied to HeLa and every compared
dataset. The border-fit left/right values describe the narrower interval used
only to fit the separator.

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

Then open <http://127.0.0.1:8000>. To listen on the network, pass
`--host 0.0.0.0`, but put the service behind authentication and HTTPS or a
trusted VPN first.

The Make targets default to `/mnt/bigssd/tickyticker/data`. Override the root,
settings file, lock file, or temporary SVG directory with `DIRECTORY`,
`SETTINGS`, `LOCK_FILE`, or `PLOT_DIRECTORY`. The directory argument of the
underlying `tickytickertextual` and
`tickytickertextual-web` commands remains optional and defaults to the process
current working directory. During development, uv resolves the direct
`tickyticker` dependency from its pinned GitHub revision.

The status row reports state only. All primary commands are shown in the compact, styled footer, which changes when focus moves between the filesystem and `:selected:` panes.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move selection |
| `l`, right arrow, Enter | Enter an ordinary directory |
| Space in middle pane | Add and mark a `.d`, reuse its cached Description, then move down |
| `Ctrl+Down` / `Ctrl+Up` | Move focus between filesystem and `:selected:` |
| Space in `:selected:` | Toggle the highlighted path as HeLa and, when choosing it, open the scan prompt |
| `y` / Calculate | Run the in-memory charge-area analysis; in review, accept the fit |
| `n` / Reject | Clear the HeLa choice while preserving all selected paths |
| `x` or click `×` | Remove a path from `:selected:` |
| `h`, left arrow, Backspace | Return to parent |
| `g` / `G` | First / last entry |
| `.` | Toggle hidden entries |
| `Ctrl+.` | Toggle folder-only mode (on initially) |
| `/` | Filter current names with a shell glob; empty input clears it |
| `s` | Edit and save the charge-analysis settings |
| `r` | Refresh current directory |
| Shift+H or Help | Open the overall usage popup |
| `q` | Quit the session |

Navigation is confined to the root passed on the command line. Symlinks are
displayed but never followed.

Rows in `:selected:` use fixed-width columns in this order: root-relative path,
Description, below-line TIC, on/above-line TIC, and an optional fit on the HeLa
row.
For example, a dataset rooted at `/a/b/c/d/e/f/g/folder.d` while serving
`/a/b/c/d` is displayed as `e/f/g/folder.d`. Existing settings files missing
the restored comparison bounds are migrated with defaults of 100–1700 m/z
without changing their other configured values.
