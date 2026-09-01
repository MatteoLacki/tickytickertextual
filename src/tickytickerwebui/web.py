"""Self-host the Textual fileviewer in a browser."""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import Sequence

from textual_serve.server import Server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("working_directory", nargs="?", type=Path, help="filesystem root to expose (default: current directory)")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", default=8000, type=int, help="listen port")
    parser.add_argument("--public-url", help="public URL when running behind a proxy")
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

    command_parts = [sys.executable, "-m", "tickytickerwebui.app", str(root)]
    if args.show_hidden:
        command_parts.append("--show-hidden")
    command = shlex.join(command_parts)

    server = Server(
        command,
        host=args.host,
        port=args.port,
        title=f"fileviewer · {root}",
        public_url=args.public_url,
    )
    server.serve()


if __name__ == "__main__":
    main()

