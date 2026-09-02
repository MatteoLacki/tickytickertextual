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
`analysis.tdf_bin`, its preview shows `GlobalMetadata.Description` and the
human-readable sizes of those two files instead of exposing its contents. The
Description is cached and reused if the dataset is added to `:selected:`. It
does not decode or preview filesystem images. Tickyticker results are rendered
separately as responsive terminal-native ASCII views.

## Charge-area analysis

In `:selected:`, Space marks or unmarks a dataset as HeLa. Marking it opens a
confirmation popup. Choosing **Yes, scan** calls tickyticker’s `analyse()`
function directly in a background worker and reveals a blinking
`:charge areas:` panel. The result tabs contain a colour-coded ASCII
dominant-charge map with the fitted charge border overlaid, an ASCII raw-event
histogram, and the numerical run summary. Intensity-map images are not shown.
The UI remains responsive while the analysis runs.

If analysis fails, the app opens a red error dialog with a colourized,
scrollable traceback and a concrete recovery suggestion. It must be
acknowledged with **OK**. Insufficient charge evidence, for example, suggests
choosing another `.d` dataset or increasing frame/scan coverage in settings.

The UI imports `tickyticker.charge_regions.analyse()` as a package API. Its
in-memory mode receives the NumPy arrays and fitted border model directly and
writes no NPZ, JSON, PNG, or log files. The regular `charge-regions` CLI remains
available in the tickyticker package for deliberate archival runs.

Press `s` to edit the analysis parameters. They are validated and atomically
stored in `/tmp/tickyticker/settings.toml` by default. A Linux `flock` held at
`/tmp/tickyticker/tickytickertextual.lock` permits only one active UI session;
the kernel releases it automatically when that process exits.

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
settings file, or lock file with `DIRECTORY`, `SETTINGS`, or `LOCK_FILE`. The
directory argument of the underlying `tickytickertextual` and
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
| `y` / Yes, scan | Run the in-memory charge-area analysis |
| `n` / No | Keep HeLa selected without running the analysis |
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

Rows in `:selected:` show a root-relative path followed by the Description on
one line. For example, a dataset rooted at `/a/b/c/d/e/f/g/folder.d` while
serving `/a/b/c/d` is displayed as `e/f/g/folder.d`.
