#!/usr/bin/env python3
"""A tiny stand-in for a copyparty server, for tests. Serves one local directory over HTTP.

Implements only what the transfer scripts rely on:

- `PUT /path/name` stores the body. Like copyparty without delete permission, it never
  overwrites: a PUT to an existing name is stored under a new name (`name-1`, `name-2`, ...).
  With `readonly=True` every PUT is refused with 403, like a volume served with `:r`.
- `GET /dir/?ls` returns JSON `{"dirs": [...], "files": [...]}`; each entry has `href` (the
  URL-quoted name, with a trailing `/` for directories), `sz` (bytes) and `ts` (mtime).
- `GET /path/name` returns the file, `404` if it does not exist.

`serve(root, readonly=False)` starts it on a free localhost port in a background thread and
returns `(server, base_url)`; call `server.stop()` when done. `server.puts` records the paths
of every PUT received, `server.gets` every file GET.
"""
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output quiet
        pass

    def _local(self, url_path):
        rel = urllib.parse.unquote(url_path.split("?", 1)[0]).lstrip("/")
        p = (self.server.root / rel).resolve()
        if p != self.server.root and self.server.root not in p.parents:
            return None
        return p

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        p = self._local(self.path)
        if p is None:
            return self._send(403, b"forbidden")
        query = urllib.parse.urlsplit(self.path).query
        if "ls" in urllib.parse.parse_qs(query, keep_blank_values=True):
            if not p.is_dir():
                return self._send(404, b"not a directory")
            dirs, files = [], []
            for child in sorted(p.iterdir()):
                st = child.stat()
                entry = {"href": urllib.parse.quote(child.name) + ("/" if child.is_dir() else ""),
                         "sz": 0 if child.is_dir() else st.st_size, "ts": int(st.st_mtime)}
                (dirs if child.is_dir() else files).append(entry)
            body = json.dumps({"dirs": dirs, "files": files}).encode()
            return self._send(200, body, "application/json")
        if not p.is_file():
            return self._send(404, b"not found")
        self.server.gets.append(str(p.relative_to(self.server.root)))
        return self._send(200, p.read_bytes(), "application/octet-stream")

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if self.server.readonly:
            return self._send(403, b"you don't have write-access here")
        p = self._local(self.path)
        if p is None:
            return self._send(403, b"forbidden")
        p.parent.mkdir(parents=True, exist_ok=True)
        target, n = p, 0
        while target.exists():
            n += 1
            target = p.with_name("{}-{}".format(p.name, n))
        target.write_bytes(body)
        self.server.puts.append(str(target.relative_to(self.server.root)))
        return self._send(201, urllib.parse.quote(str(target.relative_to(self.server.root))).encode())


def serve(root, readonly=False):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.root = Path(root).resolve()
    server.readonly = readonly
    server.puts, server.gets = [], []
    server.stop = lambda: (server.shutdown(), server.server_close())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:{}".format(server.server_address[1])


if __name__ == "__main__":
    import sys
    srv, url = serve(sys.argv[1] if len(sys.argv) > 1 else os.getcwd(),
                     readonly="--readonly" in sys.argv)
    print(url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
