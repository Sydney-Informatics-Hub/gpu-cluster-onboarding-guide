#!/usr/bin/env python3
"""loader_bench.py — measure how fast the input pipeline can deliver, without holding a GPU.

Run it in a CPU-only job (free, or far cheaper than a GPU) with the same image, storage and CPU
request as training. If the loader delivers well above what training consumes (say 2x the units/s
in the training log), the loader is not the bottleneck and more workers will not help; if it
delivers about the same, it is.

  loader_bench.py --factory mypkg.data:make_train_loader
  loader_bench.py --factory data.py:make_loader --units data.py:count_tokens --batches 100
  loader_bench.py --factory mypkg.data:make_train_loader --check-modules torchvision,PIL --json
  loader_bench.py --path /shared/project/src --factory mypkg.data:make_train_loader

  --factory MOD:FN     FN() returns a DataLoader, or any iterable of batches. MOD is a module
                       name (importable from the current directory or --path) or a .py file.
  --units MOD:FN       FN(batch) -> the number of units (samples, sequences, tokens...) in a
                       batch. Default: the batch's first dimension (for a tuple, list or dict,
                       that of its first tensor), else len(batch).
  --batches N          timed batches (default 50), after --warmup N untimed ones (default 5,
                       which also absorbs worker start-up, reported separately).
  --check-modules a,b  report whether each module can be imported here.
  --path DIR           add DIR to the import path (repeatable).
  --json               print the result as one JSON object (the environment block still goes
                       to stderr first).

The environment is printed first, before the loader is even created, so a worker crash cannot
lose it: CPU count and affinity, the cgroup CPU limit (a pod with no CPU limit can show 224 CPUs
while 8 were requested), torch threads, OMP/MKL/OpenBLAS thread variables, /dev/shm size
(DataLoader workers pass batches through it), and --check-modules.

Needs PyTorch (imported lazily). Exit status: 0 on success; 1 when the loader yields no batches
(for example a bucketing sampler that yields nothing) or none are left after warm-up; 2 when
PyTorch is missing, the factory cannot be loaded, or the arguments are wrong.
"""
import argparse
import importlib
import importlib.util
import json
import math
import os
import sys
import time


class BenchError(Exception):
    pass


def _torch():
    try:
        import torch
    except ImportError as e:
        raise BenchError("loader_bench.py needs PyTorch ({}). Run it with the Python of the "
                         "training environment.".format(e))
    return torch


def load_callable(spec):
    """MOD:FN, where MOD is a module name or a path to a .py file; FN may be dotted."""
    if ":" not in spec:
        raise BenchError("expected MODULE:FUNCTION, got {!r}".format(spec))
    mod_name, fn_name = spec.rsplit(":", 1)
    try:
        if mod_name.endswith(".py"):
            name = "loader_bench_user_" + os.path.splitext(os.path.basename(mod_name))[0]
            s = importlib.util.spec_from_file_location(name, mod_name)
            if s is None or s.loader is None:
                raise BenchError("cannot load {}".format(mod_name))
            mod = importlib.util.module_from_spec(s)
            sys.modules[name] = mod     # DataLoader workers that spawn must find it again
            s.loader.exec_module(mod)
        else:
            mod = importlib.import_module(mod_name)
    except BenchError:
        raise
    except Exception as e:
        raise BenchError("cannot import {}: {}: {}".format(mod_name, type(e).__name__, e))
    obj = mod
    for part in fn_name.split("."):
        if not hasattr(obj, part):
            raise BenchError("{} has no attribute {!r}".format(mod_name, fn_name))
        obj = getattr(obj, part)
    if not callable(obj):
        raise BenchError("{} is not callable".format(spec))
    return obj


def _first_dim(x):
    shape = getattr(x, "shape", None)
    if shape is not None and len(shape) > 0:
        return int(shape[0])
    return None


def default_units(batch):
    """The batch's first dimension; for a tuple, list or dict, that of its first tensor-like
    element; else len(batch); else 1."""
    n = _first_dim(batch)
    if n is not None:
        return n
    items = list(batch.values()) if isinstance(batch, dict) else (
        list(batch) if isinstance(batch, (list, tuple)) else [])
    for item in items:
        n = _first_dim(item)
        if n is not None:
            return n
    try:
        return len(batch)
    except TypeError:
        return 1


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def cgroup_cpu_limit():
    """(text, cores or None) from cgroup v2 cpu.max or v1 cfs quota/period; (None, None) if
    neither exists."""
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2:
        parts = v2.split()
        if parts[0] == "max":
            return "cpu.max {} (no limit)".format(v2), None
        try:
            return "cpu.max {}".format(v2), float(parts[0]) / float(parts[1])
        except (ValueError, IndexError):
            return "cpu.max {}".format(v2), None
    for d in ("/sys/fs/cgroup/cpu", "/sys/fs/cgroup/cpu,cpuacct"):
        quota, period = _read(d + "/cpu.cfs_quota_us"), _read(d + "/cpu.cfs_period_us")
        if quota and period:
            try:
                q, p = float(quota), float(period)
            except ValueError:
                continue
            if q < 0:
                return "cfs_quota_us {} (no limit)".format(quota), None
            return "cfs_quota_us {} / cfs_period_us {}".format(quota, period), q / p
    return None, None


def environment(check_modules=()):
    """Facts about the CPU side that bound loader throughput, as a dict."""
    env = {"python": sys.version.split()[0], "cpu_count": os.cpu_count()}
    if hasattr(os, "sched_getaffinity"):
        try:
            env["affinity_cpus"] = len(os.sched_getaffinity(0))
        except OSError:
            env["affinity_cpus"] = None
    text, cores = cgroup_cpu_limit()
    env["cgroup_cpu"] = text
    env["cgroup_cpu_cores"] = cores
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        env[var] = os.environ.get(var)
    try:
        st = os.statvfs("/dev/shm")
        env["dev_shm_total_bytes"] = st.f_frsize * st.f_blocks
        env["dev_shm_free_bytes"] = st.f_frsize * st.f_bavail
    except (OSError, AttributeError):
        env["dev_shm_total_bytes"] = env["dev_shm_free_bytes"] = None
    try:
        import torch
        env["torch"] = torch.__version__
        env["torch_threads"] = torch.get_num_threads()
        env["torch_interop_threads"] = torch.get_num_interop_threads()
    except ImportError:
        env["torch"] = None
    mods = {}
    for m in check_modules:
        try:
            mods[m] = importlib.util.find_spec(m) is not None
        except (ImportError, ValueError):
            mods[m] = False
    env["modules"] = mods
    return env


def _gib(b):
    return "{:.1f} GiB".format(b / 2.0 ** 30)


def format_environment(env):
    lines = ["environment (printed before the loader starts):",
             "  python {}  torch {}".format(env["python"], env.get("torch") or "missing"),
             "  os.cpu_count() {}  (nproc-like count; may be every core on the node)".format(
                 env["cpu_count"])]
    if "affinity_cpus" in env:
        lines.append("  sched_getaffinity {}  (CPUs this process may run on)".format(
            env["affinity_cpus"]))
    if env["cgroup_cpu"]:
        lines.append("  cgroup {}{}".format(env["cgroup_cpu"], "  = {:g} cores".format(
            env["cgroup_cpu_cores"]) if env["cgroup_cpu_cores"] else ""))
    else:
        lines.append("  cgroup CPU limit: none found")
    if env.get("torch_threads") is not None:
        lines.append("  torch threads {}  interop threads {}".format(
            env["torch_threads"], env["torch_interop_threads"]))
    lines.append("  " + "  ".join("{}={}".format(k, env[k] if env[k] is not None else "unset")
                                  for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
                                            "OPENBLAS_NUM_THREADS")))
    if env["dev_shm_total_bytes"] is not None:
        small = env["dev_shm_total_bytes"] < 2 ** 30
        lines.append("  /dev/shm {} total, {} free{}".format(
            _gib(env["dev_shm_total_bytes"]), _gib(env["dev_shm_free_bytes"]),
            "  (small: DataLoader workers may crash; request a larger shm)" if small else ""))
    else:
        lines.append("  /dev/shm: absent")
    for m, ok in env["modules"].items():
        lines.append("  module {}: {}".format(m, "present" if ok else "MISSING"))
    return "\n".join(lines)


def _p90(xs):
    s = sorted(xs)
    return s[max(0, int(math.ceil(0.9 * len(s))) - 1)]


def bench(loader, units_fn=None, batches=50, warmup=5):
    """Time `batches` batches after `warmup`; returns a dict of rates and times."""
    units_fn = units_fn or default_units
    t_start = time.perf_counter()
    it = iter(loader)
    first_s = None
    seen = 0
    times, units = [], 0.0
    try:
        for _ in range(warmup):
            next(it)
            seen += 1
            if first_s is None:
                first_s = time.perf_counter() - t_start
        t_prev = time.perf_counter()
        for _ in range(batches):
            b = next(it)
            now = time.perf_counter()
            times.append(now - t_prev)
            units += units_fn(b)
            seen += 1
            if first_s is None:
                first_s = now - t_start
            t_prev = time.perf_counter()     # units_fn time is not loader time
    except StopIteration:
        pass
    finally:
        del it     # shut down DataLoader workers
    total = sum(times)
    res = {"batches_seen": seen, "timed_batches": len(times), "warmup": warmup,
           "first_batch_s": first_s, "units": units,
           "units_per_s": units / total if total > 0 else None,
           "batches_per_s": len(times) / total if total > 0 else None,
           "s_per_batch_mean": total / len(times) if times else None,
           "s_per_batch_p90": _p90(times) if times else None,
           "exhausted": len(times) < batches}
    return res


def format_result(r):
    if not r["timed_batches"]:
        return ("the loader yielded {} batch{} in total, none to time: it is exhausted after "
                "warm-up or empty".format(r["batches_seen"], "" if r["batches_seen"] == 1
                                          else "es"))
    lines = ["timing ({} batches after {} warm-up{}):".format(
        r["timed_batches"], r["warmup"], "; the loader ran out early" if r["exhausted"] else ""),
        "  units/s {:.4g}   batches/s {:.4g}".format(r["units_per_s"], r["batches_per_s"]),
        "  s/batch mean {:.3g}  p90 {:.3g}".format(r["s_per_batch_mean"], r["s_per_batch_p90"])]
    if r["first_batch_s"] is not None:
        lines.append("  first batch after {:.2f} s (worker start-up included)".format(
            r["first_batch_s"]))
    lines.append("Compare units/s with what training consumes (its log's units/s): well above "
                 "it (2x or more) means the loader is not the limit.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Input-pipeline throughput on CPU (see the header).")
    ap.add_argument("--factory", required=True, help="MODULE:FUNCTION returning the loader")
    ap.add_argument("--units", help="MODULE:FUNCTION(batch) -> units in the batch")
    ap.add_argument("--batches", type=int, default=50)
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--check-modules", default="", help="comma-separated module names")
    ap.add_argument("--path", action="append", default=[], help="add to the import path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.batches < 1 or a.warmup < 0:
        ap.error("--batches must be >= 1 and --warmup >= 0")
    for p in reversed(a.path):
        sys.path.insert(0, os.path.abspath(p))
    if "" not in sys.path and os.getcwd() not in sys.path:
        sys.path.insert(0, os.getcwd())
    mods = [m.strip() for m in a.check_modules.split(",") if m.strip()]

    env = environment(mods)
    # Before anything that can crash: printed and flushed. With --json it goes to stderr, so
    # stdout stays one JSON object.
    stream = sys.stderr if a.json else sys.stdout
    stream.write(format_environment(env) + "\n")
    stream.flush()
    try:
        _torch()
        factory = load_callable(a.factory)
        units_fn = load_callable(a.units) if a.units else None
    except BenchError as e:
        sys.stderr.write("loader_bench: {}\n".format(e))
        return 2
    loader = factory()
    r = bench(loader, units_fn, a.batches, a.warmup)
    if a.json:
        print(json.dumps({"environment": env, "result": r}, indent=2))
    else:
        print(format_result(r))
    if r["batches_seen"] == 0:
        sys.stderr.write("loader_bench: the loader yielded zero batches (check the sampler: a "
                         "bucketing sampler can silently yield nothing)\n")
        return 1
    if r["timed_batches"] == 0:
        sys.stderr.write("loader_bench: nothing left to time after warm-up; lower --warmup\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
