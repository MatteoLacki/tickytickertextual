# tickytickertextual

A small, read-only filesystem navigator with a ranger-like three-pane layout.
Directories are read only when they are visited or selected; there is no file
index, recursive scan, database, upload, rename, or delete operation.

The right pane deliberately shows only child names or file metadata. It does
not decode or preview filesystem images. Application-generated image views can
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

## Keys

| Key | Action |
| --- | --- |
| `j` / `k`, arrows | Move selection |
| `l`, right arrow, Enter | Enter selected directory |
| `h`, left arrow, Backspace | Return to parent |
| `g` / `G` | First / last entry |
| `.` | Toggle hidden entries |
| `r` | Refresh current directory |
| `q` | Quit the session |

Navigation is confined to the root passed on the command line. Symlinks are
displayed but never followed.

