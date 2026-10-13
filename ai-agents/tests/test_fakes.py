"""The fakes must behave like the real tools in the ways the other tests depend on."""
import json
import unittest
import urllib.request

from helpers import Sandbox, run, calls_matching
from fakes.fake_copyparty import serve


class FakeRunai(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = self.sb.env()
        self.runai = str(self.sb.bin / "runai")

    def tearDown(self):
        self.sb.cleanup()

    def test_submit_simulates_the_pod_then_describe_and_logs(self):
        r = run([self.runai, "training", "submit", "j1", "-p", "P", "--command", "--",
                 "bash", "-c", "echo hello; exit 3"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        d1 = run([self.runai, "training", "standard", "describe", "j1", "-p", "P"], env=self.env)
        self.assertIn("Phase: Running", d1.stdout)
        d2 = run([self.runai, "training", "standard", "describe", "j1", "-p", "P"], env=self.env)
        self.assertIn("Phase: Failed", d2.stdout)
        logs = run([self.runai, "training", "standard", "logs", "j1", "-p", "P"], env=self.env)
        self.assertIn("hello", logs.stdout)
        self.assertEqual(len(calls_matching(self.sb.runai_calls(), "training", "standard",
                                            "describe")), 2)

    def test_command_cap(self):
        r = run([self.runai, "training", "submit", "j2", "--", "bash", "-c", "x" * 10001],
                env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("at most 10000 characters", r.stderr)

    def test_logs_not_ready_then_ready(self):
        self.sb.set_runai_scenario({"logs_not_ready": 2})
        run([self.runai, "training", "submit", "j3", "--", "true"], env=self.env)
        for _ in range(2):
            r = run([self.runai, "training", "standard", "logs", "j3"], env=self.env)
            self.assertIn("not ready to stream", r.stderr)
        self.assertEqual(run([self.runai, "training", "standard", "logs", "j3"],
                             env=self.env).returncode, 0)

    def test_exec_hides_the_remote_exit_code(self):
        r = run([self.runai, "workspace", "exec", "ws", "--", "sh", "-c", "exit 3"], env=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("command terminated with exit code 3", r.stderr)

    def test_rules_override_and_expire(self):
        self.sb.set_runai_scenario({"rules": [{"match": ["whoami"], "rc": 1, "times": 1,
                                               "stderr": "Error: boom"}]})
        self.assertEqual(run([self.runai, "whoami"], env=self.env).returncode, 1)
        self.assertEqual(run([self.runai, "whoami"], env=self.env).returncode, 0)


class FakeGlobus(unittest.TestCase):
    def test_transfer_then_show(self):
        sb = Sandbox(globus_scenario={"final_status": "FAILED"})
        try:
            g, env = str(sb.bin / "globus"), sb.env()
            r = run([g, "transfer", "A:/x", "B:/y", "--recursive", "-F", "json"], env=env)
            task = json.loads(r.stdout)["task_id"]
            show = json.loads(run([g, "task", "show", task, "-F", "json"], env=env).stdout)
            self.assertEqual(show["status"], "FAILED")
            self.assertEqual(run([g, "task", "wait", task, "--timeout", "5"], env=env).returncode, 0)
        finally:
            sb.cleanup()


class FakeCopyparty(unittest.TestCase):
    def test_put_ls_get_and_no_overwrite(self):
        sb = Sandbox()
        srv, url = serve(sb.pvc)
        try:
            for body in (b"one", b"two"):
                req = urllib.request.Request(url + "/d/a%20b.txt", data=body, method="PUT")
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 201)
            with urllib.request.urlopen(url + "/d/?ls") as resp:
                listing = json.loads(resp.read())
            names = sorted(f["href"] for f in listing["files"])
            self.assertEqual(names, ["a%20b.txt", "a%20b.txt-1"])
            with urllib.request.urlopen(url + "/d/a%20b.txt") as resp:
                self.assertEqual(resp.read(), b"one")
        finally:
            srv.stop()
            sb.cleanup()

    def test_readonly_refuses_put(self):
        sb = Sandbox()
        srv, url = serve(sb.pvc, readonly=True)
        try:
            req = urllib.request.Request(url + "/x", data=b"1", method="PUT")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 403)
            cm.exception.close()
        finally:
            srv.stop()
            sb.cleanup()


if __name__ == "__main__":
    unittest.main()
