"""Bounded cleanup for browser app subprocesses, including analysis threads."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import signal

from textual_serve.app_service import AppService


class BrowserAppService(AppService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stop_lock = asyncio.Lock()

    def _build_environment(self, width=80, height=24):
        environment = super()._build_environment(width, height)
        environment["TEXTUAL_DRIVER"] = "tickytickertextual.browser_driver:BrowserDriver"
        return environment

    async def _open_app_process(self, width=80, height=24):
        self._process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "exec " + self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._build_environment(width, height),
            start_new_session=True,
        )
        self._stdin = self._process.stdin
        return self._process

    async def stop(self):
        async with self._stop_lock:
            process = self._process
            if process is None:
                return
            await self._download_manager.cancel_app_downloads(app_service_id=self.app_service_id)
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self._task = None
            try:
                if process.returncode is None:
                    with suppress(TimeoutError, ConnectionError, BrokenPipeError):
                        async with asyncio.timeout(2):
                            await self.send_meta({"type": "quit"})
                            await process.communicate()
            finally:
                if process.returncode is None:
                    # Kill only the process group created for this UI session.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.communicate()
                if self._stdin is not None:
                    self._stdin.close()
                    with suppress(ConnectionError, BrokenPipeError):
                        await self._stdin.wait_closed()
                self._process = None
