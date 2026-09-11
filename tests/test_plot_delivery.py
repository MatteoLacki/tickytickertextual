import asyncio
import threading
import time

from textual.widgets import Button, Static, TabbedContent

from tickytickertextual import app as a
from test_fileviewer import _fake_charge_result
from test_multi_hela import make_dataset


def test_slow_svg_preparation_keeps_ui_responsive(tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path, 'hela.d', 'HeLa')
    started, release = threading.Event(), threading.Event()
    delivered = []
    def render(result):
        started.set()
        release.wait(3)
        return '<svg/>'
    monkeypatch.setattr(a, 'dominant_charge_svg', render)
    monkeypatch.setattr(a.FileViewerApp, '_post_browser', lambda self, payload: {'url': '/plots/ready'})
    monkeypatch.setattr(a.FileViewerApp, 'open_url', lambda self, url, **kw: delivered.append(url))
    async def exercise():
        app = a.FileViewerApp(tmp_path, bridge_url='http://test')
        async with app.run_test(size=(190, 50)) as pilot:
            settings = a.AlgorithmSettings()
            screen = a.ChargeScanScreen(dataset, settings=settings, metadata=app._dataset_metadata(dataset))
            app.push_screen(screen)
            await pilot.pause()
            screen.show_result(a.adapt_charge_scan_result(_fake_charge_result(settings), settings), 'memory')
            await pilot.pause()
            try:
                before = time.monotonic()
                worker = app.open_review_plot(screen, download=True)
                assert time.monotonic() - before < .2, 'SVG preparation blocked the UI thread'
                async with asyncio.timeout(2):
                    while not started.is_set():
                        await asyncio.sleep(.01)
                await pilot.press('right')
                assert screen.query_one('#scan-tabs', TabbedContent).active == 'scan-histogram'
                await pilot.pause(1.05)
                assert screen.query_one('#scan-download', Button).disabled
                assert app.open_review_plot(screen, download=True) is None
            finally:
                release.set()
            await worker.wait()
            assert delivered == ['/plots/ready?download=1']
            assert not screen.query_one('#scan-download', Button).disabled
    asyncio.run(exercise())


def test_slow_upload_and_failure_do_not_freeze_review(tmp_path, monkeypatch):
    dataset = make_dataset(tmp_path, 'hela.d', 'HeLa')
    upload_started, release = threading.Event(), threading.Event()
    def post(self, payload):
        if 'document' in payload:
            upload_started.set()
            release.wait(3)
            raise TimeoutError('upload timed out')
        return {'ok': True}
    monkeypatch.setattr(a.FileViewerApp, '_post_browser', post)
    monkeypatch.setattr(a, 'dominant_charge_svg', lambda result: '<svg/>')
    async def exercise():
        app = a.FileViewerApp(tmp_path, bridge_url='http://test', production=True)
        async with app.run_test(size=(190, 50)) as pilot:
            settings = a.AlgorithmSettings()
            screen = a.ChargeScanScreen(dataset, settings=settings, metadata=app._dataset_metadata(dataset))
            app.push_screen(screen)
            await pilot.pause()
            screen.show_result(a.adapt_charge_scan_result(_fake_charge_result(settings), settings), 'memory')
            await pilot.pause()
            try:
                worker = app.open_review_plot(screen, download=True)
                async with asyncio.timeout(2):
                    while not upload_started.is_set():
                        await asyncio.sleep(.01)
                await pilot.press('right')
                assert screen.query_one('#scan-tabs', TabbedContent).active == 'scan-histogram'
                assert screen.query_one('#scan-download', Button).disabled
            finally:
                release.set()
            await worker.wait()
            assert not app._plot_delivery_busy
            assert not screen.query_one('#scan-download', Button).disabled
            assert 'upload timed out' in str(screen.query_one('#scan-download-status', Static).render())
            # A failed transfer does not poison a subsequent attempt.
            monkeypatch.setattr(a.FileViewerApp, '_post_browser', lambda self, payload: {'url': '/plots/retry'})
            urls = []
            monkeypatch.setattr(a.FileViewerApp, 'open_url', lambda self, url, **kw: urls.append(url))
            await app.open_review_plot(screen, download=True).wait()
            assert urls == ['/plots/retry?download=1']
    asyncio.run(exercise())


def test_export_publications_are_nonblocking_and_keep_latest_state(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    seen = []
    def post(self, payload):
        seen.append(payload)
        if len(seen) == 1:
            started.set()
            release.wait(3)
        return {'ok': True}
    monkeypatch.setattr(a.FileViewerApp, '_post_browser', post)
    async def exercise():
        app = a.FileViewerApp(tmp_path, bridge_url='http://test')
        async with app.run_test(size=(190, 50)) as pilot:
            try:
                async with asyncio.timeout(2):
                    while not started.is_set():
                        await asyncio.sleep(.01)
                app.action_show_help()
                await pilot.pause()
                assert isinstance(app.screen, a.HelpScreen)
                assert not app._pending_publication['actions']['help']
            finally:
                release.set()
            async with asyncio.timeout(2):
                while app._publication_worker is not None:
                    await asyncio.sleep(.01)
            assert seen[-1]['actions']['help'] is False
    asyncio.run(exercise())


def test_selection_updates_do_not_rebuild_or_upload_exports(tmp_path, monkeypatch):
    from test_fileviewer import _fake_tic_result
    hela = make_dataset(tmp_path, 'hela.d', 'HeLa')
    sample = make_dataset(tmp_path, 'sample.d', 'Sample')
    seen = []
    monkeypatch.setattr(a.FileViewerApp, '_post_browser', lambda self, payload: seen.append(payload) or {'ok': True})
    async def exercise():
        app = a.FileViewerApp(tmp_path, bridge_url='http://test')
        async with app.run_test(size=(190, 50)) as pilot:
            app.selected_paths = [hela, sample]
            app.hela_paths = {hela}
            for path in app.selected_paths:
                app.tic_states[path] = a.DatasetTicState(status='complete', tic_below_line=100,
                    tic_above_line=50, result=_fake_tic_result(app.algorithm_settings))
            app._refresh_selected_pane(highlighted=0)
            await pilot.pause()
            while app._publication_worker is not None:
                await asyncio.sleep(.01)
            assert seen[-1]['ready'] and 'exports' in seen[-1]
            seen.clear()
            def unexpected():
                raise AssertionError('Selection regenerated export data')
            monkeypatch.setattr(app, '_raw_tic_export_rows', unexpected)
            app.query_one('#selected-pane', a.SelectedOptionList).highlighted = 1
            await pilot.pause()
            while app._publication_worker is not None:
                await asyncio.sleep(.01)
            assert seen and seen[-1]['actions']['estimate'] is False
            assert all('exports' not in payload for payload in seen)
    asyncio.run(exercise())
