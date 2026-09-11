import asyncio

from textual.widgets import Button, Footer, OptionList, TabbedContent

from tickytickertextual.app import AlgorithmSettings, ChargeScanScreen, FileViewerApp, HelpScreen, SettingsScreen, adapt_charge_scan_result
from test_fileviewer import _fake_charge_result
from test_multi_hela import make_dataset


def test_estimate_button_and_help_replace_footers(tmp_path):
    hela = make_dataset(tmp_path, 'hela.d', 'HeLa')
    sample = make_dataset(tmp_path, 'sample.d', 'Sample')
    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(190, 50)) as pilot:
            assert not app.query(Footer)
            assert app.query_one('#estimate-split', Button).disabled
            app.selected_paths = [hela, sample]
            app.hela_paths = {hela}
            app._refresh_selected_pane(highlighted=1)
            await pilot.pause()
            assert app.query_one('#estimate-split', Button).disabled
            app.query_one('#selected-pane', OptionList).highlighted = 0
            await pilot.pause()
            assert not app.query_one('#estimate-split', Button).disabled
            await pilot.click('#estimate-split')
            assert isinstance(app.screen, SettingsScreen)
            assert len(app.screen_stack) == 2
            assert app._pending_reference == hela and not app._analysis_running
            await pilot.press('escape')
            await pilot.click('#show-help')
            assert isinstance(app.screen, HelpScreen)
            assert 'Left/Right' in app.screen.HELP_TEXT and 'Ctrl+Down' in app.screen.HELP_TEXT
            await pilot.press('escape')
            app._analysis_running = True
            app._update_analysis_actions()
            assert app.query_one('#estimate-split', Button).disabled
            app._analysis_running = False
    asyncio.run(exercise())


def test_review_download_cooldown_survives_tab_changes(tmp_path, monkeypatch):
    hela = make_dataset(tmp_path, 'hela.d', 'HeLa')
    calls = []
    monkeypatch.setattr(FileViewerApp, 'open_review_plot', lambda *args, **kwargs: calls.append(1))
    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(190, 50)) as pilot:
            settings = AlgorithmSettings()
            screen = ChargeScanScreen(hela, settings=settings, metadata=app._dataset_metadata(hela))
            app.push_screen(screen)
            await pilot.pause()
            screen.show_result(adapt_charge_scan_result(_fake_charge_result(settings), settings), 'memory')
            await pilot.pause()
            assert not screen.query(Footer)
            button = screen.query_one('#scan-download', Button)
            await pilot.click('#scan-download')
            assert button.disabled and len(calls) == 1
            screen.on_button_pressed(Button.Pressed(button))
            screen.query_one('#scan-tabs', TabbedContent).active = 'scan-histogram'
            await pilot.pause()
            assert button.disabled and len(calls) == 1
            await pilot.pause(1.05)
            assert not button.disabled
            await pilot.click('#scan-download')
            assert len(calls) == 2
    asyncio.run(exercise())


def test_tic_progress_stays_in_active_row_and_preserves_selection(tmp_path):
    from textual.widgets import Static
    from tickytickertextual.app import DatasetTicState
    from test_fileviewer import _fake_tic_result
    hela = make_dataset(tmp_path, 'hela.d', 'HeLa')
    sample = make_dataset(tmp_path, 'sample.d', 'Sample')
    async def exercise():
        app = FileViewerApp(tmp_path)
        async with app.run_test(size=(190, 50)) as pilot:
            app.selected_paths = [hela, sample]
            app.hela_paths = {hela}
            app._tic_running = True
            app.tic_states = {path: DatasetTicState(status="queued") for path in app.selected_paths}
            app._refresh_selected_pane(highlighted=1)
            await pilot.pause()
            selected = app.query_one('#selected-pane', OptionList)
            assert abs(app.query_one('#current-region').size.height - app.query_one('#selected-region').size.height) <= 1
            assert 'Queued' in selected.get_option_at_index(0).prompt.plain
            app._set_tic_running(hela)
            app._set_tic_progress(hela, 'Processed 25 MS1 frames')
            await pilot.pause()
            cells = selected.get_option_at_index(0).prompt.plain.split(' │ ')
            assert cells[4].strip() == '25 frames'
            assert 'Queued' in selected.get_option_at_index(1).prompt.plain
            assert selected.highlighted == 1
            assert '25' not in str(app.query_one('#status-bar', Static).render())
            assert app._selected_export_rows()[0][4] == ''
            app._set_tic_result(hela, _fake_tic_result(app.algorithm_settings, below=12345, above=100), None)
            assert selected.get_option_at_index(0).prompt.plain.split(' │ ')[4].strip() == '12345'
            app._tic_running = False
    asyncio.run(exercise())
