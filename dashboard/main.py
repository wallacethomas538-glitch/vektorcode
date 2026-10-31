#!/usr/bin/env python3
"""Render entry point for the VektorCode dashboard."""
from http.server import ThreadingHTTPServer

from app import HOST, Handler, PORT, db


if __name__ == "__main__":
    db().close()
    print(f"VektorCode dashboard: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
