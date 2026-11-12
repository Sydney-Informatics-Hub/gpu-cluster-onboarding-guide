---
name: gpu-efficiency
description: Get the most out of each paid GPU-hour on a shared cluster (Run:ai, Slurm, Kubernetes) or workstation — free checks before spending, a cost estimate before asking, a first-ten-minutes utilisation check that names the bottleneck (idle, host-bound, input-bound, bursty, GPU-bound, crash loop, stuck start, NaN streak), measurement-led diagnosis and fixes for PyTorch training, guards against silent waste (non-finite losses, empty epochs, padding, lost SIGTERM, crash loops), checkpoint/resume for preemptible jobs, and a GPU-hours ledger. Use this whenever the user plans, estimates, starts, monitors or debugs GPU work — "my job shows 30% GPU utilisation", "training is slow", "is my job using the GPU", "how many GPU-hours will this take", "we have N GPU-hours left", "my job is stuck ContainerCreating/Pending", "loss is NaN", "make my training resumable" — even if they don't mention efficiency or cost.
---

# Getting the most out of each GPU-hour

> **With HPC Navigator installed, use its `hpcnav-<site>` skill instead of this one** (on Apollo,
> `hpcnav-apollo`). It carries this skill's know-how as its `gpu-efficiency` how-to page, with
> verdicts from `hpcnav verdict` in place of the scripts here. This skill is for working without
> HPC Navigator.

On a fee-for-service cluster an allocated GPU is billed whether it computes or not. Most waste is
not slow kernels but GPUs **held without computing**: a pod stuck starting, a crash loop the
scheduler keeps restarting, an idle notebook, a job waiting on one host core, NaN batches skipped
for minutes, an epoch that yields no batches. Each of those was seen in real use, and each is
caught by a cheap check or a measurement, not by a guess.

`references/GPU_EFFICIENCY_GUIDE.md` is the full guide: the gates, verdicts, diagnosis, the fix
catalogue with evidence, and training-code guards with snippets. This file is the procedure. For
the University of Sydney's Apollo cluster, use the **apollo-gpu** skill as well; its runbook §11
gives the same procedure with Apollo's commands.

<!-- In the gpu-cluster-onboarding-guide repository, the guide is ai-agents/GPU_EFFICIENCY_GUIDE.md
and the scripts are in ai-agents/scripts/; ai-agents/build_claude_skills.sh copies them in. -->

## Principles

- **GPU time is money** — and every GPU request is the user's decision. State GPUs, hours and
  GPU-hours when asking; report what was used.
- **Measure before fixing.** Two plausible bottleneck guesses were both wrong in practice; the fix
  came from a measurement.
- **An unspent budget is not a reason to run.** Decide the stop rule, the evaluation and what is
  kept before the run; never tune on the evaluation set.

## 1. Free gates — before any GPU job

Stop at the first that fails. Details and rationale: guide §2.

1. **Dry run on real data in a CPU-only job** — tens to hundreds of steps.
2. **Resume test on CPU** — a tiny model, uninterrupted vs. SIGTERM'd mid-step and resumed;
   weights match to ~1e-6; resubmitting the finished run exits at once (guide §7.8).
3. **Batches per epoch > 0, padding ratio sane** — printed by the dry run. A sampler once yielded
   zero batches per epoch, silently; mixing very different lengths once padded batches 16×.
4. **Input capacity** — `scripts/loader_bench.py` in a CPU-only job; it should exceed what the GPU
   will consume by ~1.5×.
5. **GPU code path on the smallest fraction** — correctness only; fractions are time-shared, so
   never quote their timings as throughput. *Ask first.*
6. **Throughput** — ≤ 15 minutes on one whole GPU with the input cache warm. *Ask first.*

If the code cannot checkpoint and resume, say so before any long job on a preemptible or
maintained cluster — that is a code problem to fix first (guide §7).

## 2. Estimate, then ask

```bash
scripts/gpu_ledger.py estimate --units <per epoch> --epochs <n> --throughput <units/s from gate 6> --gpus 1
```

wall = units × epochs ÷ throughput × (1 + validation share) + start-up. Multi-GPU does not scale
linearly — measure, or assume ≤ 0.9× per GPU and say so. Ask with the numbers and the stop rule.

## 3. Watch the first ten minutes

About 5 minutes into every long job, and again at about 10 — read-only, holds nothing:

```bash
scripts/gpu_watch.sh -n 12 -i 5 [--train-log <job's JSONL log>] [--restarts N] -- <exec prefix>
```

Prefixes: none (local / Jupyter terminal); `runai training standard exec <job> -p <project> --`;
`srun --jobid <id> --overlap`; `kubectl exec <pod> --`. Act on the verdict:

| verdict | action |
|---|---|
| `stuck-starting` | not running or no GPU visible: check events; a pod stuck starting holds its GPU — report the node, offer to resubmit excluding it (ask) |
| `restart-loop` | stop it (ask); read the job's own log on shared storage; make the container exit 0 after logging its code |
| `nonfinite-streak` | stop it (ask); fix the numerics first |
| `idle-held` | nothing uses the GPU: suspend or stop the workload (ask) |
| `input-bound` | more loader workers, faster storage, precomputed inputs |
| `host-bound` | remove host-device syncs, bigger batches, compile the step |
| `bursty` | batch composition or per-step host work: profile the step |
| `gpu-bound` | busy: tune batch size and precision, then leave it |
| `unclear` | measure with `step_profile.py` and `loader_bench.py` |
| `memory-headroom` (advisory) | a smaller fraction, or a bigger batch |

The reference case: 35–51 % utilisation, ~200 W of 700 W, 17 of 141 GB, one Python process at
100 % of a core — `host-bound` + `memory-headroom`. Trust the printed evidence over the label.

## 4. Diagnose by measuring, then fix

In order of cost (guide §5):
1. **Data-wait share** in the training log (snippet in guide §5.1) — high means input-bound; near
   zero with low utilisation means the step itself.
2. **`scripts/step_profile.py`** — per-phase step time and *top-level* aten op counts
   (`--demo` shows the output). Many tiny ops → host-bound dispatch; a phase slower than its ops
   explain → a hidden sync.
3. **`scripts/loader_bench.py`** — input capacity and the thread/shm environment, in a free CPU
   job.
4. **Batch composition** — units per batch and padding ratio; slow windows that repeat at the same
   steps across same-seed runs point at batch contents.

Then apply the matching entry from the fix catalogue (guide §6) — one change at a time,
re-measuring after each. Always: thread variables set to the requested cores (pods may have no
CPU limit — `nproc` 224 for an 8-core request), and enough `/dev/shm` for DataLoader workers.

## 5. Guards in training code

From guide §7, with snippets there. The ones that pay for themselves first:
- stop after N consecutive non-finite losses — never skip them indefinitely;
- refuse an epoch that yields no batches (except the tail of a resumed epoch);
- atomic checkpoints (write, then rename) at a fixed cadence **and** on SIGTERM; resume from the
  newest that loads; save optimiser, scheduler, scaler, sampler position and every RNG state, and
  restore the RNG after creating the DataLoader iterator;
- SIGTERM must reach Python (`exec`, or a forwarding wrapper); exit 143 only on a real SIGTERM;
  on schedulers that re-create failed pods, exit 0 after logging the code;
- a done-marker written only at the real end, checked at start.

`scripts/train_wrapper.sh [--log F] [--done-marker F] [--exit zero|real] -- <command>` gives any
command the signal forwarding, log tee, exit-code rules and done-marker check.

## 6. Account for every hour

```bash
scripts/gpu_ledger.py add runai --json describe.json --purpose "<why>"     # Run:ai: describe -o json --pod-limit -1 --event-limit -1
scripts/gpu_ledger.py add manual --name <job> --start <ISO> --end <ISO> --gpus 1 --purpose "<why>"
scripts/gpu_ledger.py show
```

Windows come from the scheduler's record, every restarted pod counts, and fractions are recorded
both as fraction × wall and whole-GPU wall until the facility confirms how it bills them.

## 7. Spend in gated steps

CPU dry run → fraction → 15-minute throughput check → short run with evaluation → full run. Each
rung is a gate; stop on a failure. Parallel short runs only within quota, each gated. Sunk
GPU-hours are not a reason to continue.

## Report back

Lead with anything still holding a GPU; then GPU-hours used (and how fractions were counted)
against the budget; the utilisation verdict and evidence and what changed because of it; gates
passed or failed; anything unverified and every error verbatim.
