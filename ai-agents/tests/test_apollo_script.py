"""apollo_script.sh: a local script file run in a throwaway CPU job, with a record on the PVC."""
import os
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

SCRIPT = SCRIPTS / "apollo_script.sh"


class ApolloScript(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.tmp = self.sb.root / "tmp"
        self.tmp.mkdir()
        self.local = self.sb.root / "local"
        self.local.mkdir()

    def tearDown(self):
        os.chmod(str(self.sb.workdir), 0o755)
        self.sb.cleanup()

    def script(self, text, name="job.sh"):
        p = self.local / name
        p.write_text(text)
        return p

    def run_script(self, *args, scenario=None, **env):
        if scenario is not None:
            self.sb.set_runai_scenario(scenario)
        env.setdefault("TMPDIR", str(self.tmp))
        return run([SCRIPT] + [str(a) for a in args], env=self.sb.env(**env), cwd=self.sb.root)

    def submits(self):
        return calls_matching(self.sb.runai_calls(), "training", "submit")

    def name(self):
        return self.submits()[-1][2]

    def record_dir(self):
        return self.sb.workdir / "logs" / "apollo"

    def test_arguments_survive_intact(self):
        args = ["a b", "it's", 'say "hi"', "$HOME_X", "", "back\\slash", "*", "two\nlines",
                "trailing\n", "x > y | z"]
        p = self.script('for a in "$@"; do printf \'<%s>\\n\' "$a"; done; echo "n=$#"\n')
        r = self.run_script(p, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        expected = "".join("<{}>\n".format(a) for a in args) + "n={}\n".format(len(args))
        self.assertEqual(r.stdout, expected)
        self.assertEqual(sorted(x.name for x in self.sb.workdir.iterdir()), ["logs"])

    def test_record_keeps_the_script_and_a_complete_log(self):
        text = "#!/bin/bash\necho hello\necho oops >&2\nexit 5\n"
        p = self.script(text)
        r = self.run_script(p)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(r.stdout, "hello\noops\n")
        self.assertNotIn("[apollo]", r.stdout)
        d = self.record_dir()
        self.assertEqual((d / (self.name() + ".sh")).read_text(), text)
        log = (d / (self.name() + ".log")).read_text()
        self.assertEqual(log, "hello\noops\n[apollo] exit 5\n")
        self.assertIn("record: ", r.stderr)
        self.assertIn("exited with status 5", r.stderr)

    def test_no_record(self):
        r = self.run_script("--no-record", self.script("echo hi\n"))
        self.assertEqual((r.returncode, r.stdout), (0, "hi\n"), r.stderr)
        self.assertEqual(list(self.sb.workdir.iterdir()), [])
        r = self.run_script(self.script("echo hi\n"), RECORD=0)
        self.assertEqual(list(self.sb.workdir.iterdir()), [])

    def test_unwritable_workdir_warns_and_runs(self):
        os.chmod(str(self.sb.workdir), 0o555)
        r = self.run_script(self.script("echo still ran\n"))
        self.assertEqual((r.returncode, r.stdout), (0, "still ran\n"), r.stderr)
        self.assertIn("cannot write", r.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [], "temporary files were left behind")

    def test_runs_in_workdir_and_sees_settings(self):
        r = self.run_script(self.script('echo "$PWD|$MOUNT|$WORKDIR"\n'), "--no-record")
        self.assertEqual(r.returncode, 0, r.stderr)
        pwd, mount, workdir = r.stdout.strip().split("|")
        self.assertTrue(pwd.endswith("/pvc/work"), pwd)
        self.assertEqual((mount, workdir), (str(self.sb.pvc), str(self.sb.workdir)))

    def test_too_big_is_refused_before_submit(self):
        p = self.script("# padding\n" * 800)
        r = self.run_script(p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("10,000", r.stderr)
        self.assertIn("pvc_sync_code.sh", r.stderr)
        self.assertEqual(self.submits(), [])

    def test_options_map_to_flags(self):
        r = self.run_script("--cpu", "2", "--mem", "4G", "--large-shm", "--exclude-node", "n-a",
                            "--exclude-node", "n-b", "--image", "img:1", "--prefix", "My_Job",
                            "--timeout", "60", self.script("true\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.submits()[0]
        head = argv[:argv.index("--command")]

        def values(flag):
            return [head[i + 1] for i, a in enumerate(head) if a == flag]
        self.assertEqual(values("--cpu-core-request"), ["2"])
        self.assertEqual(values("--cpu-memory-request"), ["4G"])
        self.assertEqual(values("--exclude-node"), ["n-a", "n-b"])
        self.assertEqual(values("-i"), ["img:1"])
        self.assertEqual(values("--backoff-limit"), ["0"])
        self.assertIn("--large-shm", head)
        self.assertFalse([a for a in head if a.startswith("--gpu") or a == "-g"], head)
        self.assertTrue(self.name().startswith("my-job-"), self.name())
        self.assertLessEqual(len(self.name()), 50)

    def test_default_prefix_and_long_prefix(self):
        self.run_script(self.script("true\n"))
        self.assertTrue(self.name().startswith("script-"), self.name())
        self.run_script("--prefix", "p" * 80, self.script("true\n"))
        self.assertLessEqual(len(self.name()), 50)

    def test_keep(self):
        r = self.run_script("--keep", self.script("true\n"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(calls_matching(self.sb.runai_calls(), "training", "standard", "delete"),
                         [])

    def test_usage_errors(self):
        for args in ([], ["--bogus", "x.sh"], [str(self.local / "missing.sh")], ["--cpu"]):
            with self.subTest(args=args):
                r = self.run_script(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.submits(), [])

    def test_help(self):
        r = self.run_script("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--no-record", r.stdout)
        self.assertEqual(self.sb.runai_calls(), [])


if __name__ == "__main__":
    unittest.main()
