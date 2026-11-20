#!/usr/bin/env python3
"""pvc_upload.py — upload files from this machine to cluster storage through a copyparty workspace.

The fallback route when Globus and the Research Data Store are not available. A copyparty file
server runs in a CPU workspace with the project's PVC mounted, and `runai workspace port-forward`
makes it reachable on localhost. This script PUTs a list of files to it. Safety is built in:

- DRY RUN unless --yes: it prints what would be sent and sends nothing.
- Only files under an --allow-root are read. Symlinks are resolved first, and the whole list is
  checked before any file is opened.
- Only a localhost URL is accepted (the port-forward). Data does not go to other hosts.
- Nothing remote is ever overwritten. A remote file with the same name and size is skipped
  (resume). One with a different size is reported for a human decision and not sent again,
  because copyparty would store a renamed copy beside it.
- Each upload is checked against the server's reply: the stored size, the server's SHA-512, and
  the stored name.

  pvc_upload.py upload.tsv --allow-root ~/data --url http://localhost:8080        # dry run
  pvc_upload.py upload.tsv --allow-root ~/data --url http://localhost:8080 --one-file --yes
  pvc_upload.py upload.tsv --allow-root ~/data --url http://localhost:8080 --yes --log up.jsonl

LIST is a TSV with one `local_path<TAB>remote_path` per line, or `-` for stdin. Lines starting
with # and blank lines are ignored. remote_path is relative to --url and uses `/`. One way to
build the list from a manifest:

  manifest.py build ~/data/run1 -o run1.manifest
  awk -F'\\t' -v src="$HOME/data/run1" '!/^#/ {print src "/" $1 "\\trun1/" $1}' \\
      run1.manifest > upload.tsv

Afterwards, check on the cluster side, in a pod: manifest.py check $WORKDIR/run1 run1.manifest.
A matching size is what makes resume skip a file, and size alone is not a content check.

Options: --workers N (parallel uploads, one keep-alive connection each; default 4),
--retries N and --backoff S (retry connection errors and 5xx after S, 2S, 4S... seconds;
defaults 3 and 2), --timeout S (socket timeout, default 60), --log FILE (append one JSON line
per file: path, remote, bytes, status sent|skipped|mismatch|error, seconds, detail).

Exit status: 0 every file was sent or skipped; 1 any mismatch or error (see the summary and the
log); 2 usage error or a refused list (nothing was sent).
"""
import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

CHUNK = 1 << 20
LOOPBACK = ("localhost", "127.0.0.1", "::1")


class UsageError(Exception):
    pass


def human(n):
    for unit, f in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= f:
            return "{:.1f} {}".format(n / f, unit)
    return "{} B".format(n)


def loopback_url(url):
    """(scheme, host, port, path prefix) of a localhost URL; anything else is refused."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise UsageError("--url must look like http://localhost:PORT, got {!r}".format(url))
    if host not in LOOPBACK and not host.endswith(".localhost"):
        raise UsageError("--url must point at localhost (the `runai workspace port-forward` "
                         "tunnel); {} is another host and is refused".format(host))
    if u.query or u.fragment:
        raise UsageError("--url must not have a query or fragment")
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), \
        u.path.rstrip("/")


def clean_remote(rel):
    """A safe relative remote path, or None."""
    rel = rel.lstrip("/")
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..", ".hist") for p in parts):
        return None
    if any(c in rel for c in "\r\n\t\0"):
        return None
    return rel


def read_list(path, allow_roots):
    """Validate the whole list before anything is opened: [(realpath, remote, size)]."""
    roots = []
    for r in allow_roots:
        real = os.path.realpath(os.path.expanduser(r))
        if not os.path.isdir(real):
            raise UsageError("--allow-root {} is not a directory".format(r))
        if real == os.path.dirname(real):
            raise UsageError("--allow-root / would allow everything; name the data directory")
        roots.append(real)
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        raise UsageError("cannot read {}: {}".format(path, e.strerror))
    entries, problems, seen = [], [], {}
    for no, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            problems.append("line {}: expected local_path<TAB>remote_path".format(no))
            continue
        local, remote = parts
        real = os.path.realpath(os.path.expanduser(local))
        if not any(os.path.commonpath([root, real]) == root for root in roots):
            problems.append("line {}: {} resolves to {}, outside every --allow-root".format(
                no, local, real))
            continue
        rel = clean_remote(remote)
        if rel is None:
            problems.append("line {}: unsafe remote path {!r}".format(no, remote))
            continue
        if rel in seen:
            problems.append("line {}: remote path {} is also on line {}".format(no, rel, seen[rel]))
            continue
        seen[rel] = no
        try:
            st = os.stat(real)
        except OSError as e:
            problems.append("line {}: {}: {}".format(no, local, e.strerror))
            continue
        if not os.path.isfile(real):
            problems.append("line {}: {} is not a regular file".format(no, local))
            continue
        entries.append((real, rel, st.st_size))
    if problems:
        more = "" if len(problems) <= 50 else "\n  ... and {} more".format(len(problems) - 50)
        raise UsageError("refused the list; nothing was sent:\n  " + "\n  ".join(problems[:50])
                         + more)
    if not entries:
        raise UsageError("the list is empty")
    return entries


class Client:
    """One keep-alive HTTP connection per thread."""

    def __init__(self, scheme, host, port, prefix, timeout):
        self.cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        self.host, self.port, self.prefix, self.timeout = host, port, prefix, timeout
        self.local = threading.local()

    def conn(self):
        c = getattr(self.local, "conn", None)
        if c is None:
            c = self.local.conn = self.cls(self.host, self.port, timeout=self.timeout)
        return c

    def reset(self):
        c = getattr(self.local, "conn", None)
        if c is not None:
            c.close()
        self.local.conn = None

    def path(self, rel):
        return self.prefix + "/" + urllib.parse.quote(rel, safe="/")

    def listing(self, rel_dir):
        """{name: size, or None for a directory} of one remote directory; {} if it is absent."""
        path = self.path(rel_dir + "/" if rel_dir else "") + "?ls&dots"
        try:
            c = self.conn()
            c.request("GET", path)
            r = c.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException):
            self.reset()
            raise
        if r.status == 404:
            return {}
        if r.status != 200:
            raise RuntimeError("HTTP {} listing /{}: {}".format(
                r.status, rel_dir, body[:200].decode("utf-8", "replace").strip()))
        doc = json.loads(body.decode("utf-8"))
        res = {}
        for kind in ("dirs", "files"):
            for e in doc.get(kind) or []:
                # href is the URL-quoted name, `/`-terminated for directories, and may carry a
                # `?k=` file key.
                name = urllib.parse.unquote(e.get("href", "").split("?", 1)[0].rstrip("/"))
                name = name.rsplit("/", 1)[-1]
                if name:
                    res[name] = None if kind == "dirs" else int(e.get("sz", -1))
        return res

    def put(self, local, rel, size, progress):
        """Stream one file: (HTTP status, Location, body, local sha512 hex)."""
        h = hashlib.sha512()
        sent = 0
        try:
            c = self.conn()
            c.putrequest("PUT", self.path(rel), skip_accept_encoding=True)
            c.putheader("Content-Length", str(size))
            c.putheader("Content-Type", "application/octet-stream")
            # Ask for sha512 in the reply even if the volume is set to another algorithm.
            c.putheader("ck", "sha512")
            c.endheaders()
            with open(local, "rb") as f:
                while sent < size:
                    b = f.read(min(CHUNK, size - sent))
                    if not b:
                        break
                    c.send(b)
                    h.update(b)
                    sent += len(b)
                    progress.add_bytes(len(b))
            if sent != size:
                self.reset()
                raise FileChanged("{} shrank while being sent ({} of {} bytes); a partial copy "
                                  "may be left on the server".format(local, sent, size))
            r = c.getresponse()
            body = r.read()
        except (OSError, http.client.HTTPException):
            self.reset()
            raise
        return r.status, r.getheader("Location"), body, h.hexdigest()


class FileChanged(Exception):
    pass


def check_reply(rel, size, location, body, sha512hex):
    """None if copyparty's reply confirms the upload, else (status, detail)."""
    if location:
        stored = urllib.parse.unquote(urllib.parse.urlsplit(location).path)
        if stored != "/" + rel and not stored.endswith("/" + rel):
            return ("mismatch", "the server stored it as {}: a file of that name appeared on the "
                    "server meanwhile; review both there".format(stored))
    # Default reply: "<size>\n<sha512 base64>\n<sha512 hex, 56 chars>\n<url>\n".
    lines = body.decode("utf-8", "replace").split("\n")
    if len(lines) >= 3 and lines[0].strip().isdigit():
        if int(lines[0]) != size:
            return ("error", "the server stored {} bytes, expected {}; the remote copy is bad, "
                    "remove it there before re-sending".format(lines[0].strip(), size))
        if re.fullmatch(r"[0-9a-f]{56}", lines[2].strip()) and lines[2].strip() != sha512hex[:56]:
            return ("error", "the server's checksum differs from the local file (corrupted in "
                    "transit); the remote copy is bad, remove it there before re-sending")
    return None


class Progress:
    def __init__(self, files, nbytes, every):
        self.files, self.nbytes, self.every = files, nbytes, every
        self.lock = threading.Lock()
        self.sent_bytes = 0
        self.counts = {"sent": 0, "skipped": 0, "mismatch": 0, "error": 0}
        self.start = time.monotonic()
        self.stop = threading.Event()

    def add_bytes(self, n):
        with self.lock:
            self.sent_bytes += n

    def done(self, status):
        with self.lock:
            self.counts[status] += 1

    def line(self):
        with self.lock:
            secs = max(time.monotonic() - self.start, 1e-6)
            c = dict(self.counts)
            sent = self.sent_bytes
        done = sum(c.values())
        return ("{}/{} files ({} sent, {} skipped, {} mismatch, {} error), {} sent in {:.0f}s, "
                "{:.1f} MB/s".format(done, self.files, c["sent"], c["skipped"], c["mismatch"],
                                     c["error"], human(sent), secs, sent / secs / 1e6))

    def reporter(self):
        while not self.stop.wait(self.every):
            sys.stderr.write("pvc_upload: " + self.line() + "\n")


def split(rel):
    return (rel.rsplit("/", 1) if "/" in rel else ("", rel))


def retrying(fn, retries, backoff):
    """fn() with retries on connection errors and 5xx (RuntimeError from listing)."""
    attempt = 0
    while True:
        try:
            return fn()
        except (OSError, http.client.HTTPException, RuntimeError, ValueError):
            if attempt >= retries:
                raise
        time.sleep(backoff * (2 ** attempt))
        attempt += 1


def upload_one(client, entry, listings, args, progress):
    local, rel, size = entry
    d, name = split(rel)
    listing = listings[d]
    if isinstance(listing, Exception):
        return "error", "could not list the remote directory: {}".format(listing)
    if name in listing:
        if listing[name] is None:
            return "mismatch", "a directory of that name exists on the server"
        if listing[name] == size:
            return "skipped", None
        return ("mismatch", "the server already has this file with {} bytes, the local one has "
                "{}; never overwritten, a human must decide".format(listing[name], size))
    attempt, err = 0, None
    while True:
        try:
            status, location, body, sha = client.put(local, rel, size, progress)
            if 200 <= status < 300:
                bad = check_reply(rel, size, location, body, sha)
                return bad if bad else ("sent", None)
            text = body[:200].decode("utf-8", "replace").strip()
            if status < 500:
                return "error", "HTTP {}: {}".format(status, text)
            err = "HTTP {}: {}".format(status, text)
        except FileChanged as e:
            return "error", str(e)
        except (OSError, http.client.HTTPException) as e:
            err = "{}: {}".format(type(e).__name__, e)
        if attempt >= args.retries:
            return "error", "{} (after {} attempts)".format(err, attempt + 1)
        time.sleep(args.backoff * (2 ** attempt))
        attempt += 1
        # copyparty keeps what a broken upload wrote, and a new PUT to an existing name is
        # stored under another name; so look before sending again.
        try:
            fresh = retrying(lambda: client.listing(d), args.retries, args.backoff)
        except Exception as e:
            return "error", "{}; and the directory could not be re-listed: {}".format(err, e)
        if name in fresh:
            if fresh[name] == size:
                return "sent", "the reply was lost ({}), but the server has the full size".format(
                    err)
            return ("mismatch", "a failed attempt ({}) left {} of {} bytes on the server; remove "
                    "it there, then re-run".format(err, fresh[name], size))


def main(argv=None):
    p = argparse.ArgumentParser(prog="pvc_upload.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("list", metavar="LIST", help="TSV of local_path<TAB>remote_path, or -")
    p.add_argument("--allow-root", action="append", required=True, metavar="DIR",
                   help="only files under DIR may be read (repeatable)")
    p.add_argument("--url", required=True, help="http://localhost:PORT[/path] of copyparty")
    p.add_argument("--yes", action="store_true", help="send (default: dry run)")
    p.add_argument("--one-file", action="store_true", help="only the smallest file: a route test")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--backoff", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--log", metavar="FILE", help="append one JSON line per file")
    p.add_argument("--progress-every", type=float, default=5.0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    try:
        if args.workers < 1 or args.retries < 0 or args.backoff < 0:
            raise UsageError("--workers must be >= 1, --retries and --backoff >= 0")
        scheme, host, port, prefix = loopback_url(args.url)
        entries = read_list(args.list, args.allow_root)
    except UsageError as e:
        sys.stderr.write("pvc_upload: {}\n".format(e))
        return 2

    if args.one_file:  # the smallest file; a non-empty one if there is any, to exercise the data path
        entries = [min(entries, key=lambda e: (e[2] == 0, e[2], e[1]))]
    total = sum(e[2] for e in entries)
    base = args.url.rstrip("/") + "/"
    if not args.yes:
        tops = {}
        for _, rel, _ in entries:
            top = rel.split("/", 1)[0] + "/" if "/" in rel else "(top level)"
            tops[top] = tops.get(top, 0) + 1
        print("pvc_upload: DRY RUN — nothing is sent.")
        print("  files        {}".format(len(entries)))
        print("  total        {} ({:,} bytes)".format(human(total), total))
        print("  destination  {}".format(base))
        print("  remote dirs  " + ", ".join("{} ({} file{})".format(t, n, "" if n == 1 else "s")
                                            for t, n in sorted(tops.items())))
        if args.one_file:
            print("  route test   only {} -> {} ({} bytes)".format(*entries[0]))
        print("pvc_upload: show this to the user; after they approve, re-run with --yes "
              "(--one-file --yes first tests the route with the smallest file).")
        return 0

    client = Client(scheme, host, port, prefix, args.timeout)
    progress = Progress(len(entries), total, args.progress_every)
    log_lock = threading.Lock()
    log = open(args.log, "a", encoding="utf-8") if args.log else None
    entries.sort(key=lambda e: e[1])
    results = []

    def list_dir(d):
        try:
            return d, retrying(lambda: client.listing(d), args.retries, args.backoff)
        except Exception as e:  # becomes an error for each file in that directory
            return d, e

    def work(entry):
        t0 = time.monotonic()
        try:
            status, detail = upload_one(client, entry, listings, args, progress)
        except Exception as e:  # one bad file must not stop the others
            status, detail = "error", "{}: {}".format(type(e).__name__, e)
        rec = {"path": entry[0], "remote": entry[1], "bytes": entry[2], "status": status,
               "seconds": round(time.monotonic() - t0, 3)}
        if detail:
            rec["detail"] = detail
        progress.done(status)
        if log:
            with log_lock:
                log.write(json.dumps(rec, ensure_ascii=False) + "\n")
                log.flush()
        return rec

    reporter = threading.Thread(target=progress.reporter, daemon=True)
    reporter.start()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # Each remote directory is listed once, up front, to find what is already there.
            listings = dict(pool.map(list_dir, sorted({split(e[1])[0] for e in entries})))
            results = list(pool.map(work, entries))
    except KeyboardInterrupt:
        sys.stderr.write("pvc_upload: interrupted; re-run to resume (complete files are "
                         "skipped)\n")
        return 1
    finally:
        progress.stop.set()
        if log:
            log.close()

    print("pvc_upload: " + progress.line())
    bad = [r for r in results if r["status"] in ("mismatch", "error")]
    for r in bad[:50]:
        print("  {:<8} {}: {}".format(r["status"].upper(), r["remote"], r.get("detail", "")))
    if len(bad) > 50:
        print("  ... and {} more{}".format(len(bad) - 50, " (see --log)" if args.log else ""))
    if not bad:
        print("pvc_upload: done. Verify on the cluster side with `manifest.py check` (a size "
              "match is what resume relies on; it is not a content check).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
