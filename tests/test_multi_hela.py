import asyncio
import csv
import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from aiohttp.test_utils import TestClient, TestServer
from textual.widgets import Button, OptionList, TabbedContent
from tickyticker import charge_regions
from tickytickertextual.app import (
    FileViewerApp, AlgorithmSettings, DatasetTicState, SettingsScreen,
    ChargeScanScreen, adapt_charge_scan_result, load_algorithm_settings,
    _read_dataset_metadata, volume_text, event_histogram_text, _dataset_metadata_text,
)
from tickytickertextual.plots import event_histogram_svg, combined_chromatograms_svg
from tickytickertextual.web import PlotServer
from tickytickertextual.workflow import TABLE_HEADER
from test_fileviewer import _fake_charge_result, _fake_tic_result, _write_sample_info


def make_dataset(root, name, description):
    path = root / name
    path.mkdir()
    _write_sample_info(path, description)
    return path


def test_automatic_membership_and_manual_override(tmp_path):
    hela = make_dataset(tmp_path, "a.d", "QC hELa")
    make_dataset(tmp_path, "b.d", "sample")

    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(180, 45)) as pilot:
            await pilot.press("space", "space", "ctrl+down", "g")
            assert app.hela_paths == {hela}
            assert app.chosen_path is None
            await pilot.press("space")
            assert not app.hela_paths
            app._refresh_selected_pane()
            assert not app.hela_paths
            await pilot.press("enter")
            assert not isinstance(app.screen, SettingsScreen)
            await pilot.press("space", "enter")
            assert isinstance(app.screen, SettingsScreen)
            assert app._pending_reference == hela
            assert not app.screen.query("#setting-border_mz_left")
            await pilot.press("escape")
            assert app._pending_reference is None
            assert app.hela_paths == {hela}
            await pilot.press("x")
            current = app.query_one("#current-pane", OptionList)
            current.focus()
            await pilot.pause()
            current.highlighted = 0
            app.action_select_dataset()
            assert hela in app.hela_paths

    asyncio.run(exercise())


def test_multi_hela_batch_incremental_and_zero_denominator(tmp_path, monkeypatch):
    paths = [make_dataset(tmp_path, f"h{i}.d", "HeLa") for i in range(4)]
    sample = make_dataset(tmp_path, "sample.d", "Sample")
    later = make_dataset(tmp_path, "znew.d", "Another HeLa")
    settings = AlgorithmSettings()
    fit = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    values = dict(zip(paths, [(20,10), (50,12), (40,13), (30,15)]))
    values[sample] = (70,25)
    values[later] = (60,25)
    calls = []

    def calculate(path, **kwargs):
        calls.append((path, kwargs["mz_min"], kwargs["mz_max"]))
        below, above = values[path]
        return _fake_tic_result(settings, below=below, above=above)

    monkeypatch.setattr(charge_regions, "analyse_line_tic", calculate)

    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(190,45)) as pilot:
            app.selected_paths = paths + [sample]
            app.hela_paths = set(paths)
            app.chosen_path = paths[0]
            app.accepted_fit = fit
            app._refresh_selected_pane()
            app._begin_tic_batch(fit)
            assert app._normalizers() == (None, None)
            before = list(app.selected_paths)
            app._remove_selected_at(0)
            assert app.selected_paths == before
            row = app.query_one("#selected-pane", OptionList).get_option_at_index(0).prompt
            assert row.spans[0].style.color.triplet == (119,119,119)
            for _ in range(100):
                await pilot.pause()
                if not app._tic_running:
                    break
            assert app._normalizers() == (35.0,12.5)
            rows = app._selected_export_rows()
            assert rows[-1][4:8] == (70,2.0,25,2.0)
            assert rows[0][5] == pytest.approx(20/35)
            assert rows[0][7] == .8
            assert app._results_ready()
            held = app.tic_states[sample].result
            current = app.query_one("#current-pane", OptionList)
            current.focus()
            current.highlighted = next(i for i,e in enumerate(app.entries) if e.path == later)
            app.action_select_dataset()
            assert later in app.hela_paths
            assert app.query_one("#tic-new", Button).display
            assert not app._results_ready()
            app._begin_tic_batch(fit, only_new=True)
            for _ in range(100):
                await pilot.pause()
                if not app._tic_running:
                    break
            assert app.tic_states[sample].result is held
            assert [p for p,_,_ in calls] == paths + [sample,later]
            assert all((low,high) == (350,1200) for _,low,high in calls)
            assert app._normalizers() == (40.0,15.0)
            assert not app.query_one("#tic-new", Button).display
            # Membership edits reuse absolute results.
            app.hela_paths = {paths[0]}
            app.tic_states[paths[0]] = replace(app.tic_states[paths[0]], tic_below_line=0, tic_above_line=0)
            assert app._selected_export_rows()[-2][5] == ""
            app.hela_paths.clear()
            assert app._normalizers() == (None,None)

    asyncio.run(exercise())


def test_repeat_rerun_cancel_preserves_results_and_downloads_no_files(tmp_path, monkeypatch):
    path = make_dataset(tmp_path, "hela.d", "HeLa")
    settings = AlgorithmSettings()
    fit = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    monkeypatch.setattr(charge_regions, "analyse", lambda *args, **kw: _fake_charge_result(settings))
    monkeypatch.setattr(charge_regions, "analyse_line_tic", lambda *args, **kw: _fake_tic_result(settings))
    delivered = []

    def deliver(self, stream, **kwargs):
        delivered.append((stream.read(), kwargs))
        stream.close()

    monkeypatch.setattr(FileViewerApp, "deliver_text", deliver)

    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(190,50)) as pilot:
            app.selected_paths = [path]
            app.hela_paths = {path}
            app.chosen_path = path
            app.accepted_fit = fit
            held = DatasetTicState(status="complete", tic_below_line=100, tic_above_line=200,
                                   result=_fake_tic_result(settings))
            app.tic_states[path] = held
            app._refresh_selected_pane()
            app._request_main_redo()
            await pilot.pause()
            await pilot.press("escape")
            assert app.accepted_fit is fit and app.tic_states[path] is held
            for _ in range(2):
                app._request_main_redo()
                await pilot.pause()
                await pilot.click("#settings-save")
                for _ in range(100):
                    await pilot.pause()
                    if not app._analysis_running:
                        break
                assert isinstance(app.screen, ChargeScanScreen)
                review = app.screen
                await pilot.press("right")
                assert review.query_one("#scan-tabs", TabbedContent).active == "scan-histogram"
                app.open_review_plot(review, download=True)
                assert delivered[-1][1]["save_filename"] == "event-histogram.svg"
                assert "Raw-event intensity histogram" in delivered[-1][0]
                await pilot.press("left")
                app.open_review_plot(review)
                assert delivered[-1][1]["mime_type"] == "text/html"
                await pilot.press("y")
                for _ in range(100):
                    await pilot.pause()
                    if not app._tic_running:
                        break
                assert app._results_ready()
            app._open_selected_tic_plot()
            assert "chromatograms.html" == delivered[-1][1]["save_filename"]
            assert not (tmp_path / "plots").exists()

    asyncio.run(exercise())


def test_xml_volume_and_range_migration(tmp_path):
    path = make_dataset(tmp_path, "sample.d", "HeLa")
    root = ET.Element("SampleTable")
    ET.SubElement(root, "Sample", Description="HeLa", Volume="0.5")
    ET.SubElement(root, "Property", Name="AutoSamplerVolumeUnit", Value="µl")
    ET.ElementTree(root).write(path / "SampleInfo.xml", encoding="utf-16")
    assert volume_text(_read_dataset_metadata(path)) == "0.5 µl"
    settings_file = tmp_path / "old.toml"
    settings_file.write_text('[charge_regions]\nmz_min=100\nmz_max=1700\nborder_mz_left=400\nborder_mz_right=1100\n')
    settings = load_algorithm_settings(settings_file)
    assert (settings.mz_min, settings.mz_max) == (400,1100)
    assert "border_mz" not in settings_file.read_text()
    assert settings.analysis_arguments()["border_mz_left"] == 400


@pytest.mark.parametrize("missing", ["Gradient", "Volume", "both"])
def test_missing_required_metadata_is_red_and_not_selectable(tmp_path, missing):
    path = tmp_path / "invalid.d"
    path.mkdir()
    _write_sample_info(path, "HeLa", gradient=missing not in {"Gradient", "both"})
    if missing in {"Volume", "both"}:
        xml = ET.parse(path / "SampleInfo.xml")
        xml.getroot().find(".//Sample").attrib.pop("Volume")
        xml.write(path / "SampleInfo.xml", encoding="utf-16")
    assert volume_text(None) == "..."
    metadata = _read_dataset_metadata(path)
    preview = _dataset_metadata_text(path, metadata)
    expected = 2 if missing == "both" else 1
    assert preview.plain.count("NA") == expected
    red_spans = [span for span in preview.spans
                 if preview.plain[span.start:span.end] == "NA"]
    assert len(red_spans) == expected
    assert all("#ff7b72" in str(span.style) for span in red_spans)

    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(180, 45)) as pilot:
            await pilot.press("space")
            assert app.selected_paths == []
            row = app.query_one("#current-pane", OptionList).get_option_at_index(0).prompt
            assert row.plain.count("NA") == expected
            assert sum("#ff7b72" in str(span.style) for span in row.spans) == expected

    asyncio.run(exercise())


def test_config_calculate_starts_without_confirmation(tmp_path, monkeypatch):
    path = make_dataset(tmp_path, "hela.d", "HeLa")
    calls = []

    def calculate(dataset, **kwargs):
        calls.append(dataset)
        return _fake_charge_result(AlgorithmSettings())

    monkeypatch.setattr(charge_regions, "analyse", calculate)

    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.press("space", "ctrl+down", "enter")
            assert isinstance(app.screen, SettingsScreen)
            assert str(app.screen.query_one("#settings-save", Button).label) == "Calculate"
            assert str(app.screen.query_one("#settings-cancel", Button).label) == "Reject"
            await pilot.click("#settings-cancel")
            assert not calls and app._pending_reference is None
            await pilot.press("enter")
            await pilot.click("#settings-save")
            for _ in range(100):
                await pilot.pause()
                if calls and not app._analysis_running:
                    break
            assert calls == [path]
            assert isinstance(app.screen, ChargeScanScreen)
            assert app.screen.result is not None
            assert not app.screen.query_one("#scan-question").display

    asyncio.run(exercise())


def test_scalable_plots_and_xml_escaping(tmp_path):
    settings = AlgorithmSettings()
    fit = adapt_charge_scan_result(_fake_charge_result(settings), settings)
    assert event_histogram_text(fit,120,45).plain.count("\n") > event_histogram_text(fit,120,20).plain.count("\n")
    ET.fromstring(event_histogram_svg(fit))
    svg = combined_chromatograms_svg([(Path("one.d"), "HeLa <&>", _fake_tic_result(settings)),
                                     (Path("two.d"), "Sample", _fake_tic_result(settings))])
    root = ET.fromstring(svg)
    assert root.get("height") == "2240"
    assert "one.d" in svg and "two.d" in svg and "HeLa &lt;&amp;&gt;" in svg


def test_memory_export_server_readiness_and_precision(tmp_path):
    async def exercise():
        server = PlotServer("true", bridge_token="test-secret")
        server.on_startup = lambda app: asyncio.sleep(0)
        client = TestClient(TestServer(await server._make_app()))
        async with client:
            assert (await client.get("/exports/selected.csv")).status == 404
            assert (await client.post("/_publish", json={})).status == 403
            rows = [("hela.d", "HéLa", "5m", "0.5 µl", 1234567890123456789, 1., 22, 1., "fit")]
            csv_text = FileViewerApp._delimited_text(TABLE_HEADER, rows, delimiter=",")
            tsv_text = FileViewerApp._delimited_text(TABLE_HEADER, rows, delimiter="\t")
            response = await client.post("/_publish", headers={"Authorization":"Bearer test-secret"},
                json={"ready": True, "exports":{"selected.csv":csv_text,"selected.tsv":tsv_text}})
            assert response.status == 200
            saved = await (await client.get("/exports/selected.csv")).text()
            copied = await (await client.get("/exports/selected.tsv")).text()
            assert list(csv.reader(io.StringIO(saved))) == list(csv.reader(io.StringIO(copied), delimiter="\t"))
            assert "1234567890123456789" in copied and "HéLa" in copied
            assert len(next(csv.reader(io.StringIO(saved)))) == 9
            await client.post("/_publish", headers={"Authorization":"Bearer test-secret"}, json={"ready":False})
            assert (await client.get("/exports/selected.csv")).status == 404
    asyncio.run(exercise())


def test_raw_tic_respects_range_and_shared_frame_selection(tmp_path, monkeypatch):
    class Raw:
        min_scan = max_scan = 0
        ms1_frames = np.array([1,2,3,4], dtype=np.uint32)
        def __init__(self, path): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def frame_to_retention_time(self, frames): return np.array([10.,20.,30.,40.])
        def scan_to_inv_ion_mobility(self, scans, frames): return np.array([1.])
        def mz_to_tof_frame_sorted(self, edges, frames): return np.arange(len(edges), dtype=np.uint32)+100
        def query_iter(self, frames, columns):
            assert list(frames) == [2,4]
            for frame in frames:
                yield {"scan":np.array([0,0,0,0],dtype=np.uint32),
                       "tof":np.array([99,100,101,112],dtype=np.uint32),
                       "intensity":np.array([1000,10,40,2000],dtype=np.uint32)}
    monkeypatch.setattr(charge_regions, "OpenTIMS", Raw)
    monkeypatch.setattr(charge_regions.opentimspy, "bruker_bridge_present", True)
    result = charge_regions.analyse_line_tic(tmp_path, intercept=2., slope=0., mz_min=350,mz_max=351,
                                            min_intensity=30,frame_stride=2,rt_min=20,rt_max=40,threads=1)
    assert list(result.frame_ids) == [2,4]
    assert list(result.raw_tic_per_frame) == [50,50]
    assert list(result.tic_below_line_per_frame) == [40,40]
    assert result.tic_above_line == 0
