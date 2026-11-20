#!/usr/bin/env python3
"""gpu_watch_verdict.py — turn a few minutes of GPU samples into one verdict and one action.

Pure logic on captured text, so it can be tested without a GPU and re-run on saved files.
gpu_watch.sh collects the inputs and calls this; you can also call it on files you collected
yourself. The first ten minutes of a long job are when to find out whether the GPU is working
or waiting.

  gpu_watch_verdict.py samples.csv
  gpu_watch_verdict.py samples.csv --top top.txt --train-log train.jsonl
  gpu_watch_verdict.py samples.csv --restarts 2 --json

Inputs:

  SAMPLES.csv   header `ts,gpu_index,util_gpu,mem_used_mib,mem_total_mib,power_w`, one row per
                GPU per sample. Empty values and `[N/A]` are tolerated. No rows at all means
                the job is not running, or no GPU is visible (stuck-starting).
  --top FILE    `top -bn1` output (Linux procps format) from where the job runs, to see which
                process is busy: one Python process pinned at 100 % of a core means the host
                side is the limit; several busy processes besides it are loader workers.
  --train-log   the training log as JSON lines (other lines are ignored). Keys read:
                `loss` (NaN, inf, null or "nan" count as non-finite), `data_wait_share` (0-1)
                or `data_wait` with `step_time`, and `throughput` or `units_per_s`.
  --restarts N  how many times the scheduler re-created the job's pod or container.
  --nonfinite-streak N  consecutive non-finite losses that count as a streak (default 5).

Verdicts, first match wins (VERDICTS below holds each label with its action):

  stuck-starting, restart-loop, nonfinite-streak, idle-held, input-bound, host-bound, bursty,
  gpu-bound, unclear; plus the advisory memory-headroom (ADVISORIES).

Output: one line `verdict: <label> — <evidence> — <action>` and one `advisory: ...` line per
advisory; with --json, one object {verdict, action, evidence, advisories, metrics}.

Exit status is 0 whatever the verdict (it is data, not an error); 2 when an input file cannot
be read or the arguments are wrong.
"""
import argparse
import collections
import csv
import io
import json
import math
import statistics
import sys

# --- Thresholds: every number a verdict depends on, in one place. --------------------------
IDLE_MAX_UTIL = 2.0           # % — max utilisation at or below this, over every sample: idle
LOW_UTIL_MEDIAN = 60.0        # % — median below this: the GPU is waiting on something
GPU_BOUND_MEDIAN = 85.0       # % — median at or above this: the GPU itself is the limit
INPUT_WAIT_SHARE = 0.20       # data-wait share above this (with low util): input-bound
HOST_WAIT_SHARE_MAX = 0.10    # host-bound needs the data-wait share below this, or unknown
BURSTY_WAIT_SHARE_MAX = 0.05  # bursty needs the data-wait share below this, or unknown
MAIN_PROC_BUSY_CPU = 95.0     # % of one core: the busiest process is pinned to a core
WORKER_BUSY_CPU = 90.0        # % of one core: a busy process besides the busiest one
WORKER_BUSY_MIN = 2           # at least this many busy others (loader workers): input-bound
BURSTY_STDEV = 30.0           # utilisation stdev, in points, at or above this: bursty
BURSTY_LOW_UTIL = 20.0        # % — a sample below this is a trough...
BURSTY_HIGH_UTIL = 80.0       # % — ...and one at or above this is a burst
BURSTY_LOW_SHARE = 0.30       # troughs in at least this share of samples, with some bursts
NONFINITE_STREAK = 5          # consecutive non-finite losses: stop the job
MEMORY_HEADROOM_FRAC = 0.25   # peak memory below this share of the total: advise
IMBALANCE_SPREAD = 25.0       # points between per-GPU medians: mention the imbalance

VERDICTS = collections.OrderedDict([
    ("stuck-starting",
     "not running or no GPU visible: if the job is still starting, check its events; a pod "
     "stuck in ContainerCreating holds the GPU — report the node and offer to resubmit "
     "excluding it"),
    ("restart-loop",
     "stop it; read the job's own log on shared storage; make the container exit 0 after "
     "logging its code (train_wrapper.sh)"),
    ("nonfinite-streak",
     "stop it; fix the numerics before spending more"),
    ("idle-held",
     "nothing is using the GPU: suspend or stop the workload"),
    ("input-bound",
     "the input pipeline is the limit: more workers, faster storage, precompute"),
    ("host-bound",
     "the Python host side is the limit: remove host-device syncs, bigger batches, "
     "torch.compile/CUDA graphs"),
    ("bursty",
     "batch composition or per-step host work; profile the step (step_profile.py)"),
    ("gpu-bound",
     "the GPU is busy: tune batch size and precision, then leave it"),
    ("unclear",
     "measure: step_profile.py and loader_bench.py"),
])

ADVISORIES = collections.OrderedDict([
    ("memory-headroom", "a smaller fraction or a bigger batch"),
])

# Verdicts about how well a working job uses the GPU; only these get advisories.
PERFORMANCE_VERDICTS = ("input-bound", "host-bound", "bursty", "gpu-bound", "unclear")

SAMPLE_FIELDS = ("ts", "gpu_index", "util_gpu", "mem_used_mib", "mem_total_mib", "power_w")


class InputError(Exception):
    """An input could not be read or understood."""


# --- Parsing -------------------------------------------------------------------------------

def _num(text):
    """A float from a CSV cell, or None for empty, `[N/A]`, `[Not Supported]` and the like."""
    if text is None:
        return None
    t = text.strip()
    if not t or t.startswith("["):
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def parse_samples(text):
    """Rows of the samples CSV as dicts; rows that do not parse are skipped."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if not rec or rec[0].strip() == "ts":
            continue
        if len(rec) < 5:
            continue
        rec = rec + [""] * (len(SAMPLE_FIELDS) - len(rec))
        idx = _num(rec[1])
        if idx is None:
            continue
        rows.append({
            "ts": rec[0].strip(),
            "gpu": int(idx),
            "util": _num(rec[2]),
            "mem_used": _num(rec[3]),
            "mem_total": _num(rec[4]),
            "power": _num(rec[5]),
        })
    return rows


def group_samples(rows):
    """Group rows into time points: a new point starts when the timestamp changes or a GPU
    index repeats (several samples can share a timestamp at sub-second intervals)."""
    groups, cur, seen, cur_ts = [], [], set(), None
    for r in rows:
        if cur and (r["ts"] != cur_ts or r["gpu"] in seen):
            groups.append(cur)
            cur, seen = [], set()
        cur.append(r)
        seen.add(r["gpu"])
        cur_ts = r["ts"]
    if cur:
        groups.append(cur)
    return groups


def parse_top(text):
    """Processes from `top -bn1` as dicts {pid, cpu, command}, busiest first."""
    lines = text.splitlines()
    header_at, cols = None, None
    for i, line in enumerate(lines):
        f = line.split()
        if "PID" in f and "%CPU" in f:
            header_at, cols = i, f
            break
    if header_at is None:
        return []
    cpu_i = cols.index("%CPU")
    pid_i = cols.index("PID")
    cmd_i = cols.index("COMMAND") if "COMMAND" in cols else len(cols) - 1
    procs = []
    for line in lines[header_at + 1:]:
        f = line.split()
        if len(f) <= max(cpu_i, pid_i, cmd_i):
            continue
        try:
            cpu = float(f[cpu_i].replace(",", "."))
        except ValueError:
            continue
        command = " ".join(f[cmd_i:])
        if command.split()[0] == "top":
            continue    # top itself is not the workload
        procs.append({"pid": f[pid_i], "cpu": cpu, "command": command})
    procs.sort(key=lambda p: -p["cpu"])
    return procs


_NONFINITE_STRINGS = {"nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity",
                      "-infinity"}


def _loss_is_finite(v):
    """False for NaN, inf, null and their string spellings; None if `v` is not a loss."""
    if v is None:
        return False
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return math.isfinite(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _NONFINITE_STRINGS:
            return False
        try:
            return math.isfinite(float(s))
        except ValueError:
            return None
    return None


def _finite_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def parse_train_log(text):
    """JSON objects from a training log; lines that are not JSON (or carry a prefix before the
    first `{`, as some loggers add) are tolerated."""
    records = []
    for line in text.splitlines():
        i = line.find("{")
        if i < 0:
            continue
        try:
            obj = json.loads(line[i:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def train_summary(records, streak_len=NONFINITE_STREAK):
    """Loss streaks, data-wait share and throughput from training-log records."""
    longest = run = 0
    losses = 0
    last_loss = None
    shares, throughputs = [], []
    for rec in records:
        if "loss" in rec:
            finite = _loss_is_finite(rec["loss"])
            if finite is not None:
                losses += 1
                if finite:
                    run = 0
                    last_loss = float(rec["loss"])
                else:
                    run += 1
                    longest = max(longest, run)
        share = _finite_number(rec.get("data_wait_share"))
        if share is None:
            wait, step = _finite_number(rec.get("data_wait")), _finite_number(rec.get("step_time"))
            if wait is not None and step is not None and step > 0:
                share = wait / step
        if share is not None and 0.0 <= share <= 1.0:
            shares.append(share)
        tp = _finite_number(rec.get("throughput", rec.get("units_per_s")))
        if tp is not None:
            throughputs.append(tp)
    return {
        "records": len(records),
        "losses": losses,
        "last_finite_loss": last_loss,
        "nonfinite_longest": longest,
        "nonfinite_trailing": run,
        "nonfinite_streak": longest >= streak_len,
        "data_wait_share": statistics.mean(shares) if shares else None,
        "data_wait_lines": len(shares),
        "throughput_median": statistics.median(throughputs) if throughputs else None,
    }


# --- Deciding ------------------------------------------------------------------------------

def _pct(x):
    return "{:.0f} %".format(x)


def gpu_summary(rows):
    """Per-GPU and aggregate statistics from parsed sample rows."""
    groups = group_samples(rows)
    per_gpu = collections.OrderedDict()
    for r in rows:
        g = per_gpu.setdefault(r["gpu"], {"util": [], "mem_frac": [], "mem_used": [],
                                          "mem_total": [], "power": []})
        if r["util"] is not None:
            g["util"].append(r["util"])
        if r["mem_used"] is not None:
            g["mem_used"].append(r["mem_used"])
        if r["mem_total"] is not None:
            g["mem_total"].append(r["mem_total"])
        if r["mem_used"] is not None and r["mem_total"]:
            g["mem_frac"].append(r["mem_used"] / r["mem_total"])
        if r["power"] is not None:
            g["power"].append(r["power"])
    # The aggregate series: mean utilisation across GPUs at each time point. Data-parallel
    # GPUs step together, so bursts line up and the mean keeps them.
    agg = []
    for grp in groups:
        utils = [r["util"] for r in grp if r["util"] is not None]
        if utils:
            agg.append(statistics.mean(utils))
    gpus = collections.OrderedDict()
    for idx, g in per_gpu.items():
        gpus[idx] = {
            "util_median": statistics.median(g["util"]) if g["util"] else None,
            "util_max": max(g["util"]) if g["util"] else None,
            "mem_peak_frac": max(g["mem_frac"]) if g["mem_frac"] else None,
            "mem_peak_mib": max(g["mem_used"]) if g["mem_used"] else None,
            "mem_total_mib": max(g["mem_total"]) if g["mem_total"] else None,
            "power_median_w": statistics.median(g["power"]) if g["power"] else None,
        }
    all_utils = [r["util"] for r in rows if r["util"] is not None]
    return {
        "samples": len(groups),
        "rows": len(rows),
        "gpus": gpus,
        "util_series": agg,
        "util_max_any": max(all_utils) if all_utils else None,
        "util_median": statistics.median(agg) if agg else None,
        "util_min": min(agg) if agg else None,
        "util_max": max(agg) if agg else None,
        "util_stdev": statistics.pstdev(agg) if agg else None,
        "low_share": (sum(1 for u in agg if u < BURSTY_LOW_UTIL) / len(agg)) if agg else None,
        "any_high": any(u >= BURSTY_HIGH_UTIL for u in agg),
    }


def _gpu_evidence(s):
    parts = ["util median {} (min {:.0f}, max {:.0f}, stdev {:.0f}; {} samples x {} GPU{})".format(
        _pct(s["util_median"]), s["util_min"], s["util_max"], s["util_stdev"], s["samples"],
        len(s["gpus"]), "" if len(s["gpus"]) == 1 else "s")]
    peak = _memory_peak(s)
    if peak is not None:
        frac, used, total = peak
        parts.append("memory peak {:.1f} of {:.1f} GiB ({})".format(
            used / 1024.0, total / 1024.0, _pct(100 * frac)))
    powers = [g["power_median_w"] for g in s["gpus"].values() if g["power_median_w"] is not None]
    if powers:
        parts.append("power median {:.0f} W".format(statistics.median(powers)))
    return parts


def _memory_peak(s):
    """(fraction, used MiB, total MiB) on the GPU with the highest peak memory share."""
    best = None
    for g in s["gpus"].values():
        if g["mem_peak_frac"] is None:
            continue
        if best is None or g["mem_peak_frac"] > best[0]:
            best = (g["mem_peak_frac"], g["mem_peak_mib"], g["mem_total_mib"])
    return best


def _imbalance(s):
    """A sentence on uneven or idle GPUs in a multi-GPU job, or None."""
    if len(s["gpus"]) < 2:
        return None
    meds = [(i, g["util_median"]) for i, g in s["gpus"].items() if g["util_median"] is not None]
    if len(meds) < 2:
        return None
    idle = [str(i) for i, g in s["gpus"].items()
            if g["util_max"] is not None and g["util_max"] <= IDLE_MAX_UTIL]
    spread = max(m for _, m in meds) - min(m for _, m in meds)
    if not idle and spread < IMBALANCE_SPREAD:
        return None
    text = "imbalance: per-GPU median util " + ", ".join(
        "GPU{} {:.0f}".format(i, m) for i, m in meds)
    if idle:
        text += "; GPU {} idle (is the job using every GPU it holds?)".format(", ".join(idle))
    return text


def decide(rows, procs=None, train=None, restarts=0, streak_len=NONFINITE_STREAK):
    """The verdict as a dict {verdict, action, evidence, advisories, metrics}.

    `rows` from parse_samples, `procs` from parse_top (None if unknown), `train` from
    train_summary (None if no log).
    """
    s = gpu_summary(rows)
    wait = train["data_wait_share"] if train else None
    busiest = procs[0] if procs else None
    busy_others = [p for p in (procs or [])[1:] if p["cpu"] >= WORKER_BUSY_CPU]
    metrics = {
        "samples": s["samples"], "gpus": len(s["gpus"]),
        "util_median": s["util_median"], "util_min": s["util_min"], "util_max": s["util_max"],
        "util_stdev": s["util_stdev"], "low_util_share": s["low_share"],
        "mem_peak_frac": (_memory_peak(s) or (None,))[0],
        "per_gpu": {str(i): g for i, g in s["gpus"].items()},
        "busiest_process": busiest, "busy_other_processes": len(busy_others) if procs else None,
        "data_wait_share": wait, "restarts": restarts, "train": train,
    }

    def result(label, evidence):
        return {"verdict": label, "action": VERDICTS[label], "evidence": "; ".join(evidence),
                "advisories": [], "metrics": metrics}

    def wait_text():
        if wait is None:
            return "data wait unknown (log data_wait_share to rule out input stalls)"
        return "data-wait share {} over {} log lines".format(
            _pct(100 * wait), train["data_wait_lines"])

    def top_text():
        if busiest is None:
            return "busiest process unknown (no top output)"
        return "busiest process {} (PID {}) at {} of a core; {} other process{} at >= {}".format(
            busiest["command"].split()[0], busiest["pid"], _pct(busiest["cpu"]),
            len(busy_others), "" if len(busy_others) == 1 else "es", _pct(WORKER_BUSY_CPU))

    if not rows:
        return result("stuck-starting", ["no GPU samples: nvidia-smi failed or found no GPU"])
    if restarts >= 1:
        return result("restart-loop", ["the scheduler re-created the job {} time{}".format(
            restarts, "" if restarts == 1 else "s")] + _gpu_evidence_or_none(s))
    if train and train["nonfinite_longest"] >= streak_len:
        ongoing = train["nonfinite_trailing"] >= streak_len
        return result("nonfinite-streak", [
            "{} consecutive non-finite losses{} in the last {} log records".format(
                train["nonfinite_longest"], " (still going)" if ongoing else "",
                train["records"])])
    if s["util_median"] is None:
        return result("unclear", ["{} samples but no utilisation reported ([N/A], for "
                                  "example a MIG instance)".format(s["samples"])])
    if s["util_max_any"] <= IDLE_MAX_UTIL:
        ev = ["max util {} over {} samples".format(_pct(s["util_max_any"]), s["samples"])]
        peak = _memory_peak(s)
        if peak is not None and peak[1]:
            ev.append("{:.1f} GiB of memory allocated but unused".format(peak[1] / 1024.0))
        return _finish(result("idle-held", ev), s)

    low = s["util_median"] < LOW_UTIL_MEDIAN
    gpu_ev = _gpu_evidence(s)
    if low and ((wait is not None and wait > INPUT_WAIT_SHARE)
                or len(busy_others) >= WORKER_BUSY_MIN):
        return _finish(result("input-bound", gpu_ev + [wait_text(), top_text()]), s)
    if (low and busiest is not None and busiest["cpu"] >= MAIN_PROC_BUSY_CPU
            and (wait is None or wait < HOST_WAIT_SHARE_MAX)):
        return _finish(result("host-bound", gpu_ev + [top_text(), wait_text()]), s)
    bursty = (s["util_stdev"] >= BURSTY_STDEV
              or (s["low_share"] >= BURSTY_LOW_SHARE and s["any_high"]))
    if bursty and (wait is None or wait < BURSTY_WAIT_SHARE_MAX):
        ev = gpu_ev + ["{} of samples below {}".format(_pct(100 * s["low_share"]),
                                                      _pct(BURSTY_LOW_UTIL)), wait_text()]
        return _finish(result("bursty", ev), s)
    if s["util_median"] >= GPU_BOUND_MEDIAN:
        return _finish(result("gpu-bound", gpu_ev), s)
    ev = gpu_ev + [wait_text()]
    if procs is not None:
        ev.append(top_text())
    return _finish(result("unclear", ev), s)


def _gpu_evidence_or_none(s):
    return _gpu_evidence(s) if s["util_median"] is not None else []


def _finish(res, s):
    """Add the multi-GPU note and advisories to a performance verdict."""
    note = _imbalance(s)
    if note:
        res["evidence"] += "; " + note
    peak = _memory_peak(s)
    if (res["verdict"] in PERFORMANCE_VERDICTS and peak is not None
            and peak[0] < MEMORY_HEADROOM_FRAC):
        res["advisories"].append({
            "label": "memory-headroom",
            "evidence": "peak memory {:.1f} of {:.1f} GiB ({}) on the fullest GPU".format(
                peak[1] / 1024.0, peak[2] / 1024.0, _pct(100 * peak[0])),
            "action": ADVISORIES["memory-headroom"],
        })
    return res


def verdict_from_text(samples_text, top_text=None, train_text=None, restarts=0,
                      streak_len=NONFINITE_STREAK):
    """The verdict from captured text; the entry point for tests and other tools."""
    rows = parse_samples(samples_text)
    procs = parse_top(top_text) if top_text is not None else None
    train = (train_summary(parse_train_log(train_text), streak_len)
             if train_text is not None else None)
    return decide(rows, procs, train, restarts, streak_len)


def format_human(res):
    lines = ["verdict: {} — {} — {}".format(res["verdict"], res["evidence"], res["action"])]
    for adv in res["advisories"]:
        lines.append("advisory: {} — {} — {}".format(adv["label"], adv["evidence"],
                                                     adv["action"]))
    return "\n".join(lines)


def _read(path, what):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        raise InputError("cannot read {} {}: {}".format(what, path, e.strerror or e))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="One verdict and one action from GPU samples (see the header for details).")
    ap.add_argument("samples", help="samples CSV (ts,gpu_index,util_gpu,mem_used_mib,"
                                    "mem_total_mib,power_w)")
    ap.add_argument("--top", help="saved `top -bn1` output")
    ap.add_argument("--train-log", help="training log with JSON lines")
    ap.add_argument("--restarts", type=int, default=0,
                    help="times the scheduler re-created the job (default 0)")
    ap.add_argument("--nonfinite-streak", type=int, default=NONFINITE_STREAK,
                    help="consecutive non-finite losses that count (default %(default)s)")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    args = ap.parse_args(argv)
    if args.restarts < 0 or args.nonfinite_streak < 1:
        ap.error("--restarts must be >= 0 and --nonfinite-streak >= 1")
    try:
        samples = _read(args.samples, "samples")
        top = _read(args.top, "top output") if args.top else None
        train = _read(args.train_log, "training log") if args.train_log else None
    except InputError as e:
        sys.stderr.write("gpu_watch_verdict: {}\n".format(e))
        return 2
    res = verdict_from_text(samples, top, train, args.restarts, args.nonfinite_streak)
    try:
        sys.stdout.reconfigure(errors="replace")   # a non-UTF-8 locale must not crash on "—"
    except (AttributeError, ValueError):
        pass
    if args.json:
        print(json.dumps(res, indent=2, sort_keys=False))
    else:
        print(format_human(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
