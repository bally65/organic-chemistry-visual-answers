#!/usr/bin/env python3
"""
Tiny static file server for previewing the answer-key HTML in a headless browser.

Why a custom server instead of `python -m http.server`: in some sandboxes os.getcwd()
is blocked, which crashes the stock server. This serves a fixed ABSOLUTE directory and
never calls getcwd, so it works under those restrictions.

Usage:  python serve.py <directory-to-serve> [port]
Then point the preview tool at http://127.0.0.1:<port>/<file>.html
"""
import sys, http.server, socketserver, functools

directory = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8731
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
socketserver.TCPServer.allow_reuse_address = True
print(f"serving {directory} at http://127.0.0.1:{port}")
with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
    httpd.serve_forever()
