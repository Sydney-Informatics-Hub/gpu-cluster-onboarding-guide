"""loader_bench.py: input throughput on CPU, with the environment printed first. Needs PyTorch;
without it the script must say so and exit 2, and the torch tests are skipped."""
import importlib.util
import json
import sys
import unittest

from helpers import SCRIPTS, Sandbox, run

LOADER_BENCH = SCRIPTS / "loader_bench.py"
HAVE_TORCH = importlib.util.find_spec("torch") is not None

TOY = """
import torch
from torch.utils.data import DataLoader, TensorDataset

def make():
    ds = TensorDataset(torch.randn(320, 8), torch.randint(0, 2, (320,)))
    return DataLoader(ds, batch_size=16, num_workers=0)

def tokens(batch):
    return int(batch[0].shape[0]) * 10

def empty():
    return []
"""


class DefaultUnits(unittest.TestCase):
    """The units heuristic needs no torch: anything with a `shape` counts as a tensor."""

    def test_heuristic(self):
        sys.path.insert(0, str(SCRIPTS))
        from loader_bench import default_units

        class T:
            def __init__(self, *shape):
                self.shape = shape

        self.assertEqual(default_units(T(16, 8)), 16)
        self.assertEqual(default_units((T(12, 8), T(12))), 12)
        self.assertEqual(default_units({"x": T(7, 3), "y": T(7)}), 7)
        self.assertEqual(default_units([{"a": 1}] * 5), 5)
        self.assertEqual(default_units(object()), 1)


class Base(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        (self.sb.root / "toy_data.py").write_text(TOY)

    def tearDown(self):
        self.sb.cleanup()

    def bench(self, *args):
        return run([sys.executable, LOADER_BENCH, "--path", self.sb.root] + list(args),
                   cwd=self.sb.root)


@unittest.skipIf(HAVE_TORCH, "PyTorch is installed")
class WithoutTorch(Base):
    def test_clear_message_and_exit_2_after_the_environment(self):
        r = self.bench("--factory", "toy_data:make")
        self.assertEqual(r.returncode, 2)
        self.assertIn("needs PyTorch", r.stderr)
        self.assertTrue(r.stdout.startswith("environment"), r.stdout)


@unittest.skipUnless(HAVE_TORCH, "PyTorch is not installed")
class WithTorch(Base):
    def test_environment_comes_before_the_timing(self):
        r = self.bench("--factory", "toy_data:make", "--batches", "10", "--warmup", "2",
                       "--check-modules", "json,no_such_module_here")
        self.assertEqual(r.returncode, 0, r.stderr)
        env_at, timing_at = r.stdout.find("environment"), r.stdout.find("units/s")
        self.assertEqual(env_at, 0)
        self.assertGreater(timing_at, env_at)
        for item in ("os.cpu_count()", "torch threads", "OMP_NUM_THREADS", "/dev/shm",
                     "module json: present", "module no_such_module_here: MISSING"):
            self.assertIn(item, r.stdout[:timing_at])

    def test_json_reports_positive_throughput(self):
        r = self.bench("--factory", "toy_data:make", "--batches", "8", "--warmup", "2",
                       "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stderr.startswith("environment"))
        obj = json.loads(r.stdout)
        res = obj["result"]
        self.assertGreater(res["units_per_s"], 0)
        self.assertEqual(res["timed_batches"], 8)
        self.assertEqual(res["units"], 8 * 16)
        self.assertGreater(res["s_per_batch_p90"], 0)
        self.assertIn("cpu_count", obj["environment"])

    def test_units_function_and_file_factory(self):
        r = self.bench("--factory", str(self.sb.root / "toy_data.py") + ":make",
                       "--units", "toy_data:tokens", "--batches", "4", "--warmup", "1",
                       "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["result"]["units"], 4 * 160)

    def test_short_loader_is_reported(self):
        r = self.bench("--factory", "toy_data:make", "--batches", "100", "--warmup", "0",
                       "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)["result"]
        self.assertTrue(res["exhausted"])
        self.assertEqual(res["timed_batches"], 20)

    def test_zero_batches_is_an_error(self):
        r = self.bench("--factory", "toy_data:empty")
        self.assertEqual(r.returncode, 1)
        self.assertIn("zero batches", r.stderr)

    def test_bad_factory(self):
        r = self.bench("--factory", "toy_data:nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no attribute", r.stderr)


if __name__ == "__main__":
    unittest.main()
