import asyncio
import csv
from dataclasses import replace
from datetime import date
import io

import pytest
from aiohttp.test_utils import TestClient, TestServer
from textual.widgets import Input

from tickytickertextual import app as a, web
from tickytickertextual.workflow import TABLE_HEADER
from test_multi_hela import make_dataset


def test_target_updates_amounts_without_reanalysis_or_writing_defaults(tmp_path):
    config = tmp_path / "defaults.toml"
    original = '[charge_regions]\n[injection]\ntarget_amount_ng = 200.0\n'
    config.write_text(original)
    refs = [make_dataset(tmp_path, f"h{i}.d", "HeLa") for i in range(3)]
    sample = make_dataset(tmp_path, "sample.d", "Sample")

    async def exercise():
        app = a.FileViewerApp(tmp_path, settings_path=config)
        async with app.run_test(size=(190, 50)) as pilot:
            app.selected_paths = refs + [sample]
            app.hela_paths = set(refs)
            for path, qq in zip(refs[:2] + [sample], [100, 200, 50]):
                app.tic_states[path] = a.DatasetTicState(status="complete", tic_below_line=qq, tic_above_line=999)
            app.tic_states[refs[2]] = a.DatasetTicState(status="error")
            app._refresh_selected_pane()
            assert app._selected_export_rows()[-1][-1] == "3.00"
            assert app._selected_export_rows()[2][-1] == ""
            held = dict(app.tic_states)
            target = app.query_one("#target-amount-ng", Input)
            target.value = "250"
            await pilot.pause()
            assert app._selected_export_rows()[-1][-1] == "3.75"
            assert app.tic_states == held and not app._tic_running and not app._analysis_running
            assert app.algorithm_settings.target_amount_ng == 250
            assert config.read_text() == original
            pm = app.query_one("#pm-qc-amount-ng", Input)
            pm.value = "200"
            await pilot.pause()
            assert app._selected_export_rows()[-1][-1] == "1.88"
            for invalid in ("", "0", "-1", "nan", "inf"):
                pm.value = invalid
                await pilot.pause()
                assert app._selected_export_rows()[-1][-1] == ""
            pm.value = "100"
            await pilot.pause()
            assert app._selected_export_rows()[-1][-1] == "3.75"
            assert "pm_qc_amount_ng" not in app.algorithm_settings.analysis_arguments()
            assert "target_amount_ng" not in app.algorithm_settings.analysis_arguments()
            exported = app._delimited_text(TABLE_HEADER, app._selected_export_rows(), delimiter=",")
            assert list(csv.reader(io.StringIO(exported)))[-1][-1] == "3.75"
            app.hela_paths = {refs[0]}
            assert app._selected_export_rows()[-1][-1] == "2.50"
            for invalid in ("", "0", "-1", "nan", "inf"):
                target.value = invalid
                await pilot.pause()
                assert app._selected_export_rows()[-1][-1] == ""
            target.value = "200"
            await pilot.pause()
            assert app._selected_export_rows()[-1][-1] == "2.00"
            app.tic_states[sample] = replace(app.tic_states[sample], tic_below_line=0)
            assert app._selected_export_rows()[-1][-1] == ""
            app.hela_paths.clear()
            assert all(row[-1] == "" for row in app._selected_export_rows())
        fresh = a.FileViewerApp(tmp_path, settings_path=config)
        assert fresh.algorithm_settings.target_amount_ng == 200
        assert fresh.algorithm_settings.pm_qc_amount_ng == 100
        assert config.read_text() == original
    asyncio.run(exercise())


@pytest.mark.parametrize("volume,unit,expected", [
    ("0.5", "µl", "1.01"), ("0.5", "μL", "1.01"), ("0.5", "uL", "1.01"),
    ("500", "nL", "1.01"), ("0.0005", "mL", "1.01"), ("0.0000005", "L", "1.01"),
    ("0.5", None, ""), ("0.5", "unknown", ""), (None, "µl", ""),
    ("0", "µl", ""), ("NaN", "µl", ""), ("-1", "µl", ""),
])
def test_injection_volume_conversion_and_excel_rounding(tmp_path, volume, unit, expected):
    app = a.FileViewerApp(tmp_path)
    metadata = a.DatasetMetadata(None, None, None, None, None, None, volume, unit)
    assert app._injection_amount(metadata, 100, 100.5) == expected


@pytest.mark.parametrize("value", [0, -1, float('inf'), float('nan')])
def test_invalid_default_target_is_rejected(value):
    with pytest.raises(a.ConfigurationError):
        a.AlgorithmSettings(target_amount_ng=value).validate()


@pytest.mark.parametrize("filename", ["selected.csv", "selected.tsv", "raw-tics.csv"])
def test_export_date_is_added_at_request_time(monkeypatch, filename):
    day = [date(2026, 7, 15)]
    class ExportDate:
        @classmethod
        def today(cls):
            return day[0]
    monkeypatch.setattr(web, "date", ExportDate)
    delimiter = '\t' if filename.endswith('tsv') else ','
    original = a.FileViewerApp._delimited_text(("Path", "QQ", "Injection amount (µL)"),
        [("HéLa.d", 1234567890123456789, "1.01")], delimiter=delimiter)
    async def exercise():
        server = web.PlotServer("true")
        server.exports = {filename: original}
        server.exports_ready = True
        async with TestClient(TestServer(await server._make_app())) as client:
            for export_day in (date(2026, 7, 15), date(2026, 7, 16)):
                day[0] = export_day
                response = await client.get('/exports/' + filename)
                rows = list(csv.reader(io.StringIO(await response.text()), delimiter=delimiter))
                assert rows[0] == ["Date", "Path", "QQ", "Injection amount (µL)"]
                assert rows[1] == [export_day.strftime('%d.%m.%Y'), "HéLa.d", "1234567890123456789", "1.01"]
                assert server.exports[filename] == original
    asyncio.run(exercise())
