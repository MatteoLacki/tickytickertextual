from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from tickyticker import charge_regions
from textual.widgets import Input, OptionList, Static

from tickytickertextual import app as app_module
from tickytickertextual import web
from tickytickertextual.app import (
    AlgorithmSettings,
    AnalysisErrorScreen,
    AnalysisPlot,
    ChargeScanScreen,
    FileSystemNavigator,
    FilterScreen,
    HelpScreen,
    FileViewerApp,
    InstanceAlreadyRunning,
    NavigationError,
    SettingsScreen,
    SingleInstanceLock,
    adapt_charge_scan_result,
    analysis_error_advice,
    dominant_charge_text,
    event_histogram_text,
    format_size,
    load_algorithm_settings,
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


def test_dot_d_preview_shows_cached_metadata_and_file_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "nested").mkdir()
    (ordinary / "note.txt").write_text("note")
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    (dataset / "inside").mkdir()
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.execute(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            ("Description", "HeLa quality-control sample"),
        )
    (dataset / "analysis.tdf_bin").write_bytes(bytes(2048))
    (dataset / "raw.bin").touch()

    reads = 0
    original_reader = app_module._read_dataset_description

    def counted_reader(path: Path) -> tuple[str | None, str | None]:
        nonlocal reads
        reads += 1
        return original_reader(path)

    monkeypatch.setattr(app_module, "_read_dataset_description", counted_reader)

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
            assert "HeLa quality-control sample" in rendered
            assert "analysis.tdf" in rendered
            assert format_size((dataset / "analysis.tdf").stat().st_size) in rendered
            assert "2.0 KiB" in rendered
            assert "inside" not in rendered
            assert "raw.bin" not in rendered
            assert reads == 1

            await pilot.press("space")
            selected = app.query_one("#selected-pane", OptionList)
            selected_row = str(selected.get_option_at_index(0).prompt)
            assert reads == 1
            assert "sample.d" in selected_row
            assert "HeLa quality-control sample" in selected_row
            assert str(tmp_path) not in selected_row
            assert "\n" not in selected_row

    asyncio.run(exercise())


def test_selected_nested_dataset_path_is_relative_to_root(tmp_path: Path) -> None:
    dataset = tmp_path / "e" / "f" / "g" / "folder.d"
    dataset.mkdir(parents=True)
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.execute(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            ("Description", "Nested sample"),
        )
    (dataset / "analysis.tdf_bin").touch()

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.press("enter", "enter", "enter", "space")
            selected = app.query_one("#selected-pane", OptionList)
            row = str(selected.get_option_at_index(0).prompt)
            assert "e/f/g/folder.d" in row
            assert "Nested sample" in row
            assert str(tmp_path) not in row
            assert not row.lstrip(" ×★").startswith("/")

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
            assert isinstance(app.screen, ChargeScanScreen)
            await pilot.press("n")
            await pilot.pause()

            await pilot.press("space")
            assert app.chosen_path is None
            await pilot.press("space")
            assert app.chosen_path == dataset_a.resolve()
            assert isinstance(app.screen, ChargeScanScreen)
            await pilot.press("n")
            await pilot.pause()

            await pilot.press("x")
            assert app.selected_paths == [dataset_b.resolve()]
            assert app.chosen_path is None
            assert str(current.get_option_at_index(1).prompt).startswith("▸ ")
            assert str(current.get_option_at_index(2).prompt).startswith("✓ ")

            await pilot.click("#selected-pane", offset=(2, 1))
            await pilot.pause()
            assert app.selected_paths == []

    asyncio.run(exercise())


def _fake_charge_result(
    settings: AlgorithmSettings | None = None,
) -> charge_regions.ChargeRegionResult:
    settings = settings or AlgorithmSettings()
    mz_bins = round(
        (np.ceil(settings.mz_max) - np.floor(settings.mz_min))
        / settings.mz_bin_width
    )
    intensities = np.zeros(
        (3, settings.mobility_bins, mz_bins), dtype=np.float64
    )
    intensities[0, 10:40, 10:50] = 20.0
    intensities[1, 35:70, 45:100] = 40.0
    intensities[2, 65:90, 95:140] = 60.0
    mobility_edges = np.linspace(0.6, 1.6, settings.mobility_bins + 1)
    mz_edges = np.linspace(
        np.floor(settings.mz_min), np.ceil(settings.mz_max), mz_bins + 1
    )
    histogram = np.arange(1, 129, dtype=np.uint64)
    charges = np.array([1, 2, 3], dtype=np.int64)
    line_data = {"line": {"intercept": 1.55, "slope": -0.0005}}
    return charge_regions.ChargeRegionResult(
        intensities=intensities,
        all_ms1_intensities=intensities.sum(axis=0),
        raw_event_intensity_histogram=histogram,
        one_charge_mask=np.zeros((settings.mobility_bins, mz_bins), dtype=bool),
        non_one_ms1_intensity=123.0,
        border_mz_mask=np.ones(mz_bins, dtype=bool),
        polar_origin=np.array([700.0, 1.0]),
        line_one=np.array([1.2, -0.0002]),
        line_two=np.array([1.5, -0.0004]),
        polar_boundary_radius=1.0,
        polar_boundary=np.array([[350.0, 1200.0], [1.4, 0.9]]),
        one_charge_is_inner=True,
        charges=charges,
        mz_edges=mz_edges,
        mobility_edges=mobility_edges,
        sampled_scans_per_mobility_bin=np.full(
            settings.mobility_bins, 4, dtype=np.uint32
        ),
        line_data=line_data,
        visited_ms1_frames=12,
        runtime_seconds=1.25,
        effective_threads=min(settings.threads, settings.mobility_bins),
    )


def test_native_plots_and_direct_analysis_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    settings = AlgorithmSettings()
    result = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    dominant = dominant_charge_text(result, 80, 14).plain
    histogram = event_histogram_text(result, 80, 14).plain
    assert "dominant charge" in dominant
    assert "X" in dominant
    assert "1" in dominant and "2" in dominant and "3" in dominant
    assert "raw MS1 events" in histogram
    assert "#" in histogram

    calls: list[tuple[Path, dict[str, int | float]]] = []

    def fake_analyse(
        dataset_path: Path,
        output_dir: Path | None = None,
        *,
        progress: object = None,
        **arguments: int | float,
    ) -> charge_regions.ChargeRegionResult:
        assert output_dir is None
        calls.append((Path(dataset_path), arguments))
        if callable(progress):
            progress("Processed 100 MS1 frames")
        return _fake_charge_result(settings)

    monkeypatch.setattr(charge_regions, "analyse", fake_analyse)

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("space", "ctrl+down", "space")
            assert app.chosen_path == dataset.resolve()
            assert isinstance(app.screen, ChargeScanScreen)

            await pilot.press("y")
            for _ in range(50):
                await pilot.pause()
                if not app._analysis_running:
                    break

            assert not app._analysis_running
            assert app.query_one("#analysis-pane").display
            assert app.query_one("#analysis-tabs").display
            plot = app.query_one("#analysis-dominant-plot", AnalysisPlot)
            assert plot.result is not None
            assert "dominant charge" in str(plot.render())
            data = str(app.query_one("#analysis-data", Static).render())
            assert "in memory; no analysis files written" in data
            assert "12" in data
            assert "min_intensity = 30.0" in data
            assert "intercept" in data

    asyncio.run(exercise())
    assert calls == [(dataset.resolve(), settings.analysis_arguments())]
    assert list(dataset.iterdir()) == []


def test_analysis_error_requires_acknowledgement_and_gives_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "weak-sample.d"
    dataset.mkdir()
    error_message = (
        "At least three uncensored dominant 1+ cells are required for polar "
        "fitting."
    )

    def failing_analyse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(error_message)

    monkeypatch.setattr(charge_regions, "analyse", failing_analyse)

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(120, 45)) as pilot:
            await pilot.press("space", "ctrl+down", "space", "y")
            for _ in range(50):
                await pilot.pause()
                if isinstance(app.screen, AnalysisErrorScreen):
                    break

            assert not app._analysis_running
            assert isinstance(app.screen, AnalysisErrorScreen)
            assert error_message in app.screen.error
            assert "Choose another .d dataset" in app.screen.advice
            assert "smaller MS1 frame stride" in app.screen.advice
            assert "RuntimeError" in app.screen.traceback_text
            assert error_message in app.screen.traceback_text

            await pilot.press("escape")
            assert isinstance(app.screen, AnalysisErrorScreen)
            await pilot.click("#analysis-error-ok")
            await pilot.pause()
            assert not isinstance(app.screen, AnalysisErrorScreen)
            assert app.query_one("#selected-pane", OptionList).has_focus

    asyncio.run(exercise())


def test_analysis_error_advice_covers_invalid_settings() -> None:
    advice = analysis_error_advice(ValueError("m/z limits are invalid"))
    assert "press s" in advice
    assert "review the algorithm settings" in advice


def test_settings_window_saves_validated_toml(tmp_path: Path) -> None:
    settings_path = tmp_path / ".server" / "settings.toml"

    async def exercise() -> None:
        app = FileViewerApp(tmp_path, settings_path=settings_path)
        async with app.run_test(size=(120, 45)) as pilot:
            assert settings_path.is_file()
            await pilot.press("s")
            assert isinstance(app.screen, SettingsScreen)
            app.screen.query_one("#setting-min_intensity", Input).value = "42.5"
            await pilot.click("#settings-save")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)
            assert app.algorithm_settings.min_intensity == 42.5
            assert load_algorithm_settings(settings_path).min_intensity == 42.5
            assert "[charge_regions]" in settings_path.read_text()

    asyncio.run(exercise())


def test_single_instance_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    with SingleInstanceLock(lock_path):
        with pytest.raises(InstanceAlreadyRunning):
            with SingleInstanceLock(lock_path):
                pass

    with SingleInstanceLock(lock_path):
        assert lock_path.read_text() == str(os.getpid())
