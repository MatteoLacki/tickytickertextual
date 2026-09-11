"""Self-host the Textual fileviewer in a browser."""

from __future__ import annotations

import argparse
import asyncio
import csv
from datetime import date
import io
import json
import secrets
import shlex
import sys
from pathlib import Path
from typing import Sequence

import aiohttp_jinja2
from aiohttp import web as aiohttp_web
from textual_serve.server import Server

from .session import BrowserAppService


def _welcome_logo() -> str:
    package = Path(__file__).resolve().parent
    # Keep the user's repository artwork editable; include a copy in installations.
    for path in (package.parent.parent / "logoascii.txt",
                 package.parent.parent / "logoacsii.txt", package / "logoascii.txt"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return "tickyticker"


class PlotServer(Server):
    """Host browser sessions and keep generated plots and exports in memory."""

    EXPORTS = {"selected.tsv", "selected.csv", "raw-tics.csv"}

    def __init__(
        self,
        *args: object,
        plot_directory: Path | None = None,
        export_directory: Path | None = None,
        bridge_token: str = "",
        public_url: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, public_url=public_url, **kwargs)
        self._configured_public_url = public_url.rstrip("/") if public_url else None
        self.plot_directory = plot_directory
        self.export_directory = export_directory
        self.bridge_token = bridge_token
        self.exports: dict[str, str] = {}
        self.exports_ready = False
        self._connected_uis = 0
        self._sessions = {}
        self._cleanup_tasks = set()
        self._session_lock = asyncio.Lock()
        self._restart_token = secrets.token_urlsafe(32)
        self.documents = {}
        self.actions = {}
        self._state_revision = 0

    async def on_startup(self, app):
        # The subprocess command contains the internal bridge credential.
        self.console.print(f"Serving {self.title} on {self.public_url}")

    @aiohttp_jinja2.template("app_index.html")
    async def handle_index(self, request: aiohttp_web.Request) -> dict:
        # The bind address (especially 0.0.0.0) is not a browser destination.
        # Resolve each request independently so LAN, localhost and DNS all work.
        public_url = self._configured_public_url or f"{request.scheme}://{request.host}"
        router = request.app.router
        websocket_url = public_url + str(router["websocket"].url_for())
        websocket_scheme = "wss:" if public_url.startswith("https:") else "ws:"
        websocket_url = websocket_scheme + websocket_url.split(":", 1)[1]
        try:
            font_size = int(request.query.get("fontsize", "16"))
        except ValueError:
            font_size = 16
        logo = _welcome_logo()
        return {
            "welcome_logo": logo,
            "logo_columns": max((len(line) for line in logo.splitlines()), default=1),
            "logo_lines": max(1, len(logo.splitlines())),
            "font_size": font_size,
            "restart_token": self._restart_token,
            "app_websocket_url": websocket_url,
            "config": {"static": {"url": public_url + str(router["static"].url_for(filename="/"))}},
            "application": {"name": self.title},
        }

    async def handle_websocket(self, request):
        socket = aiohttp_web.WebSocketResponse(heartbeat=15, timeout=1)
        service = BrowserAppService(
            self.command, write_bytes=socket.send_bytes, write_str=socket.send_str,
            close=socket.close, download_manager=self.download_manager, debug=self.debug,
        )
        def dimension(name, default):
            try:
                return int(request.query.get(name, default))
            except ValueError:
                return default
        try:
            async with self._session_lock:
                await socket.prepare(request)
                await service.start(dimension("width", 80), dimension("height", 24))
                self._sessions[service] = socket
                self._connected_uis = len(self._sessions)
                await socket.send_str(json.dumps(["toolbar_state", self._toolbar_state()]))
            await self._process_messages(socket, service)
        finally:
            cleanup = asyncio.create_task(self._close_session(service, socket))
            self._cleanup_tasks.add(cleanup)
            cleanup.add_done_callback(self._cleanup_tasks.discard)
            await asyncio.shield(cleanup)
        return socket

    async def _close_session(self, service, socket):
        try:
            await service.stop()
        finally:
            self._sessions.pop(service, None)
            self._connected_uis = len(self._sessions)
            if not self._connected_uis:
                self._clear_session_data()
            await socket.close()

    def _clear_session_data(self):
        self.exports_ready = False
        self.exports.clear()
        self.documents.clear()
        self.actions.clear()
        self._state_revision += 1

    async def _restart(self, request):
        if not secrets.compare_digest(request.headers.get("X-Restart-Token", ""), self._restart_token):
            raise aiohttp_web.HTTPForbidden()
        async with self._session_lock:
            sessions = list(self._sessions.items())
            await asyncio.gather(*(service.stop() for service, _ in sessions))
            await asyncio.gather(*(socket.close() for _, socket in sessions))
            for service, _ in sessions:
                self._sessions.pop(service, None)
            self._connected_uis = len(self._sessions)
            self._clear_session_data()
        return aiohttp_web.json_response({"ok": True})

    async def _action(self, request):
        if not secrets.compare_digest(request.headers.get("X-Restart-Token", ""), self._restart_token):
            raise aiohttp_web.HTTPForbidden()
        action = (await request.json()).get("action")
        if action not in {"chromatograms", "rerun", "tic-new", "help", "estimate"}:
            raise aiohttp_web.HTTPBadRequest()
        async with self._session_lock:
            if not self.actions.get(action) or len(self._sessions) != 1:
                raise aiohttp_web.HTTPConflict(text="Action is not available")
            service = next(iter(self._sessions))
            async with asyncio.timeout(2):
                await service.send_meta({"type": "browser_action", "action": action})
        return aiohttp_web.json_response({"ok": True})

    async def on_shutdown(self, app):
        await asyncio.gather(*(service.stop() for service in list(self._sessions)))
        await asyncio.gather(*list(self._cleanup_tasks), return_exceptions=True)

    async def _document(self, request):
        document = self.documents.get(request.match_info["key"])
        if document is None:
            raise aiohttp_web.HTTPNotFound(text="Plot expired; generate it again in the app")
        return aiohttp_web.Response(
            body=document["content"].encode("utf-8"), content_type=document["mime"], charset="utf-8",
            headers={"Content-Disposition": f'{document["disposition"]}; filename="{document["filename"]}"',
                     "Cache-Control": "no-store"},
        )

    async def _publish(self, request):
        if not self.bridge_token or not secrets.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.bridge_token
        ):
            raise aiohttp_web.HTTPForbidden()
        payload = await request.json()
        if "document" in payload:
            document = payload["document"]
            if (not isinstance(document, dict)
                or document.get("mime") not in {"image/svg+xml", "text/html"}
                or document.get("disposition") not in {"inline", "attachment"}
                or not isinstance(document.get("content"), str)
                or document.get("filename") not in {
                    "dominant-charge.svg", "event-histogram.svg", "chromatograms.svg",
                    "dominant-charge.html", "event-histogram.html", "chromatograms.html",
                }):
                raise aiohttp_web.HTTPBadRequest()
            key = secrets.token_urlsafe(24)
            # Keep at most one document per plot/format for the current session.
            for old_key, old in list(self.documents.items()):
                if old["filename"] == document["filename"]:
                    self.documents.pop(old_key)
            self.documents[key] = document
            return aiohttp_web.json_response({"url": f"/plots/{key}"})
        exports = payload.get("exports", self.exports)
        if not isinstance(exports, dict) or any(k not in self.EXPORTS or not isinstance(v, str) for k, v in exports.items()):
            raise aiohttp_web.HTTPBadRequest()
        actions = payload.get("actions", {})
        if not isinstance(actions, dict):
            raise aiohttp_web.HTTPBadRequest()
        self.actions = {key: bool(actions.get(key)) for key in ("chromatograms", "rerun", "tic-new", "help", "estimate")}
        self.exports_ready = bool(payload.get("ready"))
        self.exports = exports if self.exports_ready else {}
        self._state_revision += 1
        message = json.dumps(["toolbar_state", self._toolbar_state()])
        await asyncio.gather(*(socket.send_str(message) for socket in list(self._sessions.values())
                               if not socket.closed), return_exceptions=True)
        return aiohttp_web.json_response({"ok": True})

    def _toolbar_state(self):
        return {"ready": self.exports_ready, "actions": self.actions, "revision": self._state_revision}

    async def _export_status(self, request):
        return aiohttp_web.json_response(self._toolbar_state(), headers={"Cache-Control": "no-store"})

    async def _download_export(
        self, request: aiohttp_web.Request
    ) -> aiohttp_web.StreamResponse:
        filename = request.match_info["filename"]
        if filename not in self.EXPORTS:
            raise aiohttp_web.HTTPNotFound()
        if not self.exports_ready or filename not in self.exports:
            raise aiohttp_web.HTTPNotFound(text="Export is not ready yet")
        delimiter = "\t" if filename.endswith("tsv") else ","
        rows = csv.reader(io.StringIO(self.exports[filename]), delimiter=delimiter)
        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
        writer.writerow(["Date", *next(rows)])
        exported_on = date.today().strftime("%d.%m.%Y")
        writer.writerows([exported_on, *row] for row in rows)
        return aiohttp_web.Response(
            body=output.getvalue().encode("utf-8"),
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
        app.router.add_post("/restart", self._restart)
        app.router.add_post("/action", self._action)
        app.router.add_get("/plots/{key}", self._document)
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
        default=Path(__file__).with_name("defaults.toml"),
        help="read-only TOML defaults loaded for each session",
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
