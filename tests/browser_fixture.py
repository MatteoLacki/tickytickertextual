"""Deterministic native browser integration fixture (manual, not pytest)."""
import argparse
import shlex
import sys
import tempfile
from pathlib import Path

from textual.binding import Binding
from tickytickertextual.app import FileViewerApp, AlgorithmSettings, ChargeScanScreen, DatasetTicState, adapt_charge_scan_result
from tickytickertextual.web import PlotServer
from test_fileviewer import _fake_charge_result, _fake_tic_result, _write_sample_info


class BrowserFixture(FileViewerApp):
    BINDINGS = [Binding("f2", "review_fixture", show=False),
                Binding("f4", "chrom_fixture", show=False),
                Binding("f5", "download_fixture", show=False),
                Binding("f6", "dominant_fixture", show=False)]

    def on_mount(self, event):
        self.no_color = False
        self._filters = [f for f in self._filters if f.__class__.__name__ not in {"Monochrome", "NoColor"}]
        super().on_mount(event)
        settings = self.algorithm_settings
        self.accepted_fit = adapt_charge_scan_result(_fake_charge_result(settings), settings)
        for name, description, below, above in (
            ("hela.d", "HeLa browser reference", 1234567890123456789, 200),
            ("sample.d", "Sample <&> description", 500, 100),
        ):
            path = self.navigator.root / name
            self.selected_paths.append(path)
            self.selected_descriptions[path] = description
            self.tic_states[path] = DatasetTicState(status="complete", tic_below_line=below,
                tic_above_line=above, result=_fake_tic_result(settings, below=below, above=above))
        self.chosen_path = self.selected_paths[0]
        self.hela_paths = {self.chosen_path}
        self._refresh_selected_pane()

    def action_review_fixture(self):
        screen = ChargeScanScreen(self.chosen_path, settings=self.algorithm_settings,
                                  metadata=self._dataset_metadata(self.chosen_path))
        self.push_screen(screen)
        self.call_after_refresh(lambda: screen.show_result(self.accepted_fit, "memory"))

    def action_chrom_fixture(self):
        self._open_selected_tic_plot()

    def open_review_plot(self, screen, download=False):
        if not download:
            return super().open_review_plot(screen, download)
        import time
        from dataclasses import replace
        import numpy as np
        from tickytickertextual.app import dominant_charge_svg
        def render():
            time.sleep(3)
            result = replace(self.accepted_fit,
                intensities=np.full((3, 150, 1000), 100.0),
                mz_edges=np.linspace(350, 1200, 1001),
                mobility_edges=np.linspace(.6, 1.6, 151))
            return dominant_charge_svg(result)
        return self._queue_plot(render, "dominant-charge.svg", True, screen)

    def action_dominant_fixture(self):
        from tickytickertextual.app import dominant_charge_svg
        self._queue_plot(lambda: dominant_charge_svg(self.accepted_fit), "dominant-charge.svg", True)

    def action_download_fixture(self):
        from tickytickertextual.plots import event_histogram_svg
        self._queue_plot(lambda: event_histogram_svg(self.accepted_fit), "event-histogram.svg", True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.server:
        root = Path(tempfile.mkdtemp(prefix="tickyticker-browser-fixture-"))
        for name, description in (("hela.d", "HeLa browser reference"), ("sample.d", "Sample <&> description")):
            path = root / name
            path.mkdir()
            _write_sample_info(path, description)
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), "--root", str(root)])
        server = PlotServer(command, host="127.0.0.1", port=18979,
                            bridge_token="browser-test", templates_path=Path(__file__).resolve().parents[1] / "src/tickytickertextual/templates")
        server.serve()
    else:
        BrowserFixture(args.root, bridge_url="http://127.0.0.1:18979", bridge_token="browser-test").run()
