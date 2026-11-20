"""step_profile.py: per-phase times and top-level op counts. Needs PyTorch; without it the
script must say so and exit 2, and the torch tests are skipped."""
import importlib.util
import json
import sys
import unittest

from helpers import SCRIPTS, run

STEP_PROFILE = SCRIPTS / "step_profile.py"
HAVE_TORCH = importlib.util.find_spec("torch") is not None


@unittest.skipIf(HAVE_TORCH, "PyTorch is installed")
class WithoutTorch(unittest.TestCase):
    def test_clear_message_and_exit_2(self):
        r = run([sys.executable, STEP_PROFILE, "--demo"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs PyTorch", r.stderr)

    def test_help_works_without_torch(self):
        self.assertEqual(run([sys.executable, STEP_PROFILE, "--help"]).returncode, 0)


@unittest.skipUnless(HAVE_TORCH, "PyTorch is not installed")
class WithTorch(unittest.TestCase):
    def demo(self):
        r = run([sys.executable, STEP_PROFILE, "--demo", "--device", "cpu", "--steps", "6",
                 "--warmup", "3", "--profile-steps", "3", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_demo_op_counts_are_positive_and_repeatable(self):
        a, b = self.demo(), self.demo()
        self.assertEqual(list(a["phases"]), ["data", "forward", "backward", "optimizer"])
        for name, p in a["phases"].items():
            with self.subTest(phase=name):
                self.assertGreater(p["top_level_ops_per_step"], 0)
                self.assertGreater(p["mean_s"], 0)
        counts = {n: p["ops"] for n, p in a["phases"].items()}
        self.assertEqual(counts, {n: p["ops"] for n, p in b["phases"].items()})
        self.assertAlmostEqual(sum(p["share"] for p in a["phases"].values()), 1.0, places=6)

    def test_demo_table(self):
        r = run([sys.executable, STEP_PROFILE, "--demo", "--device", "cpu", "--steps", "3",
                 "--warmup", "1"])
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("phase", "mean ms", "data", "forward", "backward", "optimizer", "step"):
            self.assertIn(word, r.stdout)

    def test_only_top_level_ops_are_counted(self):
        sys.path.insert(0, str(SCRIPTS))
        import torch
        from step_profile import profile_phases
        w, x = torch.randn(4, 8), torch.randn(2, 8)
        res = profile_phases({"linear": lambda: torch.nn.functional.linear(x, w),
                              "two_adds": lambda: (x + 1) + 2},
                             steps=2, warmup=1, device="cpu", profile_steps=2)
        # linear runs aten::t and aten::addmm (or matmul) inside it: those are not counted.
        self.assertEqual(dict(res["phases"]["linear"]["ops"]), {"aten::linear": 1.0})
        self.assertEqual(dict(res["phases"]["two_adds"]["ops"]), {"aten::add": 2.0})
        self.assertEqual(res["profiled_steps"], 2)


if __name__ == "__main__":
    unittest.main()
