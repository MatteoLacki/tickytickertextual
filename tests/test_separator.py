import asyncio
from dataclasses import replace

from textual.widgets import Button, Input, Static

from tickytickertextual import app as a
from tickytickertextual.browser_driver import BrowserAction
from tickytickertextual.workflow import TABLE_HEADER
from test_fileviewer import _fake_charge_result, _fake_tic_result
from test_multi_hela import make_dataset


def test_manual_separator_runs_tic_without_reference_and_resets(tmp_path, monkeypatch):
    sample = make_dataset(tmp_path, "sample.d", "Sample")
    later = make_dataset(tmp_path, "later.d", "Sample")
    calls = []
    def calculate(path, **kwargs):
        calls.append((path, kwargs))
        return _fake_tic_result(a.AlgorithmSettings(), below=100, above=50)
    monkeypatch.setattr(a.charge_regions, "analyse_line_tic", calculate)
    async def exercise():
        app = a.FileViewerApp(tmp_path)
        async with app.run_test(size=(190, 50)) as pilot:
            intercept = app.query_one("#separator-intercept", Input)
            slope = app.query_one("#separator-slope", Input)
            assert intercept.value == slope.value == ""
            assert intercept.placeholder == slope.placeholder == "NA"
            app.selected_paths = [sample]
            app._refresh_selected_pane()
            assert not app._browser_actions()["tic-new"]
            assert "Fit Parameters" not in TABLE_HEADER
            assert len(app._selected_export_rows()[0]) == len(TABLE_HEADER) == 9
            for x, y in (("1.5", ""), ("nan", "0"), ("1.5", "inf"), ("NA", "-0.001")):
                intercept.value, slope.value = x, y
                await pilot.pause()
                assert not app._browser_actions()["tic-new"]
            intercept.value, slope.value = "1.6", "-0.001"
            await pilot.pause()
            assert app._browser_actions()["tic-new"]
            assert app.accepted_fit is None and app.chosen_path is None
            assert intercept.region.y == app.query_one("#pm-qc-amount-ng", Input).region.y
            await pilot.click("#tic-new")
            async with asyncio.timeout(3):
                while app._tic_running:
                    await asyncio.sleep(.01)
            assert len(calls) == 1 and calls[0][0] == sample
            assert calls[0][1]["intercept"] == 1.6 and calls[0][1]["slope"] == -0.001
            assert app.tic_states[sample].status == "complete"
            assert app._selected_export_rows()[0][4] == 100
            # New datasets reuse the manual separator without recalculating previous results.
            held = app.tic_states[sample]
            app.selected_paths.append(later)
            app._refresh_selected_pane()
            assert app._browser_actions()["tic-new"]
            app.on_browser_action(BrowserAction("tic-new"))
            async with asyncio.timeout(3):
                while app._tic_running:
                    await asyncio.sleep(.01)
            assert [path for path, _ in calls] == [sample, later]
            assert app.tic_states[sample] is held
            # Changing the separator invalidates all results; zero is a valid coefficient.
            intercept.value, slope.value = "0", "0"
            await pilot.pause()
            assert not app.tic_states and app._browser_actions()["tic-new"]
            app.on_browser_action(BrowserAction("tic-new"))
            async with asyncio.timeout(3):
                while app._tic_running:
                    await asyncio.sleep(.01)
            assert [path for path, _ in calls] == [sample, later, sample, later]
            assert calls[-1][1]["intercept"] == calls[-1][1]["slope"] == 0
        fresh = a.FileViewerApp(tmp_path)
        async with fresh.run_test(size=(190, 50)) as pilot:
            assert fresh.query_one("#separator-intercept", Input).value == ""
            assert fresh.query_one("#separator-slope", Input).value == ""
            assert not fresh._browser_actions()["tic-new"]
    asyncio.run(exercise())


def test_estimated_separator_populates_fields_and_preserves_exact_settings(tmp_path, monkeypatch):
    hela = make_dataset(tmp_path, "hela.d", "HeLa")
    settings = replace(a.AlgorithmSettings(), mz_min=400, frame_stride=5)
    fit = a.adapt_charge_scan_result(_fake_charge_result(settings), settings)
    calls = []
    def calculate(path, **kwargs):
        calls.append(kwargs)
        return _fake_tic_result(settings)
    monkeypatch.setattr(a.charge_regions, "analyse_line_tic", calculate)
    async def exercise():
        app = a.FileViewerApp(tmp_path)
        async with app.run_test(size=(190, 50)) as pilot:
            app.selected_paths = [hela]
            app._handle_scan_review(hela, fit)
            assert app.query_one("#separator-intercept", Input).value == "1.55"
            assert app.query_one("#separator-slope", Input).value == "-0.0005"
            assert app.query_one("#separator-intercept", Input).disabled
            assert app.query_one("#separator-slope", Input).disabled
            async with asyncio.timeout(3):
                while app._tic_running:
                    await asyncio.sleep(.01)
            await pilot.pause()
            assert not app.query_one("#separator-intercept", Input).disabled
            assert calls[0]["mz_min"] == 400 and calls[0]["frame_stride"] == 5
            assert app.tic_states[hela].status == "complete"
            assert "Fit Parameters" not in str(app.query_one("#selected-header", Static).render())
    asyncio.run(exercise())
