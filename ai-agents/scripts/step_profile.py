#!/usr/bin/env python3
"""step_profile.py — where a training step's time goes, phase by phase, and how many ops each runs.

Guessing the bottleneck is expensive: augmentation and then the data loader were both blamed
and both cleared once measured. This times each phase of a step (data, forward, backward,
optimizer step, or whatever you name), synchronising CUDA around each so the time lands in the
phase that caused it, and counts the top-level `aten::` ops each phase runs per step. Op counts
that are the same every run but much larger than the model suggests point at per-step host work
(many small ops, Python loops, host-device syncs); steady counts with slow, variable steps point
at what is in the batch rather than how it is computed.

  step_profile.py --demo                    # a toy model on CPU (or CUDA if present)
  step_profile.py --demo --steps 50 --json

As a library, with your own step split into zero-argument callables that share state:

  from step_profile import profile_phases
  state = {}
  def data():     state["x"], state["y"] = next(it)
  def forward():  state["loss"] = loss_fn(model(state["x"].cuda()), state["y"].cuda())
  def backward(): state["loss"].backward()
  def step():     opt.step(); opt.zero_grad(set_to_none=True)
  res = profile_phases({"data": data, "forward": forward, "backward": backward,
                        "step": step}, steps=20, warmup=3)
  print(format_table(res))

Top-level means an `aten::` op with no other `aten::` op in its parent chain: counting nested ops
too would include, for example, a CPU convolution fallback that cuDNN never runs. Synchronising
around each phase removes the overlap a real step has, so the phase times add up to a little
more than an unprofiled step.

Needs PyTorch (imported only when profiling). Exit status: 0 on success; 2 when PyTorch is
missing or the arguments are wrong.
"""
import argparse
import collections
import json
import sys
import time

RANGE_PREFIX = "step_profile/"   # record_function label per phase


class TorchMissing(Exception):
    pass


def _torch():
    try:
        import torch
    except ImportError as e:
        raise TorchMissing("step_profile.py needs PyTorch ({}). Run it with the Python of the "
                           "training environment.".format(e))
    return torch


def _device(torch, device):
    if device is None:
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(device)


def _aten_parent(ev):
    p = ev.cpu_parent
    while p is not None:
        if p.name.startswith("aten::"):
            return True
        p = p.cpu_parent
    return False


def _phase_of(ev, labels, ranges):
    """The phase an op ran in: its record_function ancestor, else (for ops on another thread,
    such as the CUDA autograd thread) the phase whose time range contains its start."""
    p = ev.cpu_parent
    while p is not None:
        if p.name in labels:
            return labels[p.name]
        p = p.cpu_parent
    t = ev.time_range.start
    for start, end, phase in ranges:
        if start <= t <= end:
            return phase
    return None


def profile_phases(phases, steps=20, warmup=3, device=None, profile_steps=None):
    """Mean seconds and top-level aten op counts per step for each phase.

    phases: ordered mapping name -> zero-argument callable, run in order as one step.
    steps: timed steps; warmup: untimed steps first (allocator, cuDNN autotune, optimizer
    state); device: where to synchronise (default: CUDA if available); profile_steps: steps
    under the profiler for the op counts (default min(steps, 5)).

    Returns {"device", "steps", "warmup", "profiled_steps", "step_mean_s", "phases": OrderedDict
    name -> {"mean_s", "share", "top_level_ops_per_step", "ops": {op: count per step}}}.
    """
    torch = _torch()
    from torch.profiler import ProfilerActivity, profile, record_function
    if not phases:
        raise ValueError("phases is empty")
    if steps < 1 or warmup < 0:
        raise ValueError("steps must be >= 1 and warmup >= 0")
    names = list(phases)
    dev = _device(torch, device)
    if dev.type == "cuda":
        def sync():
            torch.cuda.synchronize(dev)
    else:
        def sync():
            return None

    for _ in range(warmup):
        for n in names:
            phases[n]()
    sync()

    totals = collections.OrderedDict((n, 0.0) for n in names)
    for _ in range(steps):
        for n in names:
            sync()
            t0 = time.perf_counter()
            phases[n]()
            sync()
            totals[n] += time.perf_counter() - t0

    k = profile_steps if profile_steps else min(steps, 5)
    labels = {RANGE_PREFIX + n: n for n in names}
    with profile(activities=[ProfilerActivity.CPU]) as prof:
        for _ in range(k):
            for n in names:
                with record_function(RANGE_PREFIX + n):
                    phases[n]()
                    sync()
    events = prof.events()
    ranges = [(e.time_range.start, e.time_range.end, labels[e.name])
              for e in events if e.name in labels]
    counts = collections.OrderedDict((n, collections.Counter()) for n in names)
    for e in events:
        if not e.name.startswith("aten::") or _aten_parent(e):
            continue
        phase = _phase_of(e, labels, ranges)
        if phase is not None:
            counts[phase][e.name] += 1

    step_s = sum(totals.values()) / steps
    out = collections.OrderedDict()
    for n in names:
        mean_s = totals[n] / steps
        ops = collections.OrderedDict(
            (op, c / float(k)) for op, c in sorted(counts[n].items(), key=lambda kv: (-kv[1],
                                                                                     kv[0])))
        out[n] = {"mean_s": mean_s, "share": mean_s / step_s if step_s > 0 else 0.0,
                  "top_level_ops_per_step": sum(counts[n].values()) / float(k), "ops": ops}
    return {"device": str(dev), "steps": steps, "warmup": warmup, "profiled_steps": k,
            "step_mean_s": step_s, "phases": out}


def _count(x):
    return "{:.0f}".format(x) if x == int(x) else "{:.2f}".format(x)


def format_table(res, top=4):
    """A plain-text table of profile_phases' result."""
    lines = ["device {}; {} timed steps after {} warm-up; op counts from {} profiled step{}".format(
        res["device"], res["steps"], res["warmup"], res["profiled_steps"],
        "" if res["profiled_steps"] == 1 else "s"),
        "{:<12} {:>10} {:>7} {:>9}  {}".format("phase", "mean ms", "share", "aten ops",
                                                "most frequent top-level ops per step")]
    total_ops = 0.0
    for name, p in res["phases"].items():
        total_ops += p["top_level_ops_per_step"]
        frequent = ", ".join("{} {}".format(op, _count(c))
                             for op, c in list(p["ops"].items())[:top])
        lines.append("{:<12} {:>10.3f} {:>6.1f}% {:>9}  {}".format(
            name, p["mean_s"] * 1000, 100 * p["share"], _count(p["top_level_ops_per_step"]),
            frequent))
    lines.append("{:<12} {:>10.3f} {:>6.1f}% {:>9}".format(
        "step", res["step_mean_s"] * 1000, 100.0, _count(total_ops)))
    return "\n".join(lines)


def demo_phases(torch, device, batch=64, features=32, classes=4, seed=0):
    """A toy step (data -> forward -> backward -> optimizer step) as profile_phases input."""
    torch.manual_seed(seed)
    model = torch.nn.Sequential(torch.nn.Linear(features, 128), torch.nn.ReLU(),
                                torch.nn.Linear(128, classes)).to(device)
    opt = torch.optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    state = {}

    def data():
        state["x"] = torch.randn(batch, features).to(device)
        state["y"] = torch.randint(0, classes, (batch,)).to(device)

    def forward():
        state["loss"] = torch.nn.functional.cross_entropy(model(state["x"]), state["y"])

    def backward():
        state["loss"].backward()

    def step():
        opt.step()
        opt.zero_grad(set_to_none=True)

    return collections.OrderedDict([("data", data), ("forward", forward),
                                    ("backward", backward), ("optimizer", step)])


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Per-phase step time and top-level aten op counts (see the header; use "
                    "profile_phases() from Python for your own step).")
    ap.add_argument("--demo", action="store_true", help="profile a toy model")
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--profile-steps", type=int, default=None)
    ap.add_argument("--device", default=None, help="cpu or cuda (default: cuda if available)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.demo:
        ap.print_help(sys.stderr)
        sys.stderr.write("\nstep_profile.py: pass --demo, or import profile_phases from Python "
                         "to profile your own step.\n")
        return 2
    if a.steps < 1 or a.warmup < 0 or (a.profile_steps is not None and a.profile_steps < 1):
        ap.error("--steps and --profile-steps must be >= 1, --warmup >= 0")
    try:
        torch = _torch()
    except TorchMissing as e:
        sys.stderr.write("{}\n".format(e))
        return 2
    dev = _device(torch, a.device)
    res = profile_phases(demo_phases(torch, dev), steps=a.steps, warmup=a.warmup, device=dev,
                         profile_steps=a.profile_steps)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print(format_table(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
