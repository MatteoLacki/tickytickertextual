"""Self-host the Textual fileviewer in a browser."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Sequence

from aiohttp import web as aiohttp_web
from textual_serve.server import Server


class PlotServer(Server):
    """Serve generated SVGs and a fixed allowlist of table exports."""

    EXPORTS = {"selected.tsv", "selected.csv", "raw-tics.csv"}

    def __init__(
        self,
        *args: object,
        plot_directory: Path,
        export_directory: Path,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.plot_directory = plot_directory
        self.export_directory = export_directory

    async def _download_export(
        self, request: aiohttp_web.Request
    ) -> aiohttp_web.StreamResponse:
        filename = request.match_info["filename"]
        if filename not in self.EXPORTS:
            raise aiohttp_web.HTTPNotFound()
        path = self.export_directory / filename
        if not path.is_file():
            raise aiohttp_web.HTTPNotFound(text="Export is not ready yet")
        return aiohttp_web.FileResponse(
            path,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    async def _make_app(self):
        app = await super()._make_app()
        self.plot_directory.mkdir(parents=True, exist_ok=True)
        self.export_directory.mkdir(parents=True, exist_ok=True)
        app.router.add_static(
            "/plots",
            self.plot_directory,
            show_index=False,
            name="plots",
        )
        app.router.add_get(
            "/exports/{filename}",
            self._download_export,
            name="exports",
        )
        return app


def _effective_public_url(host: str, port: int, configured: str | None) -> str:
    if configured:
        return configured.rstrip("/")
    if port == 80:
        return f"http://{host}"
    if port == 443:
        return f"https://{host}"
    return f"http://{host}:{port}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("working_directory", nargs="?", type=Path, help="filesystem root to expose (default: current directory)")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", default=8000, type=int, help="listen port")
    parser.add_argument("--public-url", help="public URL when running behind a proxy")
    parser.add_argument(
        "--plot-directory",
        type=Path,
        default=Path("/tmp/tickyticker/plots"),
        help="directory for uniquely named high-resolution SVG views",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=Path("/tmp/tickyticker/settings.toml"),
        help="server-side charge-regions TOML settings file",
    )
    parser.add_argument(
        "--export-directory",
        type=Path,
        default=Path("/tmp/tickyticker/exports"),
        help="directory for selected-table and per-frame TIC exports",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/tmp/tickyticker/tickytickertextual.lock"),
        help="single-instance Linux lock file",
    )
    parser.add_argument("--show-hidden", action="store_true", help="show hidden entries")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    working_directory = args.working_directory or Path.cwd()
    try:
        root = working_directory.expanduser().resolve(strict=True)
    except OSError as error:
        raise SystemExit(f"Cannot open root {working_directory!s}: {error}") from error
    if not root.is_dir():
        raise SystemExit(f"Root is not a directory: {root}")

    command_parts = [
        sys.executable,
        "-m",
        "tickytickertextual.app",
        str(root),
        "--settings",
        str(args.settings),
        "--lock-file",
        str(args.lock_file),
        "--plot-directory",
        str(args.plot_directory),
        "--plot-base-url",
        _effective_public_url(args.host, args.port, args.public_url) + "/plots",
        "--export-directory",
        str(args.export_directory),
    ]
    if args.show_hidden:
        command_parts.append("--show-hidden")
    command = shlex.join(command_parts)

    server = PlotServer(
        command,
        host=args.host,
        port=args.port,
        title=f"tickytickertextual · {root}",
        public_url=args.public_url,
        templates_path=Path(__file__).with_name("templates"),
        plot_directory=args.plot_directory,
        export_directory=args.export_directory,
    )
    server.serve()


if __name__ == "__main__":
    main()
