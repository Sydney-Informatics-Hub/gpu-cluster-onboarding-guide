"""train_wrapper.sh: exit-code mapping, signal forwarding, the log, and the done-marker."""
import signal
import subprocess
import sys
import time
import unittest

from helpers import SCRIPTS, Sandbox, run

WRAPPER = SCRIPTS / "train_wrapper.sh"

# A stand-in training script: says it is ready, and on TERM or INT "checkpoints" (takes a
# moment, so the wrapper must really wait for it) and exits 0.
CHILD = r"""
import signal, sys, time
def stop(sig, frame):
    name = signal.Signals(sig).name
    print("child got " + name, flush=True)
    time.sleep(0.5)
    print("checkpoint saved", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
print("ready", flush=True)
for _ in range(600):
    time.sleep(0.1)
print("finished without a signal", flush=True)
"""


class TrainWrapper(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.env = self.sb.env()

    def tearDown(self):
        self.sb.cleanup()

    def wrap(self, *args, **kw):
        return run(["bash", WRAPPER] + [str(a) for a in args], env=kw.pop("env", self.env),
                   timeout=60, **kw)

    def test_failure_is_logged_and_mapped_to_zero(self):
        r = self.wrap("--", "sh", "-c", "echo working; exit 3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("working", r.stdout + r.stderr)
        self.assertIn("[train_wrapper] exit 3", r.stdout + r.stderr)
        self.assertIn("the real code is 3", r.stdout + r.stderr)

    def test_exit_real(self):
        r = self.wrap("--exit", "real", "--", "sh", "-c", "exit 3")
        self.assertEqual(r.returncode, 3)
        r = self.wrap("--exit", "real", "--", "true")
        self.assertEqual(r.returncode, 0)

    def test_usage_errors(self):
        for args in ([], ["--exit", "maybe", "--", "true"], ["true"], ["--log"]):
            with self.subTest(args=args):
                r = self.wrap(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("[train_wrapper]", r.stderr)

    def start_child(self, sig):
        script = self.sb.root / "child.py"
        script.write_text(CHILD)
        log = self.sb.pvc / "logs" / "train.log"
        marker = self.sb.pvc / "logs" / "DONE"
        p = subprocess.Popen(["bash", str(WRAPPER), "--log", str(log), "--done-marker",
                              str(marker), "--", sys.executable, str(script)],
                             env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            if log.exists() and "ready" in log.read_text():
                break
            time.sleep(0.05)
        else:
            p.kill()
            self.fail("the child never became ready: " + (log.read_text() if log.exists()
                                                           else "no log"))
        p.send_signal(sig)
        out, err = p.communicate(timeout=30)
        return p.returncode, out, err, log, marker

    def test_term_reaches_the_child_and_the_wrapper_exits_143(self):
        rc, out, err, log, marker = self.start_child(signal.SIGTERM)
        self.assertEqual(rc, 143, out + err)
        self.assertIn("child got SIGTERM", out)
        self.assertIn("checkpoint saved", out)       # the wrapper waited for the handler
        self.assertIn("[train_wrapper] exit 0 (TERM received", out)
        self.assertNotIn("finished without a signal", out)
        self.assertFalse(marker.exists(), "no done-marker after a signal")
        self.assertEqual(log.read_text(), out)

    def test_int_reaches_the_child(self):
        rc, out, err, _, marker = self.start_child(signal.SIGINT)
        self.assertEqual(rc, 143, out + err)
        self.assertIn("child got SIGINT", out)
        self.assertIn("INT received", out)
        self.assertFalse(marker.exists())

    def test_log_receives_everything_including_the_last_line(self):
        log = self.sb.pvc / "deep" / "dir" / "train.log"
        log.parent.mkdir(parents=True)
        log.write_text("earlier run\n")
        r = self.wrap("--log", log, "--", sys.executable, "-c",
                      "import sys\n"
                      "for i in range(3000): print('line', i)\n"
                      "sys.stderr.write('to stderr\\n'); sys.exit(4)")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = log.read_text()
        self.assertTrue(text.startswith("earlier run\n"), "appends, does not truncate")
        self.assertIn("line 2999\n", text)
        self.assertIn("to stderr\n", text)
        self.assertIn("[train_wrapper] exit 4\n", text)
        last = text.rstrip("\n").splitlines()[-1]
        self.assertTrue(last.startswith("[train_wrapper] exiting 0 (--exit zero)"), last)
        self.assertEqual(text[len("earlier run\n"):], r.stdout)

    def test_log_directory_is_created(self):
        log = self.sb.pvc / "new" / "train.log"
        r = self.wrap("--log", log, "--", "echo", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello", log.read_text())

    def test_done_marker_written_on_success_then_short_circuits(self):
        marker = self.sb.pvc / "run" / "DONE"
        flag = self.sb.root / "ran"
        cmd = ["--done-marker", marker, "--", "sh", "-c", 'echo x >> "$0"', flag]
        r = self.wrap(*cmd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(marker.exists())
        self.assertRegex(marker.read_text(), r"^completed \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        self.assertEqual(flag.read_text(), "x\n")
        r = self.wrap(*cmd)
        self.assertEqual(r.returncode, 0)
        self.assertIn("[train_wrapper] done-marker {} exists; nothing to do".format(marker),
                      r.stdout)
        self.assertEqual(flag.read_text(), "x\n", "the command must not run again")

    def test_no_done_marker_on_failure(self):
        marker = self.sb.root / "DONE"
        self.wrap("--done-marker", marker, "--", "sh", "-c", "exit 1")
        self.assertFalse(marker.exists())

    def test_a_leftover_background_process_cannot_hang_the_job(self):
        log = self.sb.root / "train.log"
        out = self.sb.root / "stdout.txt"
        env = self.sb.env(TEE_WAIT_S=1)
        t0 = time.time()
        with open(out, "w") as f:
            p = subprocess.run(["bash", str(WRAPPER), "--log", str(log), "--", "sh", "-c",
                                "sleep 6 & echo started"], env=env, stdout=f,
                               stderr=subprocess.STDOUT, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("[train_wrapper] exit 0", log.read_text())


if __name__ == "__main__":
    unittest.main()
