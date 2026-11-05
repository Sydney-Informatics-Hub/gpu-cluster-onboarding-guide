"""gpu_watch_verdict.py names the limit from captured text; gpu_watch.sh collects that text
through any exec prefix. Fake nvidia-smi and top stand in for the GPU host."""
import json
import sys
import time
import unittest

from helpers import SCRIPTS, Sandbox, calls_matching, run

sys.path.insert(0, str(SCRIPTS))
import gpu_watch_verdict as gv  # noqa: E402

VERDICT = SCRIPTS / "gpu_watch_verdict.py"
WATCH = SCRIPTS / "gpu_watch.sh"
HEADER = "ts,gpu_index,util_gpu,mem_used_mib,mem_total_mib,power_w\n"
TOTAL = 143771   # MiB on a 141 GB GPU


def samples(points, mem_used=17408, mem_total=TOTAL, power="200.5", same_ts=False):
    """A samples CSV. `points` is a list of time points; each is a util number (one GPU) or a
    list of utils, one per GPU."""
    out = [HEADER]
    for i, p in enumerate(points):
        utils = p if isinstance(p, list) else [p]
        ts = "2026-10-01T10:00:00Z" if same_ts else "2026-10-01T10:{:02d}:{:02d}Z".format(
            (i * 5) // 60, (i * 5) % 60)
        for g, u in enumerate(utils):
            out.append("{},{},{},{},{},{}\n".format(ts, g, u, mem_used, mem_total, power))
    return "".join(out)


def top(*procs):
    """`top -bn1` text (procps format) with (pid, %cpu, command) rows."""
    head = ("top - 10:00:00 up 1 day,  2:03,  0 users,  load average: 1.00, 1.00, 1.00\n"
            "Tasks:  10 total,   1 running,   9 sleeping,   0 stopped,   0 zombie\n"
            "%Cpu(s):  1.0 us,  0.5 sy,  0.0 ni, 98.0 id,  0.0 wa,  0.0 hi,  0.0 si,  0.0 st\n"
            "MiB Mem : 2063937.+total, 1000000.0 free,  50000.0 used, 1000000.0 buff/cache\n"
            "MiB Swap:      0.0 total,      0.0 free,      0.0 used. 2000000.0 avail Mem\n\n"
            "    PID USER      PR  NI    VIRT    RES    SHR S  %CPU  %MEM     TIME+ COMMAND\n")
    rows = ["{:>7} user      20   0   30.1g   5.2g   1.1g R {:>5.1f}   0.3  10:01.23 {}\n".format(
        pid, cpu, cmd) for pid, cpu, cmd in procs]
    rows.append("      1 user      20   0    4000   1000    900 S   0.0   0.0   0:00.01 bash\n")
    rows.append("    999 user      20   0    9000   3000   2000 R   6.0   0.0   0:00.01 top\n")
    return head + "".join(rows)


def jsonl(*records):
    return "".join(json.dumps(r) + "\n" for r in records)


# The pattern from a real 1-GPU run: 35-51 % util, ~200 W, 17 of 141 GB, Python at 100 %.
HOST_UTILS = [35, 42, 51, 38, 47, 44, 40, 49, 36, 45, 50, 43]
ONE_CORE = top((4242, 100.0, "python"), (4300, 3.0, "python"))


class Verdicts(unittest.TestCase):
    def verdict(self, *args, **kw):
        return gv.verdict_from_text(*args, **kw)

    def test_labels_are_exactly_the_documented_ones(self):
        self.assertEqual(list(gv.VERDICTS), [
            "stuck-starting", "restart-loop", "nonfinite-streak", "idle-held", "input-bound",
            "host-bound", "bursty", "gpu-bound", "unclear"])
        self.assertEqual(list(gv.ADVISORIES), ["memory-headroom"])
        for label, action in list(gv.VERDICTS.items()) + list(gv.ADVISORIES.items()):
            self.assertTrue(action and "\n" not in action, label)

    def test_host_bound_from_the_one_core_pattern(self):
        r = self.verdict(samples(HOST_UTILS), ONE_CORE)
        self.assertEqual(r["verdict"], "host-bound")
        self.assertIn("100 % of a core", r["evidence"])
        self.assertIn("data wait unknown", r["evidence"])
        self.assertEqual(r["action"], gv.VERDICTS["host-bound"])

    def test_host_bound_with_a_low_data_wait(self):
        log = jsonl(*[{"step": i, "loss": 2.0, "data_wait_share": 0.03} for i in range(20)])
        r = self.verdict(samples(HOST_UTILS), ONE_CORE, log)
        self.assertEqual(r["verdict"], "host-bound")
        self.assertIn("data-wait share 3 %", r["evidence"])

    def test_without_top_the_same_pattern_is_unclear(self):
        self.assertEqual(self.verdict(samples(HOST_UTILS))["verdict"], "unclear")

    def test_input_bound_from_data_wait_share(self):
        log = jsonl(*[{"loss": 1.5, "data_wait_share": 0.35} for _ in range(10)])
        r = self.verdict(samples(HOST_UTILS), ONE_CORE, log)
        self.assertEqual(r["verdict"], "input-bound")
        self.assertIn("data-wait share 35 %", r["evidence"])

    def test_input_bound_from_data_wait_and_step_time(self):
        log = jsonl(*[{"loss": 1.5, "data_wait": 0.3, "step_time": 1.0} for _ in range(10)])
        self.assertEqual(self.verdict(samples(HOST_UTILS), None, log)["verdict"], "input-bound")

    def test_input_bound_from_busy_workers_in_top(self):
        busy = top((4242, 100.0, "python"), (4301, 98.0, "python"), (4302, 95.0, "python"))
        r = self.verdict(samples(HOST_UTILS), busy)
        self.assertEqual(r["verdict"], "input-bound")
        self.assertIn("2 other processes", r["evidence"])

    def test_one_busy_worker_is_not_enough(self):
        one = top((4242, 100.0, "python"), (4301, 98.0, "python"))
        self.assertEqual(self.verdict(samples(HOST_UTILS), one)["verdict"], "host-bound")

    def test_idle_held(self):
        r = self.verdict(samples([0, 1, 0, 2, 0, 0], mem_used=30000), ONE_CORE)
        self.assertEqual(r["verdict"], "idle-held")
        self.assertIn("allocated but unused", r["evidence"])
        self.assertEqual(r["advisories"], [])

    def test_gpu_bound_without_memory_advice(self):
        r = self.verdict(samples([95, 97, 99, 96, 98, 94], mem_used=120000))
        self.assertEqual(r["verdict"], "gpu-bound")
        self.assertEqual(r["advisories"], [])

    def test_bursty_with_unknown_data_wait_says_so(self):
        r = self.verdict(samples([0, 95, 0, 95, 5, 98, 0, 96]))
        self.assertEqual(r["verdict"], "bursty")
        self.assertIn("data wait unknown", r["evidence"])

    def test_bursty_with_low_data_wait(self):
        log = jsonl(*[{"loss": 1.0, "data_wait_share": 0.02} for _ in range(10)])
        r = self.verdict(samples([0, 95, 0, 95, 5, 98, 0, 96]), None, log)
        self.assertEqual(r["verdict"], "bursty")
        self.assertIn("data-wait share 2 %", r["evidence"])

    def test_bursty_by_trough_share(self):
        # stdev under 30, but a third of the samples in troughs while others peak
        r = self.verdict(samples([18, 80, 80, 18, 80, 80, 80, 18, 80, 80]))
        self.assertLess(r["metrics"]["util_stdev"], gv.BURSTY_STDEV)
        self.assertEqual(r["verdict"], "bursty")

    def test_variance_with_moderate_data_wait_is_not_bursty(self):
        log = jsonl(*[{"loss": 1.0, "data_wait_share": 0.08} for _ in range(10)])
        r = self.verdict(samples([0, 95, 0, 95, 5, 98, 0, 96]), None, log)
        self.assertEqual(r["verdict"], "unclear")

    def test_nonfinite_streak_from_jsonl(self):
        lines = ([json.dumps({"step": i, "loss": 2.0 - i / 10}) for i in range(3)]
                 + ['{"step": 3, "loss": NaN}', '{"step": 4, "loss": null}',
                    '{"step": 5, "loss": "nan"}', '{"step": 6, "loss": Infinity}',
                    'not json at all', '[rank0] {"step": 7, "loss": "-inf"}',
                    '{"step": 8, "loss": NaN}'])
        r = self.verdict(samples([95] * 6, mem_used=120000), None, "\n".join(lines) + "\n")
        self.assertEqual(r["verdict"], "nonfinite-streak")
        self.assertIn("6 consecutive non-finite losses (still going)", r["evidence"])

    def test_a_short_nonfinite_run_is_not_a_streak(self):
        lines = jsonl({"loss": 1.0}, {"loss": float("nan")}, {"loss": None}, {"loss": "inf"},
                      {"loss": "NaN"}, {"loss": 0.9})
        r = self.verdict(samples([95] * 6, mem_used=120000), None, lines)
        self.assertEqual(r["verdict"], "gpu-bound")
        self.assertEqual(self.verdict(samples([95] * 6), None, lines, streak_len=4)["verdict"],
                         "nonfinite-streak")

    def test_restart_loop(self):
        r = self.verdict(samples([95] * 6, mem_used=120000), restarts=2)
        self.assertEqual(r["verdict"], "restart-loop")
        self.assertIn("2 times", r["evidence"])

    def test_no_samples_is_stuck_starting(self):
        for text in (HEADER, ""):
            r = self.verdict(text, ONE_CORE, restarts=3)
            self.assertEqual(r["verdict"], "stuck-starting")

    def test_unclear_middle_utilisation(self):
        r = self.verdict(samples([68, 72, 70, 75, 71, 69]))
        self.assertEqual(r["verdict"], "unclear")
        self.assertIn("step_profile.py", r["action"])

    def test_utilisation_not_reported(self):
        text = HEADER + "2026-10-01T10:00:00Z,0,,4000,143771,\n"
        self.assertEqual(self.verdict(text)["verdict"], "unclear")

    def test_memory_headroom_advisory(self):
        low = self.verdict(samples(HOST_UTILS, mem_used=17408), ONE_CORE)
        self.assertEqual([a["label"] for a in low["advisories"]], ["memory-headroom"])
        self.assertIn("17.0 of 140.4 GiB", low["advisories"][0]["evidence"])
        self.assertEqual(low["advisories"][0]["action"], gv.ADVISORIES["memory-headroom"])
        high = self.verdict(samples(HOST_UTILS, mem_used=72000), ONE_CORE)
        self.assertEqual(high["advisories"], [])

    def test_multi_gpu_balanced(self):
        r = self.verdict(samples([[92, 90, 94, 91]] * 6, mem_used=100000))
        self.assertEqual(r["verdict"], "gpu-bound")
        self.assertEqual(r["metrics"]["gpus"], 4)
        self.assertEqual(r["metrics"]["samples"], 6)
        self.assertNotIn("imbalance", r["evidence"])

    def test_multi_gpu_imbalance_and_idle_gpu(self):
        r = self.verdict(samples([[96, 0], [94, 1], [97, 0], [95, 0], [96, 1], [98, 0]],
                                 mem_used=100000))
        self.assertIn("imbalance", r["evidence"])
        self.assertIn("GPU 1 idle", r["evidence"])
        self.assertEqual(r["metrics"]["per_gpu"]["1"]["util_max"], 1)

    def test_samples_sharing_a_timestamp_are_kept_apart(self):
        r = self.verdict(samples([[90, 92]] * 5, same_ts=True, mem_used=100000))
        self.assertEqual(r["metrics"]["samples"], 5)

    def test_na_power_and_garbage_rows(self):
        text = samples(HOST_UTILS, power="[N/A]") + "garbage line\n,,,\n"
        r = self.verdict(text, ONE_CORE)
        self.assertEqual(r["verdict"], "host-bound")
        self.assertEqual(r["metrics"]["samples"], 12)


class VerdictCli(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.cleanup()

    def write(self, name, text):
        p = self.sb.root / name
        p.write_text(text)
        return p

    def test_human_line(self):
        s, t = self.write("s.csv", samples(HOST_UTILS)), self.write("top.txt", ONE_CORE)
        r = run([sys.executable, VERDICT, s, "--top", t])
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertTrue(lines[0].startswith("verdict: host-bound — "), lines[0])
        self.assertTrue(lines[0].endswith(" — " + gv.VERDICTS["host-bound"]))
        self.assertTrue(lines[1].startswith("advisory: memory-headroom — "))

    def test_json_shape(self):
        s = self.write("s.csv", samples(HOST_UTILS))
        log = self.write("t.jsonl", jsonl(*[{"loss": 1, "data_wait_share": 0.4}] * 5))
        r = run([sys.executable, VERDICT, s, "--train-log", log, "--restarts", "0", "--json"])
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertTrue({"verdict", "advisories", "evidence", "action"} <= set(obj))
        self.assertEqual(obj["verdict"], "input-bound")
        self.assertIn(obj["verdict"], gv.VERDICTS)
        self.assertIsInstance(obj["evidence"], str)
        self.assertIsInstance(obj["advisories"], list)
        self.assertEqual(obj["advisories"][0]["label"], "memory-headroom")

    def test_restarts_flag(self):
        s = self.write("s.csv", samples([95] * 4))
        r = run([sys.executable, VERDICT, s, "--restarts", "5"])
        self.assertIn("verdict: restart-loop", r.stdout)

    def test_unreadable_input_exits_2(self):
        r = run([sys.executable, VERDICT, self.sb.root / "missing.csv"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("cannot read samples", r.stderr)
        s = self.write("s.csv", samples([95] * 4))
        r = run([sys.executable, VERDICT, s, "--top", self.sb.root / "missing.txt"])
        self.assertEqual(r.returncode, 2)


FAKE_NVSMI = r"""#!/bin/sh
# One sample per call: line N of $FAKE_GPU_DIR/lines, GPUs separated by ';'; FAIL fails.
d="$FAKE_GPU_DIR"
echo "$*" >> "$d/argv"
n=$(cat "$d/count" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$d/count"
line=$(sed -n "${n}p" "$d/lines")
[ -n "$line" ] || line=$(tail -n 1 "$d/lines")
case "$line" in FAIL*) echo "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver"; exit 9 ;; esac
printf '%s\n' "$line" | tr ';' '\n'
"""
FAKE_TOP = """#!/bin/sh
[ "$*" = "-bn1" ] || { echo "unexpected top args: $*" >&2; exit 1; }
cat "$FAKE_GPU_DIR/top.txt"
"""


class GpuWatchScript(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.gdir = self.sb.root / "gpu"
        self.gdir.mkdir()
        self.sb.add_bin("nvidia-smi", FAKE_NVSMI)
        self.sb.add_bin("top", FAKE_TOP)
        (self.gdir / "top.txt").write_text(ONE_CORE)
        self.env = self.sb.env(FAKE_GPU_DIR=self.gdir, PYTHON=sys.executable)

    def tearDown(self):
        self.sb.cleanup()

    def lines(self, *samples_):
        (self.gdir / "lines").write_text("".join(s + "\n" for s in samples_))

    def watch(self, *args):
        return run(["bash", WATCH] + list(args), env=self.env, timeout=60)

    def test_local_prefix(self):
        self.lines("0, 38, 17408, 143771, 201.3", "0, 45, 17408, 143771, 199.0",
                   "0, 51, 17410, 143771, 205.9")
        out = self.sb.root / "samples.csv"
        t0 = time.time()
        r = self.watch("-n", "3", "-i", "0", "--out", out)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verdict: host-bound", r.stdout)
        self.assertIn("3 of 3 samples succeeded", r.stderr)
        argv = (self.gdir / "argv").read_text().splitlines()
        self.assertEqual(argv[0], "--query-gpu=index,utilization.gpu,memory.used,memory.total,"
                                  "power.draw --format=csv,noheader,nounits")
        rows = out.read_text().splitlines()
        self.assertEqual(rows[0], HEADER.strip())
        self.assertEqual(len(rows), 4)
        self.assertRegex(rows[1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ,0,38,17408,143771,201.3$")

    def test_through_a_runai_exec_prefix(self):
        self.lines("0, 40, 17408, 143771, 200", "0, 44, 17408, 143771, 200",
                   "0, 48, 17408, 143771, 200")
        log = self.sb.workdir / "train.jsonl"
        log.write_text(jsonl(*[{"loss": 1.0, "data_wait_share": 0.4}] * 30))
        r = self.watch("-n", "3", "-i", "0", "--train-log", log, "--json", "--",
                       "runai", "training", "standard", "exec", "job", "-p", "P", "--")
        self.assertEqual(r.returncode, 0, r.stderr)
        obj = json.loads(r.stdout)
        self.assertEqual(obj["verdict"], "input-bound")
        execs = calls_matching(self.sb.runai_calls(), "training", "standard", "exec")
        cmds = [argv[argv.index("--") + 1] for argv in execs]
        self.assertEqual(cmds, ["nvidia-smi"] * 3 + ["top", "tail"])
        self.assertIn(["tail", "-n", "500", str(log)], [a[a.index("--") + 1:] for a in execs])

    def test_failing_nvidia_smi_is_stuck_starting(self):
        self.lines("FAIL")
        r = self.watch("-n", "3", "-i", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verdict: stuck-starting", r.stdout)
        self.assertIn("no sample succeeded", r.stderr)

    def test_failing_nvidia_smi_through_runai_is_stuck_starting(self):
        self.lines("FAIL")
        r = self.watch("-n", "2", "-i", "0", "--",
                       "runai", "training", "standard", "exec", "job", "-p", "P", "--")
        self.assertIn("verdict: stuck-starting", r.stdout)

    def test_a_failed_sample_is_skipped(self):
        self.lines("FAIL", "0, 97, 120000, 143771, 650", "0, 98, 120000, 143771, 655")
        r = self.watch("-n", "3", "-i", "0")
        self.assertIn("2 of 3 samples succeeded", r.stderr)
        self.assertIn("verdict: gpu-bound", r.stdout)

    def test_na_power_is_tolerated(self):
        self.lines("0, 96, 120000, 143771, [N/A]", "0, 97, 120000, 143771, [N/A]",
                   "0, 95, 120000, 143771, [Not Supported]")
        out = self.sb.root / "s.csv"
        r = self.watch("-n", "3", "-i", "0", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verdict: gpu-bound", r.stdout)
        self.assertTrue(all(row.endswith(",") for row in out.read_text().splitlines()[1:]))

    def test_multi_gpu_rows(self):
        self.lines("0, 95, 100000, 143771, 600;1, 0, 1000, 143771, 80",
                   "0, 96, 100000, 143771, 610;1, 1, 1000, 143771, 80")
        r = self.watch("-n", "2", "-i", "0", "--json")
        obj = json.loads(r.stdout)
        self.assertEqual(obj["metrics"]["gpus"], 2)
        self.assertIn("GPU 1 idle", obj["evidence"])

    def test_restarts_are_passed_through(self):
        self.lines("0, 95, 100000, 143771, 600")
        r = self.watch("-n", "1", "--restarts", "4")
        self.assertIn("verdict: restart-loop", r.stdout)

    def test_usage_errors(self):
        for args in (["-n", "x"], ["-i", "abc"], ["--bogus"], ["-n"]):
            with self.subTest(args=args):
                r = self.watch(*args)
                self.assertEqual(r.returncode, 2)
                self.assertIn("gpu_watch:", r.stderr)


if __name__ == "__main__":
    unittest.main()
