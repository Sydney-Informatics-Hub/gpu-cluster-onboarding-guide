#!/usr/bin/env python3
"""manifest.py — record a directory tree as (path, size, sha256) and check a copy against it.

The one integrity tool for every transfer route — Globus, sftp, copyparty, a download inside a
pod. Build a manifest where the data is, move the data by any route, then check the copy where
it landed. Standard library only (Python 3.9+), so it runs on a laptop and inside any pod.

  manifest.py build ~/data/run1 -o run1.manifest                 # on the source (laptop)
  manifest.py build /Volumes/RDS/project/run1 -o run1.manifest   # RDS mounted as a network drive
  manifest.py build $WORKDIR/run1 -o $WORKDIR/run1.manifest       # cluster storage: run it in a
                                                                 # pod, e.g. via apollo_script.sh
  manifest.py check ~/results/run1 run1.manifest                 # on the destination, after
  manifest.py digest run1.manifest                               # one string per side to compare

build ROOT [-o FILE] [--no-hash] [--exclude GLOB ...] [--no-default-excludes] [--progress]
  Writes a TSV (stdout without -o): header lines `# manifest v1`, `# root: ...`,
  `# created: <UTC ISO-8601>`, `# hash: sha256|none`, then `relpath<TAB>size<TAB>sha256`
  (`-` with --no-hash), POSIX separators, sorted. Symlinks are not followed (counted and
  reported); names containing a tab, newline or carriage return are refused with a list.
  Files are hashed in 1 MiB chunks, so any size works. Paths are stored as they are on disk;
  check and digest compare them in Unicode NFC, so a tree read on macOS (which can hand back
  decomposed names, e.g. over SMB) still matches the same tree read on Linux.

check ROOT MANIFEST [--allow-extra] [--sizes-only] [--exclude GLOB ...] [--progress]
  Prints JSON lines {"status": "missing"|"size"|"hash"|"unexpected"|"error", "path": ...}, then
  {"status": "summary", "files_expected", "files_found", "bytes_expected", "missing", "size",
  "hash", "unexpected", "errors", "mode", "digest", "ok"}. A manifest built with --no-hash is
  checked by size automatically. --allow-extra tolerates (and only counts) files not in the
  manifest. `digest` is computed from the manifest's files as found under ROOT, so it equals
  `manifest.py digest MANIFEST` from the other side exactly when everything matched.

digest MANIFEST [--sizes-only]
  Prints sha256 over the sorted lines "<sha256>  <path>\\n" ("<size>  <path>\\n" without hashes).
  For a hashed manifest that is what `sha256sum` prints, so the same value comes from
  `cd ROOT && find . -type f | sed 's|^\\./||' | xargs sha256sum | LC_ALL=C sort | shasum -a 256`
  (for simple names, and with the same excludes).

Excludes: a GLOB without `/` matches any single file or directory name at any depth (`*.tmp`,
`.cache`); a GLOB with `/` matches the whole relative path (`logs/*.txt`). Default excludes are
copyparty's `.hist` index directories and macOS `.DS_Store` files; --no-default-excludes keeps
them. The output file of `build -o` is never listed in itself.

Exit status: 0 success (check: everything matched); 1 check found a mismatch, or build could not
read some files; 2 usage error, unreadable manifest, or names that cannot be represented.
"""
import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import stat
import sys
import time
import unicodedata

CHUNK = 1 << 20
DEFAULT_EXCLUDES = (".hist", ".DS_Store")
BAD_CHARS = ("\t", "\n", "\r")
ENC = dict(encoding="utf-8", errors="surrogateescape")  # round-trips non-UTF-8 names on Linux


class UsageError(Exception):
    pass


def warn(msg):
    sys.stderr.write("manifest: {}\n".format(msg))


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(n)
        n /= 1000.0
    return str(n)


class Progress:
    """Prints `done/total` to stderr at most every few seconds."""

    def __init__(self, enabled, files_total, bytes_total, every=2.0):
        self.enabled, self.every = enabled, every
        self.files_total, self.bytes_total = files_total, bytes_total
        self.files = self.bytes = 0
        self.start = self.last = time.monotonic()

    def add(self, nbytes, file_done=False):
        self.bytes += nbytes
        if file_done:
            self.files += 1
        now = time.monotonic()
        if self.enabled and now - self.last >= self.every:
            self.last = now
            self.report(now)

    def report(self, now=None):
        if not self.enabled:
            return
        now = now or time.monotonic()
        rate = self.bytes / max(now - self.start, 1e-6)
        warn("hashed {}/{} files, {} of {}, {}/s".format(
            self.files, self.files_total, human(self.bytes), human(self.bytes_total),
            human(rate)))


def excluded(relpath, name, patterns):
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatchcase(relpath, pat.strip("/")):
                return True
        elif fnmatch.fnmatchcase(name, pat):
            return True
    return False


def nfc(path):
    return unicodedata.normalize("NFC", path)


def skip_set(*paths):
    """Identify our own output files so a manifest never lists itself: by path and by inode."""
    names, inodes = set(), set()
    for p in paths:
        names.add(os.path.abspath(p))
        try:
            st = os.stat(p)
            inodes.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return names, inodes


def walk(root, patterns, skip=(set(), set())):
    """Regular files under root: ({nfc relpath: (ondisk relpath, abspath, size)}, report)."""
    files = {}
    report = {"symlinks": [], "special": [], "bad_names": [], "collisions": [], "errors": []}

    def onerror(err):
        report["errors"].append("{}: {}".format(err.filename, err.strerror))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            rel = rel_dir + "/" + d if rel_dir else d
            if excluded(rel, d, patterns):
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                report["symlinks"].append(rel)  # os.walk lists links to directories here
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in filenames:
            rel = rel_dir + "/" + f if rel_dir else f
            if excluded(rel, f, patterns):
                continue
            full = os.path.join(dirpath, f)
            try:
                st = os.lstat(full)
            except OSError as e:
                report["errors"].append("{}: {}".format(rel, e.strerror))
                continue
            if stat.S_ISLNK(st.st_mode):
                report["symlinks"].append(rel)
                continue
            if not stat.S_ISREG(st.st_mode):
                report["special"].append(rel)
                continue
            if full in skip[0] or (st.st_dev, st.st_ino) in skip[1]:
                continue
            if any(c in rel for c in BAD_CHARS):
                report["bad_names"].append(rel)
                continue
            key = nfc(rel)
            if key in files:
                report["collisions"].append("{!r} and {!r}".format(files[key][0], rel))
                continue
            files[key] = (rel, full, st.st_size)
    return files, report


def hash_file(path, progress=None):
    """(sha256 hex, bytes read), streaming in 1 MiB chunks."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
            if progress:
                progress.add(len(b))
    if progress:
        progress.add(0, file_done=True)
    return h.hexdigest(), n


def digest_of(entries, hashed):
    """entries: iterable of (path, size, sha). See the module docstring for the definition."""
    if hashed:
        lines = ["{}  {}\n".format(sha, path) for path, size, sha in entries]
    else:
        lines = ["{}  {}\n".format(size, path) for path, size, sha in entries]
    data = sorted(line.encode(**ENC) for line in lines)
    return hashlib.sha256(b"".join(data)).hexdigest()


def read_manifest(path):
    """Parse a manifest: (header dict, [(path, size, sha or None)]). Raises UsageError."""
    try:
        with open(path, "rb") as f:
            text = f.read().decode(**ENC)
    except OSError as e:
        raise UsageError("cannot read manifest {}: {}".format(path, e.strerror))
    header, entries, seen = {}, [], set()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or lines[0].strip() != "# manifest v1":
        raise UsageError("{} is not a manifest (first line must be '# manifest v1')".format(path))
    for no, line in enumerate(lines, 1):
        if line.startswith("#"):
            key, sep, val = line[1:].strip().partition(":")
            if sep:
                header[key.strip()] = val.strip()
            continue
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise UsageError("{} line {}: expected path<TAB>size<TAB>sha256".format(path, no))
        rel, size, sha = parts
        if not size.isdigit():
            raise UsageError("{} line {}: size is not a number".format(path, no))
        if sha == "-":
            sha = None
        elif len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise UsageError("{} line {}: not a sha256 hex digest".format(path, no))
        comps = rel.split("/")
        if rel.startswith("/") or any(c in ("", ".", "..") for c in comps):
            raise UsageError("{} line {}: unsafe path {!r}".format(path, no, rel))
        if rel in seen:
            raise UsageError("{} line {}: duplicate path {!r}".format(path, no, rel))
        seen.add(rel)
        entries.append((rel, int(size), sha))
    hashed = header.get("hash", "sha256" if any(e[2] for e in entries) else "none") == "sha256"
    if hashed and any(e[2] is None for e in entries):
        raise UsageError("{}: header says sha256 but some entries have no hash".format(path))
    return header, entries, hashed


def patterns_from(args):
    pats = list(args.exclude or [])
    if not args.no_default_excludes:
        pats.extend(DEFAULT_EXCLUDES)
    return pats


def report_walk(report, strict):
    """Warn about what the walk skipped; with strict, refuse what cannot be represented."""
    if report["symlinks"]:
        warn("{} symlink(s) not followed and not listed, e.g. {}".format(
            len(report["symlinks"]), ", ".join(report["symlinks"][:5])))
    if report["special"]:
        warn("{} special file(s) (fifo, socket, device) skipped, e.g. {}".format(
            len(report["special"]), ", ".join(report["special"][:5])))
    bad = []
    if report["bad_names"]:
        bad.append("names containing a tab, newline or carriage return cannot be listed; "
                   "rename or --exclude them:\n  " +
                   "\n  ".join(repr(n) for n in report["bad_names"]))
    if report["collisions"]:
        bad.append("names that differ only in Unicode normalisation:\n  " +
                   "\n  ".join(report["collisions"]))
    if bad and strict:
        raise UsageError("\n".join(bad))
    for b in bad:
        warn(b)


def cmd_build(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise UsageError("not a directory: {}".format(args.root))
    out = os.path.abspath(args.output) if args.output else None
    skip = skip_set(out, out + ".tmp") if out else (set(), set())
    files, report = walk(root, patterns_from(args), skip)
    report_walk(report, strict=True)

    keys = sorted(files, key=lambda k: files[k][0])
    total = sum(files[k][2] for k in keys)
    progress = Progress(args.progress and not args.no_hash, len(keys), total)
    rows, errors = [], list(report["errors"])
    for key in keys:
        rel, full, size = files[key]
        if args.no_hash:
            rows.append((rel, size, "-"))
            continue
        try:
            sha, nread = hash_file(full, progress)
        except OSError as e:
            errors.append("{}: {}".format(rel, e.strerror))
            continue
        if nread != size:
            warn("{} changed size while being read ({} -> {} bytes); is it still being "
                 "written?".format(rel, size, nread))
        rows.append((rel, nread, sha))
    progress.report()
    if errors:
        warn("could not read {} item(s); no manifest written (fix permissions or --exclude):"
             "\n  {}".format(len(errors), "\n  ".join(errors[:50])))
        return 1

    created = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    head = ["# manifest v1",
            "# root: " + root.replace("\n", "\\n"),
            "# created: " + created.isoformat().replace("+00:00", "Z"),
            "# hash: " + ("none" if args.no_hash else "sha256")]
    body = "".join(line + "\n" for line in head)
    body += "".join("{}\t{}\t{}\n".format(*r) for r in rows)
    if out:
        tmp = out + ".tmp"
        with open(tmp, "w", newline="\n", **ENC) as f:
            f.write(body)
        os.replace(tmp, out)
        warn("{} files, {} -> {}".format(len(rows), human(sum(r[1] for r in rows)), out))
    else:
        sys.stdout.buffer.write(body.encode(**ENC))
        sys.stdout.flush()
    return 0


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


def cmd_check(args):
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        raise UsageError("not a directory: {}".format(args.root))
    header, entries, hashed = read_manifest(args.manifest)
    if args.sizes_only:
        hashed = False
    files, report = walk(root, patterns_from(args), skip_set(args.manifest))
    report_walk(report, strict=False)

    expected = {nfc(rel): (rel, size, sha) for rel, size, sha in entries}
    counts = dict(missing=0, size=0, hash=0, unexpected=0, errors=0)
    found_rows = []
    to_hash = sum(files[k][2] for k in expected if k in files) if hashed else 0
    progress = Progress(args.progress and hashed, sum(1 for k in expected if k in files), to_hash)
    for key in sorted(expected):
        rel, size, sha = expected[key]
        if key not in files:
            counts["missing"] += 1
            emit({"status": "missing", "path": rel, "size": size})
            continue
        disk_rel, full, disk_size = files[key]
        disk_sha = None
        if hashed:
            try:
                disk_sha, disk_size = hash_file(full, progress)
            except OSError as e:
                counts["errors"] += 1
                emit({"status": "error", "path": rel, "error": e.strerror})
                continue
        found_rows.append((key, disk_size, disk_sha))
        if disk_size != size:
            counts["size"] += 1
            emit({"status": "size", "path": rel, "expected": size, "found": disk_size})
        elif hashed and disk_sha != sha:
            counts["hash"] += 1
            emit({"status": "hash", "path": rel, "expected": sha, "found": disk_sha})
    progress.report()
    for key in sorted(set(files) - set(expected)):
        counts["unexpected"] += 1
        if not args.allow_extra:
            emit({"status": "unexpected", "path": files[key][0], "size": files[key][2]})
    counts["errors"] += len(report["errors"])
    for err in report["errors"]:
        emit({"status": "error", "path": None, "error": err})

    ok = (counts["missing"] == 0 and counts["size"] == 0 and counts["hash"] == 0
          and counts["errors"] == 0 and (args.allow_extra or counts["unexpected"] == 0))
    summary = {"status": "summary", "files_expected": len(entries),
               "files_found": len(found_rows), "bytes_expected": sum(e[1] for e in entries)}
    summary.update(counts)
    summary.update({"mode": "sha256" if hashed else "size", "digest": digest_of(found_rows, hashed),
                    "ok": ok})
    emit(summary)
    warn("{}: {} of {} files found, {} missing, {} size, {} hash, {} unexpected{} ({} check)"
         .format("OK" if ok else "MISMATCH", len(found_rows), len(entries), counts["missing"],
                 counts["size"], counts["hash"], counts["unexpected"],
                 " (allowed)" if args.allow_extra and counts["unexpected"] else "",
                 "sha256" if hashed else "size-only"))
    return 0 if ok else 1


def cmd_digest(args):
    header, entries, hashed = read_manifest(args.manifest)
    if args.sizes_only:
        hashed = False
    print(digest_of(((nfc(p), s, h) for p, s, h in entries), hashed))
    return 0


def parser():
    p = argparse.ArgumentParser(prog="manifest.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    def excludes(sp):
        sp.add_argument("--exclude", action="append", metavar="GLOB",
                        help="skip names/paths matching GLOB (repeatable)")
        sp.add_argument("--no-default-excludes", action="store_true",
                        help="also list .hist/ and .DS_Store")
        sp.add_argument("--progress", action="store_true", help="report progress on stderr")

    b = sub.add_parser("build", help="write a manifest of ROOT")
    b.add_argument("root")
    b.add_argument("-o", "--output", metavar="FILE", help="write here instead of stdout")
    b.add_argument("--no-hash", action="store_true", help="sizes only (fast, weaker)")
    excludes(b)

    c = sub.add_parser("check", help="check ROOT against MANIFEST")
    c.add_argument("root")
    c.add_argument("manifest")
    c.add_argument("--allow-extra", action="store_true",
                   help="files under ROOT that are not in the manifest are not an error")
    c.add_argument("--sizes-only", action="store_true", help="compare sizes, skip hashing")
    excludes(c)

    d = sub.add_parser("digest", help="print the digest of MANIFEST")
    d.add_argument("manifest")
    d.add_argument("--sizes-only", action="store_true", help="digest of sizes, not hashes")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help(sys.stderr)
        return 2
    try:
        return {"build": cmd_build, "check": cmd_check, "digest": cmd_digest}[args.cmd](args)
    except UsageError as e:
        warn(str(e))
        return 2
    except KeyboardInterrupt:
        warn("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
