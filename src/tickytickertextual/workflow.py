"""Analysis state and browser delivery, separate from the navigation shell."""
from __future__ import annotations

import json
import io
from pathlib import Path
from urllib.request import Request, urlopen

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Static

from . import app as a
from .plots import combined_chromatograms_svg, event_histogram_svg


TABLE_HEADER = (
    "Path", "Description", "Gradient", "Volume", "QQ", "QQ / QQ-HeLa",
    "Q", "Q / Q-HeLa", "Fit Parameters",
)


class WorkflowMixin:
    """One fitting reference, multiple normalization references, immutable batches."""

    def __init__(self, *args, production=False, bridge_url=None, bridge_token=None, **kwargs):
        self.production = production
        self.bridge_url = bridge_url
        self.bridge_token = bridge_token
        self.hela_paths: set[Path] = set()
        self.hela_overrides: dict[Path, bool] = {}
        self._pending_reference = None
        self._last_publication = None
        super().__init__(*args, **kwargs)

    def notify(self, message, **kwargs):
        if not self.production:
            super().notify(message, **kwargs)

    def compose(self):
        yield Header(show_clock=True)
        yield Static(id="path-bar")
        with Horizontal(id="panes"):
            yield Static(id="parent-pane", classes="pane")
            with Vertical(id="current-region", classes="pane"):
                yield Static(id="current-header", classes="table-header")
                yield a.CurrentOptionList(id="current-pane", markup=False, compact=True)
            yield Static(id="preview-pane", classes="pane")
        with Vertical(id="selected-region", classes="pane"):
            yield Static(id="selected-header", classes="table-header")
            yield a.SelectedOptionList(id="selected-pane", markup=False, compact=True)
        with Horizontal(id="analysis-actions"):
            yield Button("See Chromatograms", id="open-tic-plot", disabled=True)
            yield Button("TIC new folders", id="tic-new", disabled=True)
            yield Button("Rerun", id="redo-analysis", disabled=True)
        yield Static(id="status-bar")
        yield Footer(compact=True)

    def action_select_dataset(self):
        if self._tic_running or self._analysis_running:
            return
        selected = self.query_one("#selected-pane", a.SelectedOptionList)
        if selected.has_focus:
            self._toggle_chosen_path()
            return
        entry = self._selected_entry()
        if entry is not None and self._is_dataset(entry):
            missing = self._missing_metadata(entry.path)
            if missing:
                self._refresh_current_marks()
                self.query_one("#status-bar", Static).update(
                    f"Cannot select {entry.name}: {' and '.join(missing)} is NA"
                )
                return
        before = set(self.selected_paths)
        super().action_select_dataset()
        for path in set(self.selected_paths) - before:
            enabled = self.hela_overrides.get(
                path, "hela" in (self.selected_descriptions.get(path) or "").lower()
            )
            if enabled:
                self.hela_paths.add(path)
        self._refresh_selected_pane()

    def _missing_metadata(self, path):
        metadata = self._dataset_metadata(path)
        missing = []
        if a._gradient_length_text(metadata) == "NA":
            missing.append("Gradient")
        if a.volume_text(metadata) == "NA":
            missing.append("Volume")
        return missing

    def _toggle_chosen_path(self):
        # Space only changes normalization membership, never launches work.
        if self._tic_running or self._analysis_running:
            return
        selected = self.query_one("#selected-pane", a.SelectedOptionList)
        index = selected.highlighted
        if index is None or index >= len(self.selected_paths):
            return
        path = self.selected_paths[index]
        enabled = path not in self.hela_paths
        self.hela_overrides[path] = enabled
        if enabled:
            self.hela_paths.add(path)
        else:
            self.hela_paths.discard(path)
            if path == self.chosen_path:
                self.chosen_path = None
                # The accepted line remains usable, with its original provenance.
        self._refresh_selected_pane()

    def action_choose_reference(self):
        if self._analysis_running or self._tic_running:
            return
        selected = self.query_one("#selected-pane", a.SelectedOptionList)
        index = selected.highlighted
        if index is None or index >= len(self.selected_paths):
            return
        path = self.selected_paths[index]
        if path not in self.hela_paths:
            self.query_one("#status-bar", Static).update("Space enables a HeLa; Enter selects it for fitting")
            return
        if self._missing_metadata(path):
            self.query_one("#status-bar", Static).update("Cannot fit this HeLa: Gradient or Volume is NA")
            return
        self._pending_reference = path
        self._refresh_selected_pane()
        self.push_screen(
            a.SettingsScreen(self.algorithm_settings, self.settings_path, run_analysis=True),
            lambda settings: self._start_reference(path, settings),
        )

    def _start_reference(self, path, settings):
        if settings is None:
            self._pending_reference = None
            self._refresh_selected_pane()
            return
        self._apply_algorithm_settings(settings)
        if self.algorithm_settings != settings:
            self._pending_reference = None
            self._refresh_selected_pane()
            return
        self.push_screen(
            a.ChargeScanScreen(path, settings=settings, metadata=self._dataset_metadata(path), auto_start=True),
            lambda result: self._handle_scan_review(path, result),
        )

    def on_option_list_option_selected(self, event):
        if event.option_list.id == "selected-pane":
            self.action_choose_reference()
        else:
            super().on_option_list_option_selected(event)

    def _handle_scan_review(self, dataset, result):
        self._pending_reference = None
        if result is not None:
            self.chosen_path = dataset
            self.hela_paths.add(dataset)
            self.accepted_fit = result
            self._begin_tic_batch(result)
        else:
            self._refresh_selected_pane()
        self.query_one("#selected-pane", a.SelectedOptionList).focus()

    def _request_main_redo(self):
        if self._analysis_running or self._tic_running or self.chosen_path is None:
            return
        self._pending_reference = self.chosen_path
        self.push_screen(
            a.SettingsScreen(self.algorithm_settings, self.settings_path, run_analysis=True),
            lambda settings: self._start_reference(self.chosen_path, settings),
        )

    def on_charge_scan_screen_redo_requested(self, event):
        self.push_screen(
            a.SettingsScreen(event.screen.settings, self.settings_path, run_analysis=True),
            lambda settings: self._redo_open_screen(event.screen, settings),
        )

    def on_button_pressed(self, event):
        if event.button.id == "tic-new":
            if self.accepted_fit is not None:
                self._begin_tic_batch(self.accepted_fit, only_new=True)
        else:
            super().on_button_pressed(event)

    def _begin_tic_batch(self, fit, only_new=False):
        if self._tic_running or self._analysis_running:
            return
        paths = tuple(path for path in self.selected_paths if not only_new or path not in self.tic_states)
        if not paths:
            return
        intercept, slope = self._line_from_result(fit)
        self._tic_running = True
        if not only_new:
            self.tic_states.clear()
        for path in paths:
            self.tic_states[path] = a.DatasetTicState(status="queued")
        self._refresh_selected_pane()
        self._execute_tic_batch(paths, fit.settings, intercept, slope)

    def _finish_tic_batch(self, errors):
        super()._finish_tic_batch(errors)
        self._refresh_selected_pane()
        references = len(self._completed_helas())
        failed_helas = len(self.hela_paths) - references
        self.query_one("#status-bar", Static).update(
            f"TIC complete · HeLa normalization: {references} included, {failed_helas} unavailable"
        )

    def _begin_charge_scan(self, screen):
        super()._begin_charge_scan(screen)
        self._update_analysis_actions()
        self._publish_exports()

    def _completed_helas(self):
        return [self.tic_states[path] for path in self.selected_paths
                if path in self.hela_paths and path in self.tic_states
                and self.tic_states[path].status == "complete"]

    def _normalizers(self):
        if self._tic_running or any(path not in self.tic_states for path in self.selected_paths):
            return None, None
        refs = self._completed_helas()
        if not refs:
            return None, None
        return (sum(int(s.tic_below_line) for s in refs) / len(refs),
                sum(int(s.tic_above_line) for s in refs) / len(refs))

    def _selected_export_rows(self):
        below_mean, above_mean = self._normalizers()
        line = self._accepted_line()
        rows = []
        for path in self.selected_paths:
            metadata = self._dataset_metadata(path)
            state = self.tic_states.get(path)
            if state is not None and state.status == "complete":
                below, above = int(state.tic_below_line), int(state.tic_above_line)
                below_relative = below / below_mean if below_mean else ""
                above_relative = above / above_mean if above_mean else ""
            else:
                below = above = "ERROR" if state is not None and state.status == "error" else ""
                below_relative = above_relative = ""
            rows.append((path.name, self.selected_descriptions.get(path, metadata.description) or "unavailable", a._gradient_length_text(metadata),
                         a.volume_text(metadata), below, below_relative, above, above_relative,
                         a._fit_cell_text(line) if path == self.chosen_path else ""))
        return rows

    def _refresh_selected_pane(self, *, highlighted=None):
        selected = self.query_one("#selected-pane", a.SelectedOptionList)
        if highlighted is None:
            highlighted = selected.highlighted
        rows = self._selected_export_rows()
        display_rows = []
        for row in rows:
            cells = [str(value) for value in row]
            for index in (5, 7):
                cells[index] = f"{row[index]:.2%}" if isinstance(row[index], float) else "—"
            for index in (4, 6):
                if cells[index] == "":
                    cells[index] = "—"
            display_rows.append(cells)
        widths = [max(len(TABLE_HEADER[i]), *(a.cell_len(r[i]) for r in display_rows))
                  if display_rows else len(TABLE_HEADER[i]) for i in range(9)]
        widths[0] = max(12, widths[0])
        widths[1] = min(36, max(12, widths[1]))
        region = self.query_one("#selected-region")
        header = Text(" " * 5, no_wrap=True)
        header.append(" │ ".join(a._column_cell(name, width, right=i in (2,3,4,5,6,7))
                                 for i, (name, width) in enumerate(zip(TABLE_HEADER, widths))))
        self.query_one("#selected-header", Static).update(header)
        # Wide numeric columns scroll horizontally; rows never wrap or shorten numbers.
        minimum_width = sum(widths) + 5 + 3 * 8
        region.styles.overflow_x = "auto"
        selected.styles.min_width = minimum_width
        self.query_one("#selected-header").styles.min_width = minimum_width
        labels = []
        for index, (path, cells) in enumerate(zip(self.selected_paths, display_rows)):
            reference = path == (self._pending_reference or self.chosen_path)
            hela = path in self.hela_paths
            label = Text(no_wrap=True, overflow="ellipsis")
            label.append(" × ", style=Style(color="#777777" if self._tic_running else "#ff7b72",
                         meta={} if self._tic_running else {"remove-selected": index}, bold=True))
            label.append("★ " if reference else "H " if hela else "  ")
            label.append(" │ ".join(a._column_cell(value.replace("\n", " ").replace("\r", " "), width,
                         right=i in (2,3,4,5,6,7)) for i, (value, width) in enumerate(zip(cells, widths))))
            if reference:
                label.stylize("bold #ffb86c on #513008", 3)
            elif hela:
                label.stylize("#8bc5ff on #12335b", 3)
            labels.append(label)
        selected.set_options(labels)
        selected.highlighted = min(highlighted or 0, len(labels)-1) if labels else None
        self._update_analysis_actions()
        self._publish_exports()

    def _current_table_widths(self):
        name_width, gradient_width = super()._current_table_widths()
        return max(1, name_width - 15), gradient_width

    def _refresh_current_marks(self):
        current = self.query_one("#current-pane", a.CurrentOptionList)
        name_width, gradient_width = self._current_table_widths()
        self.query_one("#current-header", Static).update(Text(
            "  " + a._column_cell("Path", name_width) + " │ " +
            a._column_cell("Gradient", gradient_width, right=True) + " │ " +
            a._column_cell("Volume", 12, right=True) + " ", no_wrap=True))
        for index, entry in enumerate(self.entries):
            current.replace_option_prompt_at_index(index, a._entry_label(
                entry, marked=entry.path in self.selected_paths,
                metadata=self.dataset_metadata_cache.get(entry.path),
                name_width=name_width, gradient_width=gradient_width, volume_width=12))

    def _remove_selected_at(self, index):
        if self._tic_running or self._analysis_running or not 0 <= index < len(self.selected_paths):
            return
        path = self.selected_paths.pop(index)
        self.hela_paths.discard(path)
        self.tic_states.pop(path, None)
        if path == self.chosen_path:
            self.chosen_path = None
        self._refresh_selected_pane(highlighted=max(0, index-1))
        self._refresh_current_marks()

    def _results_ready(self):
        return (not self._tic_running and not self._analysis_running
                and bool(self.selected_paths)
                and all(p in self.tic_states and self.tic_states[p].status in {"complete", "error"}
                        for p in self.selected_paths)
                and any(s.status == "complete" for s in self.tic_states.values()))

    def _update_analysis_actions(self):
        busy = self._tic_running or self._analysis_running
        self.query_one("#open-tic-plot", Button).disabled = not self._results_ready()
        self.query_one("#redo-analysis", Button).disabled = busy or self.chosen_path is None
        new = self.accepted_fit is not None and any(p not in self.tic_states for p in self.selected_paths)
        button = self.query_one("#tic-new", Button)
        button.display = new
        button.disabled = busy or not new

    def _post_browser(self, payload):
        if not self.bridge_url:
            return None
        request = Request(self.bridge_url + "/_publish", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + (self.bridge_token or "")})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def _publish_exports(self):
        if not self.bridge_url:
            return
        ready = self._results_ready()
        exports = {}
        if ready:
            rows = self._selected_export_rows()
            exports = {
                "selected.tsv": self._delimited_text(TABLE_HEADER, rows, delimiter="\t"),
                "selected.csv": self._delimited_text(TABLE_HEADER, rows, delimiter=","),
                "raw-tics.csv": self._delimited_text(
                    ("Path", "Description", "Frame", "Retention Time (s)", "Retention Time", "Raw TIC", "QQ", "Q"),
                    self._raw_tic_export_rows(), delimiter=","),
            }
        payload = {"ready": ready, "exports": exports}
        if payload == self._last_publication:
            return
        try:
            self._post_browser(payload)
            self._last_publication = payload
        except Exception as error:
            self.query_one("#status-bar", Static).update(f"Browser export failed: {error}")

    def _deliver_svg(self, svg, filename, download=False):
        try:
            if download:
                content, mime = svg, "image/svg+xml"
            else:
                from .plots import svg_viewer_html
                content, mime = svg_viewer_html(svg, filename), "text/html"
                filename = filename.removesuffix(".svg") + ".html"
            self.deliver_text(io.StringIO(content), save_filename=filename,
                              mime_type=mime, encoding="utf-8",
                              open_method="download" if download else "browser")
        except Exception as error:
            self.notify(f"Cannot deliver SVG: {error}", severity="error")

    def open_review_plot(self, screen, download=False):
        if screen.result is None:
            return
        tab = screen.query_one("#scan-tabs", a.TabbedContent).active
        if tab == "scan-fit":
            return
        histogram = tab == "scan-histogram"
        svg = event_histogram_svg(screen.result) if histogram else a.dominant_charge_svg(screen.result)
        self._deliver_svg(svg, "event-histogram.svg" if histogram else "dominant-charge.svg", download)

    def _finish_charge_scan(self, screen, result, error, traceback_text, advice):
        self._analysis_running = False
        if error is not None or result is None:
            super()._finish_charge_scan(screen, result, error, traceback_text, advice)
            return
        screen.show_result(result, "memory")

    def _open_selected_tic_plot(self):
        datasets = [(path, self.selected_descriptions.get(path) or "", self.tic_states[path].result)
                    for path in self.selected_paths if path in self.tic_states
                    and self.tic_states[path].status == "complete"]
        if datasets:
            self._deliver_svg(combined_chromatograms_svg(datasets), "chromatograms.svg")

    def on_unmount(self):
        try:
            self._post_browser({"ready": False, "exports": {}, "clear": True})
        except Exception:
            pass
