"""pvc_upload.py: laptop -> PVC through a copyparty workspace (here: the fake copyparty)."""
import hashlib
import importlib.util
import json
import os
import sys
import unittest

from helpers import SCRIPTS, Sandbox, run
from fakes.fake_copyparty import serve

SCRIPT = SCRIPTS / "pvc_upload.py"


def load_module():
    spec = importlib.util.spec_from_file_location("pvc_upload", str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PvcUpload(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.data = self.sb.root / "laptop" / "data"
        self.remote = self.sb.root / "remote"
        self.remote.mkdir()
        self.files = {
            "a.txt": b"hello\n",
            "sub/b.bin": os.urandom(3 * 1024 * 1024 + 17),   # several 1 MiB chunks
            "sub/c d é#?.txt": "unicode, space, # and ?\n".encode(),
            ".hidden.txt": b"dotfile\n",
            "empty.txt": b"",
        }
        for rel, body in self.files.items():
            p = self.data / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        self.list = self.sb.root / "upload.tsv"
        self.write_list({str(self.data / rel): "run1/" + rel for rel in self.files})
        self.log = self.sb.root / "up.jsonl"
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.stop()
        self.sb.cleanup()

    def write_list(self, mapping):
        self.list.write_text("# local\tremote\n" + "".join(
            "{}\t{}\n".format(k, v) for k, v in mapping.items()), encoding="utf-8")

    def start(self, **kw):
        self.srv, self.url = serve(self.remote, **kw)

    def upload(self, *extra, allow=None):
        argv = [sys.executable, SCRIPT, self.list, "--allow-root", allow or self.data,
                "--url", self.url, "--retries", "2", "--backoff", "0", "--log", self.log]
        return run(argv + list(extra), env=self.sb.env())

    def log_statuses(self):
        recs = [json.loads(line) for line in self.log.read_text().splitlines()]
        return {r["remote"]: r["status"] for r in recs}

    def test_dry_run_sends_nothing(self):
        self.start()
        r = self.upload()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("files        5", r.stdout)
        self.assertIn("{:,} bytes".format(sum(len(b) for b in self.files.values())), r.stdout)
        self.assertIn(self.url + "/", r.stdout)
        self.assertIn("run1/ (5 files)", r.stdout)
        self.assertEqual(self.srv.requests, [])
        self.assertFalse(self.log.exists())

    def test_yes_uploads_byte_exact_then_rerun_skips_everything(self):
        self.start()
        r = self.upload("--yes", "--workers", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for rel, body in self.files.items():
            self.assertEqual((self.remote / "run1" / rel).read_bytes(), body, rel)
        self.assertEqual(set(self.log_statuses().values()), {"sent"})
        self.assertEqual(len(self.srv.puts), 5)

        self.log.unlink()
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(set(self.log_statuses().values()), {"skipped"})
        self.assertEqual(len(self.srv.puts), 5, "a re-run must not PUT again")
        # Each remote directory was listed once per run.
        lists = [p for m, p in self.srv.requests if m == "GET" and "?ls" in p]
        self.assertEqual(len(lists), 4)

    def test_path_outside_allow_root_is_refused_before_anything_is_sent(self):
        self.start()
        outside = self.sb.root / "secret.txt"
        outside.write_text("not for upload\n")
        mapping = {str(self.data / "a.txt"): "run1/a.txt", str(outside): "run1/secret.txt"}
        self.write_list(mapping)
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("outside every --allow-root", r.stderr)
        self.assertIn("secret.txt", r.stderr)
        self.assertEqual(self.srv.requests, [])

    def test_symlink_escaping_the_allow_root_is_refused(self):
        self.start()
        outside = self.sb.root / "secret.txt"
        outside.write_text("not for upload\n")
        link = self.data / "innocent.txt"
        os.symlink(str(outside), str(link))
        self.write_list({str(link): "run1/innocent.txt"})
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("outside every --allow-root", r.stderr)
        self.assertEqual(self.srv.requests, [])

    def test_symlinked_allow_root_still_works(self):
        self.start()
        alias = self.sb.root / "alias"
        os.symlink(str(self.data), str(alias))
        self.write_list({str(alias / "a.txt"): "run1/a.txt"})
        r = self.upload("--yes", allow=alias)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_size_mismatched_remote_file_is_reported_and_not_put_again(self):
        self.start()
        (self.remote / "run1").mkdir()
        (self.remote / "run1" / "a.txt").write_bytes(b"older, longer version\n")
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("MISMATCH run1/a.txt", r.stdout)
        self.assertIn("never overwritten", r.stdout)
        self.assertEqual(self.log_statuses()["run1/a.txt"], "mismatch")
        self.assertNotIn("run1/a.txt", self.srv.puts)
        self.assertFalse(any(p.startswith("run1/a.txt-") for p in self.srv.puts))
        self.assertEqual((self.remote / "run1" / "a.txt").read_bytes(), b"older, longer version\n")
        self.assertEqual(len(self.srv.puts), 4)

    def test_one_file_sends_exactly_the_smallest_non_empty_file(self):
        self.start()
        r = self.upload("--yes", "--one-file")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.srv.puts, ["run1/a.txt"])

    def test_transient_server_errors_are_retried(self):
        self.start(put_errors={"run1/a.txt": 2})
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.srv.puts.count("run1/a.txt"), 1)
        self.assertEqual((self.remote / "run1" / "a.txt").read_bytes(), b"hello\n")

    def test_retries_give_up_with_an_error(self):
        self.start(put_errors={"run1/a.txt": 5})
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.log_statuses()["run1/a.txt"], "error")
        self.assertIn("after 3 attempts", r.stdout)

    def test_read_only_server_is_an_error(self):
        self.start(readonly=True)
        r = self.upload("--yes", "--one-file")
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 403", r.stdout)

    def test_only_localhost_urls_are_accepted(self):
        self.url = "http://data.example.org:8080"
        r = self.upload("--yes")
        self.assertEqual(r.returncode, 2)
        self.assertIn("localhost", r.stderr)

    def test_unsafe_remote_paths_are_refused(self):
        self.start()
        for remote in ("../escape.txt", "run1/../../x", "run1/.hist/x", "run1//x"):
            with self.subTest(remote=remote):
                self.write_list({str(self.data / "a.txt"): remote})
                self.assertEqual(self.upload("--yes").returncode, 2)
        self.assertEqual(self.srv.requests, [])


class CheckReply(unittest.TestCase):
    """The PUT reply is copyparty's: size, sha512 (b64), sha512 hex[:56], URL; Location header."""

    def setUp(self):
        self.mod = load_module()
        self.body = b"hello\n"
        self.sha = hashlib.sha512(self.body).hexdigest()

    def reply(self, size, hexpart, url):
        return "{}\nxxxx\n{}\n{}\n".format(size, hexpart, url).encode()

    def test_good_reply(self):
        url = "http://127.0.0.1:1/run1/a%20b.txt"
        self.assertIsNone(self.mod.check_reply("run1/a b.txt", 6, url,
                                               self.reply(6, self.sha[:56], url), self.sha))

    def test_renamed_copy_is_a_mismatch(self):
        url = "http://127.0.0.1:1/run1/a.txt-1771978661.726032-127.0.0.1"
        status, detail = self.mod.check_reply("run1/a.txt", 6, url,
                                              self.reply(6, self.sha[:56], url), self.sha)
        self.assertEqual(status, "mismatch")
        self.assertIn("stored it as", detail)

    def test_wrong_size_or_hash_is_an_error(self):
        url = "http://127.0.0.1:1/run1/a.txt"
        self.assertEqual(self.mod.check_reply("run1/a.txt", 6, url,
                                              self.reply(5, self.sha[:56], url), self.sha)[0],
                         "error")
        self.assertEqual(self.mod.check_reply("run1/a.txt", 6, url,
                                              self.reply(6, "0" * 56, url), self.sha)[0], "error")

    def test_unrecognised_reply_is_accepted(self):
        self.assertIsNone(self.mod.check_reply("run1/a.txt", 6, None, b"ok", self.sha))


if __name__ == "__main__":
    unittest.main()
