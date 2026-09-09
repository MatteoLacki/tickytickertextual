"""Self-host the Textual fileviewer in a browser."""

from __future__ import annotations

import argparse
import secrets
import shlex
import sys
from pathlib import Path
from typing import Sequence

from aiohttp import web as aiohttp_web
from textual_serve.server import Server


class PlotServer(Server):
    """Stream plots with Textual and keep table exports only in memory."""

    EXPORTS = {"selected.tsv", "selected.csv", "raw-tics.csv"}

    def __init__(
        self,
        *args: object,
        plot_directory: Path | None = None,
        export_directory: Path | None = None,
        bridge_token: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.plot_directory = plot_directory
        self.export_directory = export_directory
        self.bridge_token = bridge_token
        self.exports: dict[str, str] = {}
        self.exports_ready = False
        self._connected_uis = 0

    async def on_startup(self, app):
        # The subprocess command contains the internal bridge credential.
        self.console.print(f"Serving {self.title} on {self.public_url}")

    async def handle_websocket(self, request):
        # A disconnected UI must not leave a stale table available for copying.
        self._connected_uis += 1
        try:
            return await super().handle_websocket(request)
        finally:
            self._connected_uis -= 1
            if not self._connected_uis:
                self.exports_ready = False
                self.exports.clear()

    async def _publish(self, request):
        if not self.bridge_token or not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.bridge_token
        ):
            raise aiohttp_web.HTTPForbidden()
        payload = await request.json()
        exports = payload.get("exports", {})
        if not isinstance(exports, dict) or any(k not in self.EXPORTS or not isinstance(v, str) for k, v in exports.items()):
            raise aiohttp_web.HTTPBadRequest()
        self.exports_ready = bool(payload.get("ready"))
        self.exports = exports if self.exports_ready else {}
        return aiohttp_web.json_response({"ok": True})

    async def _export_status(self, request):
        return aiohttp_web.json_response({"ready": self.exports_ready}, headers={"Cache-Control": "no-store"})

    async def _download_export(
        self, request: aiohttp_web.Request
    ) -> aiohttp_web.StreamResponse:
        filename = request.match_info["filename"]
        if filename not in self.EXPORTS:
            raise aiohttp_web.HTTPNotFound()
        if not self.exports_ready or filename not in self.exports:
            raise aiohttp_web.HTTPNotFound(text="Export is not ready yet")
        return aiohttp_web.Response(
            body=self.exports[filename].encode("utf-8"),
            content_type="text/tab-separated-values" if filename.endswith("tsv") else "text/csv",
            charset="utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    async def _make_app(self):
        app = await super()._make_app()
        app._client_max_size = 64 * 1024 * 1024
        app.router.add_post("/_publish", self._publish)
        app.router.add_get("/exports/status", self._export_status)
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
    parser.add_argument("--production", action="store_true", help="Suppress notification popups")
    parser.add_argument("working_directory", nargs="?", type=Path, help="filesystem root to expose (default: current directory)")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", default=8000, type=int, help="listen port")
    parser.add_argument("--public-url", help="public URL when running behind a proxy")
    parser.add_argument(
        "--plot-directory",
        type=Path,
        default=Path("/tmp/tickyticker/plots"),
        help=argparse.SUPPRESS,
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
        help=argparse.SUPPRESS,
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
    token = secrets.token_urlsafe(32)
    working_directory = args.working_directory or Path.cwd()
    try:
        root = working_directory.expanduser().resolve(strict=True)
    except OSError as error:
        raise SystemExit(f"Cannot open root {working_directory!s}: {error}") from error
    if not root.is_dir():
        raise SystemExit(f"Root is not a directory: {root}")

    command_parts = [
        "env", "-u", "NO_COLOR",
        sys.executable,
        "-m",
        "tickytickertextual.app",
        str(root),
        "--settings",
        str(args.settings),
        "--lock-file",
        str(args.lock_file),
        "--bridge-url",
        _effective_public_url("127.0.0.1" if args.host in {"0.0.0.0", "localhost"} else args.host, args.port, None),
        "--bridge-token", token,
    ]
    if args.show_hidden:
        command_parts.append("--show-hidden")
    if args.production:
        command_parts.append("--production")
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
        bridge_token=token,
    )
    server.serve()


if __name__ == "__main__":
    main()
