"""pvc_download.py: results back from a read-only copyparty workspace, verified by manifest."""
import json
import os
import sys
import unittest

from helpers import SCRIPTS, Sandbox, run
from fakes.fake_copyparty import serve

SCRIPT = SCRIPTS / "pvc_download.py"
MANIFEST = SCRIPTS / "manifest.py"


class PvcDownload(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.served = self.sb.workdir                      # what copyparty exposes
        self.results = self.served / "results" / "run1"    # the manifest's root
        self.files = {
            "metrics.json": b'{"acc": 0.9}\n',
            "model/weights.bin": os.urandom(2 * 1024 * 1024 + 5),
            "plots/fig 1 é#?.txt": "unicode name\n".encode(),
            "empty.log": b"",
        }
        for rel, body in self.files.items():
            p = self.results / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        self.manifest = self.sb.root / "run1.manifest"
        self.build_manifest()
        self.dest = self.sb.root / "laptop" / "run1"
        self.log = self.sb.root / "down.jsonl"
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()
        self.sb.cleanup()

    def build_manifest(self, *extra):
        r = run([sys.executable, MANIFEST, "build", self.results, "-o", self.manifest] +
                list(extra))
        self.assertEqual(r.returncode, 0, r.stderr)

    def start(self, **kw):
        if self.srv:
            self.srv.stop()
        self.srv, self.url = serve(self.served, readonly=True, **kw)

    def download(self, *extra):
        argv = [sys.executable, SCRIPT, "--url", self.url, "--manifest", self.manifest,
                "--dest", self.dest, "--remote-prefix", "results/run1", "--log", self.log]
        return run(argv + list(extra), env=self.sb.env())

    def statuses(self):
        recs = [json.loads(line) for line in self.log.read_text().splitlines()]
        return {r["path"]: r["status"] for r in recs}

    def parts(self):
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*.part"))

    def test_dry_run_downloads_nothing(self):
        self.start()
        r = self.download()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("files        4", r.stdout)
        self.assertIn("sha256", r.stdout)
        self.assertIn("user's OK", r.stdout)
        self.assertEqual(self.srv.requests, [])
        self.assertFalse(self.dest.exists())

    def test_good_files_land_verified_and_a_rerun_keeps_them(self):
        self.start()
        r = self.download("--yes", "--workers", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel, body in self.files.items():
            self.assertEqual((self.dest / rel).read_bytes(), body, rel)
        self.assertEqual(set(self.statuses().values()), {"downloaded"})
        self.assertEqual(self.parts(), [])
        check = run([sys.executable, MANIFEST, "check", self.dest, self.manifest])
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

        gets = len(self.srv.gets)
        self.log.unlink()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(set(self.statuses().values()), {"present"})
        self.assertEqual(len(self.srv.gets), gets, "verified files must not be fetched again")

    def test_corrupted_remote_file_is_deleted_and_reported(self):
        target = self.results / "model" / "weights.bin"
        data = bytearray(target.read_bytes())
        data[1000] ^= 0xFF                               # same size, different content
        target.write_bytes(bytes(data))
        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("MISMATCH", r.stdout)
        self.assertEqual(self.statuses()["model/weights.bin"], "mismatch")
        self.assertFalse((self.dest / "model" / "weights.bin").exists())
        self.assertEqual(self.parts(), [])
        self.assertEqual((self.dest / "metrics.json").read_bytes(), self.files["metrics.json"])

    def test_remote_file_with_another_size_is_a_mismatch(self):
        (self.results / "metrics.json").write_bytes(b'{"acc": 0.95}\n')
        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.statuses()["metrics.json"], "mismatch")
        self.assertFalse((self.dest / "metrics.json").exists())
        self.assertEqual(self.parts(), [])

    def test_interrupted_transfer_leaves_only_the_part_and_a_rerun_finishes(self):
        self.start(cut={"results/run1/model/weights.bin": 100000})
        r = self.download("--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.statuses()["model/weights.bin"], "error")
        self.assertIn("kept", r.stdout)
        self.assertFalse((self.dest / "model" / "weights.bin").exists())
        self.assertEqual(self.parts(), ["model/weights.bin.part"])
        self.assertEqual((self.dest / "model" / "weights.bin.part").stat().st_size, 100000)

        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.dest / "model" / "weights.bin").read_bytes(),
                         self.files["model/weights.bin"])
        self.assertEqual(self.parts(), [])

    def test_missing_remote_file_is_reported(self):
        (self.results / "empty.log").unlink()
        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.statuses()["empty.log"], "missing")

    def test_existing_different_local_file_is_not_overwritten(self):
        (self.dest).mkdir(parents=True)
        (self.dest / "metrics.json").write_bytes(b"my own notes\n")
        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.statuses()["metrics.json"], "conflict")
        self.assertEqual((self.dest / "metrics.json").read_bytes(), b"my own notes\n")

    def test_manifest_without_hashes_checks_sizes(self):
        self.build_manifest("--no-hash")
        self.start()
        r = self.download("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("size only", r.stdout)

    def test_unsafe_manifest_and_non_local_url_are_refused(self):
        self.start()
        text = self.manifest.read_text().replace("metrics.json", "../../metrics.json")
        self.manifest.write_text(text)
        r = self.download("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unsafe path", r.stderr)
        self.build_manifest()
        self.url = "http://files.example.org:8080"
        r = self.download("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("localhost", r.stderr)
        self.assertEqual(self.srv.requests, [])


if __name__ == "__main__":
    unittest.main()
