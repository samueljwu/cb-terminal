"""Run the stdlib CB Terminal browser application."""

from __future__ import annotations

import argparse

from cb_terminal.web.server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the CB Terminal")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host; default is private localhost")
    parser.add_argument("--port", type=int, default=8000, help="Bind port; default 8000")
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
