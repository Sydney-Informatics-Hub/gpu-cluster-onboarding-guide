"""pvc_sync_code.sh: code into a running workspace through chunked `runai workspace exec` calls."""
import os
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

SCRIPT = SCRIPTS / "pvc_sync_code.sh"
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"   # 3.2 on macOS


class PvcSyncCode(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.src = self.sb.root / "project"
        self.rtmp = self.sb.root / "remote-tmp"
        self.rtmp.mkdir()
        self.files = {
            "train.py": b"print('train')\n",
            "src/model.py": b"class Model:\n    pass\n",
            "src/my module.py": "# café — unicode\n".encode(),
            "src/sub/conf.yaml": b"lr: 0.001\n",
            "configs/a.json": b'{"a": 1}\n',
        }
        self.ignored = {
            "src/weights.bin": b"\x00" * 100,
            "src/.secret.json": b"{}",
            "src/__pycache__/model.cpython-312.py": b"x",
            ".git/hooks/x.py": b"x",
        }
        for rel, data in list(self.files.items()) + list(self.ignored.items()):
            p = self.src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    def tearDown(self):
        self.sb.cleanup()

    def sync(self, *args, **env):
        e = {"REMOTE_TMP": str(self.rtmp)}
        e.update(env)
        return run([BASH, SCRIPT] + list(args), env=self.sb.env(**e), cwd=self.src)

    def execs(self):
        return calls_matching(self.sb.runai_calls(), "workspace", "exec")

    def test_round_trip_is_byte_identical(self):
        r = self.sync("devbox", "code/v1", "train.py", "src", "configs/a.json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("5 file(s) extracted", r.stdout)
        dest = self.sb.workdir / "code" / "v1"
        for rel, data in self.files.items():
            self.assertEqual((dest / rel).read_bytes(), data, rel)
        for rel in self.ignored:
            self.assertFalse((dest / rel).exists(), rel)
        got = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
        self.assertEqual(got, sorted(self.files))
        self.assertEqual(list(self.rtmp.iterdir()), [], "remote temp files left behind")
        for argv in self.execs():
            self.assertEqual(argv[argv.index("-p") + 1], "test-proj")
            self.assertNotIn("-i", argv)

    def test_chunking_makes_several_calls_each_under_the_cap(self):
        big = os.urandom(6000).hex().encode()   # incompressible-ish text
        (self.src / "src" / "big.txt").write_bytes(big)
        r = self.sync("devbox", "code", "src", CHUNK_CHARS="1000")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        execs = self.execs()
        self.assertGreaterEqual(len(execs), 5)
        for argv in execs:
            cmd = argv[argv.index("--") + 1:]
            self.assertLess(len(" ".join(cmd)), 10000)
        self.assertEqual((self.sb.workdir / "code" / "src" / "big.txt").read_bytes(), big)

    def test_default_chunks_stay_under_the_cap(self):
        (self.src / "big.txt").write_bytes(os.urandom(20000).hex().encode())
        r = self.sync("devbox", "code", "big.txt")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGreaterEqual(len(self.execs()), 3)
        for argv in self.execs():
            self.assertLess(len(" ".join(argv[argv.index("--") + 1:])), 10000)

    def test_disallowed_extension_is_refused_before_any_exec(self):
        (self.src / "notes.md").write_text("# notes\n")
        (self.src / "data.csv").write_text("a,b\n")
        r = self.sync("devbox", "code", "train.py", "notes.md", "data.csv")
        self.assertEqual(r.returncode, 2)
        self.assertIn("notes.md", r.stderr)
        self.assertIn("data.csv", r.stderr)
        self.assertEqual(self.sb.runai_calls(), [])

    def test_unsafe_destinations_and_paths_are_refused(self):
        for args in (["devbox", "../elsewhere", "train.py"], ["devbox", "/abs/path", "train.py"],
                     ["devbox", "code/../../x", "train.py"], ["devbox", "code", "../train.py"],
                     ["devbox", "code", str(self.src / "train.py")]):
            with self.subTest(args=args):
                r = self.sync(*args)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.sb.runai_calls(), [])

    def test_dry_run_sends_nothing(self):
        r = self.sync("--dry-run", "devbox", "code", "train.py", "src")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertIn("src/model.py", r.stdout)
        self.assertEqual(self.sb.runai_calls(), [])

    def test_exec_failure_is_reported(self):
        self.sb.set_runai_scenario({"rules": [{"match": ["workspace", "exec"], "rc": 1,
                                               "stderr": "Error: pod not found"}]})
        r = self.sync("devbox", "code", "train.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("pod not found", r.stderr)
        self.assertIn("workspace list", r.stderr)

    def test_a_lost_chunk_fails_the_checksum_and_extracts_nothing(self):
        (self.src / "big.txt").write_bytes(os.urandom(3000).hex().encode())
        # The first chunk "succeeds" without running, so the remote copy is incomplete.
        self.sb.set_runai_scenario({"rules": [{"match": ["workspace", "exec"], "rc": 0,
                                               "times": 1}]})
        r = self.sync("devbox", "code", "big.txt", CHUNK_CHARS="1000")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("remote exit code 3", r.stderr)
        self.assertIn("checksum mismatch", r.stderr)
        self.assertFalse((self.sb.workdir / "code").exists())


if __name__ == "__main__":
    unittest.main()
