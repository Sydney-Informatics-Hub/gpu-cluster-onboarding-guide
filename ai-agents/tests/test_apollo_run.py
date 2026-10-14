"""apollo_run.sh, and the shared library behaviour it exercises (_apollo_lib.sh)."""
import hashlib
import json
import re
import signal
import subprocess
import time
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

RUN = SCRIPTS / "apollo_run.sh"
FAKE_ID = "00000000-1111-2222-3333-444444444444"   # what the fake's whoami reports
TAG = hashlib.sha1(FAKE_ID.encode()).hexdigest()[:6]
NAME_RE = re.compile(r"^peek-[0-9a-f]{6}-\d{6}-\d+-\d+$")
DNS_ERR = ("Error: failed to submit. Error: URL is incorrect or does not exist. DNS lookup failed "
           "for 'runai.example.org': no such host")
TOKEN_ERR = ("Error: failed to get email and ID from token. Authentication failed. "
             "the token has expired")


class ApolloRunBase(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.tmp = self.sb.root / "tmp"
        self.tmp.mkdir()

    def tearDown(self):
        self.sb.cleanup()

    def run_cmd(self, *args, scenario=None, **env):
        if scenario is not None:
            self.sb.set_runai_scenario(scenario)
        env.setdefault("TMPDIR", str(self.tmp))
        return run([RUN] + list(args), env=self.sb.env(**env), cwd=self.sb.root)

    def submits(self):
        return calls_matching(self.sb.runai_calls(), "training", "submit")

    def submitted_name(self, i=-1):
        return self.submits()[i][2]

    def deletes(self):
        return calls_matching(self.sb.runai_calls(), "training", "standard", "delete")


class Quoting(ApolloRunBase):
    def test_special_characters_round_trip_without_stray_files(self):
        cmd = (r"""printf '%s\n' 'plain' 'has<angle>' 'has"quote' | grep -E '^[^"<>]+$'; """
               r"""echo '$HOME_X' "a|b" 'x > y'; echo "tab	in";""")
        r = self.run_cmd(cmd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "plain\n$HOME_X a|b x > y\ntab\tin\n")
        self.assertEqual(list(self.sb.workdir.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.sb.pvc.iterdir()), ["work"])
        self.assertEqual(sorted(p.name for p in self.sb.root.iterdir()),
                         ["bin", "globus_scenario.json", "gstate", "pvc", "runai_scenario.json",
                          "state", "tmp"])
        self.assertEqual(list(self.tmp.iterdir()), [], "temporary files were left behind")

    def test_command_travels_encoded(self):
        self.run_cmd("echo '$HOME_X' > /dev/null")
        argv = self.submits()[0]
        self.assertFalse([a for a in argv if "HOME_X" in a], argv)

    def test_output_without_trailing_newline(self):
        r = self.run_cmd("printf abc")
        self.assertEqual((r.returncode, r.stdout), (0, "abc\n"), r.stderr)

    def test_several_arguments_are_joined(self):
        r = self.run_cmd("echo", "one", "two")
        self.assertEqual(r.stdout, "one two\n", r.stderr)

    def test_runs_in_workdir_with_mount_and_workdir_set(self):
        r = self.run_cmd('pwd; echo "$MOUNT"; echo "$WORKDIR"')
        lines = r.stdout.splitlines()
        self.assertEqual(lines[1:], [str(self.sb.pvc), str(self.sb.workdir)])
        self.assertTrue(lines[0].endswith("/pvc/work"), lines[0])


class ExitStatus(ApolloRunBase):
    def test_command_exit_code_propagates_and_container_exits_zero(self):
        r = self.run_cmd("echo out; exit 7")
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(r.stdout, "out\n")
        self.assertNotIn("[apollo]", r.stdout)
        self.assertIn(": Completed", r.stderr)
        job = json.loads((self.sb.state / "jobs" / (self.submitted_name() + ".json")).read_text())
        self.assertEqual(job["rc"], 0, "the container itself must exit 0")
        self.assertIn("[apollo] exit 7", job["output"])

    def test_success(self):
        r = self.run_cmd("true")
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        self.assertEqual(len(self.deletes()), 1)

    def test_timeout_leaves_a_running_job_in_place(self):
        # deleting a job that is still working would throw its work away
        r = self.run_cmd("true", scenario={"running_polls": 10 ** 6, "simulate": False},
                         TIMEOUT_S=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("timed out", r.stderr)
        self.assertIn("still Running", r.stderr)
        self.assertIn("runai training standard logs", r.stderr)
        self.assertEqual(len(self.deletes()), 0)

    def test_timeout_deletes_a_job_that_never_started(self):
        r = self.run_cmd("true", scenario={"running_polls": 10 ** 6, "simulate": False,
                                           "start_phase": "Pending"}, TIMEOUT_S=1)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Pending", r.stderr)
        self.assertIn("never started", r.stderr)
        self.assertEqual(len(self.deletes()), 1)

    def test_failed_workload_without_exit_code(self):
        r = self.run_cmd("true", scenario={"simulate": False, "rules": [
            {"match": ["describe"], "stdout": "Name: x\nPhase: Failed"}]})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Failed", r.stderr)
        self.assertIn("without reporting an exit code", r.stderr)

    def test_slow_start_warns_once(self):
        r = self.run_cmd("echo ok", scenario={"start_phase": "Pending", "running_polls": 4},
                         STUCK_S=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("§6.5"), 1, r.stderr)
        self.assertIn("ContainerCreating", r.stderr)

    def test_no_warning_once_running(self):
        r = self.run_cmd("echo ok", scenario={"running_polls": 4}, STUCK_S=0)
        self.assertNotIn("§6.5", r.stderr)

    def test_usage_error(self):
        r = self.run_cmd()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.submits(), [])


class Logs(ApolloRunBase):
    def test_not_ready_to_stream_is_retried(self):
        r = self.run_cmd("echo ready", scenario={"logs_not_ready": 2})
        self.assertEqual((r.returncode, r.stdout), (0, "ready\n"), r.stderr)
        self.assertEqual(len(calls_matching(self.sb.runai_calls(), "training", "standard",
                                            "logs")), 3)

    def test_gives_up_after_the_retries(self):
        r = self.run_cmd("echo ready", scenario={"logs_not_ready": 10}, LOG_RETRIES=2)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not ready to stream", r.stderr)
        self.assertEqual(len(calls_matching(self.sb.runai_calls(), "training", "standard",
                                            "logs")), 3)


class Delete(ApolloRunBase):
    def test_one_failure_is_retried_silently(self):
        r = self.run_cmd("echo hi", scenario={"delete_fail": 1}, RETRY_S=0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.deletes()), 2)
        self.assertNotIn("could not delete", r.stderr)

    def test_two_failures_are_reported_and_exit_status_kept(self):
        r = self.run_cmd("exit 3", scenario={"delete_fail": 2}, RETRY_S=0)
        self.assertEqual(r.returncode, 3)
        name = self.submitted_name()
        self.assertIn("apollo: could not delete workload " + name, r.stderr)
        self.assertIn("runai training standard delete {} -p test-proj".format(name), r.stderr)

    def test_interrupt_still_deletes(self):
        self.sb.set_runai_scenario({"running_polls": 10 ** 6, "simulate": False})
        env = self.sb.env(TMPDIR=str(self.tmp), POLL_S="0.2")
        p = subprocess.Popen([str(RUN), "true"], env=env, cwd=str(self.sb.root),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        while not self.submits() and time.time() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 143, err)
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_keep_skips_deletion_and_auto_deletion(self):
        r = self.run_cmd("echo hi", KEEP=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.deletes(), [])
        self.assertIn("KEEP=1", r.stderr)
        self.assertNotIn("--auto-deletion-time-after-completion", self.submits()[0])


class Connection(ApolloRunBase):
    def check_refused(self, r, words):
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn(words, r.stderr)
        self.assertRegex(r.stderr, r"(?m)^apollo_run: ")
        self.assertEqual(self.submits(), [])

    def test_vpn_down(self):
        r = self.run_cmd("true", scenario={"rules": [
            {"match": ["project", "list"], "rc": 1, "stderr": DNS_ERR}]})
        self.check_refused(r, "VPN")

    def test_token_expired(self):
        r = self.run_cmd("true", scenario={"rules": [
            {"match": ["project", "list"], "rc": 1, "stderr": TOKEN_ERR}]})
        self.check_refused(r, "runai login")

    def test_not_logged_in(self):
        r = self.run_cmd("true", scenario={"rules": [
            {"match": ["whoami"], "rc": 1, "stderr": "Error: no token found"}]})
        self.check_refused(r, "runai login")

    def test_other_failure_is_shown_verbatim(self):
        r = self.run_cmd("true", scenario={"rules": [
            {"match": ["project", "list"], "rc": 1, "stderr": "Error: something odd (42)"}]})
        self.check_refused(r, "Error: something odd (42)")

    def test_vpn_drop_at_submit(self):
        r = self.run_cmd("true", scenario={"rules": [
            {"match": ["training", "submit"], "rc": 1, "stderr": DNS_ERR}]})
        self.assertEqual(r.returncode, 2)
        self.assertIn("VPN", r.stderr)
        self.assertNotIn("could not delete", r.stderr)


class Submit(ApolloRunBase):
    def flag_values(self, argv, flag):
        return [argv[i + 1] for i, a in enumerate(argv) if a == flag]

    def test_argv(self):
        r = self.run_cmd("true")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.submits()[0]
        head = argv[:argv.index("--command")]
        self.assertEqual(self.flag_values(head, "--backoff-limit"), ["0"])
        self.assertEqual(self.flag_values(head, "-e"),
                         ["MOUNT=" + str(self.sb.pvc), "WORKDIR=" + str(self.sb.workdir)])
        self.assertEqual(self.flag_values(head, "--cpu-core-request"), ["1"])
        self.assertEqual(self.flag_values(head, "--cpu-memory-request"), ["2G"])
        self.assertEqual(self.flag_values(head, "--existing-pvc"),
                         ["claimname=test-pvc,path=" + str(self.sb.pvc)])
        self.assertEqual(self.flag_values(head, "--auto-deletion-time-after-completion"), ["30m"])
        self.assertEqual(self.flag_values(head, "-p"), ["test-proj"])
        self.assertIn("--run-as-user", head)
        self.assertNotIn("--large-shm", head)
        self.assertNotIn("--exclude-node", head)
        self.assertFalse([a for a in head if a.startswith("--gpu") or a == "-g"], head)
        self.assertEqual(argv[argv.index("--command"):][:4], ["--command", "--", "bash", "-c"])
        self.assertLess(len(" ".join(argv[argv.index("--") + 1:])), 9500)

    def test_large_shm_and_exclude_node(self):
        self.run_cmd("true", LARGE_SHM=1, EXCLUDE_NODE="node-a  node-b", CPU=2, MEM="8G")
        head = self.submits()[0]
        head = head[:head.index("--command")]
        self.assertIn("--large-shm", head)
        self.assertEqual(self.flag_values(head, "--exclude-node"), ["node-a", "node-b"])
        self.assertEqual(self.flag_values(head, "--cpu-core-request"), ["2"])
        self.assertEqual(self.flag_values(head, "--cpu-memory-request"), ["8G"])

    def test_auto_delete_settings(self):
        self.run_cmd("true", AUTO_DELETE="")
        self.run_cmd("true", AUTO_DELETE="2h")
        self.assertNotIn("--auto-deletion-time-after-completion", self.submits()[0])
        self.assertEqual(self.flag_values(self.submits()[1],
                                          "--auto-deletion-time-after-completion"), ["2h"])

    def test_auto_delete_rejected_is_retried_without_it(self):
        r = self.run_cmd("echo ok", scenario={"rules": [
            {"match": ["submit", "--auto-deletion-time-after-completion"], "rc": 1,
             "stderr": "Error: unknown flag: --auto-deletion-time-after-completion"}]})
        self.assertEqual((r.returncode, r.stdout), (0, "ok\n"), r.stderr)
        self.assertEqual(len(self.submits()), 2)
        self.assertNotIn("--auto-deletion-time-after-completion", self.submits()[1])

    def test_names_are_unique_valid_and_tagged(self):
        self.run_cmd("true")
        self.run_cmd("true")
        a, b = self.submitted_name(0), self.submitted_name(1)
        self.assertNotEqual(a, b)
        for n in (a, b):
            self.assertRegex(n, NAME_RE)
            self.assertLessEqual(len(n), 50)
            self.assertTrue(n.startswith("peek-{}-".format(TAG)), n)

    def test_tag_override_and_fallback(self):
        self.run_cmd("true", APOLLO_TAG="Team-7")
        self.assertTrue(self.submitted_name().startswith("peek-team7-"), self.submitted_name())
        self.run_cmd("true", scenario={"rules": [
            {"match": ["whoami"], "stdout": "User someone logged in"}]})
        self.assertTrue(self.submitted_name().startswith("peek-anon-"), self.submitted_name())

    def test_name_override(self):
        r = self.run_cmd("true", NAME="my-check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.submitted_name(), "my-check")
        r = self.run_cmd("true", NAME="Bad_Name")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.submits()), 1)

    def test_too_big_is_refused_before_submit(self):
        r = self.run_cmd("echo " + "x" * 7500)
        self.assertEqual(r.returncode, 2)
        self.assertIn("10,000", r.stderr)
        self.assertIn("PVC", r.stderr)
        self.assertEqual(self.submits(), [])


class Config(ApolloRunBase):
    def test_missing_setting(self):
        env = self.sb.env(TMPDIR=str(self.tmp))
        del env["PVC"]
        r = run([RUN, "true"], env=env, cwd=self.sb.root)
        self.assertEqual(r.returncode, 2)
        self.assertIn("apollo_run: PVC is not set", r.stderr)
        self.assertIn("§3", r.stderr)

    def test_cluster_env_is_read_and_environment_wins(self):
        f = self.sb.root / "cluster.env"
        f.write_text("PROJECT=file-proj\nIMAGE=file-image\n")
        env = self.sb.env(TMPDIR=str(self.tmp), APOLLO_ENV=str(f), IMAGE="env-image")
        del env["PROJECT"]
        r = run([RUN, "true"], env=env, cwd=self.sb.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.submits()[0]
        self.assertEqual(argv[argv.index("-p") + 1], "file-proj")
        self.assertEqual(argv[argv.index("-i") + 1], "env-image")


if __name__ == "__main__":
    unittest.main()
