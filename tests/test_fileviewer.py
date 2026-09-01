from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from textual.widgets import OptionList

from tickytickertextual.app import (
    FileSystemNavigator,
    FileViewerApp,
    NavigationError,
    format_size,
)


def test_scan_is_on_demand_sorted_and_hides_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "z-file.txt").write_text("z")
    (tmp_path / "a-file.txt").write_text("a")
    (tmp_path / "middle").mkdir()
    (tmp_path / ".secret").write_text("hidden")

    navigator = FileSystemNavigator(tmp_path)
    listing = navigator.scan(tmp_path)

    assert [entry.name for entry in listing.entries] == [
        "middle",
        "a-file.txt",
        "z-file.txt",
    ]

    navigator.show_hidden = True
    assert ".secret" in {entry.name for entry in navigator.scan(tmp_path).entries}


def test_navigation_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    navigator = FileSystemNavigator(root)
    with pytest.raises(NavigationError, match="outside configured root"):
        navigator.scan(outside)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks unavailable")
def test_symlinked_directory_is_listed_but_not_enterable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "external").symlink_to(outside, target_is_directory=True)

    navigator = FileSystemNavigator(root)
    entry = navigator.scan(root).entries[0]

    assert entry.is_symlink
    assert not entry.is_dir
    with pytest.raises(NavigationError, match="outside configured root"):
        navigator.change_directory(entry.path)


def test_preview_scan_can_be_bounded(tmp_path: Path) -> None:
    for index in range(5):
        (tmp_path / f"file-{index}").touch()

    listing = FileSystemNavigator(tmp_path).scan(tmp_path, limit=3)

    assert len(listing.entries) == 3
    assert listing.truncated


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "unknown"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB")],
)
def test_format_size(value: int | None, expected: str) -> None:
    assert format_size(value) == expected


def test_app_moves_into_directory_and_back(tmp_path: Path) -> None:
    child = tmp_path / "alpha"
    child.mkdir()
    (child / "result.txt").write_text("result")
    (tmp_path / "zeta.txt").write_text("zeta")

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            option_list = app.query_one("#current-pane", OptionList)
            assert option_list.highlighted == 0
            assert app.entries[0].name == "alpha"
            await pilot.click("#parent-pane")
            assert app.focused is option_list
            await pilot.click("#preview-pane")
            assert app.focused is option_list

            await pilot.press("l")
            await pilot.pause()
            assert app.navigator.current == child
            assert [entry.name for entry in app.entries] == ["result.txt"]

            await pilot.press("h")
            await pilot.pause()
            assert app.navigator.current == tmp_path
            assert app.entries[option_list.highlighted or 0].name == "alpha"

    asyncio.run(exercise())

