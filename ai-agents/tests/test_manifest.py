"""manifest.py: build on one side, check on the other, compare one digest."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unicodedata
import unittest

from helpers import SCRIPTS, Sandbox, run

MANIFEST = SCRIPTS / "manifest.py"


def manifest(*args):
    return run([sys.executable, MANIFEST] + [str(a) for a in args])


def records(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


class Manifest(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.src = self.sb.root / "src"
        self.files = {
            "a.txt": b"alpha\n",
            "dir/b.bin": os.urandom(3 * 1024 * 1024 + 7),   # spans several 1 MiB chunks
            "dir/with space.txt": b"spaces\n",
            "unicod\u00e9/\u6570\u636e.csv": "x,y\n1,2\n".encode(),
            "empty": b"",
        }
        self.make_tree(self.src)
        self.m = self.sb.root / "src.manifest"

    def tearDown(self):
        self.sb.cleanup()

    def make_tree(self, root, files=None):
        for rel, body in (files or self.files).items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)

    def build(self, root=None, out=None, *extra):
        r = manifest("build", root or self.src, "-o", out or self.m, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def check(self, root, *extra, m=None):
        r = manifest("check", root, m or self.m, *extra)
        recs = records(r.stdout)
        self.assertEqual(recs[-1]["status"], "summary", r.stdout + r.stderr)
        return r, recs[:-1], recs[-1]

    def entries(self, m=None):
        lines = (m or self.m).read_text(encoding="utf-8").splitlines()
        return [line.split("\t") for line in lines if not line.startswith("#")]

    def test_build_format(self):
        self.build()
        lines = self.m.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "# manifest v1")
        self.assertEqual(lines[1], "# root: " + str(self.src.absolute()))
        self.assertRegex(lines[2], r"^# created: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(lines[3], "# hash: sha256")
        rows = self.entries()
        paths = [r[0] for r in rows]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(sorted(paths), sorted(self.files))
        for path, size, sha in rows:
            body = self.files[path]
            self.assertEqual(int(size), len(body))
            self.assertEqual(sha, hashlib.sha256(body).hexdigest())

    def test_build_then_check_passes(self):
        self.build()
        dst = self.sb.root / "dst"
        shutil.copytree(str(self.src), str(dst))
        r, problems, summary = self.check(dst)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(problems, [])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["files_expected"], 5)
        self.assertEqual(summary["files_found"], 5)
        self.assertEqual(summary["bytes_expected"], sum(len(b) for b in self.files.values()))
        self.assertEqual(summary["mode"], "sha256")
        self.assertEqual(summary["digest"], manifest("digest", self.m).stdout.strip())

    def test_missing_file(self):
        self.build()
        (self.src / "dir" / "with space.txt").unlink()
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(problems, [{"status": "missing", "path": "dir/with space.txt",
                                     "size": 7}])
        self.assertEqual(summary["missing"], 1)
        self.assertFalse(summary["ok"])

    def test_extra_file_and_allow_extra(self):
        self.build()
        (self.src / "dir" / "new.txt").write_text("new\n")
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(problems, [{"status": "unexpected", "path": "dir/new.txt", "size": 4}])
        r, problems, summary = self.check(self.src, "--allow-extra")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(problems, [])
        self.assertEqual(summary["unexpected"], 1)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["digest"], manifest("digest", self.m).stdout.strip())

    def test_altered_content_same_size(self):
        self.build()
        (self.src / "a.txt").write_bytes(b"ALPHA\n")
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(problems[0]["status"], "hash")
        self.assertEqual(problems[0]["path"], "a.txt")
        self.assertEqual(problems[0]["found"], hashlib.sha256(b"ALPHA\n").hexdigest())
        r, problems, summary = self.check(self.src, "--sizes-only")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(summary["mode"], "size")

    def test_size_change(self):
        self.build()
        (self.src / "a.txt").write_bytes(b"alpha, longer\n")
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(problems, [{"status": "size", "path": "a.txt", "expected": 6,
                                     "found": 14}])

    def test_no_hash_manifest_is_checked_by_size(self):
        self.build(None, None, "--no-hash")
        self.assertIn("# hash: none", self.m.read_text(encoding="utf-8"))
        self.assertEqual({r[2] for r in self.entries()}, {"-"})
        (self.src / "a.txt").write_bytes(b"ALPHA\n")           # same size: not detectable
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(summary["mode"], "size")
        (self.src / "a.txt").write_bytes(b"alpha!!\n")         # size change: detected
        r, problems, summary = self.check(self.src)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(problems[0]["status"], "size")

    def test_default_and_custom_excludes(self):
        (self.src / ".hist").mkdir()
        (self.src / ".hist" / "up2k.db").write_bytes(b"index")
        (self.src / "dir" / ".hist").mkdir()
        (self.src / "dir" / ".hist" / "th.webp").write_bytes(b"thumb")
        (self.src / ".DS_Store").write_bytes(b"finder")
        (self.src / "dir" / "scratch.tmp").write_bytes(b"tmp")
        (self.src / "logs").mkdir()
        (self.src / "logs" / "run.txt").write_bytes(b"log")
        self.build(None, None, "--exclude", "*.tmp", "--exclude", "logs/*")
        self.assertEqual(sorted(r[0] for r in self.entries()), sorted(self.files))
        r, problems, summary = self.check(self.src, "--exclude", "*.tmp", "--exclude", "logs")
        self.assertEqual(r.returncode, 0, r.stdout)
        m2 = self.sb.root / "all.manifest"
        self.build(None, m2, "--no-default-excludes")
        paths = {r[0] for r in self.entries(m2)}
        self.assertIn(".hist/up2k.db", paths)
        self.assertIn(".DS_Store", paths)

    def test_unicode_normalisation_does_not_matter(self):
        nfc = unicodedata.normalize("NFC", "caf\u00e9.txt")
        nfd = unicodedata.normalize("NFD", nfc)
        a, b = self.sb.root / "nfc", self.sb.root / "nfd"
        self.make_tree(a, {nfc: b"x"})
        self.make_tree(b, {nfd: b"x"})
        self.build(a)
        r, problems, summary = self.check(b)
        self.assertEqual(r.returncode, 0, r.stdout)
        m2 = self.sb.root / "nfd.manifest"
        self.build(b, m2)
        self.assertEqual(manifest("digest", self.m).stdout, manifest("digest", m2).stdout)

    def test_digest_equal_for_identical_trees_and_different_after_a_change(self):
        other = self.sb.root / "copy"
        shutil.copytree(str(self.src), str(other))
        m2 = self.sb.root / "copy.manifest"
        self.build()
        self.build(other, m2)
        d1 = manifest("digest", self.m).stdout.strip()
        self.assertRegex(d1, r"^[0-9a-f]{64}$")
        self.assertEqual(d1, manifest("digest", m2).stdout.strip())
        (other / "a.txt").write_bytes(b"ALPHA\n")
        self.build(other, m2)
        self.assertNotEqual(d1, manifest("digest", m2).stdout.strip())
        self.assertEqual(manifest("digest", self.m, "--sizes-only").stdout,
                         manifest("digest", m2, "--sizes-only").stdout)

    @unittest.skipUnless(shutil.which("shasum"), "needs shasum")
    def test_digest_matches_standard_tools(self):
        self.build()
        script = ("cd \"$1\" && find . -type f | sed 's|^\\./||' | LC_ALL=C sort | "
                  "while IFS= read -r f; do shasum -a 256 \"$f\"; done | LC_ALL=C sort | "
                  "shasum -a 256 | cut -d' ' -f1")
        out = subprocess.run(["bash", "-c", script, "x", str(self.src)], stdout=subprocess.PIPE,
                             universal_newlines=True, env=dict(os.environ, LC_ALL="C"))
        self.assertEqual(out.stdout.strip(), manifest("digest", self.m).stdout.strip())

    def test_symlinks_are_not_followed(self):
        (self.src / "link.txt").symlink_to(self.src / "a.txt")
        (self.src / "linkdir").symlink_to(self.src / "dir")
        r = self.build()
        self.assertIn("2 symlink(s) not followed", r.stderr)
        self.assertEqual(sorted(e[0] for e in self.entries()), sorted(self.files))

    def test_names_with_tab_or_newline_are_refused_with_a_list(self):
        (self.src / "bad\tname.txt").write_bytes(b"1")
        (self.src / "dir" / "bad\nname.txt").write_bytes(b"2")
        r = manifest("build", self.src, "-o", self.m)
        self.assertEqual(r.returncode, 2)
        self.assertIn("'bad\\tname.txt'", r.stderr)
        self.assertIn("'dir/bad\\nname.txt'", r.stderr)
        self.assertFalse(self.m.exists())

    def test_output_inside_the_root_is_not_listed(self):
        inside = self.src / "src.manifest"
        self.build(None, inside)
        self.build(None, inside)                      # a stale copy is not listed either
        self.assertNotIn("src.manifest", [e[0] for e in self.entries(inside)])
        r, problems, summary = self.check(self.src, m=inside)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_stdout_output(self):
        r = manifest("build", self.src, "--no-hash")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("# manifest v1\n"))
        self.assertIn("a.txt\t6\t-\n", r.stdout)

    def test_usage_errors_exit_2(self):
        bad = self.sb.root / "bad.manifest"
        bad.write_text("# manifest v1\n# hash: sha256\na.txt\tsix\tdeadbeef\n")
        self.assertEqual(manifest("check", self.src, bad).returncode, 2)
        bad.write_text("# manifest v1\n../escape\t1\t-\n")
        self.assertEqual(manifest("check", self.src, bad).returncode, 2)
        bad.write_text("not a manifest\n")
        self.assertEqual(manifest("digest", bad).returncode, 2)
        self.assertEqual(manifest("check", self.src, self.sb.root / "nope").returncode, 2)
        self.assertEqual(manifest("build", self.sb.root / "nope").returncode, 2)
        self.assertEqual(manifest().returncode, 2)


if __name__ == "__main__":
    unittest.main()
