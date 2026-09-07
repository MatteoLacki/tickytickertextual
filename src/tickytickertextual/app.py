"""Read-only, ranger-style filesystem navigation with Textual."""

from __future__ import annotations

import argparse
import fcntl
import fnmatch
import os
import stat
import sqlite3
import tempfile
import traceback
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence, TextIO

import numpy as np
from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from tickyticker import charge_regions
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)


class NavigationError(Exception):
    """Raised when a path cannot be safely listed."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """The bounded metadata needed to draw one directory entry."""

    name: str
    path: Path
    is_dir: bool
    is_symlink: bool
    size: int | None
    modified: float | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """One on-demand directory read."""

    path: Path
    entries: tuple[FileEntry, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    """Cached read-only metadata for one Bruker .d dataset."""

    description: str | None
    description_error: str | None
    mz_acquisition_min: float | None
    mz_acquisition_max: float | None
    mz_acquisition_error: str | None
    gradient_length_seconds: float | None
    gradient_length_error: str | None
    analysis_tdf_size: int | None
    analysis_tdf_bin_size: int | None


SETTING_DEFINITIONS = (
    ("mz_min", "Comparison m/z: minimum", float),
    ("mz_max", "Comparison m/z: maximum", float),
    ("min_intensity", "Minimum raw-event intensity", float),
    ("frame_stride", "MS1 frame stride", int),
    ("isotope_count", "Isotope count", int),
    ("mobility_bins", "Mobility bins", int),
    ("mz_bin_width", "Final m/z bin width", float),
    ("border_mz_left", "Border fit: left m/z", float),
    ("border_mz_right", "Border fit: right m/z", float),
    ("threads", "Numba threads", int),
    ("scans_per_mobility_bin", "Scans per mobility bin (0 = all)", int),
)


class ConfigurationError(ValueError):
    """Raised when the algorithm settings TOML is invalid."""


class InstanceAlreadyRunning(RuntimeError):
    """Raised when the process-wide Linux lock is already held."""


@dataclass(frozen=True, slots=True)
class AlgorithmSettings:
    """The configurable charge-regions command-line parameters."""

    mz_min: float = 100.0
    mz_max: float = 1700.0
    min_intensity: float = 30.0
    frame_stride: int = 1
    isotope_count: int = 3
    mobility_bins: int = 100
    mz_bin_width: float = 10.0
    border_mz_left: float = 350.0
    border_mz_right: float = 1200.0
    threads: int = 3
    scans_per_mobility_bin: int = 0

    def validate(self) -> None:
        if not np.isfinite(self.mz_min) or not np.isfinite(self.mz_max):
            raise ConfigurationError("Comparison m/z limits must be finite")
        if self.mz_min >= self.mz_max:
            raise ConfigurationError("Comparison m/z limits must be ordered")
        if self.isotope_count < 1 or self.mobility_bins < 1:
            raise ConfigurationError(
                "Isotope count and mobility bins must be at least 1"
            )
        if self.mz_bin_width <= 0:
            raise ConfigurationError("Final m/z bin width must be positive")
        fine_bin_count = round(self.mz_bin_width * 12.0)
        if not np.isclose(fine_bin_count / 12.0, self.mz_bin_width):
            raise ConfigurationError(
                "Final m/z bin width must be an integer multiple of 1/12 Da"
            )
        if not np.isfinite(self.min_intensity) or self.min_intensity < 0:
            raise ConfigurationError(
                "Minimum intensity must be finite and nonnegative"
            )
        if self.frame_stride < 1 or self.threads < 1:
            raise ConfigurationError("Frame stride and threads must be at least 1")
        if self.scans_per_mobility_bin < 0:
            raise ConfigurationError("Scans per mobility bin cannot be negative")
        if not (
            self.mz_min
            <= self.border_mz_left
            < self.border_mz_right
            <= self.mz_max
        ):
            raise ConfigurationError(
                "Border m/z limits must be ordered inside the comparison range"
            )

    def analysis_arguments(self) -> dict[str, int | float]:
        """Return keyword arguments accepted by tickyticker.analyse()."""
        return {
            name: getattr(self, name)
            for name, _, _ in SETTING_DEFINITIONS
        }


def settings_to_toml(settings: AlgorithmSettings) -> str:
    """Serialize the small numeric configuration without a writer dependency."""
    lines = ["[charge_regions]"]
    for name, _, value_type in SETTING_DEFINITIONS:
        value = getattr(settings, name)
        rendered = str(value) if value_type is int else repr(float(value))
        lines.append(f"{name} = {rendered}")
    return chr(10).join(lines) + chr(10)


def save_algorithm_settings(path: Path, settings: AlgorithmSettings) -> None:
    """Atomically replace the server-side TOML settings file."""
    settings.validate()
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w") as handle:
            handle.write(settings_to_toml(settings))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def load_algorithm_settings(path: Path) -> AlgorithmSettings:
    """Load settings from TOML, creating the defaults on first use."""
    path = path.expanduser()
    if not path.exists():
        settings = AlgorithmSettings()
        save_algorithm_settings(path, settings)
        return settings
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Cannot read settings {path}: {error}") from error
    section = document.get("charge_regions")
    if not isinstance(section, dict):
        raise ConfigurationError(f"Missing [charge_regions] table in {path}")

    defaults = AlgorithmSettings()
    values: dict[str, int | float] = {}
    for name, _, value_type in SETTING_DEFINITIONS:
        raw_value = section.get(name, getattr(defaults, name))
        if isinstance(raw_value, bool):
            raise ConfigurationError(f"{name} must be numeric")
        if value_type is int:
            if not isinstance(raw_value, int):
                raise ConfigurationError(f"{name} must be an integer")
            values[name] = raw_value
        else:
            if not isinstance(raw_value, (int, float)):
                raise ConfigurationError(f"{name} must be numeric")
            values[name] = float(raw_value)
    settings = AlgorithmSettings(**values)
    settings.validate()
    if {"mz_min", "mz_max"} - section.keys():
        save_algorithm_settings(path, settings)
    return settings


class SingleInstanceLock:
    """Hold a Linux advisory lock for the lifetime of the UI process."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._handle: TextIO | None = None

    def __enter__(self) -> SingleInstanceLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise InstanceAlreadyRunning(
                f"Another tickytickertextual instance holds {self.path}"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        handle = self._handle
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._handle = None


@dataclass(frozen=True, slots=True)
class ChargeScanResult:
    """Numerical analysis data adapted for terminal-native rendering."""

    settings: AlgorithmSettings
    intensities: np.ndarray
    histogram: np.ndarray
    charges: np.ndarray
    mz_edges: np.ndarray
    mobility_edges: np.ndarray
    line_data: dict[str, object]
    sampled_scans_per_mobility_bin: np.ndarray
    visited_ms1_frames: int
    runtime_seconds: float
    effective_threads: int


@dataclass(frozen=True, slots=True)
class DatasetTicState:
    """Current below/above-line TIC state for one selected dataset."""

    status: str
    tic_below_line: int | None = None
    tic_above_line: int | None = None
    error: str | None = None


def adapt_charge_scan_result(
    analysis: charge_regions.ChargeRegionResult,
    settings: AlgorithmSettings,
) -> ChargeScanResult:
    """Validate and adapt tickyticker's in-memory result for this UI."""
    if analysis.intensities.ndim != 3 or analysis.intensities.shape[0] != 3:
        raise RuntimeError("Expected intensities with three charge planes")
    if analysis.raw_event_intensity_histogram.shape != (128,):
        raise RuntimeError("Expected a 128-bin raw-event histogram")
    return ChargeScanResult(
        settings,
        analysis.intensities,
        analysis.raw_event_intensity_histogram,
        analysis.charges,
        analysis.mz_edges,
        analysis.mobility_edges,
        analysis.line_data,
        analysis.sampled_scans_per_mobility_bin,
        analysis.visited_ms1_frames,
        analysis.runtime_seconds,
        analysis.effective_threads,
    )


def analysis_error_advice(error: Exception) -> str:
    """Give the user a concrete recovery action for an analysis failure."""
    message = str(error).casefold()
    if "acquisition m/z range" in message:
        return (
            "Choose another complete .d dataset whose analysis.tdf contains "
            "MzAcqRangeLower and MzAcqRangeUpper metadata."
        )
    if (
        "uncensored dominant" in message
        or "both dominant 1+ and 2+ cells" in message
    ):
        return (
            "Choose another .d dataset with stronger charge-1/2 evidence. "
            "Alternatively, click OK, press s, use a smaller MS1 frame stride "
            "and set scans per mobility bin to 0, then retry."
        )
    if "no ms1 frames" in message:
        return (
            "Choose another .d dataset that contains MS1 survey frames; this "
            "analysis cannot run on a dataset without them."
        )
    if "effectively parallel" in message or "separator is vertical" in message:
        return (
            "The charge axes do not define a stable separator. Choose another "
            ".d dataset, or click OK and adjust the border m/z range in settings."
        )
    if isinstance(error, FileNotFoundError):
        return (
            "The dataset disappeared or is incomplete. Refresh the browser pane "
            "and choose an existing .d dataset."
        )
    if isinstance(error, PermissionError):
        return (
            "The server cannot read this dataset. Ask the server administrator "
            "to grant read access, then retry."
        )
    if isinstance(error, MemoryError):
        return (
            "Reduce the mobility-bin count, or choose a dataset acquired over a "
            "narrower m/z range, then retry the analysis."
        )
    if isinstance(error, ValueError):
        return (
            "Click OK, press s, review the algorithm settings highlighted by "
            "the error, and retry."
        )
    return (
        "Click OK and try another .d dataset. If the failure repeats, keep this "
        "traceback and report it to the server administrator."
    )


def _block_sum(values: np.ndarray, rows: int, columns: int) -> np.ndarray:
    """Sum a 2-D array into a terminal-sized rectangular grid."""
    rows = max(1, min(rows, values.shape[0]))
    columns = max(1, min(columns, values.shape[1]))
    row_edges = np.linspace(0, values.shape[0], rows + 1, dtype=int)
    column_edges = np.linspace(0, values.shape[1], columns + 1, dtype=int)
    reduced = np.zeros((rows, columns), dtype=np.float64)
    for row in range(rows):
        for column in range(columns):
            reduced[row, column] = values[
                row_edges[row] : row_edges[row + 1],
                column_edges[column] : column_edges[column + 1],
            ].sum(dtype=np.float64)
    return reduced


ASCII_LEVELS = " .:-=+*#%@"

CHARGE_STYLES = {
    0: "dim #484f58",
    1: "bold #4c78a8",
    2: "bold #f58518",
    3: "bold #54a24b",
    "X": "bold #ffffff on #6e7681",
}


def dominant_charge_text(
    result: ChargeScanResult, width: int, height: int
) -> Text:
    """Render dominant charge cells with the notebook separator overlaid."""
    plot_columns = max(8, min(width - 8, result.intensities.shape[2], 180))
    plot_rows = max(2, min(height - 3, result.intensities.shape[1], 60))
    charge_planes = np.stack(
        [
            _block_sum(result.intensities[index], plot_rows, plot_columns)
            for index in range(3)
        ]
    )
    maxima = charge_planes.max(axis=0)
    dominant = 3 - np.argmax(charge_planes[::-1], axis=0)
    dominant = np.where(maxima == 0, 0, dominant).astype(np.uint8)
    logged = np.log1p(maxima)
    positive = logged[logged > 0]
    scale = float(np.quantile(positive, 0.99)) if positive.size else 1.0
    levels = np.floor(
        np.clip(logged / max(scale, 1.0), 0.0, 1.0)
        * (len(ASCII_LEVELS) - 1)
    ).astype(np.uint8)
    levels[(maxima > 0) & (levels == 0)] = 1
    canvas: list[list[tuple[str, int | str]]] = [
        [
            (ASCII_LEVELS[int(level)], int(charge))
            for level, charge in zip(level_row, charge_row, strict=True)
        ]
        for level_row, charge_row in zip(
            levels[::-1], dominant[::-1], strict=True
        )
    ]

    line = result.line_data.get("line")
    if isinstance(line, dict):
        try:
            intercept = float(line["intercept"])
            slope = float(line["slope"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            mobility_min = float(result.mobility_edges[0])
            mobility_max = float(result.mobility_edges[-1])
            mz_min = float(result.mz_edges[0])
            mz_max = float(result.mz_edges[-1])
            if np.isfinite(intercept) and np.isfinite(slope):
                for column in range(plot_columns):
                    mz = mz_min + (column + 0.5) / plot_columns * (mz_max - mz_min)
                    mobility = intercept + slope * mz
                    fraction = (mobility_max - mobility) / (mobility_max - mobility_min)
                    row = round(fraction * (plot_rows - 1))
                    if 0 <= row < plot_rows:
                        canvas[row][column] = ("X", "X")

    output = Text(no_wrap=True)
    output.append(
        "dominant charge: blue=1  orange=2  green=3  .:-=+*#%@=log intensity  X=border",
        style="bold",
    )
    output.append(chr(10))
    tick_rows = {0, plot_rows // 2, plot_rows - 1}
    mobility_ticks = np.linspace(
        result.mobility_edges[-1], result.mobility_edges[0], plot_rows
    )
    for row, values in enumerate(canvas):
        if row in tick_rows:
            output.append(f"{mobility_ticks[row]:6.3f} ", style="dim")
        else:
            output.append("       ")
        for character, charge in values:
            output.append(character, style=CHARGE_STYLES[charge])
        output.append(chr(10))
    left = f"{result.mz_edges[0]:g}"
    right = f"{result.mz_edges[-1]:g}"
    middle = "m/z"
    gap = max(1, plot_columns - len(left) - len(middle) - len(right))
    left_gap = gap // 2
    right_gap = gap - left_gap
    output.append(
        "       " + left + " " * left_gap + middle + " " * right_gap + right,
        style="dim",
    )
    return output


def dominant_charge_svg(result: ChargeScanResult) -> str:
    """Build a scalable, full-grid dominant-charge view with its separator."""
    charge_planes = result.intensities
    maxima = charge_planes.max(axis=0)
    dominant = 3 - np.argmax(charge_planes[::-1], axis=0)
    dominant = np.where(maxima == 0, 0, dominant).astype(np.uint8)
    logged = np.log1p(maxima)
    positive = logged[logged > 0]
    scale = float(np.quantile(positive, 0.99)) if positive.size else 1.0

    width, height = 1600, 1000
    left, top, plot_width, plot_height = 100, 70, 1260, 820
    rows, columns = maxima.shape
    cell_width = plot_width / columns
    cell_height = plot_height / rows
    colours = {1: "#4c78a8", 2: "#f58518", 3: "#54a24b"}
    elements = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">'
        ),
        '<rect width="100%" height="100%" fill="#0d1117"/>',
        '<defs><clipPath id="plot"><rect x="100" y="70" '
        'width="1260" height="820"/></clipPath></defs>',
        '<g clip-path="url(#plot)">',
        (
            f'<rect x="{left}" y="{top}" width="{plot_width}" '
            f'height="{plot_height}" fill="#161b22"/>'
        ),
    ]
    for row in range(rows):
        svg_y = top + (rows - row - 1) * cell_height
        for column in range(columns):
            charge = int(dominant[row, column])
            if charge == 0:
                continue
            opacity = 0.18 + 0.82 * min(
                1.0, float(logged[row, column]) / max(scale, 1.0)
            )
            elements.append(
                f'<rect x="{left + column * cell_width:.3f}" '
                f'y="{svg_y:.3f}" width="{cell_width + 0.04:.3f}" '
                f'height="{cell_height + 0.04:.3f}" '
                f'fill="{colours[charge]}" opacity="{opacity:.3f}"/>'
            )

    line = result.line_data.get("line")
    if isinstance(line, dict):
        try:
            intercept = float(line["intercept"])
            slope = float(line["slope"])
        except (KeyError, TypeError, ValueError):
            pass
        else:
            mobility_min = float(result.mobility_edges[0])
            mobility_max = float(result.mobility_edges[-1])
            mz_min = float(result.mz_edges[0])
            mz_max = float(result.mz_edges[-1])

            def svg_y(mz: float) -> float:
                mobility = intercept + slope * mz
                return top + (
                    (mobility_max - mobility)
                    / (mobility_max - mobility_min)
                    * plot_height
                )

            elements.append(
                f'<line x1="{left}" y1="{svg_y(mz_min):.3f}" '
                f'x2="{left + plot_width}" y2="{svg_y(mz_max):.3f}" '
                'stroke="#ffffff" stroke-width="4"/>'
            )
    elements.extend(
        [
            "</g>",
            (
                f'<rect x="{left}" y="{top}" width="{plot_width}" '
                f'height="{plot_height}" fill="none" stroke="#8b949e" '
                'stroke-width="2"/>'
            ),
            '<text x="800" y="38" fill="#f0f6fc" font-size="28" '
            'text-anchor="middle" font-family="monospace">'
            "Dominant charge and fitted 1+/multicharge separator</text>",
            (
                f'<text x="{left}" y="930" fill="#c9d1d9" font-size="22" '
                f'font-family="monospace">{result.mz_edges[0]:g}</text>'
            ),
            (
                f'<text x="{left + plot_width}" y="930" fill="#c9d1d9" '
                f'font-size="22" text-anchor="end" font-family="monospace">'
                f'{result.mz_edges[-1]:g}</text>'
            ),
            '<text x="730" y="965" fill="#c9d1d9" font-size="24" '
            'text-anchor="middle" font-family="monospace">m/z</text>',
            (
                f'<text x="82" y="{top + plot_height}" fill="#c9d1d9" '
                f'font-size="20" text-anchor="end" font-family="monospace">'
                f'{result.mobility_edges[0]:.3f}</text>'
            ),
            (
                f'<text x="82" y="{top + 18}" fill="#c9d1d9" '
                f'font-size="20" text-anchor="end" font-family="monospace">'
                f'{result.mobility_edges[-1]:.3f}</text>'
            ),
            '<text x="32" y="480" fill="#c9d1d9" font-size="24" '
            'text-anchor="middle" font-family="monospace" '
            'transform="rotate(-90 32 480)">inverse ion mobility (1/K0)</text>',
            '<rect x="1400" y="160" width="28" height="28" fill="#4c78a8"/>',
            '<text x="1440" y="183" fill="#f0f6fc" font-size="22" '
            'font-family="monospace">charge 1</text>',
            '<rect x="1400" y="210" width="28" height="28" fill="#f58518"/>',
            '<text x="1440" y="233" fill="#f0f6fc" font-size="22" '
            'font-family="monospace">charge 2</text>',
            '<rect x="1400" y="260" width="28" height="28" fill="#54a24b"/>',
            '<text x="1440" y="283" fill="#f0f6fc" font-size="22" '
            'font-family="monospace">charge 3</text>',
            '<line x1="1400" y1="330" x2="1428" y2="330" '
            'stroke="#ffffff" stroke-width="4"/>',
            '<text x="1440" y="338" fill="#f0f6fc" font-size="22" '
            'font-family="monospace">separator</text>',
            "</svg>",
        ]
    )
    return "\n".join(elements) + "\n"


def write_dominant_charge_svg(
    result: ChargeScanResult, directory: Path
) -> Path:
    """Atomically create a uniquely named SVG for browser viewing."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix="dominant-charge-", suffix=".svg", dir=directory, text=True
    )
    with os.fdopen(descriptor, "w") as handle:
        handle.write(dominant_charge_svg(result))
        handle.flush()
        os.fsync(handle.fileno())
    return Path(name)


def event_histogram_text(
    result: ChargeScanResult, width: int, height: int
) -> Text:
    """Render a compact vertical log histogram, retaining one or two bins."""
    available_columns = max(8, width - 8)
    if available_columns >= 128:
        group_size = 1
    elif available_columns >= 64:
        group_size = 2
    else:
        group_size = int(np.ceil(128 / available_columns))
    grouped = np.array(
        [
            result.histogram[start : min(start + group_size, 128)].sum(
                dtype=np.uint64
            )
            for start in range(0, 128, group_size)
        ],
        dtype=np.uint64,
    )
    logarithms = np.log1p(grouped.astype(np.float64))
    maximum = float(logarithms.max()) if logarithms.size else 0.0
    plot_rows = max(4, min(height - 4, 10))
    heights = (
        np.zeros(grouped.size, dtype=int)
        if maximum == 0
        else np.rint(logarithms / maximum * plot_rows).astype(int)
    )
    threshold_bin = min(
        127, max(0, int(np.ceil(result.settings.min_intensity)) - 1)
    )
    threshold_column = threshold_bin // group_size
    output = Text(no_wrap=True)
    output.append(
        "raw MS1 events · log count · "
        f"{group_size} intensity bin{'s' if group_size > 1 else ''}/column · "
        f"minimum={result.settings.min_intensity:g} (*)",
        style="bold",
    )
    output.append(chr(10))
    for row in range(plot_rows, 0, -1):
        output.append("  |", style="dim")
        for column, bar_height in enumerate(heights):
            character = "#" if bar_height >= row else " "
            style = "bold #f2cc60" if column == threshold_column else "bold #4c78a8"
            output.append(character, style=style)
        output.append(chr(10))
    output.append("  +" + "-" * grouped.size, style="dim")
    output.append(chr(10))
    output.append(
        f"   1{' ' * max(1, grouped.size - 6)}>=128",
        style="dim",
    )
    return output


class FileSystemNavigator:
    """Read directories on demand while remaining inside a fixed root."""

    def __init__(self, root: str | os.PathLike[str], *, show_hidden: bool = False):
        try:
            resolved_root = Path(root).expanduser().resolve(strict=True)
        except OSError as error:
            raise NavigationError(f"Cannot open root {root!s}: {error}") from error
        if not resolved_root.is_dir():
            raise NavigationError(f"Root is not a directory: {resolved_root}")

        self.root = resolved_root
        self.current = resolved_root
        self.show_hidden = show_hidden

    def scan(
        self,
        path: str | os.PathLike[str],
        *,
        limit: int | None = None,
        directories_only: bool = False,
    ) -> DirectoryListing:
        """Read one directory without recursion or persistent caching."""
        directory = self._checked_directory(path)
        entries: list[FileEntry] = []
        truncated = False

        try:
            with os.scandir(directory) as iterator:
                for raw_entry in iterator:
                    if not self.show_hidden and raw_entry.name.startswith("."):
                        continue
                    entry = self._make_entry(raw_entry)
                    if directories_only and not entry.is_dir:
                        continue
                    if limit is not None and len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(entry)
        except OSError as error:
            raise NavigationError(f"Cannot read {directory}: {error}") from error

        entries.sort(
            key=lambda entry: (
                (
                    0
                    if entry.is_dir and not entry.name.casefold().endswith(".d")
                    else 1
                    if entry.is_dir
                    else 2
                ),
                entry.name.casefold(),
                entry.name,
            )
        )
        return DirectoryListing(directory, tuple(entries), truncated)

    def change_directory(
        self,
        path: str | os.PathLike[str],
        *,
        directories_only: bool = False,
    ) -> DirectoryListing:
        """Validate and enter a directory, returning its fresh listing."""
        listing = self.scan(path, directories_only=directories_only)
        self.current = listing.path
        return listing

    def parent(self) -> Path:
        """Return the parent bounded by the configured root."""
        if self.current == self.root:
            return self.root
        return self.current.parent

    def _checked_directory(self, path: str | os.PathLike[str]) -> Path:
        candidate = Path(path)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise NavigationError(f"Cannot resolve {candidate}: {error}") from error

        if not resolved.is_relative_to(self.root):
            raise NavigationError(f"Path is outside configured root: {resolved}")
        if not resolved.is_dir():
            raise NavigationError(f"Path is not a directory: {resolved}")
        return resolved

    @staticmethod
    def _make_entry(raw_entry: os.DirEntry[str]) -> FileEntry:
        try:
            metadata = raw_entry.stat(follow_symlinks=False)
        except OSError:
            size: int | None = None
            modified: float | None = None
            mode: int | None = None
        else:
            size = metadata.st_size
            modified = metadata.st_mtime
            mode = metadata.st_mode

        return FileEntry(
            name=raw_entry.name,
            path=Path(raw_entry.path),
            is_dir=raw_entry.is_dir(follow_symlinks=False),
            is_symlink=raw_entry.is_symlink(),
            size=size,
            modified=modified,
            mode=mode,
        )


def format_size(size: int | None) -> str:
    """Format a byte count for the metadata pane."""
    if size is None:
        return "unknown"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def _read_dataset_metadata(dataset_path: Path) -> DatasetMetadata:
    """Read and validate cached metadata through one read-only connection."""
    database = dataset_path / "analysis.tdf"
    analysis_tdf_size = _file_size(database)
    analysis_tdf_bin_size = _file_size(dataset_path / "analysis.tdf_bin")

    def unavailable(error: str) -> DatasetMetadata:
        return DatasetMetadata(
            description=None,
            description_error=error,
            mz_acquisition_min=None,
            mz_acquisition_max=None,
            mz_acquisition_error=error,
            gradient_length_seconds=None,
            gradient_length_error=error,
            analysis_tdf_size=analysis_tdf_size,
            analysis_tdf_bin_size=analysis_tdf_bin_size,
        )

    if not database.is_file():
        return unavailable("analysis.tdf not found")
    try:
        uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            rows = connection.execute(
                "SELECT \"Key\", \"Value\" FROM \"GlobalMetadata\" "
                "WHERE \"Key\" IN (?, ?, ?)",
                ("Description", "MzAcqRangeLower", "MzAcqRangeUpper"),
            ).fetchall()
            try:
                gradient_row = connection.execute(
                    'SELECT MAX("Time") - MIN("Time") FROM "Frames"'
                ).fetchone()
            except sqlite3.Error as error:
                raw_gradient_length = None
                gradient_error = f"Frames.Time SQLite error: {error}"
            else:
                raw_gradient_length = (
                    gradient_row[0] if gradient_row is not None else None
                )
                gradient_error = None
    except OSError as error:
        return unavailable(f"filesystem error: {error}")
    except sqlite3.Error as error:
        return unavailable(f"SQLite error: {error}")

    values = {str(key): value for key, value in rows}
    raw_description = values.get("Description")
    description = (
        str(raw_description).strip() if raw_description is not None else None
    )
    if raw_description is None:
        description_error = "GlobalMetadata.Description not found"
    elif not description:
        description_error = "GlobalMetadata.Description is empty"
        description = None
    else:
        description_error = None

    raw_mz_min = values.get("MzAcqRangeLower")
    raw_mz_max = values.get("MzAcqRangeUpper")
    try:
        mz_min = float(raw_mz_min)
        mz_max = float(raw_mz_max)
    except (TypeError, ValueError):
        mz_min = mz_max = None
        mz_error = "GlobalMetadata acquisition m/z range not found"
    else:
        if not np.isfinite(mz_min) or not np.isfinite(mz_max) or mz_min >= mz_max:
            mz_min = mz_max = None
            mz_error = "GlobalMetadata acquisition m/z range is invalid"
        else:
            mz_error = None

    try:
        gradient_length = float(raw_gradient_length)
    except (TypeError, ValueError):
        gradient_length = None
        if gradient_error is None:
            gradient_error = "Frames.Time retention span not found"
    else:
        if not np.isfinite(gradient_length) or gradient_length < 0:
            gradient_length = None
            gradient_error = "Frames.Time retention span is invalid"

    return DatasetMetadata(
        description=description,
        description_error=description_error,
        mz_acquisition_min=mz_min,
        mz_acquisition_max=mz_max,
        mz_acquisition_error=mz_error,
        gradient_length_seconds=gradient_length,
        gradient_length_error=gradient_error,
        analysis_tdf_size=analysis_tdf_size,
        analysis_tdf_bin_size=analysis_tdf_bin_size,
    )


def read_dataset_description(dataset_path: Path) -> str | None:
    """Read a dataset Description from analysis.tdf without modifying it."""
    return _read_dataset_metadata(dataset_path).description


def _dataset_has_analysis_pair(dataset_path: Path) -> bool:
    """Return whether a .d directory has both standard analysis files."""
    return all(
        (dataset_path / filename).is_file()
        for filename in ("analysis.tdf", "analysis.tdf_bin")
    )


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _dataset_metadata_text(
    dataset_path: Path, metadata: DatasetMetadata
) -> Text:
    """Render a compact .d summary instead of listing its internal files."""
    output = Text()
    output.append(dataset_path.name, style="bold cyan")
    output.append("/\n\n", style="cyan")
    output.append("Description       ", style="dim")
    if metadata.description is not None:
        output.append(metadata.description, style="#f0f6fc")
    else:
        output.append(
            f"unavailable ({metadata.description_error or 'unknown error'})",
            style="italic #ff7b72",
        )
    output.append("\nAcquisition m/z   ", style="dim")
    if metadata.mz_acquisition_error is None:
        output.append(_acquisition_mz_text(metadata), style="#f2cc60")
    else:
        output.append(
            f"unavailable ({metadata.mz_acquisition_error or 'unknown error'})",
            style="italic #ff7b72",
        )
    output.append("\nGradient length   ", style="dim")
    if metadata.gradient_length_error is None:
        output.append(_gradient_length_text(metadata), style="#54a24b")
    else:
        output.append(
            f"unavailable ({metadata.gradient_length_error or 'unknown error'})",
            style="italic #ff7b72",
        )
    output.append("\nanalysis.tdf      ", style="dim")
    output.append(format_size(metadata.analysis_tdf_size), style="#8be9fd")
    output.append("\nanalysis.tdf_bin  ", style="dim")
    output.append(format_size(metadata.analysis_tdf_bin_size), style="#8be9fd")
    return output


def _acquisition_mz_text(metadata: DatasetMetadata) -> str:
    """Format a previously validated acquisition range."""
    if (
        metadata.mz_acquisition_min is None
        or metadata.mz_acquisition_max is None
    ):
        return "unavailable"
    return f"{metadata.mz_acquisition_min:g} – {metadata.mz_acquisition_max:g}"


def _gradient_length_text(metadata: DatasetMetadata) -> str:
    """Format the cached Frames.Time span as hours, minutes, and seconds."""
    if metadata.gradient_length_seconds is None:
        return "unavailable"
    total_seconds = max(0, int(round(metadata.gradient_length_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m{seconds:02d}s"


def _column_cell(value: str, width: int) -> str:
    """Pad or ellipsize a string to an exact terminal-cell column width."""
    if len(value) > width:
        return value[: max(0, width - 1)] + "…"
    return value.ljust(width)


def _column_width(
    values: Iterable[str], *, minimum: int, maximum: int | None = None
) -> int:
    """Choose one stable width for a collection of row values."""
    width = max((len(value) for value in values), default=minimum)
    width = max(minimum, width)
    return min(width, maximum) if maximum is not None else width


def _matches_name_filter(name: str, pattern: str | None) -> bool:
    """Match a name against a case-insensitive shell-style glob."""
    if not pattern:
        return True
    return fnmatch.fnmatchcase(name.casefold(), pattern.casefold())


def _entry_label(
    entry: FileEntry,
    *,
    marked: bool = False,
    metadata: DatasetMetadata | None = None,
    name_width: int | None = None,
) -> Text:
    label = Text(no_wrap=True, overflow="ellipsis")
    if entry.is_dir:
        label.append(
            "✓ " if marked else "▸ ",
            style="bold green" if marked else "bold cyan",
        )
        directory_name = f"{entry.name}/"
        label.append(
            _column_cell(directory_name, name_width)
            if name_width is not None
            else directory_name,
            style="bold cyan",
        )
    elif entry.is_symlink:
        label.append("↗ ", style="magenta")
        symlink_name = f"{entry.name}@"
        label.append(
            _column_cell(symlink_name, name_width)
            if name_width is not None
            else symlink_name,
            style="magenta",
        )
    else:
        label.append("  ")
        label.append(
            _column_cell(entry.name, name_width)
            if name_width is not None
            else entry.name
        )
    if name_width is not None:
        label.append(" │ ", style="dim")
        if entry.is_dir and entry.name.casefold().endswith(".d"):
            label.append("Gradient ", style="dim italic")
            if metadata is None:
                label.append("…", style="dim")
            elif metadata.gradient_length_error is None:
                label.append(
                    _gradient_length_text(metadata), style="bold #54a24b"
                )
            else:
                label.append("unavailable", style="italic #ff7b72")
    return label


def _listing_text(
    entries: Iterable[FileEntry],
    *,
    selected_name: str | None = None,
    truncated: bool = False,
) -> Text:
    output = Text()
    for entry in entries:
        line = _entry_label(entry)
        if entry.name == selected_name:
            line.stylize("reverse bold")
        output.append_text(line)
        output.append("\n")
    if truncated:
        output.append("… more entries not read", style="dim italic")
    return output


class CurrentOptionList(OptionList):
    """Filesystem list with bindings advertised only while it has focus."""

    BINDINGS = [
        Binding("j,down", "app.cursor_down", "Down", key_display="j/↓"),
        Binding("k,up", "app.cursor_up", "Up", key_display="k/↑"),
        Binding("space", "app.select_dataset", "Select .d"),
        Binding("l,right,enter", "app.open_selected", "Open", key_display="l/→/Enter"),
        Binding("h,left,backspace", "app.go_parent", "Parent", key_display="h/←/⌫"),
        Binding("g", "app.first", "First"),
        Binding("G,shift+g", "app.last", "Last", key_display="G"),
        Binding(
            "ctrl+down",
            "app.focus_selected_pane",
            "Lower pane",
            key_display="Ctrl+↓",
        ),
        Binding(".", "app.toggle_hidden", "Hidden"),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "app.toggle_folders_only",
            "Folders only",
            key_display="Ctrl+.",
        ),
        Binding("/", "app.show_filter", "Filter"),
        Binding("r", "app.reload", "Reload"),
        Binding("s", "app.show_settings", "Settings"),
        Binding("H,shift+h", "app.show_help", "Help", key_display="Shift+H"),
    ]


class SelectedOptionList(OptionList):
    """Option list with a clickable remove control on each row."""

    BINDINGS = [
        Binding("j,down", "app.cursor_down", "Down", key_display="j/↓"),
        Binding("k,up", "app.cursor_up", "Up", key_display="k/↑"),
        Binding("space", "app.select_dataset", "Toggle HeLa"),
        Binding("x", "app.remove_selected", "Remove"),
        Binding("g", "app.first", "First"),
        Binding("G,shift+g", "app.last", "Last", key_display="G"),
        Binding(
            "h,ctrl+up",
            "app.focus_current_pane",
            "Upper pane",
            key_display="h/Ctrl+↑",
        ),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "app.toggle_folders_only",
            "Folders only",
            key_display="Ctrl+.",
        ),
        Binding("s", "app.show_settings", "Settings"),
        Binding("H,shift+h", "app.show_help", "Help", key_display="Shift+H"),
    ]

    class RemoveRequested(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    async def _on_click(self, event: events.Click) -> None:
        remove_index = event.style.meta.get("remove-selected")
        if isinstance(remove_index, int):
            self.highlighted = remove_index
            event.stop()
            self.post_message(self.RemoveRequested(remove_index))
            return
        await super()._on_click(event)


def _selected_description(
    description: str | None, description_error: str | None
) -> str:
    """Return the display text for a cached dataset Description."""
    return description or f"unavailable ({description_error or 'unknown error'})"


def _relative_tic(value: int, reference: int | None, *, chosen: bool) -> str:
    """Format a TIC value relative to the chosen HeLa reference."""
    if chosen and reference is not None:
        return "100.0%"
    if reference is None or reference == 0:
        return "n/a"
    return f"{100.0 * value / reference:.1f}%"


def _selected_tic_cells(
    tic_state: DatasetTicState | None,
    hela_tic_state: DatasetTicState | None,
    *,
    chosen: bool,
) -> tuple[str, str]:
    """Return stable below/above column values for one selected dataset."""
    if tic_state is None:
        return "—", "—"
    if tic_state.status in {"queued", "running"}:
        value = f"{tic_state.status}…"
        return value, value
    if tic_state.status == "error":
        return "ERROR", "ERROR"
    if (
        tic_state.tic_below_line is None
        or tic_state.tic_above_line is None
    ):
        return "—", "—"
    below_reference = (
        hela_tic_state.tic_below_line
        if hela_tic_state is not None
        else None
    )
    above_reference = (
        hela_tic_state.tic_above_line
        if hela_tic_state is not None
        else None
    )
    below_relative = _relative_tic(
        tic_state.tic_below_line, below_reference, chosen=chosen
    )
    above_relative = _relative_tic(
        tic_state.tic_above_line, above_reference, chosen=chosen
    )
    return (
        f"{tic_state.tic_below_line:,} ({below_relative} HeLa)",
        f"{tic_state.tic_above_line:,} ({above_relative} HeLa)",
    )


def _selected_path_label(
    path: Path,
    root: Path,
    index: int,
    *,
    chosen: bool,
    description: str | None,
    description_error: str | None,
    fitted_line: tuple[float, float] | None,
    tic_state: DatasetTicState | None,
    hela_tic_state: DatasetTicState | None,
    path_width: int,
    description_width: int,
    below_width: int,
    above_width: int,
) -> Text:
    relative_path = str(path.relative_to(root))
    description_text = _selected_description(description, description_error)
    below_text, above_text = _selected_tic_cells(
        tic_state, hela_tic_state, chosen=chosen
    )
    label = Text(no_wrap=True, overflow="ellipsis")
    label.append(
        " × ",
        style=Style(
            color="#ff7b72",
            bold=True,
            meta={"remove-selected": index},
        ),
    )
    label.append("★ " if chosen else "  ", style="bold yellow" if chosen else "dim")
    label.append(
        _column_cell(relative_path, path_width),
        style="bold yellow" if chosen else "#c9d1d9",
    )
    label.append(" │ ", style="dim")
    label.append(
        _column_cell(description_text, description_width),
        style="bold yellow" if chosen else "italic #8b949e",
    )
    label.append(" │ Below ", style="dim italic")
    tic_style = "bold #ff7b72" if below_text == "ERROR" else "bold #8be9fd"
    label.append(_column_cell(below_text, below_width), style=tic_style)
    label.append(" │ Above ", style="dim italic")
    tic_style = "bold #ff7b72" if above_text == "ERROR" else "bold #8be9fd"
    label.append(_column_cell(above_text, above_width), style=tic_style)
    if fitted_line is not None:
        intercept, slope = fitted_line
        label.append(" │ Fit ", style="dim italic")
        label.append(
            f"1/K0 = {intercept:.6g} {slope:+.6g}×m/z",
            style="bold #f2cc60",
        )
    if chosen:
        label.stylize("on #3d3200")
    return label


class AnalysisPlot(Static):
    """A resize-aware terminal-native analysis plot."""

    def __init__(self, mode: str, *, id: str) -> None:
        super().__init__("", id=id, markup=False)
        self.mode = mode
        self.result: ChargeScanResult | None = None

    def show_result(self, result: ChargeScanResult) -> None:
        self.result = result
        self._redraw()
        self.call_after_refresh(self._redraw)

    def on_resize(self, event: events.Resize) -> None:
        self._redraw(event.size.width, event.size.height)

    def _redraw(
        self, width: int | None = None, height: int | None = None
    ) -> None:
        if self.result is None:
            self.update(Text("waiting for analysis data", style="dim italic"))
            return
        plot_width = width if width is not None else self.size.width
        plot_height = height if height is not None else self.size.height
        if self.mode == "dominant":
            rendered = dominant_charge_text(
                self.result, plot_width, plot_height
            )
        else:
            rendered = event_histogram_text(
                self.result, plot_width, plot_height
            )
        self.update(rendered)


def fit_parameters_text(result: ChargeScanResult) -> Text:
    """Render only the accepted separator equation and its fitted parameters."""
    line = result.line_data.get("line")
    output = Text()
    if not isinstance(line, dict):
        return Text("Fitted line parameters are unavailable.", style="bold red")
    try:
        intercept = float(line["intercept"])
        slope = float(line["slope"])
    except (KeyError, TypeError, ValueError):
        return Text("Fitted line parameters are invalid.", style="bold red")
    output.append("Fitted 1+/multicharge separator\n\n", style="bold #8be9fd")
    output.append("1/K0 = intercept + slope × m/z\n\n", style="bold #f0f6fc")
    output.append("intercept  ", style="dim")
    output.append(f"{intercept:.12g}\n", style="bold #f2cc60")
    output.append("slope      ", style="dim")
    output.append(f"{slope:.12g}", style="bold #f2cc60")
    return output


class ChargeScanScreen(ModalScreen[ChargeScanResult | None]):
    """Confirm, run, and review one charge-region fit in a large modal."""

    class ScanRequested(Message):
        def __init__(self, screen: ChargeScanScreen) -> None:
            super().__init__()
            self.screen = screen

    CSS = """
    ChargeScanScreen {
        align: center middle;
        background: #000000 78%;
    }

    #scan-dialog {
        width: 98%;
        height: 96%;
        padding: 1;
        border: round #f2cc60;
        background: #161b22;
    }

    #scan-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #f2cc60;
    }

    #scan-question {
        height: 1fr;
        padding: 1 3;
    }

    #scan-loading {
        display: none;
        height: 1fr;
        content-align: center middle;
        color: #f2cc60;
        text-style: bold;
    }

    #scan-tabs {
        display: none;
        height: 1fr;
    }

    #scan-tabs TabPane {
        height: 1fr;
        padding: 0 1;
        overflow: hidden;
    }

    #scan-fit-parameters {
        height: 1fr;
        padding: 2 4;
        content-align: center middle;
    }

    #scan-histogram-plot {
        height: 16;
        content-align: center middle;
    }

    #scan-dominant-plot {
        height: 1fr;
        content-align: center middle;
    }

    #scan-buttons {
        height: 3;
        align-horizontal: right;
    }

    #scan-buttons Button {
        width: 18;
        margin-left: 1;
    }

    #scan-svg {
        display: none;
    }
    """

    BINDINGS = [
        Binding("escape,n", "decline", "No", show=False),
        Binding("y", "confirm", "Yes", show=False),
    ]

    def __init__(
        self,
        dataset: Path,
        *,
        settings: AlgorithmSettings,
        metadata: DatasetMetadata,
    ) -> None:
        super().__init__()
        self.dataset = dataset
        self.settings = settings
        self.metadata = metadata
        self.state = "confirm"
        self.result: ChargeScanResult | None = None
        self.svg_url: str | None = None
        self._loading_step = 0

    def compose(self) -> ComposeResult:
        question = (
            f"HeLa: {self.dataset}\n\n"
            "The scan can take a while and will run in a worker thread.\n"
            "Results stay in memory; no analysis artifacts will be written.\n\n"
            f"comparison m/z={self.settings.mz_min:g}–{self.settings.mz_max:g}, "
            f"dataset acquisition m/z={_acquisition_mz_text(self.metadata)}\n"
            f"isotopes={self.settings.isotope_count}, "
            f"bin={self.settings.mz_bin_width:g}, "
            f"minimum={self.settings.min_intensity:g}, "
            f"stride={self.settings.frame_stride}"
        )
        with Container(id="scan-dialog"):
            yield Static("Scan this folder for charge areas?", id="scan-title")
            yield Static(question, id="scan-question", markup=False)
            yield Static(id="scan-loading", markup=False)
            with TabbedContent(initial="scan-dominant", id="scan-tabs"):
                with TabPane("Dominant + border", id="scan-dominant"):
                    yield AnalysisPlot("dominant", id="scan-dominant-plot")
                with TabPane("Event histogram", id="scan-histogram"):
                    yield AnalysisPlot("histogram", id="scan-histogram-plot")
                with TabPane("Fit parameters", id="scan-fit"):
                    yield Static(id="scan-fit-parameters", markup=False)
            with Horizontal(id="scan-buttons"):
                yield Button("Open hi-res SVG", id="scan-svg")
                yield Button("Reject", id="scan-no", variant="error")
                yield Button("Calculate", id="scan-yes", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#scan-no", Button).focus()
        self._loading_timer = self.set_interval(
            0.4, self._animate_loading, pause=True
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-svg":
            if self.svg_url is not None:
                self.app.open_url(self.svg_url, new_tab=True)
            return
        if event.button.id == "scan-no":
            self.action_decline()
        elif event.button.id == "scan-yes":
            self.action_confirm()

    def action_decline(self) -> None:
        if self.state != "loading":
            self.dismiss(None)

    def action_confirm(self) -> None:
        if self.state == "confirm":
            self.show_loading()
            self.post_message(self.ScanRequested(self))
        elif self.state == "review" and self.result is not None:
            self.dismiss(self.result)

    def show_loading(self) -> None:
        self.state = "loading"
        self.query_one("#scan-question", Static).styles.display = "none"
        self.query_one("#scan-loading", Static).styles.display = "block"
        self.query_one("#scan-yes", Button).disabled = True
        self.query_one("#scan-no", Button).disabled = True
        self._loading_timer.resume()
        self.set_progress("calling tickyticker.analyse() directly")

    def _animate_loading(self) -> None:
        if self.state != "loading":
            return
        self._loading_step = (self._loading_step + 1) % 4
        current = str(self.query_one("#scan-loading", Static).render()).split(
            "\n", 1
        )[-1]
        self.query_one("#scan-loading", Static).update(
            f"loading charge areas{'.' * self._loading_step}\n{current}"
        )

    def set_progress(self, progress: str) -> None:
        self.query_one("#scan-loading", Static).update(
            f"loading charge areas{'.' * self._loading_step}\n{progress}"
        )

    def show_result(
        self, result: ChargeScanResult, svg_url: str | None
    ) -> None:
        self.state = "review"
        self.result = result
        self.svg_url = svg_url
        self._loading_timer.pause()
        self.query_one("#scan-loading", Static).styles.display = "none"
        tabs = self.query_one("#scan-tabs", TabbedContent)
        tabs.styles.display = "block"
        tabs.active = "scan-dominant"
        self.query_one("#scan-dominant-plot", AnalysisPlot).show_result(result)
        self.query_one("#scan-histogram-plot", AnalysisPlot).show_result(result)
        self.query_one("#scan-fit-parameters", Static).update(
            fit_parameters_text(result)
        )
        self.query_one("#scan-title", Static).update(
            "Review the fitted 1+/multicharge separator"
        )
        svg_button = self.query_one("#scan-svg", Button)
        svg_button.styles.display = "block"
        svg_button.disabled = svg_url is None
        if svg_url is None:
            svg_button.label = "SVG unavailable"
        reject = self.query_one("#scan-no", Button)
        accept = self.query_one("#scan-yes", Button)
        reject.disabled = False
        accept.disabled = False
        reject.label = "Reject"
        accept.label = "Accept fit"
        accept.variant = "success"
        accept.focus()

    def reject_after_error(self) -> None:
        self._loading_timer.pause()
        self.dismiss(None)


class AnalysisErrorScreen(ModalScreen[None]):
    """Require acknowledgement of an analysis error and explain recovery."""

    CSS = """
    AnalysisErrorScreen {
        align: center middle;
        background: #000000 78%;
    }

    #analysis-error-dialog {
        width: 104;
        max-width: 96%;
        height: 42;
        max-height: 94%;
        padding: 1 2;
        border: round #ff4d4f;
        background: #161b22;
    }

    #analysis-error-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #ff7b72;
    }

    #analysis-error-summary {
        height: auto;
        max-height: 6;
        color: #ffb3ad;
    }

    #analysis-error-action-title {
        height: 1;
        margin-top: 1;
        text-style: bold;
        color: #f2cc60;
    }

    #analysis-error-advice {
        height: auto;
        max-height: 5;
        color: #f2cc60;
    }

    #analysis-error-trace-title {
        height: 1;
        margin-top: 1;
        text-style: bold;
        color: #8be9fd;
    }

    #analysis-error-scroll {
        height: 1fr;
        margin-top: 1;
        border: solid #30363d;
        background: #0d1117;
    }

    #analysis-error-traceback {
        height: auto;
        width: 1fr;
        padding: 0 1;
    }

    #analysis-error-buttons {
        height: 3;
        align-horizontal: right;
        margin-top: 1;
    }

    #analysis-error-ok {
        width: 14;
    }
    """

    def __init__(
        self,
        dataset: Path,
        error: str,
        traceback_text: str,
        advice: str,
        *,
        title: str = "CHARGE-AREA ANALYSIS FAILED",
    ) -> None:
        super().__init__()
        self.dataset = dataset
        self.error = error
        self.traceback_text = traceback_text
        self.advice = advice
        self.error_title = title

    def compose(self) -> ComposeResult:
        with Container(id="analysis-error-dialog"):
            yield Static(self.error_title, id="analysis-error-title")
            yield Static(
                f"Dataset: {self.dataset}\n{self.error}",
                id="analysis-error-summary",
                markup=False,
            )
            yield Static("What to do next", id="analysis-error-action-title")
            yield Static(self.advice, id="analysis-error-advice", markup=False)
            yield Static("Technical traceback", id="analysis-error-trace-title")
            with VerticalScroll(id="analysis-error-scroll"):
                yield Static(
                    Syntax(
                        self.traceback_text,
                        "pytb",
                        theme="monokai",
                        word_wrap=True,
                        background_color="default",
                    ),
                    id="analysis-error-traceback",
                )
            with Horizontal(id="analysis-error-buttons"):
                yield Button("OK", id="analysis-error-ok", variant="error")

    def on_mount(self) -> None:
        self.query_one("#analysis-error-ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "analysis-error-ok":
            self.dismiss(None)


class SettingsScreen(ModalScreen[AlgorithmSettings | None]):
    """Edit the persisted charge-regions algorithm configuration."""

    CSS = """
    SettingsScreen {
        align: center middle;
        background: #000000 70%;
    }

    #settings-dialog {
        width: 86;
        max-width: 96%;
        height: 40;
        max-height: 96%;
        padding: 1 2;
        border: round #58a6ff;
        background: #161b22;
    }

    #settings-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #8be9fd;
    }

    #settings-path {
        height: 2;
        color: #8b949e;
    }

    #settings-fields {
        height: 1fr;
    }

    .setting-row {
        height: 3;
    }

    .setting-label {
        width: 36;
        padding: 1 1 0 0;
        text-align: right;
    }

    .setting-input {
        width: 1fr;
    }

    #settings-error {
        height: 2;
        color: #ff7b72;
    }

    #settings-buttons {
        height: 3;
        align-horizontal: right;
    }

    #settings-buttons Button {
        width: 14;
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self, settings: AlgorithmSettings, settings_path: Path | None
    ) -> None:
        super().__init__()
        self.settings = settings
        self.settings_path = settings_path

    def compose(self) -> ComposeResult:
        path_text = (
            str(self.settings_path)
            if self.settings_path is not None
            else "session-only settings"
        )
        with Container(id="settings-dialog"):
            yield Static("charge-regions settings", id="settings-title")
            yield Static(
                f"TOML: {path_text}\n"
                "Comparison m/z bounds apply to every fit and TIC pass; "
                "dataset acquisition bounds are shown separately.",
                id="settings-path",
                markup=False,
            )
            with VerticalScroll(id="settings-fields"):
                for name, label, value_type in SETTING_DEFINITIONS:
                    with Horizontal(classes="setting-row"):
                        yield Label(label, classes="setting-label")
                        yield Input(
                            value=str(getattr(self.settings, name)),
                            type="integer" if value_type is int else "number",
                            id=f"setting-{name}",
                            classes="setting-input",
                        )
            yield Static(id="settings-error", markup=False)
            with Horizontal(id="settings-buttons"):
                yield Button("Defaults", id="settings-defaults")
                yield Button("Cancel", id="settings-cancel")
                yield Button("Save", id="settings-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#setting-mz_min", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-save":
            self._save()
        elif event.button.id == "settings-defaults":
            defaults = AlgorithmSettings()
            for name, _, _ in SETTING_DEFINITIONS:
                self.query_one(f"#setting-{name}", Input).value = str(
                    getattr(defaults, name)
                )
            self.query_one("#settings-error", Static).update("")
        elif event.button.id == "settings-cancel":
            self.dismiss(None)

    def _save(self) -> None:
        values: dict[str, int | float] = {}
        try:
            for name, _, value_type in SETTING_DEFINITIONS:
                raw_value = self.query_one(f"#setting-{name}", Input).value
                if not raw_value.strip():
                    raise ConfigurationError(f"{name} cannot be empty")
                values[name] = value_type(raw_value)
            settings = AlgorithmSettings(**values)
            settings.validate()
        except (ValueError, ConfigurationError) as error:
            self.query_one("#settings-error", Static).update(str(error))
            return
        self.dismiss(settings)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilterScreen(ModalScreen[str | None]):
    """Prompt for a non-recursive current-directory glob filter."""

    CSS = """
    FilterScreen {
        align: center middle;
        background: #000000 70%;
    }

    #filter-dialog {
        width: 72;
        max-width: 95%;
        height: 12;
        padding: 1 2;
        border: round #58a6ff;
        background: #161b22;
    }

    #filter-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #8be9fd;
    }

    #filter-input {
        margin: 1 0;
    }

    #filter-hint {
        height: 1;
        color: #8b949e;
    }

    #filter-buttons {
        height: 3;
        align-horizontal: right;
    }

    #filter-buttons Button {
        width: 12;
        margin-left: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel_filter", "Cancel", show=False)]

    def __init__(self, pattern: str | None) -> None:
        super().__init__()
        self.pattern = pattern or ""

    def compose(self) -> ComposeResult:
        with Container(id="filter-dialog"):
            yield Static("Filter current directory", id="filter-title")
            yield Input(
                value=self.pattern,
                placeholder="e.g. *_13214.d",
                id="filter-input",
            )
            yield Static("Shell glob; empty input clears the filter", id="filter-hint")
            with Horizontal(id="filter-buttons"):
                yield Button("Clear", id="filter-clear")
                yield Button("Cancel", id="filter-cancel")
                yield Button("Apply", id="filter-apply", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "filter-apply":
            self.dismiss(self.query_one("#filter-input", Input).value.strip())
        elif event.button.id == "filter-clear":
            self.dismiss("")
        elif event.button.id == "filter-cancel":
            self.dismiss(None)

    def action_cancel_filter(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Basic keyboard and mouse usage."""

    CSS = """
    HelpScreen {
        align: center middle;
        background: #000000 70%;
    }

    #help-dialog {
        width: 76;
        max-width: 95%;
        height: 32;
        max-height: 95%;
        padding: 1 2;
        border: round #58a6ff;
        background: #161b22;
    }

    #help-title {
        height: 2;
        text-align: center;
        text-style: bold;
        color: #8be9fd;
    }

    #help-content {
        height: 1fr;
        overflow-y: auto;
    }

    #help-close {
        width: 16;
        height: 3;
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("escape", "close_help", "Close", show=False),
        Binding("H,shift+h", "close_help", "Close", show=False),
    ]

    HELP_TEXT = """[b]Filesystem[/b]
  j/k or arrows     move in the middle pane
  l/right/Enter     enter an ordinary directory
  h/left/Backspace  return to the parent
  g/G               jump to first/last entry
  .                 toggle hidden entries
  Ctrl+.            toggle folders-only mode (on initially)
  /                 filter current names with a shell glob
                    (empty input clears the filter)
  r                 reload
  s                 edit charge-regions algorithm settings

[b].d datasets[/b]
  Current row       show cached gradient length; columns remain aligned
  Preview           for a complete .d, show Description, acquisition m/z range,
                    gradient length, and analysis file sizes instead of contents
  Space             add highlighted .d folder to :selected:, read its
                    cached Description, then move down
  Ctrl+Down         move focus to :selected:
  Ctrl+Up           return focus to the filesystem pane
  Click             focus a row in :selected:
  Selected row      aligned path | Description | Below | Above | optional Fit
  j/k or arrows     move through selected paths
  g/G               jump to first/last selected path
  Space             choose a path as HeLa and open the fit-review popup
  Calculate         fit in the popup, then review dominant-charge map,
                    compact histogram, curve parameters, and hi-res SVG
  Accept fit        run thresholded below/above TIC for every selected path
                    and report each value plus its percentage of HeLa
  Reject            return with all selected paths preserved and no chosen HeLa
  x or click ×      remove the current path
  h                 return focus to the middle pane

[b]Analysis settings[/b]
  m/z min/max       primary comparison window used for every dataset
  Acquisition m/z   read per dataset from GlobalMetadata and shown as context
  s                 edit and save algorithm settings

Shift+H opens this help. Escape or Close dismisses it. q quits."""

    def compose(self) -> ComposeResult:
        with Container(id="help-dialog"):
            yield Static("tickytickertextual help", id="help-title")
            yield Static(self.HELP_TEXT, id="help-content")
            yield Button("Close", id="help-close", variant="primary")

    def action_close_help(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help-close":
            self.dismiss()



class FileViewerApp(App[None]):
    """Three-pane read-only filesystem navigator."""

    TITLE = "tickytickertextual"
    SUB_TITLE = "read-only · on-demand"

    CSS = """
    Screen {
        background: #0d1117;
        color: #c9d1d9;
    }

    Header {
        height: 1;
        background: #161b22;
        color: #d8dee9;
    }

    #path-bar {
        height: 1;
        padding: 0 1;
        background: #1f2933;
        color: #8be9fd;
        text-style: bold;
    }

    #panes {
        height: 3fr;
    }

    .pane {
        height: 100%;
        border: tall #30363d;
        background: #0d1117;
        padding: 0 1;
    }

    .pane:focus {
        border: tall #58a6ff;
    }

    #parent-pane {
        width: 1fr;
    }

    #current-pane {
        width: 2fr;
        padding: 0;
    }

    #preview-pane {
        width: 2fr;
    }

    OptionList > .option-list--option-highlighted {
        background: #1f6feb;
        color: #ffffff;
        text-style: bold;
    }

    #selected-pane {
        height: 1fr;
        min-height: 5;
        padding: 0;
    }

    #status-bar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #8b949e;
    }

    Footer {
        height: 1;
        background: #21262d;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("space", "select_dataset", "Select .d", show=False),
        Binding("x", "remove_selected", "Remove", show=False),
        Binding("H,shift+h", "show_help", "Help", show=False),
        Binding("ctrl+down", "focus_selected_pane", "Pane down", show=False),
        Binding("ctrl+up", "focus_current_pane", "Pane up", show=False),
        Binding("l", "open_selected", "Open", show=False),
        Binding("right", "open_selected", "Open", show=False),
        Binding("h", "go_parent", "Parent", show=False),
        Binding("left", "go_parent", "Parent", show=False),
        Binding("backspace", "go_parent", "Parent", show=False),
        Binding("g", "first", "First", show=False),
        Binding("G,shift+g", "last", "Last", show=False),
        Binding(".", "toggle_hidden", "Hidden", show=False),
        Binding(
            "ctrl+full_stop,ctrl+.",
            "toggle_folders_only",
            "Folders only",
            show=False,
        ),
        Binding("/", "show_filter", "Filter", show=False),
        Binding("r", "reload", "Reload", show=False),
        Binding("s", "show_settings", "Settings", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        show_hidden: bool = False,
        settings_path: str | os.PathLike[str] | None = None,
        plot_directory: str | os.PathLike[str] = "/tmp/tickyticker/plots",
        plot_base_url: str | None = None,
    ):
        super().__init__()
        self.navigator = FileSystemNavigator(root, show_hidden=show_hidden)
        self.entries: tuple[FileEntry, ...] = ()
        self.folders_only = True
        self.name_filter: str | None = None
        self.selected_paths: list[Path] = []
        self.dataset_metadata_cache: dict[Path, DatasetMetadata] = {}
        self.selected_descriptions: dict[Path, str | None] = {}
        self.selected_description_errors: dict[Path, str | None] = {}
        self.chosen_path: Path | None = None
        self.accepted_fit: ChargeScanResult | None = None
        self.tic_states: dict[Path, DatasetTicState] = {}
        self._tic_running = False
        self.plot_directory = Path(plot_directory).expanduser()
        self.plot_base_url = plot_base_url.rstrip("/") if plot_base_url else None
        self.settings_path = (
            Path(settings_path).expanduser()
            if settings_path is not None
            else None
        )
        self.algorithm_settings = (
            load_algorithm_settings(self.settings_path)
            if self.settings_path is not None
            else AlgorithmSettings()
        )
        self._analysis_running = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="path-bar")
        with Horizontal(id="panes"):
            yield Static(id="parent-pane", classes="pane")
            yield CurrentOptionList(id="current-pane", classes="pane", markup=False, compact=True)
            yield Static(id="preview-pane", classes="pane")
        yield SelectedOptionList(id="selected-pane", classes="pane", markup=False, compact=True)
        yield Static(id="status-bar")
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.query_one("#parent-pane").border_title = "parent"
        self.query_one("#current-pane").border_title = "current"
        self.query_one("#preview-pane").border_title = "selection"
        self.query_one("#selected-pane").border_title = ":selected:"
        self._open_directory(self.navigator.root)
        current = self.query_one("#current-pane", CurrentOptionList)
        current.focus()
        self.call_after_refresh(current.focus)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if event.option_list.id == "current-pane":
            self._update_selection(event.option_index)
        elif event.option_list.id == "selected-pane":
            self._update_selected_status(event.option_index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "current-pane":
            self.action_open_selected()

    def on_selected_option_list_remove_requested(
        self, event: SelectedOptionList.RemoveRequested
    ) -> None:
        self._remove_selected_at(event.index)

    def _focused_option_list(self) -> OptionList:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            return selected
        return self.query_one("#current-pane", OptionList)

    def action_cursor_down(self) -> None:
        self._focused_option_list().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._focused_option_list().action_cursor_up()

    def action_first(self) -> None:
        self._focused_option_list().action_first()

    def action_last(self) -> None:
        self._focused_option_list().action_last()

    def action_open_selected(self) -> None:
        if self.query_one("#selected-pane", SelectedOptionList).has_focus:
            return
        entry = self._selected_entry()
        if entry is None:
            return
        if entry.is_symlink:
            self.notify("Symlinks are displayed but not followed", severity="warning")
            return
        if not entry.is_dir:
            self.notify("Read-only navigator: files are not opened")
            return
        self._open_directory(entry.path)

    def action_go_parent(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            self.query_one("#current-pane", OptionList).focus()
            self._update_selection(
                self.query_one("#current-pane", OptionList).highlighted
            )
            return
        if self.navigator.current == self.navigator.root:
            self.notify("Already at the configured root")
            return
        previous_name = self.navigator.current.name
        self._open_directory(self.navigator.parent(), highlight_name=previous_name)

    def action_select_dataset(self) -> None:
        if self._tic_running:
            self.notify(
                "Wait for the selected-dataset TIC pass to finish",
                severity="warning",
            )
            return
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if selected.has_focus:
            self._toggle_chosen_path()
            return

        entry = self._selected_entry()
        if entry is None or not self._is_dataset(entry):
            self.notify("Space selects .d directories only", severity="warning")
            return
        path = entry.path
        if path not in self.selected_paths:
            metadata = self._dataset_metadata(path)
            self.selected_descriptions[path] = metadata.description
            self.selected_description_errors[path] = metadata.description_error
            self.selected_paths.append(path)
            self._refresh_selected_pane(highlighted=len(self.selected_paths) - 1)
            self.notify(f"Selected {path}")
            if metadata.description_error:
                self.notify(
                    f"Description unavailable: {metadata.description_error}",
                    severity="warning",
                )
        else:
            self.notify(f"Already selected {path}")
        self._refresh_current_marks()
        current = self.query_one("#current-pane", CurrentOptionList)
        if (
            current.highlighted is not None
            and current.highlighted < current.option_count - 1
        ):
            current.action_cursor_down()

    def action_remove_selected(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if not selected.has_focus or selected.highlighted is None:
            return
        self._remove_selected_at(selected.highlighted)

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_show_settings(self) -> None:
        if self._analysis_running or self._tic_running:
            self.notify("Wait for the current calculation to finish", severity="warning")
            return
        self.push_screen(
            SettingsScreen(self.algorithm_settings, self.settings_path),
            self._apply_algorithm_settings,
        )

    def _apply_algorithm_settings(
        self, settings: AlgorithmSettings | None
    ) -> None:
        if settings is None:
            return
        try:
            if self.settings_path is not None:
                save_algorithm_settings(self.settings_path, settings)
        except (OSError, ConfigurationError) as error:
            self.notify(f"Cannot save settings: {error}", severity="error")
            return
        self.algorithm_settings = settings
        location = (
            str(self.settings_path)
            if self.settings_path is not None
            else "this session"
        )
        self.notify(f"Algorithm settings saved to {location}")

    def action_focus_selected_pane(self) -> None:
        self._focus_selected()

    def action_focus_current_pane(self) -> None:
        current = self.query_one("#current-pane", CurrentOptionList)
        current.focus()
        self._update_selection(current.highlighted)

    def action_reload(self) -> None:
        selected = self._selected_entry()
        self._open_directory(
            self.navigator.current,
            highlight_name=selected.name if selected is not None else None,
        )

    def action_toggle_hidden(self) -> None:
        selected = self._selected_entry()
        self.navigator.show_hidden = not self.navigator.show_hidden
        self._open_directory(
            self.navigator.current,
            highlight_name=selected.name if selected is not None else None,
        )
        state = "shown" if self.navigator.show_hidden else "hidden"
        self.notify(f"Hidden entries are {state}")

    def action_toggle_folders_only(self) -> None:
        current_entry = self._selected_entry()
        selected_pane = self.query_one("#selected-pane", SelectedOptionList)
        selected_focused = selected_pane.has_focus
        self.folders_only = not self.folders_only
        self._open_directory(
            self.navigator.current,
            highlight_name=(
                current_entry.name if current_entry is not None else None
            ),
        )
        if selected_focused:
            selected_pane.focus()
            self._update_selected_status(selected_pane.highlighted)
        state = "on" if self.folders_only else "off"
        self.notify(f"Folders-only mode is {state}")

    def action_show_filter(self) -> None:
        self.push_screen(FilterScreen(self.name_filter), self._apply_name_filter)

    def _apply_name_filter(self, pattern: str | None) -> None:
        if pattern is None:
            return
        self.name_filter = pattern or None
        self.action_reload()
        if self.name_filter:
            self.notify(f"Name filter: {self.name_filter}")
        else:
            self.notify("Name filter cleared")

    def _refresh_selected_pane(self, *, highlighted: int | None = None) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        if highlighted is None:
            highlighted = selected.highlighted
        fitted_line = self._accepted_line()
        hela_tic_state = (
            self.tic_states.get(self.chosen_path)
            if self.chosen_path is not None
            else None
        )
        relative_paths = [
            str(path.relative_to(self.navigator.root))
            for path in self.selected_paths
        ]
        descriptions = [
            _selected_description(
                self.selected_descriptions.get(path),
                self.selected_description_errors.get(path),
            )
            for path in self.selected_paths
        ]
        tic_cells = [
            _selected_tic_cells(
                self.tic_states.get(path),
                hela_tic_state,
                chosen=path == self.chosen_path,
            )
            for path in self.selected_paths
        ]
        path_width = _column_width(
            relative_paths, minimum=16, maximum=44
        )
        description_width = _column_width(
            descriptions, minimum=20, maximum=40
        )
        below_width = _column_width(
            (below for below, _ in tic_cells), minimum=20
        )
        above_width = _column_width(
            (above for _, above in tic_cells), minimum=20
        )
        selected.set_options(
            [
                _selected_path_label(
                    path,
                    self.navigator.root,
                    index,
                    chosen=path == self.chosen_path,
                    description=self.selected_descriptions.get(path),
                    description_error=self.selected_description_errors.get(path),
                    fitted_line=(
                        fitted_line if path == self.chosen_path else None
                    ),
                    tic_state=self.tic_states.get(path),
                    hela_tic_state=hela_tic_state,
                    path_width=path_width,
                    description_width=description_width,
                    below_width=below_width,
                    above_width=above_width,
                )
                for index, path in enumerate(self.selected_paths)
            ]
        )
        if not self.selected_paths:
            selected.highlighted = None
            self._update_selected_status(None)
            return
        selected.highlighted = min(highlighted or 0, len(self.selected_paths) - 1)
        selected.scroll_to_highlight()

    def _refresh_current_marks(self) -> None:
        current = self.query_one("#current-pane", CurrentOptionList)
        for index, entry in enumerate(self.entries):
            current.replace_option_prompt_at_index(
                index,
                _entry_label(
                    entry,
                    marked=entry.path in self.selected_paths,
                    metadata=self.dataset_metadata_cache.get(entry.path),
                    name_width=self._current_name_column_width(),
                ),
            )

    def _focus_selected(self, path: Path | None = None) -> None:
        if not self.selected_paths:
            self.notify("No .d folders selected", severity="warning")
            return
        selected = self.query_one("#selected-pane", SelectedOptionList)
        selected.focus()
        if path in self.selected_paths:
            selected.highlighted = self.selected_paths.index(path)
        elif selected.highlighted is None:
            selected.highlighted = 0
        selected.scroll_to_highlight()
        self._update_selected_status(selected.highlighted)

    def _remove_selected_at(self, index: int) -> None:
        if self._tic_running:
            self.notify(
                "Wait for the selected-dataset TIC pass to finish",
                severity="warning",
            )
            return
        if not 0 <= index < len(self.selected_paths):
            return
        removed = self.selected_paths.pop(index)
        self.selected_descriptions.pop(removed, None)
        self.selected_description_errors.pop(removed, None)
        self.tic_states.pop(removed, None)
        if self.chosen_path == removed:
            self.chosen_path = None
            self.accepted_fit = None
            self.tic_states.clear()
        next_index = min(index, len(self.selected_paths) - 1) if self.selected_paths else None
        self._refresh_selected_pane(highlighted=next_index)
        self._refresh_current_marks()
        self.query_one("#selected-pane", SelectedOptionList).focus()
        self.notify(f"Removed {removed}")

    def _toggle_chosen_path(self) -> None:
        selected = self.query_one("#selected-pane", SelectedOptionList)
        index = selected.highlighted
        if index is None or not 0 <= index < len(self.selected_paths):
            return
        path = self.selected_paths[index]
        choosing = self.chosen_path != path
        if choosing:
            self.chosen_path = path
            self.accepted_fit = None
            self.tic_states.clear()
            message = ":HELA CHOSEN:"
        else:
            self.chosen_path = None
            self.accepted_fit = None
            self.tic_states.clear()
            message = ":HELA UNSELECTED:"
        self._refresh_selected_pane(highlighted=index)
        selected.focus()
        self.notify(message)
        if choosing:
            settings = self.algorithm_settings
            metadata = self._dataset_metadata(path)
            self.push_screen(
                ChargeScanScreen(path, settings=settings, metadata=metadata),
                lambda result: self._handle_scan_review(path, result),
            )

    def _handle_scan_review(
        self,
        dataset: Path,
        result: ChargeScanResult | None,
    ) -> None:
        self.query_one("#selected-pane", SelectedOptionList).focus()
        if result is None:
            if self.chosen_path == dataset:
                self.chosen_path = None
            self.accepted_fit = None
            self.tic_states.clear()
            self._refresh_selected_pane()
            self.notify("Fit rejected; selected datasets were preserved")
            return
        self.accepted_fit = result
        self._refresh_selected_pane()
        self.notify("Fit accepted; calculating TIC for selected datasets")
        self._begin_tic_batch(result)

    def on_charge_scan_screen_scan_requested(
        self, event: ChargeScanScreen.ScanRequested
    ) -> None:
        self._begin_charge_scan(event.screen)

    def _begin_charge_scan(self, screen: ChargeScanScreen) -> None:
        if self._analysis_running:
            self.notify(
                "A charge-area scan is already running", severity="warning"
            )
            return
        self._analysis_running = True
        self._execute_charge_scan(screen)

    @work(
        thread=True,
        exclusive=True,
        group="charge-regions",
        exit_on_error=False,
    )
    def _execute_charge_scan(self, screen: ChargeScanScreen) -> None:
        try:
            analysis = charge_regions.analyse(
                screen.dataset,
                **screen.settings.analysis_arguments(),
                progress=lambda message: self.call_from_thread(
                    screen.set_progress, message
                ),
            )
            result = adapt_charge_scan_result(analysis, screen.settings)
        except Exception as error:
            self.call_from_thread(
                self._finish_charge_scan,
                screen,
                None,
                str(error),
                traceback.format_exc(),
                analysis_error_advice(error),
            )
        else:
            self.call_from_thread(
                self._finish_charge_scan,
                screen,
                result,
                None,
                None,
                None,
            )

    def _finish_charge_scan(
        self,
        screen: ChargeScanScreen,
        result: ChargeScanResult | None,
        error: str | None,
        traceback_text: str | None,
        advice: str | None,
    ) -> None:
        self._analysis_running = False
        if error is not None or result is None:
            self.notify("Charge-area scan failed", severity="error")
            self.push_screen(
                AnalysisErrorScreen(
                    screen.dataset,
                    error or "Unknown analysis failure",
                    traceback_text or "No traceback was available.",
                    advice
                    or "Click OK, choose another .d dataset, and try again.",
                ),
                lambda _: screen.reject_after_error(),
            )
            return
        svg_url: str | None = None
        try:
            svg_path = write_dominant_charge_svg(result, self.plot_directory)
        except OSError as svg_error:
            self.notify(f"Cannot create SVG: {svg_error}", severity="warning")
        else:
            svg_url = (
                f"{self.plot_base_url}/{svg_path.name}"
                if self.plot_base_url is not None
                else svg_path.resolve().as_uri()
            )
        screen.show_result(result, svg_url)
        self.notify(f"Charge-area fit ready for review: {screen.dataset.name}")

    @staticmethod
    def _line_from_result(result: ChargeScanResult) -> tuple[float, float]:
        line = result.line_data.get("line")
        if not isinstance(line, dict):
            raise RuntimeError("Fitted line parameters are unavailable")
        try:
            intercept = float(line["intercept"])
            slope = float(line["slope"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Fitted line parameters are invalid") from error
        if not np.isfinite(intercept) or not np.isfinite(slope):
            raise RuntimeError("Fitted line parameters are not finite")
        return intercept, slope

    def _accepted_line(self) -> tuple[float, float] | None:
        if self.accepted_fit is None:
            return None
        try:
            return self._line_from_result(self.accepted_fit)
        except RuntimeError:
            return None

    def _begin_tic_batch(self, fit: ChargeScanResult) -> None:
        if self.chosen_path is None:
            return
        try:
            intercept, slope = self._line_from_result(fit)
        except RuntimeError as error:
            self.push_screen(
                AnalysisErrorScreen(
                    self.chosen_path,
                    str(error),
                    traceback.format_exc(),
                    "Reject this fit and choose another HeLa dataset.",
                )
            )
            return
        self._tic_running = True
        paths = [self.chosen_path] + [
            path for path in self.selected_paths if path != self.chosen_path
        ]
        self.tic_states = {
            path: DatasetTicState(status="queued") for path in paths
        }
        self._refresh_selected_pane()
        self._execute_tic_batch(tuple(paths), fit.settings, intercept, slope)

    def _set_tic_running(self, path: Path) -> None:
        self.tic_states[path] = DatasetTicState(status="running")
        self._refresh_selected_pane()
        self.query_one("#status-bar", Static).update(
            f"Calculating below/above TIC: {path.name}"
        )

    def _set_tic_progress(self, path: Path, message: str) -> None:
        self.query_one("#status-bar", Static).update(
            f"TIC {path.name}: {message}"
        )

    def _set_tic_result(
        self,
        path: Path,
        result: charge_regions.LineTicResult | None,
        error: str | None,
    ) -> None:
        if result is None:
            self.tic_states[path] = DatasetTicState(
                status="error", error=error or "unknown error"
            )
        else:
            self.tic_states[path] = DatasetTicState(
                status="complete",
                tic_below_line=result.tic_below_line,
                tic_above_line=result.tic_above_line,
            )
        self._refresh_selected_pane()

    @work(
        thread=True,
        exclusive=True,
        group="line-tic",
        exit_on_error=False,
    )
    def _execute_tic_batch(
        self,
        paths: tuple[Path, ...],
        settings: AlgorithmSettings,
        intercept: float,
        slope: float,
    ) -> None:
        errors: list[tuple[Path, str, str]] = []
        for path in paths:
            self.call_from_thread(self._set_tic_running, path)
            try:
                result = charge_regions.analyse_line_tic(
                    path,
                    intercept=intercept,
                    slope=slope,
                    mz_min=settings.mz_min,
                    mz_max=settings.mz_max,
                    min_intensity=settings.min_intensity,
                    threads=settings.threads,
                    frame_stride=settings.frame_stride,
                    progress=lambda message, current=path: self.call_from_thread(
                        self._set_tic_progress, current, message
                    ),
                )
            except Exception as error:
                trace = traceback.format_exc()
                errors.append((path, str(error), trace))
                self.call_from_thread(
                    self._set_tic_result, path, None, str(error)
                )
            else:
                self.call_from_thread(
                    self._set_tic_result, path, result, None
                )
        self.call_from_thread(self._finish_tic_batch, errors)

    def _finish_tic_batch(
        self, errors: list[tuple[Path, str, str]]
    ) -> None:
        self._tic_running = False
        completed = sum(
            state.status == "complete" for state in self.tic_states.values()
        )
        self.query_one("#status-bar", Static).update(
            f"TIC complete: {completed} succeeded, {len(errors)} failed"
        )
        if errors:
            summary = "; ".join(
                f"{path.name}: {message}" for path, message, _ in errors
            )
            traces = "\n\n".join(
                f"Dataset: {path}\n{trace}" for path, _, trace in errors
            )
            self.push_screen(
                AnalysisErrorScreen(
                    errors[0][0],
                    f"{len(errors)} selected dataset(s) failed: {summary}",
                    traces,
                    "Failed rows remain marked ERROR and were excluded. "
                    "Click OK, inspect those datasets, and retry them later.",
                    title="SELECTED-DATASET TIC FAILED",
                ),
                lambda _: self.query_one(
                    "#selected-pane", SelectedOptionList
                ).focus(),
            )
        else:
            self.notify("Below/above TIC calculations completed")

    def _update_selected_status(self, index: int | None) -> None:
        status = self.query_one("#status-bar", Static)
        if index is None or not self.selected_paths:
            status.update("0 selected .d folders")
            return
        status.update(
            f"{index + 1}/{len(self.selected_paths)} selected .d folders"
        )

    @staticmethod
    def _is_dataset(entry: FileEntry) -> bool:
        return (
            entry.is_dir
            and not entry.is_symlink
            and entry.name.casefold().endswith(".d")
        )

    def _dataset_metadata(self, path: Path) -> DatasetMetadata:
        """Return cached .d metadata, reading SQLite at most once per path."""
        metadata = self.dataset_metadata_cache.get(path)
        if metadata is None:
            metadata = _read_dataset_metadata(path)
            self.dataset_metadata_cache[path] = metadata
        return metadata

    def _current_name_column_width(self) -> int:
        """Return one bounded filename column width for the current pane."""
        names = [
            f"{entry.name}/" if entry.is_dir else entry.name
            for entry in self.entries
        ]
        return _column_width(names, minimum=12, maximum=44)


    def _open_directory(self, path: Path, *, highlight_name: str | None = None) -> None:
        try:
            listing = self.navigator.change_directory(
                path, directories_only=self.folders_only
            )
        except NavigationError as error:
            self.notify(str(error), severity="error")
            return

        self.entries = tuple(
            entry
            for entry in listing.entries
            if _matches_name_filter(entry.name, self.name_filter)
        )
        name_width = self._current_name_column_width()
        option_list = self.query_one("#current-pane", OptionList)
        option_list.set_options(
            [
                _entry_label(
                    entry,
                    marked=entry.path in self.selected_paths,
                    metadata=self.dataset_metadata_cache.get(entry.path),
                    name_width=name_width,
                )
                for entry in self.entries
            ]
        )

        highlighted = 0 if self.entries else None
        if highlight_name is not None:
            highlighted = next(
                (
                    index
                    for index, entry in enumerate(self.entries)
                    if entry.name == highlight_name
                ),
                highlighted,
            )
        option_list.highlighted = highlighted
        if highlighted is not None:
            option_list.scroll_to_highlight()

        mode = "folders only" if self.folders_only else "folders + files"
        filter_status = f" · filter: {self.name_filter}" if self.name_filter else ""
        self.query_one("#path-bar", Static).update(
            f"{listing.path}  [{mode}{filter_status}]"
        )
        self._update_parent_pane()
        self._update_selection(highlighted)

    def _update_parent_pane(self) -> None:
        content = self.query_one("#parent-pane", Static)
        if self.navigator.current == self.navigator.root:
            root_text = Text()
            root_text.append("configured root\n", style="bold cyan")
            root_text.append(str(self.navigator.root), style="dim")
            root_text.append("\n\nNavigation is confined to this tree.", style="dim")
            content.update(root_text)
            return

        try:
            listing = self.navigator.scan(
                self.navigator.current.parent,
                limit=500,
                directories_only=self.folders_only,
            )
        except NavigationError as error:
            content.update(Text(str(error), style="red"))
            return
        content.update(
            _listing_text(
                listing.entries,
                selected_name=self.navigator.current.name,
                truncated=listing.truncated,
            )
        )

    def _update_selection(self, index: int | None) -> None:
        preview = self.query_one("#preview-pane", Static)
        status = self.query_one("#status-bar", Static)

        if index is None or not 0 <= index < len(self.entries):
            preview.update(Text("empty directory", style="dim italic"))
            status.update(f"{len(self.entries)} entries · read-only")
            return

        entry = self.entries[index]
        if (
            entry.is_dir
            and self._is_dataset(entry)
            and _dataset_has_analysis_pair(entry.path)
        ):
            metadata = self._dataset_metadata(entry.path)
            preview.update(
                _dataset_metadata_text(entry.path, metadata)
            )
            self.query_one(
                "#current-pane", CurrentOptionList
            ).replace_option_prompt_at_index(
                index,
                _entry_label(
                    entry,
                    marked=entry.path in self.selected_paths,
                    metadata=metadata,
                    name_width=self._current_name_column_width(),
                ),
            )
        elif entry.is_dir:
            try:
                listing = self.navigator.scan(
                    entry.path,
                    limit=500,
                    directories_only=(
                        self.folders_only and not self._is_dataset(entry)
                    ),
                )
            except NavigationError as error:
                preview.update(Text(str(error), style="red"))
            else:
                if listing.entries:
                    preview.update(
                        _listing_text(listing.entries, truncated=listing.truncated)
                    )
                else:
                    preview.update(Text("empty directory", style="dim italic"))
        else:
            preview.update(self._metadata_text(entry))

        kind = "directory" if entry.is_dir else "symlink" if entry.is_symlink else "file"
        status.update(
            f"{index + 1}/{len(self.entries)} · {kind} · "
            f"{format_size(entry.size)} · read-only"
        )

    @staticmethod
    def _metadata_text(entry: FileEntry) -> Text:
        metadata = Text()
        metadata.append(entry.name, style="bold #f0f6fc")
        metadata.append("\n\n")
        metadata.append("type      ", style="dim")
        metadata.append("symbolic link" if entry.is_symlink else "file")
        metadata.append("\n")
        metadata.append("size      ", style="dim")
        metadata.append(format_size(entry.size))
        metadata.append("\n")
        metadata.append("modified  ", style="dim")
        if entry.modified is None:
            metadata.append("unknown")
        else:
            metadata.append(datetime.fromtimestamp(entry.modified).isoformat(sep=" ", timespec="seconds"))
        metadata.append("\n")
        metadata.append("mode      ", style="dim")
        metadata.append(stat.filemode(entry.mode) if entry.mode is not None else "unknown")
        if entry.is_symlink:
            metadata.append("\n")
            metadata.append("target    ", style="dim")
            try:
                metadata.append(os.readlink(entry.path))
            except OSError:
                metadata.append("unavailable", style="red")
        metadata.append("\n\nFile content previews are disabled.", style="dim italic")
        return metadata

    def _selected_entry(self) -> FileEntry | None:
        highlighted = self.query_one("#current-pane", OptionList).highlighted
        if highlighted is None or not 0 <= highlighted < len(self.entries):
            return None
        return self.entries[highlighted]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("working_directory", nargs="?", type=Path, help="filesystem root to expose (default: current directory)")
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("/tmp/tickyticker/settings.toml"),
        help="server-side charge-regions TOML settings file",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/tmp/tickyticker/tickytickertextual.lock"),
        help="Linux advisory lock preventing concurrent UI instances",
    )
    parser.add_argument(
        "--plot-directory",
        type=Path,
        default=Path("/tmp/tickyticker/plots"),
        help="directory for uniquely named dominant-charge SVG views",
    )
    parser.add_argument(
        "--plot-base-url",
        help="browser-visible base URL serving files from --plot-directory",
    )
    parser.add_argument(
        "--show-hidden",
        action="store_true",
        help="show dotfiles and dot-directories initially",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    working_directory = args.working_directory or Path.cwd()
    try:
        with SingleInstanceLock(args.lock_file):
            app = FileViewerApp(
                working_directory,
                show_hidden=args.show_hidden,
                settings_path=args.settings,
                plot_directory=args.plot_directory,
                plot_base_url=args.plot_base_url,
            )
            app.run()
    except (
        NavigationError,
        ConfigurationError,
        InstanceAlreadyRunning,
        OSError,
    ) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
