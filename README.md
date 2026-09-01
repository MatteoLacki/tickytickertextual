# tickytickertextual

A small, read-only filesystem navigator with a ranger-like three-pane layout.
Directories are read only when they are visited or selected; there is no generated
file index, recursive scan, application database, upload, rename, or delete operation.

Folder-only mode is enabled initially. Ordinary folders are listed before `.d`
datasets, while files can be revealed with `Ctrl+.`. The right pane follows the
same mode for ordinary folders but shows all contents when previewing a `.d`.
It does not decode or preview filesystem images. Application-generated image views can
be added later without coupling them to filesystem browsing.

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

The directory argument is optional for both commands and defaults to the process current working directory.

The status row reports state only. All primary commands are shown in the compact, styled footer, which changes when focus moves between the filesystem and `:selected:` panes.

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move selection |
| `l`, right arrow, Enter | Enter an ordinary directory |
| Space in middle pane | Add and mark a `.d`, read its `analysis.tdf` Description, then move down |
| `Ctrl+Down` / `Ctrl+Up` | Move focus between filesystem and `:selected:` |
| Space in `:selected:` | Toggle the highlighted path as HeLa |
| `x` or click `×` | Remove a path from `:selected:` |
| `h`, left arrow, Backspace | Return to parent |
| `g` / `G` | First / last entry |
| `.` | Toggle hidden entries |
| `Ctrl+.` | Toggle folder-only mode (on initially) |
| `/` | Filter current names with a shell glob; empty input clears it |
| `r` | Refresh current directory |
| Shift+H or Help | Open the overall usage popup |
| `q` | Quit the session |

Navigation is confined to the root passed on the command line. Symlinks are
displayed but never followed.

