from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import Input, OptionList, Static

from tickytickertextual import web
from tickytickertextual.app import (
    FileSystemNavigator,
    FilterScreen,
    HelpScreen,
    FileViewerApp,
    NavigationError,
    format_size,
    read_dataset_description,
)


def test_scan_is_on_demand_sorted_and_hides_dotfiles(tmp_path: Path) -> None:
    (tmp_path / "z-file.txt").write_text("z")
    (tmp_path / "a-file.txt").write_text("a")
    (tmp_path / "middle").mkdir()
    (tmp_path / "alpha.d").mkdir()
    (tmp_path / ".secret").write_text("hidden")

    navigator = FileSystemNavigator(tmp_path)
    listing = navigator.scan(tmp_path)

    assert [entry.name for entry in listing.entries] == [
        "middle",
        "alpha.d",
        "a-file.txt",
        "z-file.txt",
    ]

    assert [
        entry.name
        for entry in navigator.scan(tmp_path, directories_only=True).entries
    ] == ["middle", "alpha.d"]

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


def test_reads_dataset_description_from_global_metadata(tmp_path: Path) -> None:
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    assert read_dataset_description(dataset) is None

    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.execute(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            ("Description", "2022-148-01 4P Mix"),
        )

    assert read_dataset_description(dataset) == "2022-148-01 4P Mix"


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
            assert app.focused is option_list
            assert option_list.highlighted == 0
            assert app.entries[0].name == "alpha"
            assert [entry.name for entry in app.entries] == ["alpha"]
            assert app.folders_only

            await pilot.press("ctrl+full_stop")
            await pilot.pause()
            assert not app.folders_only
            assert [entry.name for entry in app.entries] == ["alpha", "zeta.txt"]

            await pilot.press("ctrl+full_stop")
            await pilot.pause()
            assert app.folders_only
            assert [entry.name for entry in app.entries] == ["alpha"]

            await pilot.click("#parent-pane")
            assert app.focused is option_list

            await pilot.press("shift+h")
            assert isinstance(app.screen, HelpScreen)
            await pilot.click("#help-close")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            await pilot.click("#preview-pane")
            assert app.focused is option_list

            await pilot.press("l")
            await pilot.pause()
            assert app.navigator.current == child
            assert [entry.name for entry in app.entries] == []

            await pilot.press("ctrl+full_stop")
            await pilot.pause()
            assert [entry.name for entry in app.entries] == ["result.txt"]

            await pilot.press("h")
            await pilot.pause()
            assert app.navigator.current == tmp_path
            assert app.entries[option_list.highlighted or 0].name == "alpha"

    asyncio.run(exercise())


def test_folder_mode_preview_keeps_all_dot_d_contents(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "nested").mkdir()
    (ordinary / "note.txt").write_text("note")
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    (dataset / "inside").mkdir()
    (dataset / "analysis.tdf").touch()
    (dataset / "raw.bin").touch()

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            preview = app.query_one("#preview-pane", Static)
            assert [entry.name for entry in app.entries] == ["ordinary", "sample.d"]
            assert "nested" in str(preview.render())
            assert "note.txt" not in str(preview.render())

            await pilot.press("j")
            await pilot.pause()
            rendered = str(preview.render())
            assert "inside" in rendered
            assert "analysis.tdf" in rendered
            assert "raw.bin" in rendered

    asyncio.run(exercise())


def test_slash_glob_filter_can_apply_and_clear(tmp_path: Path) -> None:
    (tmp_path / "ordinary").mkdir()
    (tmp_path / "run_1.d").mkdir()
    matching = tmp_path / "sample_13214.d"
    matching.mkdir()

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("slash")
            assert isinstance(app.screen, FilterScreen)
            app.screen.query_one("#filter-input", Input).value = "*_13214.d"
            await pilot.press("enter")
            await pilot.pause()

            assert not isinstance(app.screen, FilterScreen)
            assert app.name_filter == "*_13214.d"
            assert [entry.path for entry in app.entries] == [matching.resolve()]
            assert "filter: *_13214.d" in str(
                app.query_one("#path-bar", Static).render()
            )

            await pilot.press("slash")
            app.screen.query_one("#filter-input", Input).value = ""
            await pilot.press("enter")
            await pilot.pause()
            assert app.name_filter is None
            assert [entry.name for entry in app.entries] == [
                "ordinary",
                "run_1.d",
                "sample_13214.d",
            ]

    asyncio.run(exercise())


def test_web_template_bridges_browser_ctrl_full_stop() -> None:
    template = (
        Path(web.__file__).with_name("templates") / "app_index.html"
    ).read_text()

    assert "event.ctrlKey" in template
    assert "#terminal .xterm-helper-textarea" in template
    assert "textarea.focus" in template
    assert "event.key === \".\"" in template
    assert "\\u001b[46;5u" in template


def test_dot_d_selection_focus_choose_and_remove(tmp_path: Path) -> None:
    dataset_a = tmp_path / "alpha.d"
    dataset_a.mkdir()
    dataset_b = tmp_path / "beta.d"
    dataset_b.mkdir()
    (tmp_path / "ordinary").mkdir()
    for dataset, description in (
        (dataset_a, "Alpha sample description"),
        (dataset_b, ""),
    ):
        with sqlite3.connect(dataset / "analysis.tdf") as connection:
            connection.execute(
                "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
            )
            connection.execute(
                "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
                ("Description", description),
            )

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(110, 36)) as pilot:
            current = app.query_one("#current-pane", OptionList)
            selected = app.query_one("#selected-pane", OptionList)

            def shown_descriptions() -> set[str]:
                return {
                    binding.description
                    for _, binding, _, _ in app.active_bindings.values()
                    if binding.show
                }

            assert {
                "Down",
                "Up",
                "Select .d",
                "Open",
                "Parent",
                "First",
                "Last",
                "Lower pane",
                "Hidden",
                "Folders only",
                "Filter",
                "Reload",
                "Help",
                "Quit",
            } <= shown_descriptions()
            assert "Toggle HeLa" not in shown_descriptions()
            assert "Remove" not in shown_descriptions()

            await pilot.press("j", "space")
            assert app.selected_paths == [dataset_a.resolve()]
            assert selected.option_count == 1
            assert app.focused is current
            assert current.highlighted == 2
            assert str(current.get_option_at_index(1).prompt).startswith("✓ ")
            assert app.selected_descriptions[dataset_a.resolve()] == (
                "Alpha sample description"
            )
            assert "Alpha sample description" in str(
                selected.get_option_at_index(0).prompt
            )

            await pilot.press("space")
            assert app.selected_paths == [dataset_a.resolve(), dataset_b.resolve()]
            assert selected.option_count == 2
            assert current.highlighted == 2
            assert str(current.get_option_at_index(2).prompt).startswith("✓ ")
            assert app.selected_description_errors[dataset_b.resolve()] == (
                "GlobalMetadata.Description is empty"
            )
            assert "GlobalMetadata.Description is empty" in str(
                selected.get_option_at_index(1).prompt
            )

            await pilot.press("ctrl+down")
            await pilot.pause()
            assert app.focused is selected
            assert selected.highlighted == 1
            assert {
                "Down",
                "Up",
                "Toggle HeLa",
                "Remove",
                "First",
                "Last",
                "Upper pane",
                "Folders only",
                "Help",
                "Quit",
            } <= shown_descriptions()
            assert "Select .d" not in shown_descriptions()
            assert "Open" not in shown_descriptions()

            await pilot.press("ctrl+full_stop")
            await pilot.pause()
            assert not app.folders_only
            assert app.focused is selected
            await pilot.press("ctrl+full_stop")
            await pilot.pause()
            assert app.folders_only
            assert app.focused is selected

            await pilot.press("H")
            assert isinstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)

            await pilot.press("ctrl+up")
            assert app.focused is current
            await pilot.press("ctrl+down", "k", "space")
            assert selected.highlighted == 0
            assert app.chosen_path == dataset_a.resolve()

            await pilot.press("space")
            assert app.chosen_path is None
            await pilot.press("space")
            assert app.chosen_path == dataset_a.resolve()

            await pilot.press("x")
            assert app.selected_paths == [dataset_b.resolve()]
            assert app.chosen_path is None
            assert str(current.get_option_at_index(1).prompt).startswith("▸ ")
            assert str(current.get_option_at_index(2).prompt).startswith("✓ ")

            await pilot.click("#selected-pane", offset=(2, 1))
            await pilot.pause()
            assert app.selected_paths == []

    asyncio.run(exercise())
