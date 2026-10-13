# GPU efficiency guide — getting the most out of each GPU-hour

**What this is.** A procedure for an AI coding agent, or a researcher, about to spend GPU time
on a shared, fee-for-service cluster. It covers when to spend, how to check a running job, how to
find the real bottleneck, and how to guard training code against silent waste. It is written to
be executed: every step says what it costs and what success looks like.

**Scope.** Cluster-agnostic and PyTorch-first. Scheduler-specific commands for Run:ai, Slurm and
plain Kubernetes are in §10. For the University of Sydney's Apollo cluster,
[`APOLLO_AGENT_GUIDE.md`](APOLLO_AGENT_GUIDE.md) §11 applies this guide with Apollo's commands.

**Evidence.** The rules come from real sessions on a Run:ai cluster with H200 GPUs
(September–October 2026). Each claim is tagged:

| tag | meaning |
|---|---|
| *verified* | measured or observed in those sessions |
| *observed once* | seen once, cause not established |
| *vendor docs* | stated by NVIDIA, PyTorch or scheduler documentation, not re-measured here |
| *unverified* | inferred; treat as a hypothesis |

**Scripts.** `scripts/…` paths refer to `ai-agents/scripts/` in the
[onboarding-guide repository](https://github.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide),
or to `scripts/` in the installed Claude skill. All are read-only towards jobs, need only the
Python standard library (except the two profilers, which need torch), and have a usage header.

## Contents

1. [Principles](#1-principles)
2. [Free gates before any GPU job](#2-free-gates-before-any-gpu-job)
3. [Estimate the cost](#3-estimate-the-cost)
4. [Watch the first ten minutes](#4-watch-the-first-ten-minutes)
5. [Diagnose by measuring](#5-diagnose-by-measuring)
6. [Fix catalogue](#6-fix-catalogue)
7. [Guards in training code](#7-guards-in-training-code)
8. [Accounting](#8-accounting)
9. [Spending strategy](#9-spending-strategy)
10. [Scheduler notes](#10-scheduler-notes)
11. [Report-back format](#11-report-back-format)

---

## 1. Principles

- **GPU time is money.** On a fee-for-service cluster an allocated GPU is billed whether it
  computes or not: while the image pulls, while the pod is stuck starting, while a notebook sits
  idle.
- **Most waste is idle or held GPUs, not slow kernels.** Nearly all the GPU time lost in the
  sessions behind this guide went to things a checklist catches (*verified*):
  - a crash loop the scheduler restarted five times, holding the GPU throughout;
  - a pod stuck in `ContainerCreating` for 11 minutes with its GPU allocated;
  - 12 minutes of skipped non-finite-loss batches, with almost no progress;
  - a sampler that yielded zero batches per epoch, silently;
  - a job at 35–51 % utilisation, waiting on one host core.
- **Measure before fixing.** Two plausible bottleneck guesses were both wrong (§5.5). The fix that
  helped came from a measurement.
- **An unspent budget is not a reason to run.** Run because the experiment answers a question.
- **Decide the stop rule, the evaluation and what you keep before the run,** and never tune on
  the evaluation set. A run without a stop rule ends when the budget does.
- **For agents:** every GPU request is the user's decision. State GPUs, hours and GPU-hours when
  asking, and report what was actually used.

## 2. Free gates before any GPU job

Each gate costs nothing or almost nothing and catches a failure that would otherwise be paid for
in GPU-hours. Run them in this order; stop at the first that fails.

| # | gate | how | catches | cost |
|---|---|---|---|---|
| 1 | the code runs on real data | a short dry run (tens to hundreds of steps) in a **CPU-only** job | data paths, shapes, crashes, host-side memory | free |
| 2 | it resumes exactly | §7.8: a tiny model on CPU, uninterrupted vs. SIGTERM'd and resumed | unsaved state, RNG order, signal handling | free |
| 3 | every epoch yields batches | print batches per epoch, units per batch and the padding ratio from the dry run | samplers that yield nothing; padding waste | free |
| 4 | the input pipeline can keep up | `scripts/loader_bench.py` in a CPU-only job (§5.3) | an input-bound job before it starts | free |
| 5 | the GPU code path works | a few steps on the **smallest GPU fraction** that fits | device placement, dtype, CUDA/library mismatches | a few GPU-minutes |
| 6 | throughput, for the estimate | ≤ 15 minutes on one whole GPU, input cache warm, steady-state units/s after warm-up | the number §3 needs | ~0.25 GPU-h |

Gate 1 and gate 2 each caught real bugs before a long run (*verified*). Gate 3 exists because a
bucketing sampler with large batch sizes and `drop_last` yielded **zero** batches per epoch
without any error; the run would have "trained" on nothing (*verified*).

**The padding ratio** is padded elements ÷ real elements per batch. Near 1.0 is good; 1.5 means a
third of the compute pads. Merging very short and very long sequences once padded batches to
**16×** their content and ended in non-finite losses and a crash (*verified*).

**A fraction is for correctness, not speed.** A fractional GPU is time-shared, so its timings are
not throughput figures (*verified*). Measure throughput on a whole GPU.

## 3. Estimate the cost

```
wall_hours = units_per_epoch × epochs ÷ throughput_units_per_s ÷ 3600 × (1 + validation_share)
             + start_up_hours
GPU_hours  = wall_hours × GPUs            (× fraction, if a fraction is billed as one)
```

`scripts/gpu_ledger.py estimate --units U --epochs E --throughput T [--gpus G] [--fraction F]
[--val-frac 0.1] [--startup-min 5]` does the arithmetic and prints the assumptions.

- **Throughput comes from gate 6,** measured on the hardware, data and batch size of the real
  run — never from a paper or a laptop.
- **More GPUs do not scale linearly.** Measure with the planned GPU count, or assume at most
  0.9 × per GPU and say so.
- **Add start-up:** image pulls, data staging, compilation. Minutes per job, more on a cold node.
- **State it when asking:** "1 GPU × ~6 h ≈ 6 GPU-h; throughput 2,400 units/s measured in a
  12-minute check; stop rule: no dev improvement in 3 evaluations."

## 4. Watch the first ten minutes

Problems show early. A job that is idle, starved or looping in minute 5 will still be wasting
money in hour 5. Check every long job about 5 minutes in, and again at about 10.

```bash
scripts/gpu_watch.sh -n 12 -i 5 [--train-log <path to the job's JSONL log>] -- <exec prefix>
```

It samples `nvidia-smi` (utilisation, memory, power) twelve times over a minute, takes one `top`
snapshot, and reads the tail of the training log if given. It runs everything through the
**exec prefix** (empty for local or a Jupyter terminal; §10 lists the prefixes) and prints a
verdict. It never changes the job.

| verdict | signal (starting thresholds; constants at the top of `gpu_watch_verdict.py`) | action |
|---|---|---|
| `stuck-starting` | no samples: the pod is not running, or no GPU is visible | check the job's events; a pod stuck starting holds its GPU — report the node, offer to resubmit excluding it |
| `restart-loop` | the job has restarted (`--restarts N` from the scheduler's record) | stop it; read the job's own log on shared storage; make the container exit 0 after logging its code (§7.7) |
| `nonfinite-streak` | the training log shows ≥ 5 consecutive non-finite losses | stop it; fix the numerics before spending more (§7.1) |
| `idle-held` | utilisation ≤ 2 % in every sample | nothing is using the GPU: suspend or stop the workload |
| `input-bound` | median utilisation < 60 %, and a data-wait share > 20 % or several loader workers near 100 % CPU | the input is the limit: workers, storage, precompute (§6) |
| `host-bound` | median utilisation < 60 %, the busiest process ≥ 95 % of one core, data wait low | the Python host side is the limit: syncs, small kernels, batch size, compile (§6) |
| `bursty` | utilisation swings between idle and full, data wait < 5 % | batch composition or per-step host work: profile the step (§5) |
| `gpu-bound` | median utilisation ≥ 85 % | the GPU is busy: tune batch size and precision, then leave it |
| `unclear` | none of the above | measure: `step_profile.py` and `loader_bench.py` (§5) |
| `memory-headroom` (advisory) | peak memory < 25 % of the GPU's | a smaller GPU fraction, or a bigger batch |

The verdicts are checked in that order, and the first match wins. The thresholds are starting
heuristics, not laws: the evidence printed with the verdict matters more than the label.

**Watch whole GPUs, not fractions.** On a fractional GPU, the utilisation `nvidia-smi` reports
probably covers every workload sharing that GPU (*unverified*), so a verdict there says little
about your job. MIG instances report utilisation as `[N/A]`, which yields `unclear`.

**The reference case** is a 1-GPU job measured at 35–51 % utilisation, ~200 W of a 700 W card,
17 of 141 GB, with the main Python process at 100 % of one core (*verified*). That is `host-bound`
with `memory-headroom`: a top-end GPU paid for to wait on the host.

**Without the script:** run `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,power.draw --format=csv`
a few times, a few seconds apart, plus `top -bn1`, through the same prefix, and read them against
the table.

## 5. Diagnose by measuring

Fix nothing until a measurement says what to fix. Four measurements cover most cases.

### 5.1 Data-wait share — in the training log

The fraction of each logging window spent waiting for the next batch. It separates input-bound
(high) from host- or GPU-bound (low). Log it next to throughput:

```python
import json, time, torch

def train_epoch(loader, train_step, log_file, device, log_every=50):
    it = iter(loader)
    wait, t0, step = 0.0, time.perf_counter(), 0
    while True:
        t = time.perf_counter()
        try:
            batch = next(it)
        except StopIteration:
            break
        wait += time.perf_counter() - t
        loss, units = train_step(batch)                 # units: samples, tokens... in this batch
        step += 1
        if step % log_every == 0:
            if device.type == "cuda":
                torch.cuda.synchronize()                # once per window, so wall time is honest
            window = time.perf_counter() - t0
            log_file.write(json.dumps({"step": step, "loss": float(loss),
                                       "data_wait_share": round(wait / window, 4)}) + "\n")
            log_file.flush()
            wait, t0 = 0.0, time.perf_counter()
```

`gpu_watch.sh --train-log` reads `loss` and `data_wait_share` from exactly this kind of JSONL. Low
utilisation with near-zero data wait points at the training step, not the input (*verified*).

### 5.2 Per-phase step time and op counts — `step_profile.py`

`scripts/step_profile.py` times the phases of one step: input transfer, forward, loss, backward,
optimiser. For each phase it also counts the **top-level** `aten::` operations. Nested ops are
excluded: counting them would include, for example, a CPU convolution fallback that cuDNN never
runs.

```python
from step_profile import profile_phases
res = profile_phases({"to_device": lambda: ..., "forward": lambda: ..., "backward": lambda: ...},
                     steps=20, warmup=3)
```

`step_profile.py --demo` shows the output on a toy model. How to read it:
- **Many small ops with little time each** points at host-bound dispatch. Fuse the ops, compile
  the step, or use bigger batches.
- **A phase much slower than its op count explains** usually hides a host-device sync (§6).
- **The same phase dominating on CPU and GPU** is real compute, so look at batch size and
  precision.

### 5.3 Input capacity — `loader_bench.py` in a free CPU job

```bash
scripts/loader_bench.py --factory mypkg.data:make_loader --units mypkg.data:batch_units --batches 50
```

It builds your DataLoader and times batches after a warm-up. It reports units/s, seconds per
batch, and the environment that decides loader speed: cores visible vs. allowed, the cgroup CPU
quota, torch threads, the `*_NUM_THREADS` variables and the `/dev/shm` size. The environment is
printed **before** the loader starts, so a worker crash cannot lose it. Compare the result with
what the GPU will consume (gate 6). A loader below about 1.5× the consumption rate makes the job
input-bound.

### 5.4 Batch composition

Log units per batch and the padding ratio per batch. If slow windows **repeat at the same steps
across runs with the same seed**, the cause is in what those batches contain, not in the hardware
(*verified*).

### 5.5 Worked example: two wrong guesses

A sequence model trained at ~45 % GPU utilisation (*verified*; details anonymised):
1. **Guess 1: augmentation is slow.** `step_profile.py` put it under 5 % of the step. Wrong.
2. **Guess 2: the data loader.** `loader_bench.py` on CPU delivered ~2.3× what training consumed,
   and the training log showed a data-wait share of 1–5 %. Wrong.
3. **What measurement found:**
   - top-level op counts showed a fixed per-step host cost: hundreds of small ops plus hidden
     syncs;
   - the slow windows recurred at the same steps across runs with the same seed, so batch
     composition mattered: mixed lengths padded heavily.
4. **What did not help:** an allocator change (`expandable_segments`). It stopped memory growth
   but did not change speed.

The lesson is the order: measure, then fix the measured thing.

## 6. Fix catalogue

Each entry says when it helps, when it doesn't, and the evidence. Apply the one the measurement
points at; re-measure after each.

| fix | helps when | doesn't help when | evidence |
|---|---|---|---|
| **Bigger batches** (scale the LR accordingly) | host-bound: fixed per-step cost amortised; memory headroom available | already GPU-bound; convergence is sensitive to batch size | *verified* (per-step host cost) |
| **Length-aware batching** (bucket by size) | padding ratio high | lengths are uniform | *verified* (16× padding). The bucket buffer must be able to fill a bucket, or `drop_last` yields **zero** batches: check batches per epoch |
| **Vectorise per-sample Python loops** (augmentation, masking, collation) | `step_profile.py` shows Python time per sample | the loop is already a tensor op | *verified* |
| **Remove hidden host-device syncs**: `.item()`, `float(t)`, `.cpu()`, `.tolist()`, `lengths.max()` used for a shape, `isfinite` checks every step, printing tensors | host-bound, bursty | GPU-bound | *verified*. Accumulate on the GPU; read values once per logging window |
| **Cache module walks** (e.g. `model.modules()` every step to set flags) | many small Python ops per step | — | *verified* |
| **Pinned memory and `non_blocking=True` copies** | input transfer visible in the profile | data already on the GPU | *vendor docs* |
| **Thread limits** = the cores you requested (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `torch.set_num_threads`) | always, on shared nodes. Pods may have **no CPU limit**: `nproc` showed 224 in a pod that requested 8, and torch started 112 threads | — | *verified*. A single-process CPU stage ran ~4× faster with one thread than with the default |
| **More DataLoader workers**, `persistent_workers=True`, enough shared memory | input-bound | host-bound; workers then just compete for cores | *verified*. Workers die with a "bus error … insufficient shared memory" when `/dev/shm` is small |
| **Mixed precision** (bf16 autocast on recent GPUs) | GPU-bound, matmul/conv heavy | host-bound | *vendor docs* |
| **`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`** | memory growth or fragmentation | speed | *verified* in one test: memory, not speed |
| **`torch.compile` / CUDA graphs** | host-bound with many small kernels and stable shapes | shapes vary (recompiles); short jobs (compile time) | *unverified* here |
| **Precompute inputs** (decode, resize, tokenise once; store in a fast format) | input-bound and the transform is deterministic | the transform is random augmentation | *vendor docs* / common practice |
| **Right-size the GPU** | `memory-headroom`; the model is small | — | *verified* (17 of 141 GB) |
| **Evaluate and checkpoint less often** | evaluation or checkpoint writes show up as idle gaps | — | *unverified* |
| **More GPUs** | only after one GPU is efficient, and scaling is measured | the job cannot feed one GPU | — |

## 7. Guards in training code

These snippets are PyTorch; the ideas carry to any framework. They cost almost nothing and each
one would have saved real GPU time.

### 7.1 Stop on a non-finite streak

Skipping non-finite batches looks safe and is not. A job skipped them for 12 minutes and made
almost no progress (*verified*).

```python
class NonFiniteGuard:
    """Raise after `limit` consecutive non-finite losses instead of skipping them for ever."""
    def __init__(self, limit=5):
        self.limit, self.streak = limit, 0

    def check(self, loss):
        if torch.isfinite(loss):       # a host-device sync: for very short steps, count on the
            self.streak = 0            # GPU and check once per logging window instead
            return True
        self.streak += 1
        if self.streak >= self.limit:
            raise RuntimeError(f"{self.streak} non-finite losses in a row; stopping")
        return False                   # caller skips this batch
```

### 7.2 Refuse an epoch with no batches

```python
n = 0
for batch in loader:
    n += 1
    ...
if n == 0 and not resumed_mid_epoch:   # the tail of a resumed epoch may legitimately be empty
    raise RuntimeError("epoch yielded no batches: check sampler bucket size, batch size, drop_last")
```

### 7.3 Never pad very different lengths together

When a run mixes sources of very different sequence length, keep them in separate streams and
alternate batches between them. Don't merge them into one pool that pads short items to the
length of long ones (*verified*: 16× padding and a crash).

### 7.4 Time the data wait

See §5.1. The log line costs one sync per window.

### 7.5 Checkpoint atomically; resume from the newest that loads

```python
import os, torch
from pathlib import Path

def save_checkpoint(state, path: Path):
    tmp = path.with_name(path.name + ".tmp")
    torch.save(state, tmp)
    os.replace(tmp, path)              # atomic within one filesystem: never a half-written file

def load_latest(ckpt_dir: Path):
    for p in sorted(ckpt_dir.glob("step_*.pt"), key=lambda p: int(p.stem.split("_")[1]),
                    reverse=True):
        try:
            return torch.load(p, map_location="cpu", weights_only=False), p   # your own files only
        except Exception as e:         # truncated by a kill mid-write, or corrupt
            print(f"skipping unreadable checkpoint {p}: {e}")
    return None, None
```

Checkpoint at a fixed step cadence **and** on SIGTERM (§7.7). Keep the last few checkpoints, and
keep the best-on-dev one **outside** that pruning.

### 7.6 Save everything that decides the next step

```python
import random, numpy as np
state = {
    "model": model.state_dict(), "optimizer": opt.state_dict(), "scheduler": sched.state_dict(),
    "scaler": scaler.state_dict() if scaler is not None else None,
    "step": step, "epoch": epoch, "sampler": sampler_state,         # position within the epoch
    "rng": {"torch": torch.get_rng_state(), "numpy": np.random.get_state(),
            "python": random.getstate(),
            "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None},
}
```

Also save any module attribute that changes during training but is not in `state_dict()`. A
resume test found one in a published model's code (*verified*). On resume, **restore the RNG
states after creating the DataLoader iterator**: creating it draws a base seed from the torch RNG.

### 7.7 Signals, exit codes and the done-marker

```python
import signal, sys, time
stop = False
def on_term(signum, frame):
    global stop
    stop = True                        # finish this step, checkpoint, then leave
signal.signal(signal.SIGTERM, on_term)

for batch in loader:
    train_step(batch)
    if stop:
        save_checkpoint(state(), ckpt_dir / f"step_{step}.pt")
        sys.exit(143)                  # interrupted, not finished
...
(run_dir / "DONE").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))   # only at the real end
```

- **Make sure SIGTERM reaches Python.** If the job's command is `bash -c '…; python train.py'`,
  bash is PID 1, and the signal can stop at bash (*verified*). Use `exec python …`, or a wrapper
  that forwards it.
- **Exit codes.** Exit 143 only after a real SIGTERM. Some schedulers re-create a pod whose
  container exits non-zero, whatever the retry limit says: Run:ai did, five times (*verified*). On
  those, exit 0 after logging the real code, so a crash ends the job instead of looping it.
- **Done-marker.** Write it only at the real end, and check for it at start. A resubmitted
  finished job then exits at once. Resubmission after maintenance or preemption becomes safe.

`scripts/train_wrapper.sh [--log F] [--done-marker F] [--exit zero|real] -- <command>` does all
three around any command:
- it forwards TERM/INT to the child;
- it tees output to a log on shared storage;
- it logs the child's exit code and exits 143 on a real TERM, otherwise 0 (or the real code);
- it short-circuits on an existing done-marker, and writes the marker only on a clean,
  uninterrupted success.

### 7.8 The resume test

Before the first long job, on CPU, for free:
1. Train a tiny model for N steps uninterrupted; save the final weights.
2. Train it again, send SIGTERM mid-step around N/2, resume, and finish N steps.
3. Compare the weights: they should match to ~1e-6. A larger difference means some state is not
   saved, or the RNG is restored in the wrong order.
4. Resubmit the finished run: it must exit at once without training.

### 7.9 Decide before the run

Write these down before the first step:
- the stop rule (e.g. no dev improvement in K evaluations);
- the evaluation (data, metric, cadence);
- which checkpoints are kept.

Then the run cannot quietly become a budget sink.

## 8. Accounting

Keep a ledger in the project repository: date, job, GPUs, fraction, wall hours, GPU-hours,
running total, purpose. Nothing else will.

- **Take the window from the scheduler's own record,** from allocation (first `Scheduled`/`Bound`)
  to the terminal event, not from your memory of when you submitted.
- **Count every restarted pod.** Each re-created pod was allocated time.
- **Say how fractions are counted.** Whether a fraction is billed as a fraction is a question for
  the facility. Until it is answered, record both fraction × wall and whole-GPU wall.

```bash
scripts/gpu_ledger.py add manual --name <job> --start <ISO time> --end <ISO time> --gpus 1 --purpose "<why>"
scripts/gpu_ledger.py add runai --json <describe -o json output> --purpose "<why>"    # Run:ai
scripts/gpu_ledger.py show
```

## 9. Spending strategy

- **Climb the ladder, cheapest rung first:** CPU dry run → fraction → 15-minute throughput check
  → a short run with evaluation → the full run. Each rung is a gate, and a failure stops the
  climb.
- **Parallel short runs** only when the quota allows it and each run is gated on its own.
- **Stop early on a failed gate or a broken stop rule.** Sunk GPU-hours are not a reason to
  continue.
- **Leave unused budget unused.**

## 10. Scheduler notes

| | Run:ai | Slurm | Kubernetes | local / Jupyter |
|---|---|---|---|---|
| `gpu_watch.sh` prefix | `-- runai training standard exec <job> -p <project> --` | `-- srun --jobid <id> --overlap` | `-- kubectl exec <pod> --` | (none) |
| allocation record | `runai training standard describe <job> -p <project> -o json --pod-limit -1 --event-limit -1` | `sacct -j <id> --format=JobID,Start,End,AllocTRES` | pod events and status times | your own timer |
| restart behaviour | re-creates failed pods even with `--backoff-limit 0` (*verified*); exit 0 after logging | `--requeue` only if asked | Job `backoffLimit`, `restartPolicy` | — |
| stuck start | pod stays `ContainerCreating` past `--pod-running-timeout` (*verified*); exclude the node | `PD` with a reason in `squeue` | pod events | — |
| restart count for `--restarts` | number of pods in `describe` | `sacct` requeue count | `restartCount` | — |

For Run:ai on Apollo specifically, see `APOLLO_AGENT_GUIDE.md` §6.5 and §11.

## 11. Report-back format

After any GPU work, report in this order:
1. **Anything still holding a GPU** — first, always.
2. **GPU-hours used,** with how fractions were counted, and the running total against the
   budget.
3. **Utilisation observed** (verdict and evidence from §4) and what was changed because of it.
4. **Gates passed or failed,** and the stop rule's state.
5. **Anything you could not verify,** and every error verbatim.
