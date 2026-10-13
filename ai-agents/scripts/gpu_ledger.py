#!/usr/bin/env python3
"""gpu_ledger.py — estimate GPU-hours before a run, and record what a run actually held after it.

On a fee-for-service cluster a GPU-hour is money. Estimate first, from throughput measured on
the real job; afterwards, record the allocation window the scheduler reports, not the time the
training loop ran: a pod stuck starting, or re-created after crashes, holds the GPU too.

  gpu_ledger.py estimate --units 1200000 --epochs 10 --throughput 850 --gpus 2
  gpu_ledger.py add runai --json describe.json --purpose "baseline, seed 1"
  gpu_ledger.py add runai --json describe.json --now 2026-10-14T09:00:00Z   # still running
  gpu_ledger.py add manual --name run7 --start 2026-10-01T09:00Z --end 2026-10-01T13:30Z --gpus 1
  gpu_ledger.py show --ledger gpu_ledger.md

estimate:  wall = U x E / T x (1 + val_frac) + startup, with T the units/s measured for the
           whole job (all GPUs together, steady state). Prints wall hours and GPU-hours, also
           fraction-weighted with --fraction.

add runai: reads `runai training standard describe JOB -p PROJECT -o json` (pass
           `--pod-limit -1 --event-limit -1`: by default it lists at most 10 pods and 50
           events). The window runs from the first `Scheduled` or `Bound` event to the last
           terminal event (`Completed`, `Succeeded`, `Failed`, or a deletion); with no terminal
           event the job is still running and --now is required. If every pod has start and end
           times, the union of the pod windows is used instead, and the output says so. The GPU
           amount is searched under `compute` (allocated first, then requested; devices and
           portion) and the path used is printed; --gpus/--fraction override it. Do not take
           the amount from the table view: it shows GPU 1.00 even for a 0.1 fraction. These
           field names are partly unverified: an unrecognised shape is an error that names what
           was expected, never a guess.

add manual: any other scheduler: --name, --start, --end, --gpus [--fraction].

show:      the ledger's rows and totals.

The ledger is a CSV (default gpu_ledger.csv), or a markdown table if the file name ends in .md,
with the columns in COLUMNS. Each row keeps both fraction x wall (gpu_h_fraction) and whole-GPU
wall (gpu_h_whole), with running totals of each, because whether a fraction is billed as a
fraction is not confirmed. Times without a timezone are read as UTC.

Exit status: 0 on success; 1 when a row is refused (the workload is already in the ledger: use
--replace) or the ledger does not exist (show); 2 on a usage error or unreadable input.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

COLUMNS = ["date", "workload", "gpus", "fraction", "wall_h", "gpu_h_fraction", "gpu_h_whole",
           "total_fraction", "total_whole", "purpose", "method"]
DEFAULT_LEDGER = "gpu_ledger.csv"

# Run:ai describe -o json: assumed field names. `events[].reason` and `events[].createdAt` are
# verified; the rest are tolerant guesses until real captures confirm them (tests/fixtures).
START_REASONS = ("Scheduled", "Bound")
END_REASONS = ("Completed", "Succeeded", "Failed")      # plus any reason containing "delet"
RUNNING_PHASES = ("Pending", "Initializing", "Creating", "ContainerCreating", "Running",
                  "Resuming", "Restarting")
TERMINAL_PHASES = ("Completed", "Succeeded", "Failed", "Stopped", "Deleted", "Deleting")
EVENT_PATHS = ("events", "status.events")
PHASE_PATHS = ("phase", "status.phase", "status")
NAME_PATHS = ("name", "metadata.name", "workloadName")
POD_PATHS = ("podsInfo.pods", "pods", "status.pods")
POD_START_KEYS = ("startedAt", "startTime", "scheduledAt", "createdAt", "creationTimestamp")
POD_END_KEYS = ("completedAt", "finishedAt", "endTime", "completionTime", "deletedAt",
                "deletionTimestamp")
GPU_PATHS = (     # (path, kind): allocated first, then requested
    ("compute.allocatedResources.gpuCompute", "allocated"),
    ("compute.allocatedResources.gpu", "allocated"),
    ("compute.allocated.gpu", "allocated"),
    ("allocatedResources.gpuCompute", "allocated"),
    ("allocatedResources.gpu", "allocated"),
    ("compute.requestedResources.gpuCompute", "requested"),
    ("compute.requestedResources.gpu", "requested"),
    ("compute.requested.gpu", "requested"),
    ("requestedResources.gpuCompute", "requested"),
    ("requestedResources.gpu", "requested"),
    ("compute", "requested"),          # flat submit-style fields such as gpuDevicesRequest
    ("spec.compute", "requested"),
)
DEVICE_KEYS = ("devices", "gpuDevices", "gpuDevicesRequest", "deviceCount", "count", "gpus")
PORTION_KEYS = ("portion", "gpuPortion", "gpuPortionRequest", "fraction")
AMOUNT_KEYS = ("amount", "value", "request", "total", "gpu", "gpuCompute")
DEFAULT_EVENT_LIMIT = 50   # describe's default caps: hitting one means the list may be cut
DEFAULT_POD_LIMIT = 10

MD_PREFACE = ("# GPU ledger\n\nBoth columns are kept until it is confirmed whether a GPU "
              "fraction is billed as a fraction (gpu_h_fraction) or as a whole GPU "
              "(gpu_h_whole).\n\n")


class LedgerError(Exception):
    """Bad input; the message says what was expected. Exit status 2."""


class Refused(Exception):
    """A valid request that is refused. Exit status 1."""


# --- Small helpers -------------------------------------------------------------------------

def parse_iso(text, what="time"):
    """An aware UTC datetime from ISO 8601 (Z, offsets, any fractional digits; naive is UTC)."""
    if isinstance(text, (int, float)):
        raise LedgerError("{}: expected an ISO 8601 string, got {!r}".format(what, text))
    s = str(text).strip()
    if s.lower() == "now":
        return datetime.now(timezone.utc).replace(microsecond=0)
    s = re.sub(r"[zZ]$", "+00:00", s)
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s)
    # Python 3.9 accepts only 3 or 6 fractional digits; Kubernetes may give 0-9.
    m = re.match(r"^(.*?[T ]\d\d:\d\d(?::\d\d)?)(?:\.(\d+))?(.*)$", s)
    if m and m.group(2):
        s = m.group(1) + "." + (m.group(2) + "000000")[:6] + m.group(3)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise LedgerError("{}: cannot read {!r} as an ISO 8601 time (e.g. "
                          "2026-10-01T09:00:00Z)".format(what, text))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_path(data, path):
    """The value at a dotted path, or None."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def fmt_h(x):
    return "{:.3f}".format(x)


def fmt_frac(f):
    return "{:g}".format(f)


def fmt_num(x):
    """1,200,000 rather than 1.2e+06; up to 4 significant decimals otherwise."""
    return "{:,.0f}".format(x) if x == int(x) else "{:,.4g}".format(x)


def gpus_and_fraction(gpus, fraction):
    """Validate --gpus/--fraction; a --gpus below 1 without --fraction means one device at
    that fraction (`--gpus 0.1`)."""
    if gpus is not None and gpus <= 0:
        raise LedgerError("--gpus must be positive")
    if fraction is not None and not 0 < fraction <= 1:
        raise LedgerError("--fraction must be in (0, 1]")
    if gpus is not None and gpus < 1:
        if fraction is not None:
            raise LedgerError("give --gpus as a whole number of devices with --fraction")
        return 1, gpus
    if gpus is not None and gpus != int(gpus):
        raise LedgerError("--gpus must be a whole number of devices (use --fraction for a "
                          "share of each)")
    return (int(gpus) if gpus is not None else None), fraction


# --- estimate --------------------------------------------------------------------------------

def estimate(units, epochs, throughput, gpus=1, fraction=None, val_frac=0.1, startup_min=5.0):
    """Wall hours and GPU-hours for a planned run, as a dict."""
    if units <= 0 or epochs <= 0 or throughput <= 0:
        raise LedgerError("--units, --epochs and --throughput must be positive")
    if val_frac < 0 or startup_min < 0:
        raise LedgerError("--val-frac and --startup-min must be >= 0")
    gpus, fraction = gpus_and_fraction(gpus, fraction)
    train_s = units * epochs / throughput * (1 + val_frac)
    wall_s = train_s + startup_min * 60
    wall_h = wall_s / 3600.0
    res = {"train_s": train_s, "startup_s": startup_min * 60, "wall_s": wall_s, "wall_h": wall_h,
           "gpus": gpus, "fraction": fraction, "gpu_h_whole": wall_h * gpus,
           "gpu_h_fraction": wall_h * gpus * fraction if fraction is not None else None}
    return res


def cmd_estimate(a):
    r = estimate(a.units, a.epochs, a.throughput, a.gpus, a.fraction, a.val_frac, a.startup_min)
    plural = "" if r["gpus"] == 1 else "s"
    out = [
        "wall = U × E ÷ T × (1 + val_frac) + startup",
        "     = {} × {} ÷ {}/s × (1 + {:g}) + {:g} min".format(
            fmt_num(a.units), fmt_num(a.epochs), fmt_num(a.throughput), a.val_frac,
            a.startup_min),
        "     = {:,.0f} s + {:,.0f} s = {:,.0f} s = {:.2f} h".format(
            r["train_s"], r["startup_s"], r["wall_s"], r["wall_h"]),
        "GPU-hours, whole GPUs:    {:.2f} h × {} GPU{} = {:.2f}".format(
            r["wall_h"], r["gpus"], plural, r["gpu_h_whole"]),
    ]
    if r["fraction"] is not None:
        out.append("GPU-hours, fraction {:g}: {:.2f} h × {} GPU{} × {:g} = {:.2f}".format(
            r["fraction"], r["wall_h"], r["gpus"], plural, r["fraction"], r["gpu_h_fraction"]))
        out.append("(both shown: whether a fraction is billed as a fraction is not confirmed)")
    out.append("T is units/s for the whole job (all GPUs together), measured on the real job "
               "after warm-up, not a guess or a per-GPU figure.")
    print("\n".join(out))
    return 0


# --- The ledger file -----------------------------------------------------------------------

def _is_md(path):
    return path.lower().endswith(".md")


def _md_cells(line):
    cells = re.split(r"(?<!\\)\|", line.strip())
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return [c.strip().replace("\\|", "|") for c in cells]


def read_ledger(path):
    """(rows, preface, suffix): rows as dicts of strings; the text around a markdown table."""
    if not os.path.exists(path):
        return [], None, ""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise LedgerError("cannot read ledger {}: {}".format(path, e.strerror or e))
    if not _is_md(path):
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames and list(reader.fieldnames) != COLUMNS:
            raise LedgerError("{} has columns {} but a ledger has {}".format(
                path, ",".join(reader.fieldnames), ",".join(COLUMNS)))
        return [dict(r) for r in reader], None, ""
    lines = text.splitlines(True)
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and _md_cells(line) == COLUMNS:
            start = i
            break
    if start is None:
        raise LedgerError("{} has no ledger table (a header row with {})".format(
            path, " | ".join(COLUMNS)))
    end = start + 2
    rows = []
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        cells = _md_cells(lines[end])
        if len(cells) != len(COLUMNS):
            raise LedgerError("{}: table row {} has {} cells, expected {}".format(
                path, end + 1, len(cells), len(COLUMNS)))
        rows.append(dict(zip(COLUMNS, cells)))
        end += 1
    return rows, "".join(lines[:start]), "".join(lines[end:])


def with_totals(rows):
    """Recompute the running totals in order."""
    tf = tw = 0.0
    for r in rows:
        tf += float(r["gpu_h_fraction"])
        tw += float(r["gpu_h_whole"])
        r["total_fraction"], r["total_whole"] = fmt_h(tf), fmt_h(tw)
    return rows


def write_ledger(path, rows, preface=None, suffix=""):
    rows = with_totals(rows)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    buf = io.StringIO()
    if _is_md(path):
        buf.write(MD_PREFACE if preface is None else preface)
        buf.write("| " + " | ".join(COLUMNS) + " |\n")
        buf.write("|" + "|".join(" --- " for _ in COLUMNS) + "|\n")
        for r in rows:
            cells = [str(r.get(c, "")).replace("\n", " ").replace("|", "\\|") for c in COLUMNS]
            buf.write("| " + " | ".join(cells) + " |\n")
        buf.write(suffix)
    else:
        w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)


def make_row(start, workload, gpus, fraction, wall_h, purpose, method):
    frac = fraction if fraction is not None else 1.0
    return {"date": start.strftime("%Y-%m-%d"), "workload": workload, "gpus": str(gpus),
            "fraction": fmt_frac(frac), "wall_h": fmt_h(wall_h),
            "gpu_h_fraction": fmt_h(wall_h * gpus * frac), "gpu_h_whole": fmt_h(wall_h * gpus),
            "total_fraction": "", "total_whole": "", "purpose": purpose or "",
            "method": method}


def append_row(path, row, replace=False):
    rows, preface, suffix = read_ledger(path)
    same = [r for r in rows if r["workload"] == row["workload"]]
    if same and not replace:
        raise Refused("{} is already in {} ({} row{}); pass --replace to update it, for "
                      "example a running job recorded earlier with --now".format(
                          row["workload"], path, len(same), "" if len(same) == 1 else "s"))
    rows = [r for r in rows if r["workload"] != row["workload"]] + [row]
    write_ledger(path, rows, preface, suffix)
    return with_totals(rows)


def report_row(path, row, rows):
    print("GPU-hours: {} as a fraction ({} GPU{} × {} × {} h), {} as whole GPUs — both kept: "
          "whether a fraction is billed as a fraction is not confirmed".format(
              row["gpu_h_fraction"], row["gpus"], "" if row["gpus"] == "1" else "s",
              row["fraction"], row["wall_h"], row["gpu_h_whole"]))
    print("ledger {}: {} row{}; totals {} GPU-h (fraction), {} GPU-h (whole)".format(
        path, len(rows), "" if len(rows) == 1 else "s", rows[-1]["total_fraction"],
        rows[-1]["total_whole"]))


# --- add runai -----------------------------------------------------------------------------

def interpret_gpu(value):
    """(devices, fraction or None) from a GPU field, 'zero', or None if unrecognised."""
    n = as_number(value)
    if n is not None:
        if n <= 0:
            return "zero"
        if n < 1:
            return 1, n
        if n == int(n):
            return int(n), None
        devices = int(math.ceil(n))
        return devices, n / devices
    if isinstance(value, dict):
        devices = next((as_number(value[k]) for k in DEVICE_KEYS
                        if k in value and as_number(value[k]) is not None), None)
        portion = next((as_number(value[k]) for k in PORTION_KEYS
                        if k in value and as_number(value[k]) is not None), None)
        if devices is None and portion is None:
            for k in AMOUNT_KEYS:
                if k in value:
                    inner = interpret_gpu(value[k])
                    if inner is not None:
                        return inner
            return None
        if portion is not None and portion <= 0:
            portion = None
        if devices is None or devices <= 0:
            if portion is None:
                return "zero"
            devices = 1
        frac = portion if portion is not None and portion < 1 else None
        return int(round(devices)), frac
    return None


def find_gpu(data):
    """(devices, fraction, path, kind, notes) from the describe JSON."""
    notes = []
    for path, kind in GPU_PATHS:
        v = get_path(data, path)
        if v is None:
            continue
        got = interpret_gpu(v)
        if got is None:
            continue
        if got == "zero":
            notes.append("{} reads 0 (released after the job ended?)".format(path))
            continue
        return got[0], got[1], path, kind, notes
    compute = data.get("compute") if isinstance(data, dict) else None
    seen = (" `compute` has keys: " + ", ".join(sorted(compute))) if isinstance(
        compute, dict) else " There is no `compute` object."
    raise LedgerError(
        "no GPU amount found. Looked for a number or {{devices, portion}} at: {}.{}{} "
        "Pass --gpus (and --fraction) to record it.".format(
            ", ".join(p for p, _ in GPU_PATHS), seen,
            (" " + "; ".join(notes) + ".") if notes else ""))


def _first(data, paths):
    for p in paths:
        v = get_path(data, p)
        if v is not None:
            return v, p
    return None, None


def merge_windows(windows):
    """Total seconds covered by a set of (start, end) windows, counting overlaps once."""
    total = 0.0
    cur_s = cur_e = None
    for s, e in sorted(windows):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        elif e > cur_e:
            cur_e = e
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return total


def runai_window(data, now=None):
    """The allocation window from describe JSON, as a dict; raises LedgerError on a shape it
    does not recognise."""
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise LedgerError("expected a JSON object from `runai training standard describe JOB "
                          "-p PROJECT -o json`")
    events, epath = _first(data, EVENT_PATHS)
    if not isinstance(events, list):
        raise LedgerError("expected an `events` list of {{reason, createdAt}} (looked at {}); "
                          "top-level keys are: {}. Is this `describe ... -o json` output?".format(
                              ", ".join(EVENT_PATHS), ", ".join(sorted(data)) or "none"))
    evs = []
    for e in events:
        if isinstance(e, dict) and e.get("reason") and e.get("createdAt"):
            evs.append((parse_iso(e["createdAt"], "event createdAt"), str(e["reason"])))
    if not evs:
        raise LedgerError("{} has {} entries but none with both `reason` and `createdAt`".format(
            epath, len(events)))
    evs.sort()
    warnings = []
    if len(events) == DEFAULT_EVENT_LIMIT:
        warnings.append("exactly {} events: describe may have cut the list; re-run it with "
                        "--event-limit -1".format(DEFAULT_EVENT_LIMIT))
    starts = [t for t, r in evs if r in START_REASONS]
    if not starts:
        raise LedgerError("no {} event among: {}. Was it ever scheduled? (or re-run describe "
                          "with --event-limit -1)".format(
                              " or ".join(START_REASONS),
                              ", ".join(sorted(set(r for _, r in evs)))))
    first_start = starts[0]

    def is_end(reason):
        return reason in END_REASONS or "delet" in reason.lower()

    ends = [(t, r) for t, r in evs if is_end(r)]
    phase, _ = _first(data, PHASE_PATHS)
    phase = phase if isinstance(phase, str) else None
    restarted_after_end = bool(ends) and any(t > ends[-1][0] for t in starts)
    if phase in RUNNING_PHASES or not ends or restarted_after_end:
        if phase in TERMINAL_PHASES and not ends:
            raise LedgerError("phase is {} but there is no {} or deletion event; re-run describe "
                              "with --event-limit -1, or record it with `add manual`".format(
                                  phase, "/".join(END_REASONS)))
        if now is None:
            raise LedgerError("the workload is still running (phase {}, no terminal event "
                              "after the last {}); pass --now ISO (or --now now) to record it "
                              "up to then".format(phase or "unknown", "/".join(START_REASONS)))
        end, end_how, running = now, "--now (still running)", True
    else:
        end, end_how, running = ends[-1][0], ends[-1][1], False
    if end <= first_start:
        raise LedgerError("the window ends ({}) before it starts ({})".format(iso(end),
                                                                            iso(first_start)))
    first_reason = next(r for t, r in evs if t == first_start and r in START_REASONS)
    res = {"start": first_start, "end": end, "running": running, "warnings": warnings,
           "workload_s": (end - first_start).total_seconds(),
           "method": "events {} -> {}".format(first_reason, end_how)}

    pods, ppath = _first(data, POD_PATHS)
    if isinstance(pods, list) and pods:
        if len(pods) == DEFAULT_POD_LIMIT:
            warnings.append("exactly {} pods: describe may have cut the list; re-run it with "
                            "--pod-limit -1".format(DEFAULT_POD_LIMIT))
        windows, keys = [], set()
        for p in pods:
            if not isinstance(p, dict):
                windows = None
                break
            sk = next((k for k in POD_START_KEYS if p.get(k)), None)
            ek = next((k for k in POD_END_KEYS if p.get(k)), None)
            if sk is None:
                windows = None
                break
            ps = parse_iso(p[sk], "pod " + sk)
            if ek is not None:
                pe = parse_iso(p[ek], "pod " + ek)
            elif running and str(p.get("phase", "")) in RUNNING_PHASES:
                pe, ek = now, "--now"
            else:
                windows = None
                break
            keys.add("{}/{}".format(sk, ek))
            windows.append((ps, max(pe, ps)))
        if windows:
            res["pod_s"] = merge_windows(windows)
            res["method"] = "union of {} pod window{} ({}, {})".format(
                len(windows), "" if len(windows) == 1 else "s", ppath, ", ".join(sorted(keys)))
            res["start"] = min(s for s, _ in windows)
            res["end"] = max(e for _, e in windows)
    res["seconds"] = res.get("pod_s", res["workload_s"])
    return res


def load_json(path):
    try:
        if path == "-":
            text = sys.stdin.read()
        else:
            with open(path, encoding="utf-8") as f:
                text = f.read()
    except OSError as e:
        raise LedgerError("cannot read {}: {}".format(path, e.strerror or e))
    try:
        return json.loads(text)
    except ValueError:
        raise LedgerError("{} is not JSON; run describe with -o json (the table view is not "
                          "enough: it shows GPU 1.00 even for a fraction)".format(path))


def cmd_add_runai(a):
    data = load_json(a.json)
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    now = parse_iso(a.now, "--now") if a.now else None
    win = runai_window(data, now)
    for w in win["warnings"]:
        print("warning: " + w)
    gpus, fraction = gpus_and_fraction(a.gpus, a.fraction)
    if gpus is not None:
        gpu_from = "--gpus"
    else:
        gpus, found_frac, path, kind, notes = find_gpu(data)
        for n in notes:
            print("note: " + n)
        gpu_from = path
        print("GPU amount: {} device{}{} from {} ({})".format(
            gpus, "" if gpus == 1 else "s",
            " at fraction {:g}".format(found_frac) if found_frac is not None else "",
            path, kind))
        if fraction is None:
            fraction = found_frac
    name, _ = _first(data, NAME_PATHS)
    if not name:
        name = os.path.splitext(os.path.basename(a.json))[0]
    hours = win["seconds"] / 3600.0
    print("window {} → {} ({}): {:.3f} h".format(iso(win["start"]), iso(win["end"]),
                                                  win["method"], hours))
    if "pod_s" in win:
        print("used the union of the pod windows; the workload window from the events was "
              "{:.3f} h".format(win["workload_s"] / 3600.0))
    if win["running"]:
        print("still running: recorded up to --now; record it again with --replace when it ends")
    row = make_row(win["start"], str(name), gpus, fraction, hours, a.purpose,
                   "runai: {}; gpu from {}".format(win["method"], gpu_from))
    rows = append_row(a.ledger, row, a.replace)
    report_row(a.ledger, row, rows)
    return 0


def cmd_add_manual(a):
    start = parse_iso(a.start, "--start")
    end = parse_iso(a.end, "--end")
    if end <= start:
        raise LedgerError("--end must be after --start")
    gpus, fraction = gpus_and_fraction(a.gpus, a.fraction)
    hours = (end - start).total_seconds() / 3600.0
    print("window {} → {}: {:.3f} h".format(iso(start), iso(end), hours))
    row = make_row(start, a.name, gpus, fraction, hours, a.purpose, "manual")
    rows = append_row(a.ledger, row, a.replace)
    report_row(a.ledger, row, rows)
    return 0


def cmd_show(a):
    if not os.path.exists(a.ledger):
        sys.stderr.write("gpu_ledger: no ledger at {} (nothing recorded yet)\n".format(a.ledger))
        return 1
    rows, _, _ = read_ledger(a.ledger)
    rows = with_totals(rows)
    cols = COLUMNS
    widths = [max([len(c)] + [len(str(r.get(c, ""))) for r in rows]) for c in cols]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip())
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)).rstrip())
    tf = rows[-1]["total_fraction"] if rows else fmt_h(0)
    tw = rows[-1]["total_whole"] if rows else fmt_h(0)
    print("total: {} GPU-h as fractions, {} GPU-h as whole GPUs, over {} row{} (both kept: "
          "whether a fraction is billed as a fraction is not confirmed)".format(
              tf, tw, len(rows), "" if len(rows) == 1 else "s"))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Estimate and record GPU-hours (see the header).")
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("estimate", help="wall hours and GPU-hours for a planned run")
    e.add_argument("--units", type=float, required=True, help="units (samples, sequences, "
                                                              "tokens...) per epoch")
    e.add_argument("--epochs", type=float, required=True)
    e.add_argument("--throughput", type=float, required=True,
                   help="units/s for the whole job, measured")
    e.add_argument("--gpus", type=float, default=1)
    e.add_argument("--fraction", type=float, help="share of each GPU, e.g. 0.5")
    e.add_argument("--val-frac", type=float, default=0.1,
                   help="validation time as a share of training time (default 0.1)")
    e.add_argument("--startup-min", type=float, default=5.0,
                   help="minutes from allocation to the first step (default 5)")
    e.set_defaults(func=cmd_estimate)

    add = sub.add_parser("add", help="record a finished (or running) workload")
    asub = add.add_subparsers(dest="source")

    def common(p):
        p.add_argument("--purpose", default="", help="what the run was for")
        p.add_argument("--ledger", default=DEFAULT_LEDGER,
                       help="CSV, or markdown if it ends in .md (default %(default)s)")
        p.add_argument("--replace", action="store_true",
                       help="replace the workload's existing row")

    r = asub.add_parser("runai", help="from `runai training standard describe JOB -o json`")
    r.add_argument("--json", required=True, help="the describe JSON file, or - for stdin")
    r.add_argument("--now", help="end of the window for a running job (ISO 8601, or 'now')")
    r.add_argument("--gpus", type=float, help="override the GPU amount found in the JSON")
    r.add_argument("--fraction", type=float, help="override the fraction found in the JSON")
    common(r)
    r.set_defaults(func=cmd_add_runai)

    m = asub.add_parser("manual", help="any other scheduler")
    m.add_argument("--name", required=True)
    m.add_argument("--start", required=True, help="ISO 8601, e.g. 2026-10-01T09:00:00Z")
    m.add_argument("--end", required=True)
    m.add_argument("--gpus", type=float, required=True)
    m.add_argument("--fraction", type=float)
    common(m)
    m.set_defaults(func=cmd_add_manual)

    s = sub.add_parser("show", help="rows and totals")
    s.add_argument("--ledger", default=DEFAULT_LEDGER)
    s.set_defaults(func=cmd_show)
    return ap, add


def main(argv=None):
    ap, add = build_parser()
    a = ap.parse_args(argv)
    if not getattr(a, "func", None):
        (add if a.cmd == "add" else ap).print_help(sys.stderr)
        return 2
    try:
        sys.stdout.reconfigure(errors="replace")   # a non-UTF-8 locale must not crash on "×"
    except (AttributeError, ValueError):
        pass
    try:
        return a.func(a)
    except LedgerError as e:
        sys.stderr.write("gpu_ledger: {}\n".format(e))
        return 2
    except Refused as e:
        sys.stderr.write("gpu_ledger: {}\n".format(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
