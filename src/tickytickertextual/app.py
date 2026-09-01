"""Read-only, ranger-style filesystem navigation with Textual."""

from __future__ import annotations

import argparse
import fnmatch
import os
import stat
import sqlite3
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
from textual.widgets import Button, Footer, Header, Input, OptionList, Static


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
        directories_only: bool = False,
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
                    entry = self._make_entry(raw_entry)
                    if directories_only and not entry.is_dir:
                        continue
                    if limit is not None and len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(entry)
        except OSError as error:
            raise NavigationError(f"Cannot read {directory}: {error}") from error

        entries.sort(
            key=lambda entry: (
                (
                    0
                    if entry.is_dir and not entry.name.casefold().endswith(".d")
                    else 1
                    if entry.is_dir
                    else 2
                ),
                entry.name.casefold(),
                entry.name,
            )
        )
        return DirectoryListing(directory, tuple(entries), truncated)

    def change_directory(
        self,
        path: str | os.PathLike[str],
        *,
        directories_only: bool = False,
    ) -> DirectoryListing:
        """Validate and enter a directory, returning its fresh listing."""
        listing = self.scan(path, directories_only=directories_only)
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


def _read_dataset_description(
    dataset_path: Path,
) -> tuple[str | None, str | None]:
    """Return a Description and a diagnostic without modifying analysis.tdf."""
    database = dataset_path / "analysis.tdf"
    if not database.is_file():
        return None, "analysis.tdf not found"
    try:
        uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                "SELECT \"Value\" FROM \"GlobalMetadata\" "
                "WHERE \"Key\" = ? LIMIT 1",
                ("Description",),
            ).fetchone()
    except OSError as error:
        return None, f"filesystem error: {error}"
    except sqlite3.Error as error:
        return None, f"SQLite error: {error}"
    if row is None or row[0] is None:
        return None, "GlobalMetadata.Description not found"
    description = str(row[0]).strip()
    if not description:
        return None, "GlobalMetadata.Description is empty"
    return description, None


def read_dataset_description(dataset_path: Path) -> str | None:
    """Read a dataset Description from analysis.tdf without modifying it."""
    description, _ = _read_dataset_description(dataset_path)
    return description


def _matches_name_filter(name: str, pattern: str | None) -> bool:
    """Match a name against a case-insensitive shell-style glob."""
    if not pattern:
        return True
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


def _entry_label(entry: FileEntry, *, marked: bool = False) -> Text:
    label = Text(no_wrap=True, overflow="ellipsis")
    if entry.is_dir:
        label.append(
            "✓ " if marked else "▸ ",
            style="bold green" if marked else "bold cyan",
        )
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


class CurrentOptionList(OptionList):
    """Filesystem list with bindings advertised only while it has focus."""

    BINDINGS = [
        Binding("j,down", "app.cursor_down", "Down", key_display="j/↓"),
        Binding("k,up", "app.cursor_up", "Up", key_display="k/↑"),
        Binding("space", "app.select_dataset", "Select .d"),
        Binding("l,right,enter", "app.open_selected", "Open", key_display="l/→/Enter"),
        Binding("h,left,backspace", "app.go_parent", "Parent", key_display="h/←/⌫"),
        Binding("g", "app.first", "First"),
        Binding("G,shift+g", "app.last", "Last", key_display="G"),
        Binding(
            "ctrl+down",
            "app.focus_selected_pane",
            "Lower pane",
            key_display="Ctrl+↓",
        ),
        Binding(".", "app.toggle_hidden", "Hidden"),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "app.toggle_folders_only",
            "Folders only",
            key_display="Ctrl+.",
        ),
        Binding("/", "app.show_filter", "Filter"),
        Binding("r", "app.reload", "Reload"),
        Binding("H,shift+h", "app.show_help", "Help", key_display="Shift+H"),
    ]


class SelectedOptionList(OptionList):
    """Option list with a clickable remove control on each row."""

    BINDINGS = [
        Binding("j,down", "app.cursor_down", "Down", key_display="j/↓"),
        Binding("k,up", "app.cursor_up", "Up", key_display="k/↑"),
        Binding("space", "app.select_dataset", "Toggle HeLa"),
        Binding("x", "app.remove_selected", "Remove"),
        Binding("g", "app.first", "First"),
        Binding("G,shift+g", "app.last", "Last", key_display="G"),
        Binding(
            "h,ctrl+up",
            "app.focus_current_pane",
            "Upper pane",
            key_display="h/Ctrl+↑",
        ),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "app.toggle_folders_only",
            "Folders only",
            key_display="Ctrl+.",
        ),
        Binding("H,shift+h", "app.show_help", "Help", key_display="Shift+H"),
    ]

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


def _selected_path_label(
    path: Path,
    index: int,
    *,
    chosen: bool,
    description: str | None,
    description_error: str | None,
) -> Text:
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
    label.append("\n      ")
    label.append("Description: ", style="dim italic")
    label.append(
        description or f"unavailable ({description_error or 'unknown error'})",
        style="bold yellow" if chosen else "italic #8b949e",
    )
    if chosen:
        label.stylize("on #3d3200")
    return label


class FilterScreen(ModalScreen[str | None]):
    """Prompt for a non-recursive current-directory glob filter."""

    CSS = """
    FilterScreen {
        align: center middle;
        background: #000000 70%;
    }

    #filter-dialog {
        width: 72;
        max-width: 95%;
        height: 12;
        padding: 1 2;
        border: round #58a6ff;
        background: #161b22;
    }

    #filter-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #8be9fd;
    }

    #filter-input {
        margin: 1 0;
    }

    #filter-hint {
        height: 1;
        color: #8b949e;
    }

    #filter-buttons {
        height: 3;
        align-horizontal: right;
    }

    #filter-buttons Button {
        width: 12;
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel_filter", "Cancel", show=False)]

    def __init__(self, pattern: str | None) -> None:
        super().__init__()
        self.pattern = pattern or ""

    def compose(self) -> ComposeResult:
        with Container(id="filter-dialog"):
            yield Static("Filter current directory", id="filter-title")
            yield Input(
                value=self.pattern,
                placeholder="e.g. *_13214.d",
                id="filter-input",
            )
            yield Static("Shell glob; empty input clears the filter", id="filter-hint")
            with Horizontal(id="filter-buttons"):
                yield Button("Clear", id="filter-clear")
                yield Button("Cancel", id="filter-cancel")
                yield Button("Apply", id="filter-apply", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-apply":
            self.dismiss(self.query_one("#filter-input", Input).value.strip())
        elif event.button.id == "filter-clear":
            self.dismiss("")
        elif event.button.id == "filter-cancel":
            self.dismiss(None)

    def action_cancel_filter(self) -> None:
        self.dismiss(None)


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
        height: 32;
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
        Binding("H,shift+h", "close_help", "Close", show=False),
    ]

    HELP_TEXT = """[b]Filesystem[/b]
  j/k or arrows     move in the middle pane
  l/right/Enter     enter an ordinary directory
  h/left/Backspace  return to the parent
  g/G               jump to first/last entry
  .                 toggle hidden entries
  Ctrl+.            toggle folders-only mode (on initially)
  /                 filter current names with a shell glob
                    (empty input clears the filter)
  r                 reload

[b].d datasets[/b]
  Preview           shows all contents even in folders-only mode
  Space             add highlighted .d folder to :selected:, read its
                    analysis.tdf Description, then move down
  Ctrl+Down         move focus to :selected:
  Ctrl+Up           return focus to the filesystem pane
  Click             focus a row in :selected:
  j/k or arrows     move through selected paths
  g/G               jump to first/last selected path
  Space             toggle the current path as HeLa
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
        Binding("space", "select_dataset", "Select .d", show=False),
        Binding("x", "remove_selected", "Remove", show=False),
        Binding("H,shift+h", "show_help", "Help", show=False),
        Binding("ctrl+down", "focus_selected_pane", "Pane down", show=False),
        Binding("ctrl+up", "focus_current_pane", "Pane up", show=False),
        Binding("l", "open_selected", "Open", show=False),
        Binding("right", "open_selected", "Open", show=False),
        Binding("h", "go_parent", "Parent", show=False),
        Binding("left", "go_parent", "Parent", show=False),
        Binding("backspace", "go_parent", "Parent", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G,shift+g", "last", "Last", show=False),
        Binding(".", "toggle_hidden", "Hidden", show=False),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "toggle_folders_only",
            "Folders only",
            show=False,
        ),
        Binding("/", "show_filter", "Filter", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, root: str | os.PathLike[str], *, show_hidden: bool = False):
        super().__init__()
        self.navigator = FileSystemNavigator(root, show_hidden=show_hidden)
        self.entries: tuple[FileEntry, ...] = ()
        self.folders_only = True
        self.name_filter: str | None = None
        self.selected_paths: list[Path] = []
        self.selected_descriptions: dict[Path, str | None] = {}
        self.selected_description_errors: dict[Path, str | None] = {}
        self.chosen_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="path-bar")
        with Horizontal(id="panes"):
            yield Static(id="parent-pane", classes="pane")
            yield CurrentOptionList(id="current-pane", classes="pane", markup=False, compact=True)
            yield Static(id="preview-pane", classes="pane")
        yield SelectedOptionList(id="selected-pane", classes="pane", markup=False, compact=True)
        yield Static(id="status-bar")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.query_one("#parent-pane").border_title = "parent"
        self.query_one("#current-pane").border_title = "current"
        self.query_one("#preview-pane").border_title = "selection"
        self.query_one("#selected-pane").border_title = ":selected:"
        self._open_directory(self.navigator.root)
        current = self.query_one("#current-pane", CurrentOptionList)
        current.focus()
        self.call_after_refresh(current.focus)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "current-pane":
            self._update_selection(event.option_index)
        elif event.option_list.id == "selected-pane":
            self._update_selected_status(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "current-pane":
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
            self._toggle_chosen_path()
            return

        entry = self._selected_entry()
        if entry is None or not self._is_dataset(entry):
            self.notify("Space selects .d directories only", severity="warning")
            return
        path = entry.path
        if path not in self.selected_paths:
            description, description_error = _read_dataset_description(path)
            self.selected_descriptions[path] = description
            self.selected_description_errors[path] = description_error
            self.selected_paths.append(path)
            self._refresh_selected_pane(highlighted=len(self.selected_paths) - 1)
            self.notify(f"Selected {path}")
            if description_error:
                self.notify(
                    f"Description unavailable: {description_error}",
                    severity="warning",
                )
        else:
            self.notify(f"Already selected {path}")
        self._refresh_current_marks()
        current = self.query_one("#current-pane", CurrentOptionList)
        if (
            current.highlighted is not None
            and current.highlighted < current.option_count - 1
        ):
            current.action_cursor_down()

    def action_remove_selected(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if not selected.has_focus or selected.highlighted is None:
            return
        self._remove_selected_at(selected.highlighted)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_focus_selected_pane(self) -> None:
        self._focus_selected()

    def action_focus_current_pane(self) -> None:
        current = self.query_one("#current-pane", CurrentOptionList)
        current.focus()
        self._update_selection(current.highlighted)

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

    def action_toggle_folders_only(self) -> None:
        current_entry = self._selected_entry()
        selected_pane = self.query_one("#selected-pane", SelectedOptionList)
        selected_focused = selected_pane.has_focus
        self.folders_only = not self.folders_only
        self._open_directory(
            self.navigator.current,
            highlight_name=(
                current_entry.name if current_entry is not None else None
            ),
        )
        if selected_focused:
            selected_pane.focus()
            self._update_selected_status(selected_pane.highlighted)
        state = "on" if self.folders_only else "off"
        self.notify(f"Folders-only mode is {state}")

    def action_show_filter(self) -> None:
        self.push_screen(FilterScreen(self.name_filter), self._apply_name_filter)

    def _apply_name_filter(self, pattern: str | None) -> None:
        if pattern is None:
            return
        self.name_filter = pattern or None
        self.action_reload()
        if self.name_filter:
            self.notify(f"Name filter: {self.name_filter}")
        else:
            self.notify("Name filter cleared")

    def _refresh_selected_pane(self, *, highlighted: int | None = None) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if highlighted is None:
            highlighted = selected.highlighted
        selected.set_options(
            [
                _selected_path_label(
                    path,
                    index,
                    chosen=path == self.chosen_path,
                    description=self.selected_descriptions.get(path),
                    description_error=self.selected_description_errors.get(path),
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

    def _refresh_current_marks(self) -> None:
        current = self.query_one("#current-pane", CurrentOptionList)
        for index, entry in enumerate(self.entries):
            current.replace_option_prompt_at_index(
                index,
                _entry_label(entry, marked=entry.path in self.selected_paths),
            )

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
        self.selected_descriptions.pop(removed, None)
        self.selected_description_errors.pop(removed, None)
        if self.chosen_path == removed:
            self.chosen_path = None
        next_index = min(index, len(self.selected_paths) - 1) if self.selected_paths else None
        self._refresh_selected_pane(highlighted=next_index)
        self._refresh_current_marks()
        self.query_one("#selected-pane", SelectedOptionList).focus()
        self.notify(f"Removed {removed}")

    def _toggle_chosen_path(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        index = selected.highlighted
        if index is None or not 0 <= index < len(self.selected_paths):
            return
        path = self.selected_paths[index]
        if self.chosen_path == path:
            self.chosen_path = None
            message = ":HELA UNSELECTED:"
        else:
            self.chosen_path = path
            message = ":HELA CHOSEN:"
        self._refresh_selected_pane(highlighted=index)
        selected.focus()
        self.notify(message)

    def _update_selected_status(self, index: int | None) -> None:
        status = self.query_one("#status-bar", Static)
        if index is None or not self.selected_paths:
            status.update("0 selected .d folders")
            return
        status.update(
            f"{index + 1}/{len(self.selected_paths)} selected .d folders"
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
            listing = self.navigator.change_directory(
                path, directories_only=self.folders_only
            )
        except NavigationError as error:
            self.notify(str(error), severity="error")
            return

        self.entries = tuple(
            entry
            for entry in listing.entries
            if _matches_name_filter(entry.name, self.name_filter)
        )
        option_list = self.query_one("#current-pane", OptionList)
        option_list.set_options(
            [
                _entry_label(entry, marked=entry.path in self.selected_paths)
                for entry in self.entries
            ]
        )

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

        mode = "folders only" if self.folders_only else "folders + files"
        filter_status = f" · filter: {self.name_filter}" if self.name_filter else ""
        self.query_one("#path-bar", Static).update(
            f"{listing.path}  [{mode}{filter_status}]"
        )
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
            listing = self.navigator.scan(
                self.navigator.current.parent,
                limit=500,
                directories_only=self.folders_only,
            )
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
                listing = self.navigator.scan(
                    entry.path,
                    limit=500,
                    directories_only=(
                        self.folders_only and not self._is_dataset(entry)
                    ),
                )
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

