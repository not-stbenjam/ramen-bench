#!/usr/bin/env python3
"""Serve the public benchmark without exposing local dot-directories or judge credentials."""

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class PublicFiles(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_head(self):
        parts = Path(unquote(urlsplit(self.path).path)).parts
        if any(part.startswith(".") for part in parts):
            self.send_error(404)
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--bind", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), PublicFiles)
    print(f"Serving public Ramen Bench files on port {args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
