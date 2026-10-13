"""gpu_ledger.py: estimate arithmetic, manual and Run:ai rows, CSV and markdown ledgers.

The Run:ai fixtures are synthetic stand-ins (see fixtures/README.md)."""
import csv
import json
import sys
import unittest
from pathlib import Path

from helpers import FIXTURES, SCRIPTS, Sandbox, run

sys.path.insert(0, str(SCRIPTS))
import gpu_ledger as gl  # noqa: E402

LEDGER = SCRIPTS / "gpu_ledger.py"


class Estimate(unittest.TestCase):
    def test_arithmetic(self):
        r = gl.estimate(units=1000000, epochs=10, throughput=1000, gpus=2, fraction=0.5,
                        val_frac=0.1, startup_min=5)
        self.assertAlmostEqual(r["train_s"], 11000.0)
        self.assertAlmostEqual(r["wall_s"], 11300.0)
        self.assertAlmostEqual(r["wall_h"], 11300 / 3600.0)
        self.assertAlmostEqual(r["gpu_h_whole"], 2 * 11300 / 3600.0)
        self.assertAlmostEqual(r["gpu_h_fraction"], 11300 / 3600.0)

    def test_cli_shows_the_formula_and_both_figures(self):
        r = run([sys.executable, LEDGER, "estimate", "--units", "1000000", "--epochs", "10",
                 "--throughput", "1000", "--gpus", "2", "--fraction", "0.5"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wall = U × E ÷ T × (1 + val_frac) + startup", r.stdout)
        self.assertIn("1,000,000 × 10 ÷ 1,000/s × (1 + 0.1) + 5 min", r.stdout)
        self.assertIn("= 3.14 h", r.stdout)
        self.assertIn("3.14 h × 2 GPUs = 6.28", r.stdout)
        self.assertIn("3.14 h × 2 GPUs × 0.5 = 3.14", r.stdout)
        self.assertIn("whole job", r.stdout)

    def test_no_fraction_line_without_fraction(self):
        r = run([sys.executable, LEDGER, "estimate", "--units", "100", "--epochs", "1",
                 "--throughput", "1", "--startup-min", "0", "--val-frac", "0"])
        self.assertIn("= 100 s = 0.03 h", r.stdout)
        self.assertNotIn("fraction", r.stdout.split("whole GPUs")[1].split("\n")[1])

    def test_bad_values(self):
        r = run([sys.executable, LEDGER, "estimate", "--units", "0", "--epochs", "1",
                 "--throughput", "1"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("must be positive", r.stderr)


class Helpers(unittest.TestCase):
    def test_interpret_gpu_shapes(self):
        self.assertEqual(gl.interpret_gpu(1), (1, None))
        self.assertEqual(gl.interpret_gpu("1.00"), (1, None))
        self.assertEqual(gl.interpret_gpu(0.1), (1, 0.1))
        self.assertEqual(gl.interpret_gpu(0), "zero")
        self.assertEqual(gl.interpret_gpu({"devices": 2, "portion": 0.5}), (2, 0.5))
        self.assertEqual(gl.interpret_gpu({"gpuDevicesRequest": 1, "gpuPortionRequest": 0.25}),
                         (1, 0.25))
        self.assertEqual(gl.interpret_gpu({"portion": 0.3}), (1, 0.3))
        self.assertEqual(gl.interpret_gpu({"request": "2"}), (2, None))
        self.assertIsNone(gl.interpret_gpu("10G"))
        self.assertIsNone(gl.interpret_gpu({"memory": "10G"}))

    def test_parse_iso_variants(self):
        a = gl.parse_iso("2026-09-21T10:00:00.123456789Z")
        b = gl.parse_iso("2026-09-21T20:00:00+10:00")
        c = gl.parse_iso("2026-09-21T10:00:00.5+0000")
        d = gl.parse_iso("2026-09-21 10:00")
        self.assertEqual(gl.iso(a), "2026-09-21T10:00:00Z")
        self.assertEqual(b, a.replace(microsecond=0))
        self.assertEqual(c.microsecond, 500000)
        self.assertEqual(gl.iso(d), "2026-09-21T10:00:00Z")
        with self.assertRaises(gl.LedgerError):
            gl.parse_iso("yesterday")

    def test_merge_windows_counts_overlap_once(self):
        t = gl.parse_iso
        w = [(t("2026-01-01T00:00Z"), t("2026-01-01T01:00Z")),
             (t("2026-01-01T00:30Z"), t("2026-01-01T01:30Z")),
             (t("2026-01-01T02:00Z"), t("2026-01-01T02:15Z"))]
        self.assertEqual(gl.merge_windows(w), 1.75 * 3600)


class LedgerFiles(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def ledger(self, *args):
        return run([sys.executable, LEDGER] + [str(a) for a in args], cwd=self.sb.root)

    def rows(self, path):
        rows, _, _ = gl.read_ledger(str(path))
        return rows

    def add_two_manual(self, path):
        r1 = self.ledger("add", "manual", "--name", "run1", "--start", "2026-10-01T09:00:00Z",
                         "--end", "2026-10-01T13:00:00Z", "--gpus", "2", "--purpose",
                         "baseline | seed 1", "--ledger", path)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        r2 = self.ledger("add", "manual", "--name", "run2", "--start", "2026-10-02T09:00:00Z",
                         "--end", "2026-10-02T11:00:00Z", "--gpus", "1", "--fraction", "0.25",
                         "--ledger", path)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        return r2

    def check_two_manual(self, rows):
        self.assertEqual([r["workload"] for r in rows], ["run1", "run2"])
        self.assertEqual(rows[0]["date"], "2026-10-01")
        self.assertEqual((rows[0]["wall_h"], rows[0]["gpu_h_fraction"], rows[0]["gpu_h_whole"]),
                         ("4.000", "8.000", "8.000"))
        self.assertEqual((rows[1]["fraction"], rows[1]["gpu_h_fraction"],
                          rows[1]["gpu_h_whole"]), ("0.25", "0.500", "2.000"))
        self.assertEqual((rows[1]["total_fraction"], rows[1]["total_whole"]),
                         ("8.500", "10.000"))
        self.assertEqual(rows[0]["purpose"], "baseline | seed 1")
        self.assertEqual(rows[0]["method"], "manual")

    def test_manual_csv_with_running_totals(self):
        path = self.sb.root / "gpu_ledger.csv"
        r = self.add_two_manual(path)
        self.assertIn("totals 8.500 GPU-h (fraction), 10.000 GPU-h (whole)", r.stdout)
        with open(path) as f:
            self.assertEqual(next(csv.reader(f)), gl.COLUMNS)
        self.check_two_manual(self.rows(path))

    def test_default_ledger_is_csv_in_the_cwd(self):
        r = self.ledger("add", "manual", "--name", "x", "--start", "2026-10-01T09:00Z",
                        "--end", "2026-10-01T10:00Z", "--gpus", "0.5")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = self.rows(self.sb.root / "gpu_ledger.csv")
        self.assertEqual((rows[0]["gpus"], rows[0]["fraction"], rows[0]["gpu_h_fraction"]),
                         ("1", "0.5", "0.500"))

    def test_manual_markdown_and_show(self):
        path = self.sb.root / "ledger.md"
        self.add_two_manual(path)
        text = path.read_text()
        self.assertIn("| " + " | ".join(gl.COLUMNS) + " |", text)
        self.assertIn("baseline \\| seed 1", text)
        self.check_two_manual(self.rows(path))
        # Text the user adds around the table survives a rewrite.
        path.write_text("# My runs\n\n" + text.split("\n\n", 2)[2] + "\nNotes below.\n")
        self.ledger("add", "manual", "--name", "run3", "--start", "2026-10-03T09:00Z",
                    "--end", "2026-10-03T10:00Z", "--gpus", "1", "--ledger", path)
        text = path.read_text()
        self.assertTrue(text.startswith("# My runs\n"))
        self.assertTrue(text.endswith("\nNotes below.\n"))
        show = self.ledger("show", "--ledger", path)
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertIn("total: 9.500 GPU-h as fractions, 11.000 GPU-h as whole GPUs, over 3 rows",
                      show.stdout)

    def test_show_without_a_ledger(self):
        r = self.ledger("show", "--ledger", self.sb.root / "none.csv")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no ledger", r.stderr)

    def test_duplicate_refused_then_replaced(self):
        path = self.sb.root / "l.csv"
        args = ["add", "manual", "--name", "a", "--start", "2026-10-01T09:00Z", "--end",
                "2026-10-01T10:00Z", "--gpus", "1", "--ledger", path]
        self.assertEqual(self.ledger(*args).returncode, 0)
        r = self.ledger(*args)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--replace", r.stderr)
        args[args.index("2026-10-01T10:00Z")] = "2026-10-01T12:00Z"
        self.assertEqual(self.ledger(*(args + ["--replace"])).returncode, 0)
        rows = self.rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["wall_h"], "3.000")

    def test_manual_rejects_reversed_window(self):
        r = self.ledger("add", "manual", "--name", "a", "--start", "2026-10-01T10:00Z",
                        "--end", "2026-10-01T09:00Z", "--gpus", "1")
        self.assertEqual(r.returncode, 2)


class AddRunai(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.path = self.sb.root / "l.csv"

    def tearDown(self):
        self.sb.cleanup()

    def add(self, fixture, *extra):
        src = fixture if isinstance(fixture, Path) else FIXTURES / (
            "runai_describe_" + fixture + ".json")
        return run([sys.executable, LEDGER, "add", "runai", "--json", src, "--ledger",
                    self.path] + [str(a) for a in extra])

    def only_row(self):
        rows, _, _ = gl.read_ledger(str(self.path))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def write_json(self, obj):
        p = self.sb.root / "d.json"
        p.write_text(json.dumps(obj))
        return p

    def test_whole_gpu(self):
        r = self.add("whole_gpu", "--purpose", "baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from compute.allocatedResources.gpuCompute (allocated)", r.stdout)
        self.assertIn("events Scheduled -> Completed", r.stdout)
        row = self.only_row()
        self.assertEqual((row["workload"], row["date"], row["gpus"], row["fraction"]),
                         ("train-baseline", "2026-09-20", "1", "1"))
        self.assertEqual((row["wall_h"], row["gpu_h_fraction"], row["gpu_h_whole"]),
                         ("2.500", "2.500", "2.500"))
        self.assertEqual(row["purpose"], "baseline")
        self.assertIn("runai", row["method"])

    def test_fraction_keeps_both_columns(self):
        r = self.add("fraction")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("compute.allocatedResources.gpuCompute reads 0", r.stdout)
        self.assertIn("from compute.requestedResources.gpuCompute (requested)", r.stdout)
        row = self.only_row()
        self.assertEqual((row["fraction"], row["wall_h"], row["gpu_h_fraction"],
                          row["gpu_h_whole"]), ("0.1", "4.000", "0.400", "4.000"))
        self.assertIn("0.400 as a fraction", r.stdout)
        self.assertIn("4.000 as whole GPUs", r.stdout)

    def test_recreated_pods_use_the_union_of_pod_windows(self):
        r = self.add("recreated_pods")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("union of 5 pod windows", r.stdout)
        self.assertIn("the workload window from the events was 0.867 h", r.stdout)
        row = self.only_row()
        self.assertEqual(row["wall_h"], "{:.3f}".format(49 / 60.0))
        self.assertIn("union of 5 pod windows", row["method"])

    def test_running_requires_now(self):
        r = self.add("running")
        self.assertEqual(r.returncode, 2)
        self.assertIn("still running", r.stderr)
        self.assertIn("--now", r.stderr)
        self.assertFalse(self.path.exists())
        r = self.add("running", "--now", "2026-10-14T09:00:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still running", r.stdout)
        row = self.only_row()
        self.assertEqual((row["gpus"], row["wall_h"], row["gpu_h_whole"]),
                         ("2", "3.000", "6.000"))

    def test_gpus_override(self):
        r = self.add("whole_gpu", "--gpus", "1", "--fraction", "0.5")
        self.assertEqual(r.returncode, 0, r.stderr)
        row = self.only_row()
        self.assertEqual((row["fraction"], row["gpu_h_fraction"]), ("0.5", "1.250"))
        self.assertIn("gpu from --gpus", row["method"])

    def test_unrecognised_shape_names_what_was_expected(self):
        r = self.add(self.write_json({"name": "x", "status": "weird"}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("events", r.stderr)
        self.assertIn("reason", r.stderr)
        self.assertIn("createdAt", r.stderr)

    def test_missing_gpu_amount_names_the_paths_and_the_override(self):
        doc = json.loads((FIXTURES / "runai_describe_whole_gpu.json").read_text())
        doc["compute"] = {"cpu": 8}
        p = self.write_json(doc)
        r = self.add(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("compute.allocatedResources.gpuCompute", r.stderr)
        self.assertIn("--gpus", r.stderr)
        self.assertIn("`compute` has keys: cpu", r.stderr)
        self.assertEqual(self.add(p, "--gpus", "1").returncode, 0)

    def test_flat_submit_style_compute_fields(self):
        doc = json.loads((FIXTURES / "runai_describe_whole_gpu.json").read_text())
        doc["compute"] = {"gpuDevicesRequest": 1, "gpuPortionRequest": 0.5}
        r = self.add(self.write_json(doc))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from compute (requested)", r.stdout)
        self.assertEqual(self.only_row()["fraction"], "0.5")

    def test_not_json(self):
        p = self.sb.root / "table.txt"
        p.write_text("NAME  GPU\njob   1.00\n")
        r = self.add(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("-o json", r.stderr)

    def test_no_scheduled_event(self):
        p = self.write_json({"name": "x", "events": [
            {"reason": "Submitted", "createdAt": "2026-10-01T09:00:00Z"}]})
        r = self.add(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Scheduled or Bound", r.stderr)

    def test_truncated_events_warn(self):
        doc = json.loads((FIXTURES / "runai_describe_whole_gpu.json").read_text())
        filler = [{"reason": "Pulling", "createdAt": "2026-09-20T09:00:06Z"}] * (
            50 - len(doc["events"]))
        doc["events"] = doc["events"][:4] + filler + doc["events"][4:]
        r = self.add(self.write_json(doc))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--event-limit -1", r.stdout)

    def test_terminal_then_rescheduled_counts_as_running(self):
        p = self.write_json({"name": "x", "phase": "Running",
                             "compute": {"allocatedResources": {"gpuCompute": 1}},
                             "events": [
                                 {"reason": "Scheduled", "createdAt": "2026-10-01T09:00:00Z"},
                                 {"reason": "Failed", "createdAt": "2026-10-01T09:05:00Z"},
                                 {"reason": "Scheduled", "createdAt": "2026-10-01T09:06:00Z"}]})
        r = self.add(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--now", r.stderr)
        r = self.add(p, "--now", "2026-10-01T10:00:00Z")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.only_row()["wall_h"], "1.000")

    def test_describe_without_events_is_an_error(self):
        # The shape the fake runai synthesises has no events: an error, not a guess.
        r = self.add(self.write_json({"name": "j", "phase": "Completed",
                                      "podsInfo": {"pods": [{"name": "j-0-0"}]}}))
        self.assertEqual(r.returncode, 2)
        self.assertIn("expected an `events` list", r.stderr)


if __name__ == "__main__":
    unittest.main()
