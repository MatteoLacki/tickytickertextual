"""Deliver browser button clicks as app messages without keyboard shortcuts."""
from textual.drivers.web_driver import WebDriver
from textual.message import Message


class BrowserAction(Message):
    def __init__(self, action: str):
        super().__init__()
        self.action = action


class BrowserDriver(WebDriver):
    def on_meta(self, packet_type, payload):
        if packet_type == "browser_action":
            self._app.post_message(BrowserAction(str(payload.get("action", ""))))
        else:
            super().on_meta(packet_type, payload)
