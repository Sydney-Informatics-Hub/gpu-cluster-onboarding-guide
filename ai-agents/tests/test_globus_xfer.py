"""globus_xfer.sh: dry run by default, destination allow-list, and every outcome of a transfer."""
import os
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

SCRIPT = SCRIPTS / "globus_xfer.sh"
# /bin/bash is 3.2 on macOS, which is the oldest shell the scripts must run on.
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"

LAPTOP = "11111111-1111-1111-1111-111111111111"
PVC = "22222222-2222-2222-2222-222222222222"
RDS = "33333333-3333-3333-3333-333333333333"
OTHER = "44444444-4444-4444-4444-444444444444"
NAMES = {LAPTOP: "My Laptop (GCP)", PVC: "Project Storage", RDS: "Research Data Store",
         OTHER: "Somewhere Else"}


class GlobusXfer(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(globus_scenario={"collections": NAMES})

    def tearDown(self):
        self.sb.cleanup()

    def xfer(self, *args, scenario=None, **env):
        if scenario is not None:
            scenario.setdefault("collections", NAMES)
            self.sb.set_globus_scenario(scenario)
        e = {"GLOBUS_PVC_COLLECTION": PVC, "GLOBUS_RDS_COLLECTION": RDS.upper(),
             "GLOBUS_EXTRA_ALLOWED": "", "GLOBUS_POLL_S": "1"}
        e.update(env)
        return run([BASH, SCRIPT] + list(args), env=self.sb.env(**e))

    def transfers(self):
        return calls_matching(self.sb.globus_calls(), "transfer")

    def test_dry_run_submits_nothing_and_names_both_collections(self):
        r = self.xfer(LAPTOP + ":/Users/me/data/run1/", PVC + ":/project/run1/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("My Laptop (GCP)", r.stdout)
        self.assertIn("Project Storage", r.stdout)
        self.assertIn("--yes", r.stdout)
        self.assertIn("globus transfer", r.stdout)
        self.assertEqual(self.transfers(), [])

    def test_destination_outside_the_allow_list_is_refused(self):
        r = self.xfer("--yes", PVC + ":/project/out/", OTHER + ":/elsewhere/")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not an approved collection", r.stderr)
        self.assertIn("--allow-external", r.stderr)
        self.assertEqual(self.sb.globus_calls(), [])

    def test_allow_external_accepts_with_a_warning(self):
        r = self.xfer("--yes", "--allow-external", PVC + ":/project/out/model.pt",
                      OTHER + ":/elsewhere/model.pt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("leaving approved infrastructure", r.stderr)
        self.assertIn("explicit OK", r.stderr)
        self.assertEqual(len(self.transfers()), 1)

    def test_no_allow_list_configured_is_refused_with_discovery_hint(self):
        r = self.xfer("--yes", LAPTOP + ":/a", PVC + ":/b", GLOBUS_PVC_COLLECTION="",
                      GLOBUS_RDS_COLLECTION="")
        self.assertEqual(r.returncode, 2)
        self.assertIn("globus endpoint search", r.stderr)
        self.assertIn("GLOBUS_PVC_COLLECTION", r.stderr)
        self.assertEqual(self.transfers(), [])

    def test_extra_allowed_list_is_honoured(self):
        r = self.xfer(PVC + ":/project/out/x", OTHER + ":/x", GLOBUS_EXTRA_ALLOWED=" a  " + OTHER)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("GLOBUS_EXTRA_ALLOWED", r.stdout)

    def test_success_prints_task_id_link_and_summary(self):
        r = self.xfer("--yes", LAPTOP + ":/data/run1/", RDS + ":/archive/run1/")
        self.assertEqual(r.returncode, 0, r.stderr)
        task = "00000000-0000-0000-0000-000000000001"
        self.assertIn("task {} submitted".format(task), r.stdout)
        self.assertIn("https://app.globus.org/activity/" + task, r.stdout)
        self.assertIn("SUCCEEDED", r.stdout)
        argv = self.transfers()[0]
        for flag in ("--verify-checksum", "--preserve-timestamp", "--recursive"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sync-level") + 1], "checksum")
        self.assertRegex(argv[argv.index("--label") + 1], r"^apollo-\d{8}-\d{6}$")
        self.assertEqual(argv[argv.index("-F") + 1], "json")
        wait = calls_matching(self.sb.globus_calls(), "task", "wait")[0]
        self.assertEqual(wait[wait.index("--timeout") + 1], "3600")
        self.assertEqual(wait[wait.index("--timeout-exit-code") + 1], "50")

    def test_directory_source_adds_recursive_and_a_file_does_not(self):
        self.xfer("--yes", "--no-wait", LAPTOP + ":/data/dir/", PVC + ":/p/dir/")
        self.xfer("--yes", "--no-wait", LAPTOP + ":/data/file.bin", PVC + ":/p/file.bin")
        self.xfer("--yes", "--no-wait", "--recursive", LAPTOP + ":/data/dir2", PVC + ":/p/dir2")
        first, second, third = self.transfers()
        self.assertIn("--recursive", first)
        self.assertNotIn("--recursive", second)
        self.assertIn("--recursive", third)

    def test_no_wait_returns_after_submission(self):
        r = self.xfer("--yes", "--no-wait", "--label", "my-run", LAPTOP + ":/d/", PVC + ":/p/")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("globus task show", r.stdout)
        self.assertEqual(calls_matching(self.sb.globus_calls(), "task", "wait"), [])
        argv = self.transfers()[0]
        self.assertEqual(argv[argv.index("--label") + 1], "my-run")

    def test_failed_task_exits_1_and_shows_error_events(self):
        scen = {"final_status": "FAILED", "wait_fails_on_failure": True, "task_fields":
                {"faults": 2, "files_transferred": 1},
                "error_events": [{"time": "2026-10-14 01:02:03+00:00", "code": "PERMISSION_DENIED",
                                  "is_error": True, "description": "permission denied",
                                  "details": "Error (transfer) ... /p/run1/a.bin: Permission denied"}]}
        r = self.xfer("--yes", LAPTOP + ":/d/", PVC + ":/p/", scenario=scen)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FAILED", r.stdout)
        self.assertIn("2 faults", r.stdout)
        self.assertIn("PERMISSION_DENIED", r.stderr)
        self.assertIn("Permission denied", r.stderr)
        events = calls_matching(self.sb.globus_calls(), "task", "event-list")[0]
        self.assertIn("--filter-errors", events)

    def test_wait_timeout_exits_4_with_how_to_check_later(self):
        r = self.xfer("--yes", "--timeout", "5", LAPTOP + ":/d/", PVC + ":/p/",
                      scenario={"wait_timeouts": 1})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("globus task show 00000000-0000-0000-0000-000000000001", r.stderr)
        wait = calls_matching(self.sb.globus_calls(), "task", "wait")[0]
        self.assertEqual(wait[wait.index("--timeout") + 1], "5")
        self.assertEqual(wait[wait.index("--polling-interval") + 1], "1")

    def test_task_still_active_after_wait_is_treated_as_timeout(self):
        # e.g. an older CLI that ignores --timeout-exit-code: the status decides.
        scen = {"task_fields": {"status": "ACTIVE", "nice_status": "Queued"},
                "rules": [{"match": ["task", "wait"], "rc": 1}]}
        r = self.xfer("--yes", LAPTOP + ":/d/", PVC + ":/p/", scenario=scen)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("still running", r.stderr)

    def test_missing_login_exits_2_and_asks_the_user(self):
        r = self.xfer("--yes", LAPTOP + ":/d/", PVC + ":/p/", scenario={"logged_in": False})
        self.assertEqual(r.returncode, 2)
        self.assertIn("globus login", r.stderr)
        self.assertIn("human step", r.stderr)
        self.assertEqual(self.transfers(), [])

    def test_consent_required_exits_3_and_prints_the_consent_command(self):
        r = self.xfer("--yes", LAPTOP + ":/d/", PVC + ":/p/",
                      scenario={"consent_scopes": [PVC]})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("globus session consent 'urn:globus:auth:scope:transfer.api.globus.org:"
                      "all[*https://auth.globus.org/scopes/{}/data_access]'".format(PVC), r.stdout)
        self.assertIn("USER", r.stdout)

    def test_consent_text_form_is_passed_through(self):
        msg = ("The resource you are trying to access requires you to consent to additional "
               "access for the Globus CLI.\nPlease run:\n\n  globus session consent 'scope:x'\n\n"
               "to login with the required scopes.")
        r = self.xfer("--yes", LAPTOP + ":/d/", PVC + ":/p/",
                      scenario={"rules": [{"match": ["transfer"], "stdout": msg, "rc": 4}]})
        self.assertEqual(r.returncode, 3)
        self.assertIn("globus session consent 'scope:x'", r.stdout)

    def test_gcsv5_collection_name_resolves_through_the_transfer_api(self):
        r = self.xfer(LAPTOP + ":/d/", PVC + ":/p/", scenario={"gcsv5": [PVC]})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Project Storage", r.stdout)
        self.assertEqual(len(calls_matching(self.sb.globus_calls(), "api", "transfer")), 1)

    def test_globus_missing_explains_install_and_login(self):
        r = self.xfer(LAPTOP + ":/d/", PVC + ":/p/", GLOBUS=str(self.sb.root / "nope" / "globus"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("pipx install globus-cli", r.stderr)
        self.assertIn("globus login", r.stderr)

    def test_usage_errors(self):
        for args in ([LAPTOP + ":/d/"], ["not-a-uuid:/d", PVC + ":/p"],
                     ["--sync-level", "fast", LAPTOP + ":/d", PVC + ":/p"],
                     ["--timeout", "soon", LAPTOP + ":/d", PVC + ":/p"]):
            with self.subTest(args=args):
                self.assertEqual(self.xfer(*args).returncode, 2)
        self.assertEqual(self.transfers(), [])


if __name__ == "__main__":
    unittest.main()
