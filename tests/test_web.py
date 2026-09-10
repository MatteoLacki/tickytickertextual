"""Exercise browser startup URLs through the actual HTTP routes."""

import asyncio
from html.parser import HTMLParser
from pathlib import Path
import shlex
import sys
from urllib.parse import urlsplit

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import pytest

from tickytickertextual.web import PlotServer


class PageLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.script = None
        self.stylesheet = None
        self.websocket = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and "src" in attrs:
            self.script = attrs["src"]
        if tag == "link" and attrs.get("href", "").endswith("xterm.css"):
            self.stylesheet = attrs["href"]
        if attrs.get("id") == "terminal":
            self.websocket = attrs["data-session-websocket-url"]


@pytest.mark.parametrize("public_url", [None, "https://viewer.example/app/"])
def test_browser_urls_follow_request_or_explicit_proxy(public_url):
    async def exercise():
        server = PlotServer(
            "true", host="0.0.0.0", public_url=public_url,
            templates_path=Path(__file__).parents[1] / "src/tickytickertextual/templates",
        )
        async with TestClient(TestServer(await server._make_app())) as client:
            for host in ("192.168.1.222:8000", "localhost:8000"):
                response = await client.get("/", headers={"Host": host})
                assert response.status == 200
                html = await response.text()
                links = PageLinks(html)
                origin = public_url.rstrip("/") if public_url else f"http://{host}"
                assert links.script == origin + "/static/js/textual.js"
                assert links.stylesheet == origin + "/static/css/xterm.css"
                assert links.websocket == origin.replace("https:", "wss:").replace("http:", "ws:") + "/ws"
                assert "0.0.0.0" not in html
                if not public_url:
                    for asset in (links.script, links.stylesheet):
                        asset_response = await client.get(urlsplit(asset).path)
                        assert asset_response.status == 200
                        assert await asset_response.read()
    asyncio.run(exercise())


def test_browser_websocket_starts_app(tmp_path):
    async def exercise():
        command = shlex.join([
            sys.executable, "-m", "tickytickertextual.app", str(tmp_path),
            "--settings", str(tmp_path / "settings.toml"),
            "--lock-file", str(tmp_path / "session.lock"),
        ])
        server = PlotServer(
            command, host="0.0.0.0",
            templates_path=Path(__file__).parents[1] / "src/tickytickertextual/templates",
        )
        async with TestClient(TestServer(await server._make_app())) as client:
            page = await client.get("/", headers={"Host": "192.168.1.222:8000"})
            links = PageLinks(await page.text())
            async with client.ws_connect(urlsplit(links.websocket).path) as websocket:
                async with asyncio.timeout(20):
                    while True:
                        message = await websocket.receive()
                        if message.type == WSMsgType.BINARY:
                            assert message.data
                            break
                        assert message.type == WSMsgType.TEXT, message
            async with asyncio.timeout(10):
                while server._connected_uis:
                    await asyncio.sleep(0.01)
    asyncio.run(exercise())


def test_memory_plot_downloads_and_restart(tmp_path):
    async def exercise():
        server = PlotServer("true", bridge_token="secret")
        async with TestClient(TestServer(await server._make_app())) as client:
            content = '<svg xmlns="http://www.w3.org/2000/svg"><text>' + 'HeLa µ ' * 200000 + '</text></svg>'
            payload = {"document": {"content": content, "filename": "dominant-charge.svg",
                                    "mime": "image/svg+xml", "disposition": "attachment"}}
            assert (await client.post('/_publish', json=payload)).status == 403
            response = await client.post('/_publish', json=payload, headers={"Authorization": "Bearer secret"})
            url = (await response.json())["url"]
            for _ in range(2):
                download = await client.get(url)
                assert download.status == 200
                assert await download.read() == content.encode('utf-8')
                assert 'attachment' in download.headers['Content-Disposition']
            assert (await client.post('/restart')).status == 403
            response = await client.post('/restart', headers={"X-Restart-Token": server._restart_token})
            assert response.status == 200
            assert not server.documents and not server.exports_ready
            assert (await client.get(url)).status == 404
    asyncio.run(exercise())


def test_restart_terminates_unresponsive_session_and_releases_lock(tmp_path):
    async def exercise():
        import fcntl
        script = (
            "import fcntl,time; "
            f"lock=open({str(tmp_path / 'session.lock')!r},'w'); "
            "fcntl.flock(lock,fcntl.LOCK_EX); "
            "print('__GANGLION__',flush=True); time.sleep(60)"
        )
        server = PlotServer(shlex.join([sys.executable, '-c', script]))
        async with TestClient(TestServer(await server._make_app())) as client:
            socket = await client.ws_connect('/ws')
            async with asyncio.timeout(5):
                while not server._sessions or not (tmp_path / 'session.lock').exists():
                    await asyncio.sleep(.01)
            service = next(iter(server._sessions))
            process = service._process
            async with asyncio.timeout(6):
                response = await client.post('/restart', headers={"X-Restart-Token": server._restart_token})
            assert response.status == 200 and process.returncode is not None
            with (tmp_path / 'session.lock').open() as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert not server._sessions
            await socket.close()
            # A fresh connection gets a fresh process after the reset.
            new_socket = await client.ws_connect('/ws')
            async with asyncio.timeout(5):
                while not server._sessions:
                    await asyncio.sleep(.01)
            assert next(iter(server._sessions))._process.pid != process.pid
            await new_socket.close()
    asyncio.run(exercise())
