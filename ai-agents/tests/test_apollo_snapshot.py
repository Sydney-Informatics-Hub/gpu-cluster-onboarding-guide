"""apollo_snapshot.sh: save before maintenance, compare after."""
import json
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

SNAPSHOT = SCRIPTS / "apollo_snapshot.sh"

BEFORE = [
    {"name": "job-a", "phase": "Running"},
    {"name": "job-b", "phase": "Running"},
    {"name": "job-c", "phase": "Running"},
    {"name": "job-d", "phase": "Pending"},
    {"name": "job-e", "phase": "Completed"},
    {"name": "job-f", "phase": "Running"},
    {"name": "job-g", "phase": "Pending"},
    {"name": "job-h", "phase": "Running"},
    {"name": "job-i", "phase": "Stopped"},
]
AFTER = [
    {"name": "job-a", "phase": "Completed"},
    {"name": "job-b", "phase": "Failed"},
    {"name": "job-d", "phase": "Stopped"},
    {"name": "job-e", "phase": "Completed"},
    {"name": "job-f", "phase": "Running"},
    {"name": "job-g", "phase": "Pending"},
    {"name": "job-h", "phase": "Pending"},
    {"name": "job-i", "phase": "Stopped"},
    {"name": "job-new", "phase": "Running"},
]
EXPECTED = {
    "job-a": "finished", "job-b": "failed", "job-c": "missing", "job-d": "interrupted",
    "job-e": "finished", "job-f": "still-running", "job-g": "still-pending",
    "job-h": "interrupted", "job-i": "unchanged",
}


class ApolloSnapshot(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(runai_scenario={
            "list_training": {"workloads": BEFORE},
            "list_workspace": [{"name": "ws-1", "phase": "Running"}],
            "describe_json": {"job-a": {"name": "job-a", "phase": "Running",
                                        "pods": [{"nodeName": "node-7"}]}}})
        self.dir = self.sb.root / "snap"

    def tearDown(self):
        self.sb.cleanup()

    def snap(self, *args, scenario=None):
        if scenario is not None:
            self.sb.set_runai_scenario(scenario)
        return run([SNAPSHOT] + [str(a) for a in args], env=self.sb.env(), cwd=self.sb.root)

    def describes(self):
        return calls_matching(self.sb.runai_calls(), "training", "standard", "describe")

    def test_save_named_workloads(self):
        r = self.snap("save", "--dir", self.dir, "job-a", "job-x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads((self.dir / "training_list.json").read_text()),
                         {"workloads": BEFORE})
        self.assertEqual(json.loads((self.dir / "workspace_list.json").read_text()),
                         [{"name": "ws-1", "phase": "Running"}])
        self.assertEqual(json.loads((self.dir / "describe" / "job-a.json").read_text())
                         ["pods"][0]["nodeName"], "node-7")
        self.assertFalse((self.dir / "describe" / "job-x.json").exists())
        self.assertTrue((self.dir / "describe" / "job-x.err").exists())
        self.assertIn("could not describe job-x", r.stderr)
        self.assertIn("project=test-proj", (self.dir / "meta.txt").read_text())
        argv = self.describes()[0]
        self.assertEqual(argv[3], "job-a")
        self.assertEqual(argv[argv.index("-o") + 1], "json")
        self.assertIn("--pod-limit=-1", argv)
        self.assertIn("--event-limit=-1", argv)
        self.assertEqual(calls_matching(self.sb.runai_calls(), "training", "submit"), [])

    def test_save_everything_to_the_default_directory(self):
        r = self.snap("save")
        self.assertEqual(r.returncode, 0, r.stderr)
        dirs = list((self.sb.root / "apollo_snapshots").iterdir())
        self.assertEqual(len(dirs), 1)
        self.assertRegex(dirs[0].name, r"^\d{8}-\d{6}$")
        self.assertTrue((dirs[0] / "training_list.json").is_file())
        self.assertEqual(sorted(c[3] for c in self.describes()), sorted(j["name"] for j in BEFORE))

    def test_save_caps_describes_at_50(self):
        many = [{"name": "job-%02d" % i, "phase": "Running"} for i in range(55)]
        r = self.snap("save", "--dir", self.dir, scenario={"list_training": many})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.describes()), 50)
        self.assertIn("first 50", r.stderr)

    def test_compare_classifies(self):
        self.assertEqual(self.snap("save", "--dir", self.dir).returncode, 0)
        r = self.snap("compare", self.dir, scenario={
            "list_training": {"workloads": AFTER},
            "list_workspace": [{"name": "ws-1", "phase": "Stopped"}]})
        self.assertEqual(r.returncode, 0, r.stderr)
        table, _, tail = r.stdout.partition("resubmit candidates:")
        got = {}
        for line in table.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 5:
                got[parts[1]] = (parts[0], parts[4])
        expected = {n: ("training", c) for n, c in EXPECTED.items()}
        expected["ws-1"] = ("workspace", "interrupted")
        self.assertEqual(got, expected)
        self.assertNotIn("job-new", r.stdout)
        candidates, _, requeued = tail.partition("re-queued")
        for name in ("job-b", "job-c", "job-d", "ws-1"):
            self.assertIn("  " + name + "  ", candidates)
        for name in ("job-a", "job-e", "job-f", "job-g", "job-h", "job-i"):
            self.assertNotIn("  " + name + "  ", candidates)
        self.assertIn("job-h", requeued)
        self.assertIn("done-marker", r.stdout)
        self.assertIn("§6.8", r.stdout)

    def test_compare_needs_a_snapshot(self):
        r = self.snap("compare", self.sb.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("training_list.json", r.stderr)

    def test_compare_with_unparsable_list(self):
        self.snap("save", "--dir", self.dir)
        r = self.snap("compare", self.dir, scenario={"rules": [
            {"match": ["training", "list"], "stdout": "not json at all"}]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("not json at all", r.stdout)
        self.assertIn("assumed:", r.stderr)

    def test_usage(self):
        for args in ([], ["frobnicate"], ["compare"]):
            with self.subTest(args=args):
                self.assertEqual(self.snap(*args).returncode, 2)


if __name__ == "__main__":
    unittest.main()
