"""apollo_cleanup.sh: lists the caller's own throwaway jobs and deletes only finished ones."""
import hashlib
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

CLEANUP = SCRIPTS / "apollo_cleanup.sh"
TAG = hashlib.sha1(b"00000000-1111-2222-3333-444444444444").hexdigest()[:6]
OTHER_TAG = "abcdef" if TAG != "abcdef" else "fedcba"

MINE_DONE = "peek-{}-101010-11-1".format(TAG)
MINE_FAILED = "script-{}-101010-12-2".format(TAG)
MINE_RUNNING = "peek-{}-101010-13-3".format(TAG)
MINE_PENDING = "script-{}-101010-14-4".format(TAG)
OTHERS = "peek-{}-101010-15-5".format(OTHER_TAG)
OTHER_PREFIX = "probe-{}-101010-16-6".format(TAG)
UNRELATED = "train-main"

TRAININGS = {"workloads": [
    {"name": MINE_DONE, "phase": "Completed", "createdAt": "2026-09-13T01:02:03Z"},
    {"workloadName": MINE_FAILED, "status": "Failed", "createdAt": "2026-10-14T00:00:00.12Z"},
    {"name": MINE_RUNNING, "phase": "Running"},
    {"name": MINE_PENDING, "phase": "Pending"},
    {"name": OTHERS, "phase": "Completed"},
    {"name": OTHER_PREFIX, "phase": "Completed"},
    {"name": UNRELATED, "phase": "Completed"},
]}
WORKSPACES = [{"name": "ws-old", "phase": "Stopped"}, {"name": "ws-live", "phase": "Running"},
              {"name": "ws-paused", "status": "Suspended"}]


class ApolloCleanup(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(runai_scenario={"list_training": TRAININGS,
                                          "list_workspace": WORKSPACES})

    def tearDown(self):
        self.sb.cleanup()

    def cleanup(self, *args, scenario=None, **env):
        if scenario is not None:
            self.sb.set_runai_scenario(scenario)
        env.setdefault("RETRY_S", "0")
        return run([CLEANUP] + list(args), env=self.sb.env(**env), cwd=self.sb.root)

    def deleted(self):
        return sorted(c[3] for c in calls_matching(self.sb.runai_calls(), "training", "standard",
                                                   "delete"))

    def row(self, out, name):
        lines = [ln for ln in out.splitlines() if ln.startswith(name + " ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0]

    def test_dry_run_lists_only_own_jobs_and_deletes_nothing(self):
        r = self.cleanup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would delete", self.row(r.stdout, MINE_DONE))
        self.assertIn("would delete", self.row(r.stdout, MINE_FAILED))
        self.assertIn("keep", self.row(r.stdout, MINE_RUNNING))
        self.assertIn("keep", self.row(r.stdout, MINE_PENDING))
        self.assertRegex(self.row(r.stdout, MINE_DONE), r"Completed +\d+d\d+h ")
        for name in (OTHERS, OTHER_PREFIX, UNRELATED):
            self.assertNotIn(name, r.stdout)
        self.assertIn("Dry run", r.stdout)
        self.assertEqual(self.deleted(), [])

    def test_yes_deletes_exactly_own_finished_jobs(self):
        r = self.cleanup("--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deleted(), sorted([MINE_DONE, MINE_FAILED]))
        self.assertIn("Deleted 2 of 2", r.stdout)

    def test_prefix_option(self):
        r = self.cleanup("--prefix", "probe", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deleted(), [OTHER_PREFIX])
        self.assertNotIn(MINE_DONE, r.stdout)

    def test_all_tags_lists_others_and_never_deletes(self):
        r = self.cleanup("--all-tags", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("list only", self.row(r.stdout, OTHERS))
        self.assertIn(MINE_DONE, r.stdout)
        self.assertNotIn(OTHER_PREFIX, r.stdout)
        self.assertEqual(self.deleted(), [])

    def test_suspended_workspaces_are_listed_not_deleted(self):
        r = self.cleanup("--yes")
        self.assertIn("runai workspace delete ws-old -p test-proj", r.stdout)
        self.assertIn("runai workspace delete ws-paused -p test-proj", r.stdout)
        self.assertNotIn("ws-live", r.stdout)
        self.assertEqual(calls_matching(self.sb.runai_calls(), "workspace", "delete"), [])

    def test_kubernetes_style_items(self):
        items = {"items": [
            {"metadata": {"name": MINE_DONE, "creationTimestamp": "2026-10-14T00:00:00+0000"},
             "status": {"phase": "Succeeded"}},
            {"metadata": {"name": MINE_RUNNING}, "status": {"phase": "Running"}}]}
        r = self.cleanup("--yes", scenario={"list_training": items, "list_workspace": []})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deleted(), [MINE_DONE])

    def test_failed_delete_is_reported_but_not_fatal(self):
        r = self.cleanup("--yes", scenario={"list_training": TRAININGS, "delete_fail": 10})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("could not delete workload " + MINE_DONE, r.stderr)
        self.assertIn("Deleted 0 of 2", r.stdout)

    def test_unparsable_json(self):
        r = self.cleanup(scenario={"rules": [
            {"match": ["training", "list"], "stdout": "<html>gateway error</html>"}]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("<html>gateway error</html>", r.stdout)
        self.assertIn("assumed:", r.stderr)
        self.assertEqual(self.deleted(), [])

    def test_nothing_to_do(self):
        r = self.cleanup("--yes", scenario={"list_training": [], "list_workspace": []})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No throwaway trainings of yours", r.stdout)
        self.assertIn(TAG, r.stdout)

    def test_vpn_down(self):
        r = self.cleanup(scenario={"rules": [
            {"match": ["project", "list"], "rc": 1,
             "stderr": "Error: DNS lookup failed for 'h': no such host"}]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("VPN", r.stderr)

    def test_does_not_need_pvc_settings(self):
        env = self.sb.env(RETRY_S="0")
        for k in ("PVC", "MOUNT", "WORKDIR"):
            del env[k]
        r = run([CLEANUP], env=env, cwd=self.sb.root)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
