"""Analysis state and browser delivery, separate from the navigation shell."""
from __future__ import annotations

import asyncio
import importlib
import io
import json
import math
import re
import time
from dataclasses import replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.request import Request, urlopen

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Header, Input, Label, Static

from .browser_driver import BrowserAction
from .plots import combined_chromatograms_svg, event_histogram_svg


class _LazyModule:
    """Defer importing `app` until first attribute access.

    app.py imports WorkflowMixin from this module, so an eager `from . import
    app` here is circular. Python's sys.modules cache tolerates that under a
    normal import (it just hands back the still-initialising app module), but
    multiprocessing's spawn start method can re-execute app.py under the name
    __mp_main__ (needed whenever the process's __main__ is
    tickytickertextual.app, i.e. always, since it's launched via `python -m`)
    without registering it under its real dotted name - so that tolerance
    doesn't apply there, and the cycle raises ImportError instead. Deferring
    the import until an attribute is actually used, well after both modules
    have finished loading in any context, avoids it.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._module = None

    def __getattr__(self, attr: str) -> object:
        if self._module is None:
            self._module = importlib.import_module(self._name, __package__)
        return getattr(self._module, attr)


a = _LazyModule(".app")


TABLE_HEADER = (
    "Path", "Description", "Gradient", "Volume", "QQ", "QQ / QQ-HeLa",
    "Q", "Q / Q-HeLa", "Injection amount (µL)",
)

_TIC_PROGRESS_PATTERN = re.compile(r"^Processed (\d+)/(\d+) MS1 frames$")
_TIC_STARTING_PATTERN = re.compile(r"^Processing (\d+) MS1 frames across \d+ workers$")


class WorkflowMixin:
    """One fitting reference, multiple normalization references, immutable batches."""

    def __init__(self, *args, production=False, bridge_url=None, bridge_token=None, **kwargs):
        self.production = production
        self.bridge_url = bridge_url
        self.bridge_token = bridge_token
        self.hela_paths: set[Path] = set()
        self.hela_overrides: dict[Path, bool] = {}
        self._pending_reference = None
        self._export_cache = {}
        self._export_cache_ready = False
        self._published_exports = None
        self._tic_progress = {}
        self._tic_start_wait = None
        self._selected_display_rows = []
        self._selected_column_widths = []
        self._last_publication = None
        self._pending_publication = None
        self._publication_worker = None
        self._plot_delivery_busy = False
        self._closing = False
        self._separator_line = None
        self._separator_settings = None
        self._separator_inputs_valid = False
        self._target_amount_valid = True
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
        with Horizontal(id="injection-settings"):
            yield Label("QC: HeLa · PM QC amount (ng)")
            yield Input(str(self.algorithm_settings.pm_qc_amount_ng), type="number", id="pm-qc-amount-ng")
            yield Label("MM target amount (ng)")
            yield Input(str(self.algorithm_settings.target_amount_ng), type="number", id="target-amount-ng")
            yield Label("separator: intercept=")
            yield Input(placeholder="NA", type="number", id="separator-intercept")
            yield Label("slope=")
            yield Input(placeholder="NA", type="number", id="separator-slope")
            yield Static(id="target-error")
        with Horizontal(id="analysis-actions"):
            yield Button("Help", id="show-help")
            yield Button("Estimate charge split", id="estimate-split", disabled=True)
            yield Button("See Chromatograms", id="open-tic-plot", disabled=True)
            yield Button("TIC new folders", id="tic-new", disabled=True)
            yield Button("Rerun", id="redo-analysis", disabled=True)
        yield Static("Enter separator values and click TIC new folders, or highlight a HeLa and click Estimate charge split.",
                     id="analysis-hint")
        yield Static(id="status-bar")

    def on_mount(self, event):
        event.prevent_default()
        super().on_mount()
        self.set_interval(0.25, self._update_tic_start_wait)
        if self.bridge_url:
            self.query_one("#analysis-actions").display = False

    def on_input_changed(self, event: Input.Changed):
        if event.input.id in {"separator-intercept", "separator-slope"}:
            event.stop()
            self._separator_changed()
            return
        if event.input.id not in {"target-amount-ng", "pm-qc-amount-ng"}:
            return
        event.stop()
        try:
            settings = replace(self.algorithm_settings,
                               target_amount_ng=float(self.query_one("#target-amount-ng", Input).value),
                               pm_qc_amount_ng=float(self.query_one("#pm-qc-amount-ng", Input).value))
            settings.validate()
        except (ValueError, a.ConfigurationError):
            self._target_amount_valid = False
            self.query_one("#target-error", Static).update("Enter amounts greater than zero")
        else:
            self._target_amount_valid = True
            self.algorithm_settings = settings
            self.query_one("#target-error", Static).update("")
        self._refresh_selected_pane()

    def _set_separator_line(self, line, settings):
        self._separator_line = line
        self._separator_settings = settings
        self._separator_inputs_valid = True
        with self.prevent(Input.Changed):
            self.query_one("#separator-intercept", Input).value = str(line[0])
            self.query_one("#separator-slope", Input).value = str(line[1])

    def _separator_changed(self):
        try:
            line = tuple(float(self.query_one(selector, Input).value)
                         for selector in ("#separator-intercept", "#separator-slope"))
            valid = all(math.isfinite(value) for value in line)
        except ValueError:
            valid = False
        self._separator_inputs_valid = valid
        if valid and line != self._separator_line:
            self._separator_line = line
            self._separator_settings = self.algorithm_settings
            # Never normalize results calculated with different separators together.
            self.tic_states.clear()
            self._tic_progress.clear()
            self._refresh_selected_pane()
            self.query_one("#status-bar", Static).update(
                "Separator changed · click TIC new folders to calculate selected folders"
            )
        else:
            self._update_analysis_actions()
            self._publish_exports()

    def _apply_algorithm_settings(self, settings):
        super()._apply_algorithm_settings(settings)
        if settings is not None:
            self.query_one("#target-amount-ng", Input).value = str(self.algorithm_settings.target_amount_ng)
            self.query_one("#pm-qc-amount-ng", Input).value = str(self.algorithm_settings.pm_qc_amount_ng)

    def on_browser_action(self, event: BrowserAction):
        if not self._browser_actions().get(event.action):
            return
        if event.action == "help":
            self.action_show_help()
        elif event.action == "estimate":
            self.action_choose_reference()
        elif event.action == "chromatograms":
            self._open_selected_tic_plot()
        elif event.action == "rerun":
            self._request_main_redo()
        elif event.action == "tic-new":
            self._begin_tic_batch(only_new=True)

    def on_descendant_focus(self, event):
        # Keep the browser toolbar in sync when a modal opens or closes.
        self._publish_exports()

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
        if self._analysis_running or self._tic_running or len(self.screen_stack) != 1:
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
        event.prevent_default()
        event.stop()
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
        if (self._analysis_running or self._tic_running or self.chosen_path is None
                or len(self.screen_stack) != 1 or self._pending_reference is not None):
            return
        self._pending_reference = self.chosen_path
        self.push_screen(
            a.SettingsScreen(self.algorithm_settings, self.settings_path, run_analysis=True),
            lambda settings: self._start_reference(self.chosen_path, settings),
        )

    def on_charge_scan_screen_redo_requested(self, event):
        event.prevent_default()
        event.stop()
        self.push_screen(
            a.SettingsScreen(event.screen.settings, self.settings_path, run_analysis=True),
            lambda settings: self._redo_open_screen(event.screen, settings),
        )

    def on_button_pressed(self, event):
        event.prevent_default()
        event.stop()
        if event.button.id == "show-help":
            self.action_show_help()
        elif event.button.id == "estimate-split":
            self.action_choose_reference()
        elif event.button.id == "tic-new":
            self._begin_tic_batch(only_new=True)
        else:
            super().on_button_pressed(event)

    def _begin_tic_batch(self, fit=None, only_new=False):
        if self._tic_running or self._analysis_running:
            return
        if fit is not None:
            self._set_separator_line(self._line_from_result(fit), fit.settings)
        else:
            self._separator_changed()
        paths = tuple(path for path in self.selected_paths if not only_new or path not in self.tic_states)
        if not paths:
            return
        if not self._separator_inputs_valid or self._separator_line is None:
            return
        intercept, slope = self._separator_line
        settings = self._separator_settings or self.algorithm_settings
        self._tic_running = True
        if not only_new:
            self.tic_states.clear()
            self._tic_progress.clear()
        for path in paths:
            self.tic_states[path] = a.DatasetTicState(status="queued")
        self._refresh_selected_pane()
        # Paint the main screen and queued rows before raw-data initialization.
        self.call_after_refresh(self._execute_tic_batch, paths, settings, intercept, slope)

    def _set_tic_running(self, path):
        self.tic_states[path] = a.DatasetTicState(status="running")
        self._tic_progress[path] = "Starting…"
        self._tic_start_wait = (path, time.monotonic())
        self._refresh_tic_row(path)

    def _set_tic_progress(self, path, message):
        state = self.tic_states.get(path)
        if state is None or state.status != "running":
            return
        self._tic_start_wait = None
        self._tic_progress[path] = self._format_tic_progress(message)
        self._refresh_tic_row(path)

    @staticmethod
    def _format_tic_progress(message: str) -> str:
        if message == "TIC analysis complete":
            return "Finishing…"
        # "Processing N MS1 frames across P workers" (once, before any chunk
        # has finished) and "Processed {done}/{total} MS1 frames" (once per
        # completed worker chunk, so ~12 steps by default - both from the
        # worker-process split in charge_regions) render as an approximate
        # bar, kept short so this doesn't widen the QQ column. Anything else
        # falls back to plain trimmed text.
        if (match := _TIC_STARTING_PATTERN.match(message)) is not None:
            done, total = 0, int(match.group(1))
        elif (match := _TIC_PROGRESS_PATTERN.match(message)) is not None:
            done, total = int(match.group(1)), int(match.group(2))
        else:
            return message.removeprefix("Processed ").replace(" MS1 frames", " frames")
        fraction = done / total if total else 1.0
        bar_width = 6
        filled = round(fraction * bar_width)
        bar = "▓" * filled + "░" * (bar_width - filled)
        return f"{bar} {round(fraction * 100)}%"

    def _update_tic_start_wait(self):
        if self._tic_start_wait is None or not self._tic_running:
            return
        path, started = self._tic_start_wait
        elapsed = int(time.monotonic() - started)
        if elapsed < 1:
            return
        message = f"Starting {elapsed}s"
        if self._tic_progress.get(path) != message:
            self._tic_progress[path] = message
            self._refresh_tic_row(path)

    def _set_tic_result(self, path, result, error):
        self._tic_start_wait = None
        super()._set_tic_result(path, result, error)

    def _refresh_tic_row(self, path):
        if path not in self.selected_paths:
            return
        index = self.selected_paths.index(path)
        progress = self._tic_progress[path]
        if (len(self._selected_display_rows) != len(self.selected_paths)
                or a.cell_len(progress) > self._selected_column_widths[4]):
            self._refresh_selected_pane(refresh_exports=False)
            return
        cells = self._selected_display_rows[index]
        cells[4] = progress
        self.query_one("#selected-pane", a.SelectedOptionList).replace_option_prompt_at_index(
            index, self._selected_row_label(index, path, cells, self._selected_column_widths),
        )

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

    def _injection_amount(self, metadata, sample_qq, mean_hela_qq):
        if self._analysis_running or self._tic_running or not self._target_amount_valid:
            return ""
        try:
            volume = Decimal(metadata.volume)
            unit = (metadata.volume_unit or "").strip().casefold().replace("μ", "u").replace("µ", "u")
            factor = {"ul": Decimal(1), "nl": Decimal("0.001"),
                      "ml": Decimal(1000), "l": Decimal(1000000)}.get(unit)
            sample = Decimal(str(sample_qq))
            reference = Decimal(str(mean_hela_qq))
            target = Decimal(str(self.algorithm_settings.target_amount_ng))
            if (factor is None or not all(value.is_finite() and value > 0
                                         for value in (volume, sample, reference, target))):
                return ""
            amount = reference / sample * (target / Decimal(str(self.algorithm_settings.pm_qc_amount_ng))) * volume * factor
            return format(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        except (InvalidOperation, TypeError, ValueError):
            return ""

    def _selected_export_rows(self):
        below_mean, above_mean = self._normalizers()
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
                         self._injection_amount(metadata, below, below_mean)))
        return rows

    def _refresh_selected_pane(self, *, highlighted=None, refresh_exports=True):
        selected = self.query_one("#selected-pane", a.SelectedOptionList)
        if highlighted is None:
            highlighted = selected.highlighted
        rows = self._selected_export_rows()
        display_rows = []
        for path, row in zip(self.selected_paths, rows):
            cells = [str(value) for value in row]
            for index in (5, 7):
                cells[index] = f"{row[index]:.2%}" if isinstance(row[index], float) else "—"
            for index in (4, 6):
                if cells[index] == "":
                    cells[index] = "—"
            state = self.tic_states.get(path)
            if state is not None and state.status in {"queued", "running"}:
                cells[4] = "Queued" if state.status == "queued" else self._tic_progress.get(path, "Starting…")
            display_rows.append(cells)
        widths = [max(len(TABLE_HEADER[i]), *(a.cell_len(r[i]) for r in display_rows))
                  if display_rows else len(TABLE_HEADER[i]) for i in range(len(TABLE_HEADER))]
        widths[4] = max(12, widths[4])
        widths[0] = max(12, widths[0])
        widths[1] = min(36, max(12, widths[1]))
        region = self.query_one("#selected-region")
        header = Text(" " * 5, no_wrap=True)
        header.append(" │ ".join(a._column_cell(name, width, right=i in (2,3,4,5,6,7,8))
                                 for i, (name, width) in enumerate(zip(TABLE_HEADER, widths))))
        self.query_one("#selected-header", Static).update(header)
        # Wide numeric columns scroll horizontally; rows never wrap or shorten numbers.
        minimum_width = sum(widths) + 5 + 3 * (len(TABLE_HEADER) - 1)
        region.styles.overflow_x = "auto"
        selected.styles.min_width = minimum_width
        self.query_one("#selected-header").styles.min_width = minimum_width
        self._selected_display_rows = display_rows
        self._selected_column_widths = widths
        labels = [self._selected_row_label(index, path, cells, widths)
                  for index, (path, cells) in enumerate(zip(self.selected_paths, display_rows))]
        selected.set_options(labels)
        selected.highlighted = min(highlighted or 0, len(labels)-1) if labels else None
        self._update_analysis_actions()
        self._publish_exports(refresh_data=refresh_exports)

    def _selected_row_label(self, index, path, cells, widths):
        reference = path == (self._pending_reference or self.chosen_path)
        hela = path in self.hela_paths
        label = Text(no_wrap=True, overflow="ellipsis")
        label.append(" × ", style=Style(color="#777777" if self._tic_running else "#ff7b72",
                     meta={} if self._tic_running else {"remove-selected": index}, bold=True))
        label.append("★ " if reference else "H " if hela else "  ")
        label.append(" │ ".join(a._column_cell(value.replace("\n", " ").replace("\r", " "), width,
                     right=i in (2,3,4,5,6,7,8)) for i, (value, width) in enumerate(zip(cells, widths))))
        if reference:
            label.stylize("bold #ffb86c on #513008", 3)
        elif hela:
            label.stylize("#8bc5ff on #12335b", 3)
        return label

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

    def _update_selected_status(self, index):
        super()._update_selected_status(index)
        self._publish_exports()

    def _eligible_reference(self):
        selected = self.query("#selected-pane")
        index = selected.first().highlighted if selected else None
        if index is None or index >= len(self.selected_paths):
            return False
        path = self.selected_paths[index]
        return path in self.hela_paths and not self._missing_metadata(path)

    def _browser_actions(self):
        busy = self._tic_running or self._analysis_running
        main_screen = len(self.screen_stack) == 1
        return {
            "help": not isinstance(self.screen, a.HelpScreen),
            "estimate": main_screen and not busy and self._eligible_reference(),
            "chromatograms": not busy and not self._plot_delivery_busy and any(
                state.status == "complete" and state.result is not None
                for path, state in self.tic_states.items() if path in self.selected_paths
            ),
            "rerun": main_screen and not busy and self.chosen_path is not None and self.accepted_fit is not None,
            "tic-new": main_screen and not busy and self._separator_inputs_valid and self._separator_line is not None
                       and any(path not in self.tic_states for path in self.selected_paths),
        }

    def _update_analysis_actions(self):
        busy = self._tic_running or self._analysis_running
        actions = self._browser_actions()
        self.query_one("#estimate-split", Button).disabled = not actions["estimate"]
        self.query_one("#open-tic-plot", Button).disabled = not actions["chromatograms"]
        self.query_one("#redo-analysis", Button).disabled = not actions["rerun"]
        new = any(p not in self.tic_states for p in self.selected_paths)
        button = self.query_one("#tic-new", Button)
        button.display = new
        button.disabled = not actions["tic-new"]
        self.query_one("#separator-intercept", Input).disabled = busy
        self.query_one("#separator-slope", Input).disabled = busy

    def _post_browser(self, payload):
        if not self.bridge_url:
            return None
        request = Request(self.bridge_url + "/_publish", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + (self.bridge_token or "")})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def _publish_exports(self, *, refresh_data=False):
        if not self.bridge_url or self._closing:
            return
        ready = self._results_ready()
        if not ready:
            self._export_cache = {}
        elif refresh_data or not self._export_cache_ready:
            rows = self._selected_export_rows()
            self._export_cache = {
                "selected.tsv": self._delimited_text(TABLE_HEADER, rows, delimiter="\t"),
                "selected.csv": self._delimited_text(TABLE_HEADER, rows, delimiter=","),
                "raw-tics.csv": self._delimited_text(
                    ("Path", "Description", "Frame", "Retention Time (s)", "Retention Time", "Raw TIC", "QQ", "Q"),
                    self._raw_tic_export_rows(), delimiter=","),
            }
        self._export_cache_ready = ready
        payload = {"ready": ready, "exports": self._export_cache, "actions": self._browser_actions()}
        if payload == self._last_publication:
            return
        # Keep one publication in flight and coalesce rapid focus/state updates.
        self._last_publication = payload
        self._pending_publication = payload
        if self._publication_worker is None:
            self._publication_worker = self.run_worker(
                self._flush_publications(), group="browser-exports", exit_on_error=False,
            )

    async def _flush_publications(self):
        try:
            while self._pending_publication is not None and not self._closing:
                payload = self._pending_publication
                self._pending_publication = None
                try:
                    update = payload.copy()
                    if update["exports"] == self._published_exports:
                        del update["exports"]
                    await asyncio.to_thread(self._post_browser, update)
                    self._published_exports = payload["exports"]
                except Exception as error:
                    if self._last_publication == payload:
                        self._last_publication = None
                    if not self._closing:
                        self.query_one("#status-bar", Static).update(f"Browser export failed: {error}")
        finally:
            self._publication_worker = None

    def _prepare_plot(self, render, filename, download):
        content = render()
        mime = "image/svg+xml"
        if not download:
            from .plots import svg_viewer_html
            content, mime = svg_viewer_html(content, filename), "text/html"
            filename = filename.removesuffix(".svg") + ".html"
        url = None
        if self.bridge_url:
            result = self._post_browser({"document": {
                "content": content, "filename": filename, "mime": mime,
                "disposition": "attachment" if download else "inline",
            }})
            url = result["url"] + ("?download=1" if download else "")
        return content, filename, mime, url

    def _queue_plot(self, render, filename, download=False, screen=None):
        if self._plot_delivery_busy or self._closing:
            return None
        self._plot_delivery_busy = True
        if screen is not None:
            screen.set_download_status(True, "Preparing download…")
        else:
            self.query_one("#status-bar", Static).update("Preparing plot…")
        self._update_analysis_actions()
        self._publish_exports()
        return self.run_worker(
            self._deliver_plot(render, filename, download, screen),
            group="plot-delivery", exit_on_error=False,
        )

    async def _deliver_plot(self, render, filename, download, screen):
        message = "Download ready" if download else "Plot ready"
        try:
            content, filename, mime, url = await asyncio.to_thread(
                self._prepare_plot, render, filename, download,
            )
            # Closing a review cancels its pending browser launch.
            if self._closing or (screen is not None and screen not in self.screen_stack):
                return
            if url is not None:
                self.open_url(url, new_tab=True)
            else:
                self.deliver_text(io.StringIO(content), save_filename=filename,
                                  mime_type=mime, encoding="utf-8",
                                  open_method="download" if download else "browser")
        except Exception as error:
            message = f"Plot failed: {error}. Try again."
        finally:
            self._plot_delivery_busy = False
            if not self._closing:
                if screen is not None and screen in self.screen_stack:
                    screen.set_download_status(False, message)
                else:
                    self.query_one("#status-bar", Static).update(message)
                self._update_analysis_actions()
                self._publish_exports()

    def _deliver_svg(self, svg, filename, download=False):
        return self._queue_plot(lambda: svg, filename, download)

    def open_review_plot(self, screen, download=False):
        result = screen.result
        if result is None:
            return None
        tab = screen.query_one("#scan-tabs", a.TabbedContent).active
        if tab == "scan-fit":
            return None
        histogram = tab == "scan-histogram"
        render = event_histogram_svg if histogram else a.dominant_charge_svg
        return self._queue_plot(
            lambda: render(result),
            "event-histogram.svg" if histogram else "dominant-charge.svg",
            download, screen,
        )

    def _finish_charge_scan(self, screen, result, error, traceback_text, advice):
        self._analysis_running = False
        if error is not None or result is None:
            super()._finish_charge_scan(screen, result, error, traceback_text, advice)
            return
        screen.show_result(result, "memory")

    def _open_selected_tic_plot(self):
        datasets = [(path, self.selected_descriptions.get(path) or "", self.tic_states[path].result)
                    for path in self.selected_paths if path in self.tic_states
                    and self.tic_states[path].status == "complete"
                    and self.tic_states[path].result is not None]
        if datasets:
            return self._queue_plot(lambda: combined_chromatograms_svg(datasets), "chromatograms.svg")

    async def on_unmount(self):
        self._closing = True
        self._pending_publication = None
        try:
            await asyncio.to_thread(self._post_browser, {"ready": False, "exports": {}, "clear": True})
        except Exception:
            pass
