#!/usr/bin/env python3
"""A tiny stand-in for a copyparty server, for tests. Serves one local directory over HTTP.

Implements only what the transfer scripts rely on, shaped after copyparty's own code
(copyparty/httpcli.py, `tx_browser`, `tx_ls`, `handle_stash`; checked 2026-10-14):

- `PUT /path/name` stores the body and answers 201 with a `Location` header and the body
  "<size>\\n<sha512 base64>\\n<sha512 hex, first 56 chars>\\n<url>\\n". Like copyparty without
  delete permission, it never overwrites: a PUT to an existing name is stored under a new name
  (`name-1`, `name-2`, ...; real copyparty appends a timestamp) and the reply's URL says so.
  With `readonly=True` every PUT is refused with 403, like a volume served with `:r`.
- `GET /dir/?ls` returns JSON `{"dirs": [...], "files": [...], ...}`; each entry has `href`
  (the URL-quoted name, with a trailing `/` for directories), `sz` (bytes), `ts` (mtime),
  `lead`, `ext` and `tags`. Dotfiles are left out unless the query also has `dots`.
- `GET /path/name` returns the file, `404` if it does not exist.

`serve(root, readonly=False, cut=None, put_errors=None)` starts it on a free localhost port in a
background thread and returns `(server, base_url)`; call `server.stop()` when done.
`cut={relpath: n}` makes a GET of that file announce the full length, send `n` bytes and close
the connection (an interrupted download). `put_errors={relpath: n}` answers the first `n` PUTs
to that path with 503 without storing anything. `server.puts` records the paths of every PUT
stored, `server.gets` every file GET, `server.requests` every (method, path) received.
"""
import base64
import hashlib
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

    def _rel(self, p):
        return str(p.relative_to(self.server.root)).replace(os.sep, "/")

    def _send(self, code, body=b"", ctype="text/plain", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(("GET", self.path))
        p = self._local(self.path)
        if p is None:
            return self._send(403, b"forbidden")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                      keep_blank_values=True)
        if "ls" in query:
            if not p.is_dir():
                return self._send(404, b"not a directory")
            dirs, files = [], []
            for child in sorted(p.iterdir()):
                if child.name.startswith(".") and "dots" not in query:
                    continue
                st = child.stat()
                is_dir = child.is_dir()
                entry = {"lead": "-",
                         "href": urllib.parse.quote(child.name) + ("/" if is_dir else ""),
                         "sz": 0 if is_dir else st.st_size,
                         "ext": "---" if is_dir else (child.suffix.lstrip(".") or "%"),
                         "ts": int(st.st_mtime), "tags": {}}
                (dirs if is_dir else files).append(entry)
            body = json.dumps({"dirs": dirs, "files": files, "taglist": [], "acct": "*",
                               "perms": ["read"] if self.server.readonly else
                               ["read", "write"]}).encode()
            return self._send(200, body, "application/json")
        if not p.is_file():
            return self._send(404, b"not found")
        rel = self._rel(p)
        self.server.gets.append(rel)
        data = p.read_bytes()
        if rel in self.server.cut:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data[:self.server.cut[rel]])
            self.wfile.flush()
            self.close_connection = True
            return None
        return self._send(200, data, "application/octet-stream")

    def do_PUT(self):
        self.server.requests.append(("PUT", self.path))
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        if self.server.readonly:
            return self._send(403, b"you don't have write-access here")
        p = self._local(self.path)
        if p is None:
            return self._send(403, b"forbidden")
        rel = self._rel(p)
        with self.server.lock:
            if self.server.put_errors.get(rel, 0) > 0:
                self.server.put_errors[rel] -= 1
                return self._send(503, b"server busy")
            p.parent.mkdir(parents=True, exist_ok=True)
            target, n = p, 0
            while target.exists():
                n += 1
                target = p.with_name("{}-{}".format(p.name, n))
            target.write_bytes(body)
            self.server.puts.append(self._rel(target))
        url = "http://{}:{}/{}".format(self.server.server_address[0],
                                       self.server.server_address[1],
                                       urllib.parse.quote(self._rel(target)))
        sha = hashlib.sha512(body).digest()
        reply = "{}\n{}\n{}\n{}\n".format(len(body), base64.urlsafe_b64encode(sha)[:44].decode(),
                                          sha.hex()[:56], url)
        return self._send(201, reply.encode(), headers={"Location": url})


def serve(root, readonly=False, cut=None, put_errors=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.root = Path(root).resolve()
    server.readonly = readonly
    server.cut = dict(cut or {})
    server.put_errors = dict(put_errors or {})
    server.lock = threading.Lock()
    server.puts, server.gets, server.requests = [], [], []
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
