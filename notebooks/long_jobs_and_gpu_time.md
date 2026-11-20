# Running long jobs and using GPU time well

The Apollo GPU cluster is a fee-for-service facility. A GPU is charged to your project from the moment it is allocated to your workload until the workload stops: while the container image downloads, while a notebook sits idle, and while a job waits on its data. Long jobs can also be interrupted, by higher-priority work (preemption) or by planned maintenance.

This page collects the habits that save the most GPU time and protect long jobs. Each one comes from real use of the cluster. If you work with an AI coding agent, the [agent guide](https://github.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide/tree/main/ai-agents) teaches it the same rules.

## Don't hold a GPU you are not using

* **Stop or suspend interactive workspaces as soon as you pause.** A Jupyter or Marimo workspace with a GPU is charged overnight whether or not a cell is running. In the web UI, select the workload and click **STOP**. From the CLI, run `runai workspace suspend <name> -p <project>`. Files on your PVC are kept. Anything outside the PVC is lost, including packages installed with `pip` at runtime.
* **Test on a CPU or a GPU fraction first.** CPU-only workloads do not use your GPU allocation. A fractional GPU (for example a `small-fraction` compute resource, one tenth of a GPU) is enough to check that your code runs on a GPU. Take a whole GPU only for the real run.
* **Don't leave failed jobs restarting.** If a training job's script crashes, the cluster starts it again, up to 6 more times, holding the GPU each time. Submitting with `--backoff-limit 0` does not prevent this; `--backoff-limit 1` limits it to one retry. Check a new job's status a few minutes after it starts, and stop it if it keeps restarting.
* **Watch for jobs that never start.** If a GPU job stays in *ContainerCreating* or *Initializing* for more than a few minutes, the GPU may already be allocated. Check the job's events, and contact SIH if it does not move.

## Check that your job actually uses the GPU

Look at GPU utilisation in the first ten minutes of every long job. A job measured on Apollo was using its GPU only 35–51 % of the time, because one CPU core was doing work the GPU had to wait for.

* In a Jupyter or Marimo terminal: `nvidia-smi`, or `watch -n 5 nvidia-smi` to refresh it.
* For a training job, from the CLI: `runai training standard exec <job> -p <project> -- nvidia-smi`

Read the **GPU-Util** and **Memory-Usage** columns over a minute or so:

| What you see | Likely cause | What to try |
|---|---|---|
| GPU-Util near 0 % | nothing is running on the GPU | stop the workload, or check the code really uses `cuda` |
| GPU-Util low, and `top` shows several `python` processes busy | the data loading cannot keep up | more DataLoader workers; faster storage; prepare inputs once ahead of time |
| GPU-Util low, and `top` shows one `python` process at 100 % | per-step work on the CPU | bigger batches; avoid `.item()` or `.cpu()` calls every step |
| GPU-Util high (above ~85 %) | the GPU is busy | good — tune batch size and mixed precision, then leave it |
| Memory-Usage a small fraction of the total | the GPU is larger than the job needs | a GPU fraction, or a bigger batch |

The [GPU efficiency guide](https://github.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide/blob/main/ai-agents/GPU_EFFICIENCY_GUIDE.md) explains how to measure these properly and which fixes help when.

## Use the CPU cores you asked for, and no more

Workloads can see all of a node's CPU cores, not just the ones you requested. Libraries such as PyTorch, NumPy and MKL then start one thread per core. They compete with other users and often run *slower*: one job on Apollo ran about four times faster with a single thread. Set the thread count to the number of cores you requested before importing these libraries:

```python
import os
for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[var] = "8"          # the number of CPU cores your workload requested
import torch
torch.set_num_threads(8)
```

## Give DataLoader workers enough shared memory

PyTorch DataLoader workers pass data through `/dev/shm`, which is small by default. If you see

```
ERROR: Unexpected bus error encountered in worker. This might be caused by insufficient shared memory (shm).
```

submit the workload with the `--large-shm` CLI flag. This applies to CPU-only workloads too. If you start workloads from the web UI and hit this error, contact SIH.

## Make long jobs resumable

Any job longer than a few minutes can be interrupted, by preemption or by maintenance. Write your training code so that an interrupted job can simply be started again:

1. **Save a checkpoint to your PVC regularly,** for example every N steps. Write it to a temporary file first, then rename it, so an interruption never leaves a half-written checkpoint.
2. **Save everything needed to continue:** the model, the optimiser, the learning-rate scheduler, the step number and the random-number-generator states, not just the model weights.
3. **On start-up, resume from the newest checkpoint that loads.**
4. **Handle the stop signal.** When a job is preempted it receives `SIGTERM`. Catch it, save a checkpoint, and exit. If your job's command is `bash -c "...; python train.py"`, start Python with `exec python train.py` so the signal reaches it.
5. **Mark the end.** Write a small "done" file when training really finishes, and check for it at start-up, so restarting a finished job does nothing.
6. **Test it before your first long run.** Run a small model on CPU, stop it half-way, resume it, and check that it finishes with the same result as an uninterrupted run.

## Planned maintenance

SIH takes the cluster down for maintenance from time to time. All running workloads stop, and **your PVC cannot be reached until the cluster is back**.

**Before a maintenance window**

* Don't start long jobs that would still be running when it begins, unless they checkpoint and you will restart them.
* Note which jobs are running, how you submitted them, and where their checkpoints are.
* Copy anything you will need during the window, such as results or a checkpoint to evaluate, to RDS (Globus is easiest; see [How to transfer data](data_transfer.md)).

**After the window**

* Check your files are where you left them.
* Check your software environment still works, especially after driver or software upgrades. Since the October 2026 update, the NVIDIA driver supports CUDA up to version 13.0. Images and Python environments built for CUDA 12 keep working (we checked the NVIDIA PyTorch 25.06 image). If one fails with "CUDA error 803", remove any `.../cuda/compat/lib` entry from `LD_LIBRARY_PATH`.
* Resubmit jobs that had not finished. If they resume from their newest checkpoint, they lose only the work since that checkpoint.

## Estimate before you run

Before a long run, measure how fast your job is in a short test (10–15 minutes on one GPU), then estimate:

> hours ≈ items per epoch × epochs ÷ items per second ÷ 3600, plus some time for validation and start-up

Multiply by the number of GPUs for GPU-hours. More GPUs rarely make a job proportionally faster, so measure before scaling up. Keep a simple record of each job's GPUs and hours, so you know how much of your project's allocation you have used.
