"""Read-only, ranger-style filesystem navigation with Textual."""

from __future__ import annotations

import argparse
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, OptionList, Static


class NavigationError(Exception):
    """Raised when a path cannot be safely listed."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """The bounded metadata needed to draw one directory entry."""

    name: str
    path: Path
    is_dir: bool
    is_symlink: bool
    size: int | None
    modified: float | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """One on-demand directory read."""

    path: Path
    entries: tuple[FileEntry, ...]
    truncated: bool = False


class FileSystemNavigator:
    """Read directories on demand while remaining inside a fixed root."""

    def __init__(self, root: str | os.PathLike[str], *, show_hidden: bool = False):
        try:
            resolved_root = Path(root).expanduser().resolve(strict=True)
        except OSError as error:
            raise NavigationError(f"Cannot open root {root!s}: {error}") from error
        if not resolved_root.is_dir():
            raise NavigationError(f"Root is not a directory: {resolved_root}")

        self.root = resolved_root
        self.current = resolved_root
        self.show_hidden = show_hidden

    def scan(
        self,
        path: str | os.PathLike[str],
        *,
        limit: int | None = None,
    ) -> DirectoryListing:
        """Read one directory without recursion or persistent caching."""
        directory = self._checked_directory(path)
        entries: list[FileEntry] = []
        truncated = False

        try:
            with os.scandir(directory) as iterator:
                for raw_entry in iterator:
                    if not self.show_hidden and raw_entry.name.startswith("."):
                        continue
                    if limit is not None and len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(self._make_entry(raw_entry))
        except OSError as error:
            raise NavigationError(f"Cannot read {directory}: {error}") from error

        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.casefold(), entry.name))
        return DirectoryListing(directory, tuple(entries), truncated)

    def change_directory(self, path: str | os.PathLike[str]) -> DirectoryListing:
        """Validate and enter a directory, returning its fresh listing."""
        listing = self.scan(path)
        self.current = listing.path
        return listing

    def parent(self) -> Path:
        """Return the parent bounded by the configured root."""
        if self.current == self.root:
            return self.root
        return self.current.parent

    def _checked_directory(self, path: str | os.PathLike[str]) -> Path:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise NavigationError(f"Cannot resolve {candidate}: {error}") from error

        if not resolved.is_relative_to(self.root):
            raise NavigationError(f"Path is outside configured root: {resolved}")
        if not resolved.is_dir():
            raise NavigationError(f"Path is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _make_entry(raw_entry: os.DirEntry[str]) -> FileEntry:
        try:
            metadata = raw_entry.stat(follow_symlinks=False)
        except OSError:
            size: int | None = None
            modified: float | None = None
            mode: int | None = None
        else:
            size = metadata.st_size
            modified = metadata.st_mtime
            mode = metadata.st_mode

        return FileEntry(
            name=raw_entry.name,
            path=Path(raw_entry.path),
            is_dir=raw_entry.is_dir(follow_symlinks=False),
            is_symlink=raw_entry.is_symlink(),
            size=size,
            modified=modified,
            mode=mode,
        )


def format_size(size: int | None) -> str:
    """Format a byte count for the metadata pane."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def _entry_label(entry: FileEntry) -> Text:
    label = Text(no_wrap=True, overflow="ellipsis")
    if entry.is_dir:
        label.append("▸ ", style="bold cyan")
        label.append(entry.name, style="bold cyan")
        label.append("/", style="cyan")
    elif entry.is_symlink:
        label.append("↗ ", style="magenta")
        label.append(entry.name, style="magenta")
        label.append("@", style="dim magenta")
    else:
        label.append("  ")
        label.append(entry.name)
    return label


def _listing_text(
    entries: Iterable[FileEntry],
    *,
    selected_name: str | None = None,
    truncated: bool = False,
) -> Text:
    output = Text()
    for entry in entries:
        line = _entry_label(entry)
        if entry.name == selected_name:
            line.stylize("reverse bold")
        output.append_text(line)
        output.append("\n")
    if truncated:
        output.append("… more entries not read", style="dim italic")
    return output


class SelectedOptionList(OptionList):
    """Option list with a clickable remove control on each row."""

    class RemoveRequested(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    async def _on_click(self, event: events.Click) -> None:
        remove_index = event.style.meta.get("remove-selected")
        if isinstance(remove_index, int):
            self.highlighted = remove_index
            event.stop()
            self.post_message(self.RemoveRequested(remove_index))
            return
        await super()._on_click(event)


def _selected_path_label(path: Path, index: int, *, chosen: bool) -> Text:
    label = Text(no_wrap=True, overflow="ellipsis")
    label.append(
        " × ",
        style=Style(
            color="#ff7b72",
            bold=True,
            meta={"remove-selected": index},
        ),
    )
    label.append("★ " if chosen else "  ", style="bold yellow" if chosen else "dim")
    label.append(str(path), style="bold yellow" if chosen else "#c9d1d9")
    if chosen:
        label.stylize("on #3d3200")
    return label


class HelpScreen(ModalScreen[None]):
    """Basic keyboard and mouse usage."""

    CSS = """
    HelpScreen {
        align: center middle;
        background: #000000 70%;
    }

    #help-dialog {
        width: 76;
        max-width: 95%;
        height: 28;
        max-height: 95%;
        padding: 1 2;
        border: round #58a6ff;
        background: #161b22;
    }

    #help-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #8be9fd;
    }

    #help-content {
        height: 1fr;
    }

    #help-close {
        width: 16;
        height: 3;
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("escape", "close_help", "Close", show=False),
        Binding("shift+h", "close_help", "Close", show=False),
    ]

    HELP_TEXT = """[b]Filesystem[/b]
  j/k or arrows     move in the middle pane
  l/right/Enter     enter an ordinary directory
  h/left/Backspace  return to the parent
  .                 toggle hidden entries
  r                 reload

[b].d datasets[/b]
  Space             add highlighted .d folder to :selected:
  Enter             jump from an already-selected .d to :selected:
  Click             focus a row in :selected:
  j/k or arrows     move through selected paths
  Space             mark the current path as HELA CHOSEN
  x or click ×      remove the current path
  h                 return focus to the middle pane

Shift+H opens this help. Escape or Close dismisses it. q quits."""

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Static("tickytickertextual help", id="help-title")
            yield Static(self.HELP_TEXT, id="help-content")
            yield Button("Close", id="help-close", variant="primary")

    def action_close_help(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.dismiss()



class FileViewerApp(App[None]):
    """Three-pane read-only filesystem navigator."""

    TITLE = "tickytickertextual"
    SUB_TITLE = "read-only · on-demand"

    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
    }

    Header {
        height: 1;
        background: #161b22;
        color: #d8dee9;
    }

    #path-bar {
        height: 1;
        padding: 0 1;
        background: #1f2933;
        color: #8be9fd;
        text-style: bold;
    }

    #panes {
        height: 3fr;
    }

    .pane {
        height: 100%;
        border: tall #30363d;
        background: #0d1117;
        padding: 0 1;
    }

    .pane:focus {
        border: tall #58a6ff;
    }

    #parent-pane {
        width: 1fr;
    }

    #current-pane {
        width: 2fr;
        padding: 0;
    }

    #preview-pane {
        width: 2fr;
    }

    OptionList > .option-list--option-highlighted {
        background: #1f6feb;
        color: #ffffff;
        text-style: bold;
    }

    #selected-pane {
        height: 1fr;
        min-height: 5;
        padding: 0;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #8b949e;
    }

    Footer {
        height: 1;
        background: #21262d;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "select_dataset", "Select .d"),
        Binding("x", "remove_selected", "Remove"),
        Binding("shift+h", "show_help", "Help"),
        Binding("l", "open_selected", "Open"),
        Binding("right", "open_selected", "Open", show=False),
        Binding("h", "go_parent", "Parent"),
        Binding("left", "go_parent", "Parent", show=False),
        Binding("backspace", "go_parent", "Parent", show=False),
        Binding("g", "first", "First", show=False),
        Binding("shift+g", "last", "Last", show=False),
        Binding(".", "toggle_hidden", "Hidden"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, root: str | os.PathLike[str], *, show_hidden: bool = False):
        super().__init__()
        self.navigator = FileSystemNavigator(root, show_hidden=show_hidden)
        self.entries: tuple[FileEntry, ...] = ()
        self.selected_paths: list[Path] = []
        self.chosen_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="path-bar")
        with Horizontal(id="panes"):
            yield Static(id="parent-pane", classes="pane")
            yield OptionList(id="current-pane", classes="pane", markup=False, compact=True)
            yield Static(id="preview-pane", classes="pane")
        yield SelectedOptionList(id="selected-pane", classes="pane", markup=False, compact=True)
        yield Static(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#parent-pane").border_title = "parent"
        self.query_one("#current-pane").border_title = "current"
        self.query_one("#preview-pane").border_title = "selection"
        self.query_one("#selected-pane").border_title = ":selected:"
        self._open_directory(self.navigator.root)
        self.query_one("#current-pane", OptionList).focus()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "current-pane":
            self._update_selection(event.option_index)
        elif event.option_list.id == "selected-pane":
            self._update_selected_status(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "current-pane":
            return
        entry = self._selected_entry()
        if (
            entry is not None
            and self._is_dataset(entry)
            and entry.path in self.selected_paths
        ):
            self._focus_selected(entry.path)
            return
        self.action_open_selected()

    def on_selected_option_list_remove_requested(
        self, event: SelectedOptionList.RemoveRequested
    ) -> None:
        self._remove_selected_at(event.index)

    def _focused_option_list(self) -> OptionList:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            return selected
        return self.query_one("#current-pane", OptionList)

    def action_cursor_down(self) -> None:
        self._focused_option_list().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._focused_option_list().action_cursor_up()

    def action_first(self) -> None:
        self._focused_option_list().action_first()

    def action_last(self) -> None:
        self._focused_option_list().action_last()

    def action_open_selected(self) -> None:
        if self.query_one("#selected-pane", SelectedOptionList).has_focus:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        if entry.is_symlink:
            self.notify("Symlinks are displayed but not followed", severity="warning")
            return
        if not entry.is_dir:
            self.notify("Read-only navigator: files are not opened")
            return
        self._open_directory(entry.path)

    def action_go_parent(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            self.query_one("#current-pane", OptionList).focus()
            self._update_selection(
                self.query_one("#current-pane", OptionList).highlighted
            )
            return
        if self.navigator.current == self.navigator.root:
            self.notify("Already at the configured root")
            return
        previous_name = self.navigator.current.name
        self._open_directory(self.navigator.parent(), highlight_name=previous_name)

    def action_select_dataset(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            self._choose_selected_path()
            return

        entry = self._selected_entry()
        if entry is None or not self._is_dataset(entry):
            self.notify("Space selects .d directories only", severity="warning")
            return
        path = entry.path
        if path not in self.selected_paths:
            self.selected_paths.append(path)
            self._refresh_selected_pane(highlighted=len(self.selected_paths) - 1)
            self.notify(f"Selected {path}")
        else:
            self.notify(f"Already selected {path}")

    def action_remove_selected(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if not selected.has_focus or selected.highlighted is None:
            return
        self._remove_selected_at(selected.highlighted)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())


    def action_reload(self) -> None:
        selected = self._selected_entry()
        self._open_directory(
            self.navigator.current,
            highlight_name=selected.name if selected is not None else None,
        )

    def action_toggle_hidden(self) -> None:
        selected = self._selected_entry()
        self.navigator.show_hidden = not self.navigator.show_hidden
        self._open_directory(
            self.navigator.current,
            highlight_name=selected.name if selected is not None else None,
        )
        state = "shown" if self.navigator.show_hidden else "hidden"
        self.notify(f"Hidden entries are {state}")

    def _refresh_selected_pane(self, *, highlighted: int | None = None) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if highlighted is None:
            highlighted = selected.highlighted
        selected.set_options(
            [
                _selected_path_label(
                    path, index, chosen=path == self.chosen_path
                )
                for index, path in enumerate(self.selected_paths)
            ]
        )
        if not self.selected_paths:
            selected.highlighted = None
            self._update_selected_status(None)
            return
        selected.highlighted = min(highlighted or 0, len(self.selected_paths) - 1)
        selected.scroll_to_highlight()

    def _focus_selected(self, path: Path | None = None) -> None:
        if not self.selected_paths:
            self.notify("No .d folders selected", severity="warning")
            return
        selected = self.query_one("#selected-pane", SelectedOptionList)
        selected.focus()
        if path in self.selected_paths:
            selected.highlighted = self.selected_paths.index(path)
        elif selected.highlighted is None:
            selected.highlighted = 0
        selected.scroll_to_highlight()
        self._update_selected_status(selected.highlighted)

    def _remove_selected_at(self, index: int) -> None:
        if not 0 <= index < len(self.selected_paths):
            return
        removed = self.selected_paths.pop(index)
        if self.chosen_path == removed:
            self.chosen_path = None
        next_index = min(index, len(self.selected_paths) - 1) if self.selected_paths else None
        self._refresh_selected_pane(highlighted=next_index)
        self.query_one("#selected-pane", SelectedOptionList).focus()
        self.notify(f"Removed {removed}")

    def _choose_selected_path(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        index = selected.highlighted
        if index is None or not 0 <= index < len(self.selected_paths):
            return
        self.chosen_path = self.selected_paths[index]
        self._refresh_selected_pane(highlighted=index)
        selected.focus()
        self.notify(":HELA CHOSEN:")

    def _update_selected_status(self, index: int | None) -> None:
        status = self.query_one("#status-bar", Static)
        if index is None or not self.selected_paths:
            status.update("0 selected .d folders · Space adds datasets")
            return
        status.update(
            f"{index + 1}/{len(self.selected_paths)} selected .d folders · "
            "Space choose · x remove · h return"
        )

    @staticmethod
    def _is_dataset(entry: FileEntry) -> bool:
        return (
            entry.is_dir
            and not entry.is_symlink
            and entry.name.casefold().endswith(".d")
        )


    def _open_directory(self, path: Path, *, highlight_name: str | None = None) -> None:
        try:
            listing = self.navigator.change_directory(path)
        except NavigationError as error:
            self.notify(str(error), severity="error")
            return

        self.entries = listing.entries
        option_list = self.query_one("#current-pane", OptionList)
        option_list.set_options([_entry_label(entry) for entry in self.entries])

        highlighted = 0 if self.entries else None
        if highlight_name is not None:
            highlighted = next(
                (
                    index
                    for index, entry in enumerate(self.entries)
                    if entry.name == highlight_name
                ),
                highlighted,
            )
        option_list.highlighted = highlighted
        if highlighted is not None:
            option_list.scroll_to_highlight()

        self.query_one("#path-bar", Static).update(str(listing.path))
        self._update_parent_pane()
        self._update_selection(highlighted)

    def _update_parent_pane(self) -> None:
        content = self.query_one("#parent-pane", Static)
        if self.navigator.current == self.navigator.root:
            root_text = Text()
            root_text.append("configured root\n", style="bold cyan")
            root_text.append(str(self.navigator.root), style="dim")
            root_text.append("\n\nNavigation is confined to this tree.", style="dim")
            content.update(root_text)
            return

        try:
            listing = self.navigator.scan(self.navigator.current.parent, limit=500)
        except NavigationError as error:
            content.update(Text(str(error), style="red"))
            return
        content.update(
            _listing_text(
                listing.entries,
                selected_name=self.navigator.current.name,
                truncated=listing.truncated,
            )
        )

    def _update_selection(self, index: int | None) -> None:
        preview = self.query_one("#preview-pane", Static)
        status = self.query_one("#status-bar", Static)

        if index is None or not 0 <= index < len(self.entries):
            preview.update(Text("empty directory", style="dim italic"))
            status.update(f"{len(self.entries)} entries · read-only")
            return

        entry = self.entries[index]
        if entry.is_dir:
            try:
                listing = self.navigator.scan(entry.path, limit=500)
            except NavigationError as error:
                preview.update(Text(str(error), style="red"))
            else:
                if listing.entries:
                    preview.update(
                        _listing_text(listing.entries, truncated=listing.truncated)
                    )
                else:
                    preview.update(Text("empty directory", style="dim italic"))
        else:
            preview.update(self._metadata_text(entry))

        kind = "directory" if entry.is_dir else "symlink" if entry.is_symlink else "file"
        status.update(
            f"{index + 1}/{len(self.entries)} · {kind} · "
            f"{format_size(entry.size)} · read-only"
        )

    @staticmethod
    def _metadata_text(entry: FileEntry) -> Text:
        metadata = Text()
        metadata.append(entry.name, style="bold #f0f6fc")
        metadata.append("\n\n")
        metadata.append("type      ", style="dim")
        metadata.append("symbolic link" if entry.is_symlink else "file")
        metadata.append("\n")
        metadata.append("size      ", style="dim")
        metadata.append(format_size(entry.size))
        metadata.append("\n")
        metadata.append("modified  ", style="dim")
        if entry.modified is None:
            metadata.append("unknown")
        else:
            metadata.append(datetime.fromtimestamp(entry.modified).isoformat(sep=" ", timespec="seconds"))
        metadata.append("\n")
        metadata.append("mode      ", style="dim")
        metadata.append(stat.filemode(entry.mode) if entry.mode is not None else "unknown")
        if entry.is_symlink:
            metadata.append("\n")
            metadata.append("target    ", style="dim")
            try:
                metadata.append(os.readlink(entry.path))
            except OSError:
                metadata.append("unavailable", style="red")
        metadata.append("\n\nFile content previews are disabled.", style="dim italic")
        return metadata

    def _selected_entry(self) -> FileEntry | None:
        highlighted = self.query_one("#current-pane", OptionList).highlighted
        if highlighted is None or not 0 <= highlighted < len(self.entries):
            return None
        return self.entries[highlighted]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("working_directory", nargs="?", type=Path, help="filesystem root to expose (default: current directory)")
    parser.add_argument(
        "--show-hidden",
        action="store_true",
        help="show dotfiles and dot-directories initially",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    working_directory = args.working_directory or Path.cwd()
    try:
        app = FileViewerApp(working_directory, show_hidden=args.show_hidden)
    except NavigationError as error:
        raise SystemExit(str(error)) from error
    app.run()


if __name__ == "__main__":
    main()

