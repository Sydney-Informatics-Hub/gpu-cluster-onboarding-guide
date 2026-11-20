#!/usr/bin/env python3
"""pvc_download.py — fetch results from cluster storage through a read-only copyparty workspace.

The fallback route back to this machine when Globus is not available. A copyparty server runs in
a CPU workspace with the PVC mounted read-only, and `runai workspace port-forward` exposes it on
localhost. MANIFEST lists what to fetch. Build it on the cluster with `manifest.py build`,
hashed if possible, so every file is verified against what was there.

Moving data off the cluster needs the user's OK first, so this is a DRY RUN unless --yes is
given. With --yes, each manifest entry goes through these steps:

- If DEST/<path> already matches the manifest's size and sha256, it is kept and not downloaded.
- If DEST/<path> exists and differs, it is reported as a conflict and left alone. Local files are
  never overwritten; move it away and re-run.
- Otherwise the file is streamed to DEST/<path>.part while being hashed, and size and sha256 are
  compared with the manifest. A match is renamed into place. A mismatch has its .part deleted and
  is reported.
- If the connection breaks mid-file, the .part is left behind, and a re-run starts that file again.

  manifest.py build $WORKDIR/results/run1 -o $WORKDIR/results/run1.manifest     # in a pod
  pvc_download.py --url http://localhost:8080 --remote-prefix results/run1 \\
      --manifest run1.manifest --dest ~/results/run1                             # dry run
  pvc_download.py ... --yes --workers 4 --log down.jsonl

To get the manifest itself, fetch it like any file, e.g.
`curl -o run1.manifest http://localhost:8080/results/run1.manifest`.

Options: --remote-prefix P (where the manifest's root is under --url), --workers N (parallel
downloads, one keep-alive connection each; default 4), --timeout S (socket timeout, default 60),
--log FILE (append one JSON line per file: path, bytes, status, seconds, detail). Statuses:
downloaded | present (already matched) | mismatch | missing | conflict | error.

Exit status: 0 every file is present in DEST and verified; 1 anything else (see the summary);
2 usage error or an unreadable or unsafe manifest.
"""
import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

CHUNK = 1 << 20
LOOPBACK = ("localhost", "127.0.0.1", "::1")
ENC = dict(encoding="utf-8", errors="surrogateescape")
OK = ("downloaded", "present")


class UsageError(Exception):
    pass


def human(n):
    for unit, f in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= f:
            return "{:.1f} {}".format(n / f, unit)
    return "{} B".format(n)


def read_manifest(path):
    """[(relpath, size, sha256 or None)] from a `manifest.py build` file (same rules as there)."""
    try:
        with open(path, "rb") as f:
            lines = f.read().decode(**ENC).split("\n")
    except OSError as e:
        raise UsageError("cannot read manifest {}: {}".format(path, e.strerror))
    if not lines or lines[0].strip() != "# manifest v1":
        raise UsageError("{} is not a manifest (first line must be '# manifest v1')".format(path))
    entries, seen = [], set()
    for no, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1].isdigit():
            raise UsageError("{} line {}: expected path<TAB>size<TAB>sha256".format(path, no))
        rel, size, sha = parts
        sha = None if sha == "-" else sha
        if sha is not None and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise UsageError("{} line {}: not a sha256 hex digest".format(path, no))
        # The path becomes a local file name: nothing absolute, no `..`, no empty parts.
        if rel.startswith("/") or any(c in ("", ".", "..") for c in rel.split("/")) \
                or "\\" in rel or "\0" in rel:
            raise UsageError("{} line {}: unsafe path {!r}".format(path, no, rel))
        if rel in seen:
            raise UsageError("{} line {}: duplicate path {!r}".format(path, no, rel))
        seen.add(rel)
        entries.append((rel, int(size), sha))
    clash = sorted(r for r, _, _ in entries if r.endswith(".part") and r[:-5] in seen)
    if clash:
        raise UsageError("the manifest lists both X and X.part (the temporary name used while "
                         "downloading): {}".format(", ".join(clash[:5])))
    return entries


def loopback_url(url):
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or not host:
        raise UsageError("--url must look like http://localhost:PORT, got {!r}".format(url))
    if host not in LOOPBACK and not host.endswith(".localhost"):
        raise UsageError("--url must point at localhost (the `runai workspace port-forward` "
                         "tunnel); {} is another host and is refused".format(host))
    return u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80), \
        u.path.rstrip("/")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


class Interrupted(Exception):
    """The connection ended before the announced length: keep the .part."""


class Fetcher:
    def __init__(self, scheme, host, port, prefix, timeout, progress):
        self.cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        self.host, self.port, self.prefix, self.timeout = host, port, prefix, timeout
        self.progress = progress
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

    def fetch(self, remote, part, expected_size):
        """Stream remote into part: ("ok", sha256, n) | ("missing"|"error", detail, None)."""
        path = self.prefix + "/" + urllib.parse.quote(remote, safe="/")
        try:
            c = self.conn()
            c.request("GET", path)
            r = c.getresponse()
            if r.status != 200:
                r.read()
                return ("missing" if r.status == 404 else "error"), \
                    "HTTP {} for /{}".format(r.status, remote), None
            length = r.getheader("Content-Length")
            length = int(length) if length and length.isdigit() else None
            if length is not None and length != expected_size:
                self.reset()  # don't drain a body we already know is wrong
                return "mismatch", "the server has {} bytes, the manifest says {}".format(
                    length, expected_size), None
            h, n = hashlib.sha256(), 0
            with open(part, "wb") as f:
                while True:
                    b = r.read(CHUNK)
                    if not b:
                        break
                    f.write(b)
                    h.update(b)
                    n += len(b)
                    self.progress.add_bytes(len(b))
            # http.client returns b"" (no exception) when the peer closes early.
            if length is not None and n < length:
                self.reset()
                raise Interrupted("connection closed after {} of {} bytes".format(n, length))
            return "ok", h.hexdigest(), n
        except (OSError, http.client.HTTPException) as e:
            self.reset()
            raise Interrupted("{}: {}".format(type(e).__name__, e))


class Progress:
    def __init__(self, files, nbytes, every):
        self.files, self.nbytes, self.every = files, nbytes, every
        self.lock = threading.Lock()
        self.got = 0
        self.counts = {}
        self.start = time.monotonic()
        self.stop = threading.Event()

    def add_bytes(self, n):
        with self.lock:
            self.got += n

    def done(self, status):
        with self.lock:
            self.counts[status] = self.counts.get(status, 0) + 1

    def line(self):
        with self.lock:
            secs = max(time.monotonic() - self.start, 1e-6)
            counts, got = dict(self.counts), self.got
        parts = ", ".join("{} {}".format(v, k) for k, v in sorted(counts.items()))
        return "{}/{} files ({}), {} of {} received in {:.0f}s, {:.1f} MB/s".format(
            sum(counts.values()), self.files, parts or "none yet", human(got), human(self.nbytes),
            secs, got / secs / 1e6)

    def reporter(self):
        while not self.stop.wait(self.every):
            sys.stderr.write("pvc_download: " + self.line() + "\n")


def local_path(dest, rel):
    return os.path.join(dest, *rel.split("/"))


def download_one(fetcher, entry, args):
    rel, size, sha = entry
    final = local_path(args.dest, rel)
    remote = (args.remote_prefix + "/" + rel) if args.remote_prefix else rel
    if os.path.lexists(final):
        if not os.path.isfile(final) or os.path.islink(final):
            return "conflict", "{} exists and is not a regular file".format(final)
        if os.path.getsize(final) == size and (sha is None or sha256_of(final) == sha):
            return "present", None
        return "conflict", ("{} exists and differs from the manifest; it is not overwritten. "
                            "Move it away and re-run".format(final))
    os.makedirs(os.path.dirname(final), exist_ok=True)
    part = final + ".part"
    try:
        status, got, n = fetcher.fetch(remote, part, size)
    except Interrupted as e:
        return "error", "{}; {} kept, a re-run starts it again".format(e, os.path.basename(part))
    if status != "ok":
        if os.path.exists(part):
            os.unlink(part)
        return status, got
    if n != size or (sha is not None and got != sha):
        os.unlink(part)
        what = "size {} (manifest {})".format(n, size) if n != size else \
            "sha256 {} (manifest {})".format(got, sha)
        return "mismatch", "downloaded {}; deleted. The remote file changed since the manifest " \
                           "was built, or was corrupted".format(what)
    os.replace(part, final)
    return "downloaded", None if sha else "size checked only (manifest has no hashes)"


def main(argv=None):
    p = argparse.ArgumentParser(prog="pvc_download.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="http://localhost:PORT[/path] of copyparty")
    p.add_argument("--manifest", required=True, help="built on the cluster with manifest.py")
    p.add_argument("--dest", required=True, help="local directory to fill")
    p.add_argument("--remote-prefix", default="", help="the manifest root's path under --url")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--yes", action="store_true", help="download (default: dry run)")
    p.add_argument("--log", metavar="FILE", help="append one JSON line per file")
    p.add_argument("--progress-every", type=float, default=5.0, help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    try:
        if args.workers < 1:
            raise UsageError("--workers must be >= 1")
        scheme, host, port, prefix = loopback_url(args.url)
        prefix_parts = [x for x in args.remote_prefix.split("/") if x]
        if any(x in (".", "..") for x in prefix_parts):
            raise UsageError("--remote-prefix must not contain '.' or '..'")
        args.remote_prefix = "/".join(prefix_parts)
        entries = read_manifest(args.manifest)
        args.dest = os.path.abspath(os.path.expanduser(args.dest))
        if os.path.exists(args.dest) and not os.path.isdir(args.dest):
            raise UsageError("--dest {} exists and is not a directory".format(args.dest))
    except UsageError as e:
        sys.stderr.write("pvc_download: {}\n".format(e))
        return 2

    total = sum(e[1] for e in entries)
    hashed = all(e[2] for e in entries)
    source = args.url.rstrip("/") + "/" + (args.remote_prefix + "/" if args.remote_prefix else "")
    if not args.yes:
        present = sum(1 for rel, size, _ in entries
                      if os.path.isfile(local_path(args.dest, rel))
                      and os.path.getsize(local_path(args.dest, rel)) == size)
        print("pvc_download: DRY RUN — nothing is downloaded.")
        print("  files        {}".format(len(entries)))
        print("  total        {} ({:,} bytes)".format(human(total), total))
        print("  from         {}".format(source))
        print("  into         {}".format(args.dest))
        print("  verified by  {}".format("sha256 and size" if hashed else
                                         "size only (the manifest has no hashes)"))
        if present:
            print("  {} file(s) already there with the right size (checked again, not "
                  "re-downloaded)".format(present))
        print("pvc_download: moving data off the cluster needs the user's OK; after they "
              "approve, re-run with --yes.")
        return 0

    os.makedirs(args.dest, exist_ok=True)
    progress = Progress(len(entries), total, args.progress_every)
    fetcher = Fetcher(scheme, host, port, prefix, args.timeout, progress)
    log_lock = threading.Lock()
    log = open(args.log, "a", encoding="utf-8") if args.log else None

    def work(entry):
        t0 = time.monotonic()
        try:
            status, detail = download_one(fetcher, entry, args)
        except OSError as e:  # local trouble (disk full, permissions): report, carry on
            status, detail = "error", "{}: {}".format(type(e).__name__, e)
        rec = {"path": entry[0], "bytes": entry[1], "status": status,
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
            results = list(pool.map(work, entries))
    except KeyboardInterrupt:
        sys.stderr.write("pvc_download: interrupted; re-run to continue (verified files are "
                         "kept)\n")
        return 1
    finally:
        progress.stop.set()
        if log:
            log.close()

    print("pvc_download: " + progress.line())
    bad = [r for r in results if r["status"] not in OK]
    for r in bad[:50]:
        print("  {:<10} {}: {}".format(r["status"].upper(), r["path"], r.get("detail", "")))
    if len(bad) > 50:
        print("  ... and {} more{}".format(len(bad) - 50, " (see --log)" if args.log else ""))
    if not bad:
        print("pvc_download: all {} files present in {} and verified by {}.".format(
            len(results), args.dest, "sha256" if hashed else "size only"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
