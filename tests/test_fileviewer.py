from __future__ import annotations

import asyncio
import os
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
from tickyticker import charge_regions
from textual.widgets import Input, OptionList, Static, TabbedContent

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
    HISTOGRAM_THRESHOLD_STYLE,
    FileViewerApp,
    InstanceAlreadyRunning,
    NavigationError,
    SettingsScreen,
    SingleInstanceLock,
    adapt_charge_scan_result,
    analysis_error_advice,
    dominant_charge_svg,
    dominant_charge_text,
    event_histogram_text,
    format_duration,
    format_size,
    load_algorithm_settings,
    read_dataset_description,
    write_dominant_charge_svg,
)


def _write_sample_info(dataset: Path, description: str) -> None:
    root = ET.Element("SampleTable")
    ET.SubElement(root, "Sample", Description=description)
    ET.ElementTree(root).write(
        dataset / "SampleInfo.xml",
        encoding="utf-16",
        xml_declaration=True,
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


def test_reads_dataset_description_from_sample_info_xml(tmp_path: Path) -> None:
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    assert read_dataset_description(dataset) is None

    _write_sample_info(dataset, "2022-148-01 4P Mix")

    assert read_dataset_description(dataset) == "2022-148-01 4P Mix"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "unknown"), (0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB")],
)
def test_format_size(value: int | None, expected: str) -> None:
    assert format_size(value) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "unavailable"),
        (0.0, "0s"),
        (42.0, "42s"),
        (1498.964768, "24m59s"),
        (3661.0, "1h01m01s"),
    ],
)
def test_format_duration(seconds: float | None, expected: str) -> None:
    assert format_duration(seconds) == expected


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
    _write_sample_info(dataset, "HeLa quality-control sample")
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.executemany(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            (
                ("MzAcqRangeLower", "99.993561"),
                ("MzAcqRangeUpper", "1700.000000"),
            ),
        )
        connection.execute("CREATE TABLE Frames (Time REAL NOT NULL)")
        connection.executemany(
            "INSERT INTO Frames (Time) VALUES (?)",
            ((0.0,), (1200.0,), (3661.4,)),
        )
    (dataset / "analysis.tdf_bin").write_bytes(bytes(2048))
    (dataset / "raw.bin").touch()

    reads = 0
    original_reader = app_module._read_dataset_metadata

    def counted_reader(path: Path) -> app_module.DatasetMetadata:
        nonlocal reads
        reads += 1
        return original_reader(path)

    monkeypatch.setattr(app_module, "_read_dataset_metadata", counted_reader)

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
            current_row = str(
                app.query_one("#current-pane", OptionList)
                .get_option_at_index(1)
                .prompt
            )
            ordinary_row = str(
                app.query_one("#current-pane", OptionList)
                .get_option_at_index(0)
                .prompt
            )
            current_header = str(
                app.query_one("#current-header", Static).render()
            )
            assert current_row.index("│") == ordinary_row.index("│")
            assert current_row.index("│") == current_header.index("│")
            assert "Path" in current_header
            assert "Gradient" in current_header
            assert "acq. m/z" not in current_row
            assert "Gradient" not in current_row
            assert "1h01m01s" in current_row
            assert current_row.endswith("s ")
            assert (
                current_header.index("Gradient") + len("Gradient")
                == current_row.index("1h01m01s") + len("1h01m01s")
            )
            assert "HeLa quality-control sample" in rendered
            assert "Acquisition m/z" not in rendered
            assert "Gradient length" in rendered
            assert "1h01m01s" in rendered
            assert "analysis.tdf" in rendered
            assert format_size((dataset / "analysis.tdf").stat().st_size) in rendered
            assert "2.0 KiB" in rendered
            assert "inside" not in rendered
            assert "raw.bin" not in rendered
            assert reads == 1

            await pilot.press("space")
            selected = app.query_one("#selected-pane", OptionList)
            selected_row = str(selected.get_option_at_index(0).prompt)
            selected_header = str(
                app.query_one("#selected-header", Static).render()
            )
            assert reads == 1
            assert "sample.d" in selected_row
            assert "HeLa quality-control sample" in selected_row
            assert "Acq. m/z" not in selected_row
            assert "99.9936" not in selected_row
            assert all(
                heading in selected_header
                for heading in (
                    "Path",
                    "Description",
                    "Below",
                    "Above",
                    "Fit Parameters",
                )
            )
            assert "Below" not in selected_row
            assert "Above" not in selected_row
            assert selected_row.index("sample.d") < selected_row.index(
                "HeLa quality-control sample"
            )
            assert str(tmp_path) not in selected_row
            assert "\n" not in selected_row

    asyncio.run(exercise())


def test_selected_nested_dataset_path_is_relative_to_root(tmp_path: Path) -> None:
    dataset = tmp_path / "e" / "f" / "g" / "folder.d"
    dataset.mkdir(parents=True)
    _write_sample_info(dataset, "Nested sample")
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
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
        _write_sample_info(dataset, description)
        with sqlite3.connect(dataset / "analysis.tdf") as connection:
            connection.execute(
                "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
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
                "SampleInfo.xml Sample.Description is empty"
            )
            assert "unavailable (SampleInfo.xml" in str(
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
            assert app.chosen_path is None
            assert app.selected_paths == [dataset_a.resolve(), dataset_b.resolve()]

            await pilot.press("space")
            assert app.chosen_path == dataset_a.resolve()
            assert isinstance(app.screen, ChargeScanScreen)
            await pilot.press("n")
            await pilot.pause()
            assert app.chosen_path is None

            await pilot.press("x")
            assert app.selected_paths == [dataset_b.resolve()]
            assert app.chosen_path is None
            assert str(current.get_option_at_index(1).prompt).startswith("▸ ")
            assert str(current.get_option_at_index(2).prompt).startswith("✓ ")

            await pilot.click("#selected-pane", offset=(2, 0))
            await pilot.pause()
            assert app.selected_paths == []

    asyncio.run(exercise())


def _fake_charge_result(
    settings: AlgorithmSettings | None = None,
    *,
    mz_min: float = 100.0,
    mz_max: float = 1700.0,
) -> charge_regions.ChargeRegionResult:
    settings = settings or AlgorithmSettings()
    mz_bins = round(
        (np.ceil(mz_max) - np.floor(mz_min))
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
        np.floor(mz_min), np.ceil(mz_max), mz_bins + 1
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


def _fake_tic_result(
    settings: AlgorithmSettings,
    *,
    below: int = 100,
    above: int = 200,
) -> charge_regions.LineTicResult:
    frame_ids = np.array([1, 3, 5], dtype=np.uint32)
    below_per_frame = np.array(
        [below // 4, below // 4, below - 2 * (below // 4)],
        dtype=np.uint64,
    )
    above_per_frame = np.array(
        [above // 4, above // 4, above - 2 * (above // 4)],
        dtype=np.uint64,
    )
    return charge_regions.LineTicResult(
        tic_below_line=below,
        tic_above_line=above,
        frame_ids=frame_ids,
        retention_times_seconds=np.array([60.0, 120.0, 180.0]),
        tic_below_line_per_frame=below_per_frame,
        tic_above_line_per_frame=above_per_frame,
        raw_tic_per_frame=below_per_frame + above_per_frame + 10,
        line_intercept=1.55,
        line_slope=-0.0005,
        mz_min=settings.mz_min,
        mz_max=settings.mz_max,
        rt_min=settings.rt_min,
        rt_max=settings.rt_max,
        min_intensity=settings.min_intensity,
        frame_stride=settings.frame_stride,
        visited_ms1_frames=len(frame_ids),
        runtime_seconds=0.25,
        effective_threads=settings.threads,
    )


def test_modal_review_svg_and_accepted_split_tic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    _write_sample_info(dataset, "HeLa test sample")
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.executemany(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            (
                ("MzAcqRangeLower", "99.993561"),
                ("MzAcqRangeUpper", "1700.000000"),
            ),
        )
    (dataset / "analysis.tdf_bin").touch()
    initial_files = sorted(path.name for path in dataset.iterdir())
    settings = AlgorithmSettings()
    result = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    dominant = dominant_charge_text(result, 80, 14).plain
    histogram_render = event_histogram_text(result, 80, 14)
    histogram = histogram_render.plain
    assert "dominant charge" in dominant
    assert "X" in dominant
    assert "1" in dominant and "2" in dominant and "3" in dominant
    assert "event count (log scale)" in histogram
    assert "raw intensity" in histogram
    assert "#" in histogram
    minimum_start = histogram.index("minimum=30")
    minimum_end = minimum_start + len("minimum=30")
    threshold_marker = histogram.rindex("*")
    assert any(
        span.start <= minimum_start
        and span.end >= minimum_end
        and span.style == HISTOGRAM_THRESHOLD_STYLE
        for span in histogram_render.spans
    )
    assert any(
        span.start <= threshold_marker < span.end
        and span.style == HISTOGRAM_THRESHOLD_STYLE
        for span in histogram_render.spans
    )
    svg = dominant_charge_svg(result)
    assert svg.startswith("<?xml")
    assert "Dominant charge and fitted 1+/multicharge separator" in svg
    assert 'stroke="#ffffff"' in svg
    standalone_svg = write_dominant_charge_svg(result, tmp_path / ".standalone")
    assert standalone_svg.read_text() == svg

    calls: list[tuple[Path, dict[str, int | float]]] = []
    tic_calls: list[tuple[Path, dict[str, object]]] = []

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

    def fake_line_tic(
        dataset_path: Path,
        **arguments: object,
    ) -> charge_regions.LineTicResult:
        progress = arguments.pop("progress", None)
        tic_calls.append((Path(dataset_path), dict(arguments)))
        if callable(progress):
            progress("TIC analysis complete")
        return _fake_tic_result(settings)

    monkeypatch.setattr(charge_regions, "analyse", fake_analyse)
    monkeypatch.setattr(charge_regions, "analyse_line_tic", fake_line_tic)

    async def exercise() -> None:
        plot_directory = tmp_path / "plots"
        app = FileViewerApp(
            tmp_path,
            plot_directory=plot_directory,
            plot_base_url="http://example.test/plots",
        )
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
            assert isinstance(app.screen, ChargeScanScreen)
            review = app.screen
            assert review.state == "review"
            assert review.query_one("#scan-tabs").display
            plot = review.query_one("#scan-dominant-plot", AnalysisPlot)
            assert plot.result is not None
            held_result = plot.result
            original_render_key = plot._last_render_key
            original_plot = str(plot.render())
            assert "dominant charge" in original_plot
            await pilot.resize_terminal(90, 36)
            await pilot.pause()
            assert plot.result is held_result
            assert plot._last_render_key != original_render_key
            assert str(plot.render()) != original_plot
            compact_histogram = review.query_one(
                "#scan-histogram-plot", AnalysisPlot
            )
            tabs = review.query_one("#scan-tabs", TabbedContent)
            tabs.active = "scan-histogram"
            await pilot.pause()
            assert "event count (log scale)" in str(compact_histogram.render())
            fit = str(review.query_one("#scan-fit-parameters", Static).render())
            assert "intercept" in fit
            assert "slope" in fit
            assert "runtime" not in fit
            assert review.svg_url is not None
            assert review.svg_url.startswith(
                "http://example.test/plots/dominant-charge-"
            )
            assert len(list(plot_directory.glob("dominant-charge-*.svg"))) == 1

            await pilot.press("y")
            await pilot.pause()
            for _ in range(100):
                await pilot.pause()
                if not app._tic_running and not isinstance(
                    app.screen, ChargeScanScreen
                ):
                    break

            assert not app._tic_running
            assert not isinstance(app.screen, ChargeScanScreen)
            assert app.accepted_fit is not None
            selected_row = str(
                app.query_one("#selected-pane", OptionList)
                .get_option_at_index(0)
                .prompt
            )
            assert "100 (100.0%)" in selected_row
            assert "200 (100.0%)" in selected_row
            assert "a=1.55" in selected_row
            assert "b=-0.0005" in selected_row
            assert "Below" not in selected_row
            assert "Above" not in selected_row

    asyncio.run(exercise())
    assert calls == [(dataset.resolve(), settings.analysis_arguments())]
    assert tic_calls == [
        (
            dataset.resolve(),
            {
                "intercept": 1.55,
                "slope": -0.0005,
                "mz_min": settings.mz_min,
                "mz_max": settings.mz_max,
                "min_intensity": settings.min_intensity,
                "threads": settings.threads,
                "frame_stride": settings.frame_stride,
                "rt_min": settings.rt_min,
                "rt_max": settings.rt_max,
            },
        )
    ]
    assert sorted(path.name for path in dataset.iterdir()) == initial_files


def test_split_tic_continues_after_dataset_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = tuple(
        (tmp_path / name).resolve()
        for name in ("hela.d", "sample.d", "broken.d")
    )
    for path in paths:
        path.mkdir()
    settings = AlgorithmSettings()
    fit = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    calls: list[Path] = []

    def fake_line_tic(
        dataset_path: Path,
        **arguments: object,
    ) -> charge_regions.LineTicResult:
        path = Path(dataset_path)
        calls.append(path)
        if path == paths[2]:
            raise RuntimeError("cannot read raw peaks")
        if path == paths[1]:
            return _fake_tic_result(settings, below=50, above=400)
        return _fake_tic_result(settings, below=100, above=200)

    monkeypatch.setattr(charge_regions, "analyse_line_tic", fake_line_tic)

    async def exercise() -> None:
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(160, 45)) as pilot:
            selected = app.query_one("#selected-pane", OptionList)
            app.selected_paths = list(paths)
            for path in paths:
                metadata = app._dataset_metadata(path)
                app.selected_descriptions[path] = metadata.description
                app.selected_description_errors[path] = metadata.description_error
            app.chosen_path = paths[0]
            app.accepted_fit = fit
            app._refresh_selected_pane()
            app._begin_tic_batch(fit)

            for _ in range(200):
                await pilot.pause()
                if not app._tic_running and isinstance(
                    app.screen, AnalysisErrorScreen
                ):
                    break

            assert not app._tic_running
            assert calls == list(paths)
            assert app.tic_states[paths[0]].status == "complete"
            assert app.tic_states[paths[1]].status == "complete"
            assert app.tic_states[paths[2]].status == "error"
            hela_row = str(selected.get_option_at_index(0).prompt)
            sample_row = str(selected.get_option_at_index(1).prompt)
            broken_row = str(selected.get_option_at_index(2).prompt)
            assert "100 (100.0%)" in hela_row
            assert "200 (100.0%)" in hela_row
            assert "50 (50.0%)" in sample_row
            assert "400 (200.0%)" in sample_row
            assert broken_row.count("ERROR") == 2
            assert "a=1.55" in hela_row
            assert "a=1.55" not in sample_row
            assert "a=1.55" not in broken_row
            separators = [
                [index for index, character in enumerate(row) if character == "│"]
                for row in (hela_row, sample_row, broken_row)
            ]
            assert separators[0] == separators[1] == separators[2]
            assert isinstance(app.screen, AnalysisErrorScreen)
            assert app.screen.error_title == "SELECTED-DATASET TIC FAILED"
            assert "Failed rows remain marked ERROR" in app.screen.advice

            await pilot.click("#analysis-error-ok")
            await pilot.pause()
            assert selected.has_focus

    asyncio.run(exercise())


def test_analysis_error_requires_acknowledgement_and_gives_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "weak-sample.d"
    dataset.mkdir()
    _write_sample_info(dataset, "Weak HeLa sample")
    with sqlite3.connect(dataset / "analysis.tdf") as connection:
        connection.execute(
            "CREATE TABLE GlobalMetadata (Key TEXT PRIMARY KEY, Value TEXT)"
        )
        connection.executemany(
            "INSERT INTO GlobalMetadata (Key, Value) VALUES (?, ?)",
            (
                ("MzAcqRangeLower", "100"),
                ("MzAcqRangeUpper", "1700"),
            ),
        )
    (dataset / "analysis.tdf_bin").touch()
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
            assert app.chosen_path is None
            assert app.selected_paths == [dataset.resolve()]
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
            assert len(app.screen.query("#setting-mz_min")) == 1
            assert len(app.screen.query("#setting-mz_max")) == 1
            app.screen.query_one("#setting-mz_min", Input).value = "150"
            app.screen.query_one("#setting-mz_max", Input).value = "1600"
            app.screen.query_one("#setting-min_intensity", Input).value = "42.5"
            await pilot.click("#settings-save")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)
            assert app.algorithm_settings.mz_min == 150.0
            assert app.algorithm_settings.mz_max == 1600.0
            assert app.algorithm_settings.min_intensity == 42.5
            loaded = load_algorithm_settings(settings_path)
            assert loaded.mz_min == 150.0
            assert loaded.mz_max == 1600.0
            assert loaded.min_intensity == 42.5
            assert "[charge_regions]" in settings_path.read_text()
            assert "mz_min = 150.0" in settings_path.read_text()
            assert "mz_max = 1600.0" in settings_path.read_text()

    asyncio.run(exercise())


def test_loading_legacy_settings_restores_missing_comparison_bounds(
    tmp_path: Path,
) -> None:
    settings_path = tmp_path / "settings.toml"
    settings_path.write_text(
        """[charge_regions]
min_intensity = 42.0
frame_stride = 5
"""
    )

    settings = load_algorithm_settings(settings_path)

    assert settings.min_intensity == 42.0
    assert settings.frame_stride == 5
    assert settings.mz_min == 100.0
    assert settings.mz_max == 1700.0
    migrated = settings_path.read_text()
    assert "mz_min = 100.0" in migrated
    assert "mz_max = 1700.0" in migrated
    assert "min_intensity = 42.0" in migrated
    assert "frame_stride = 5" in migrated


def test_browser_exports_are_written_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = tmp_path / "sample.d"
    dataset.mkdir()
    _write_sample_info(dataset, "HéLa μ-sample")
    export_directory = tmp_path / "exports"
    real_fdopen = os.fdopen
    export_encodings: list[str | None] = []

    def checked_fdopen(
        descriptor: int, mode: str, **kwargs: object
    ) -> object:
        export_encodings.append(kwargs.get("encoding"))
        return real_fdopen(descriptor, mode, **kwargs)

    monkeypatch.setattr(app_module.os, "fdopen", checked_fdopen)

    async def exercise() -> None:
        app = FileViewerApp(tmp_path, export_directory=export_directory)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("space")
            await pilot.pause()

    asyncio.run(exercise())
    selected = (export_directory / "selected.tsv").read_bytes().decode("utf-8")
    assert "HéLa μ-sample" in selected
    assert "—" in selected
    assert export_encodings
    assert set(export_encodings) == {"utf-8"}


def test_single_instance_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    with SingleInstanceLock(lock_path):
        with pytest.raises(InstanceAlreadyRunning):
            with SingleInstanceLock(lock_path):
                pass

    with SingleInstanceLock(lock_path):
        assert lock_path.read_text() == str(os.getpid())
