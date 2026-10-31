#!/usr/bin/env python3
"""Render entry point for the VektorCode dashboard."""
from http.server import ThreadingHTTPServer

from app import Handler, PORT, db


if __name__ == "__main__":
    db().close()
    print(f"VektorCode dashboard: http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
