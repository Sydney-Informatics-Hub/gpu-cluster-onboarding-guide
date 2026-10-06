# How to run NAMD on the Apollo GPU Cluster

[NAMD](https://www.ks.uiuc.edu/Research/namd/) is a molecular dynamics simulation code used to study the physical movements of large biomolecular systems (proteins, membranes, viruses) over time. NAMD 3.x introduced **GPU-resident mode**, which keeps force calculation, integration, and constraints entirely on the GPU between timesteps, giving a large speedup over the older GPU-offload approach.

This guide submits NAMD as a Run:AI training workload using the official `nvcr.io/hpc/namd` container from NGC, using the [STMV](https://www.ks.uiuc.edu/Research/namd/benchmarks/) dataset - a ~1.07 million atom satellite tobacco mosaic virus capsid - as a worked example.

:::{.callout-note}
**What's provided:**

- The `nvcr.io/hpc/namd:3.0.1` container image, pulled automatically from NGC on first run

**What you need to set up once:**

- Your NAMD input files (structure, coordinates, and a `.namd` config) on your PVC - [Step 1](#step-1-get-your-input-files)
:::

## Prerequisites

This guide assumes you have already installed and logged in to the `runai` CLI - see the [CLI setup guide](CLI.html) - and have a default project set:

```bash
runai project set ${RUNAI_PROJECT}
```

## Step 1: Prepare your input files

This guide uses the STMV benchmark dataset, published by the [NAMD team at UIUC](https://www.ks.uiuc.edu/Research/namd/benchmarks/). Its input files (`stmv.pdb`, `stmv.psf`, `par_all27_prot_na.inp`) plus a GPU-resident config (`stmv_gpures_nve.namd`, which sets `CUDASOAintegrate on`) need to be on your PVC:

```
/scratch/${RUNAI_PROJECT}/namd/stmv_gpu/
├── stmv.pdb
├── stmv.psf
├── par_all27_prot_na.inp
└── stmv_gpures_nve.namd   # GPU-resident mode
```

:::{.callout-important}
**GPU-resident vs. GPU-offload mode** is selected by which `.namd` config file you point NAMD at, not by a command-line flag. A config setting `CUDASOAintegrate on` enables NAMD 3's GPU-resident code path; leaving it off (or omitting it) uses the older GPU-offload path instead.
:::

For your own system, replace these with your own structure/coordinate/parameter files and `.namd` config.

## Step 2: Submit the job

```bash
#!/bin/bash
# NAMD 3.0.1 — STMV GPU-resident, 1 GPU

JOB_NAME="namd-stmv-gpures-1gpu"
DATA_DIR="/scratch/${RUNAI_PROJECT}/namd/stmv_gpu"

runai training submit "${JOB_NAME}" \
  -p "${RUNAI_PROJECT}" \
  --image nvcr.io/hpc/namd:3.0.1 \
  --image-pull-policy IfNotPresent \
  -c \
  --gpu-devices-request 1 \
  --cpu-core-request 8 \
  --cpu-memory-request 32Gi \
  --existing-pvc claimname="pvc-${RUNAI_PROJECT}",path="/scratch/${RUNAI_PROJECT}" \
  --working-dir "${DATA_DIR}" \
  -- bash -c "namd3 +p8 +setcpuaffinity +devices 0 stmv_gpures_nve.namd |& tee out_stmv_gpures_nve_1gpu.log; true"
```

- `+p8` runs 8 Charm++ worker threads, matching `--cpu-core-request 8`
- `+devices 0` selects the single GPU Run:AI allocates to the job
- the `; true` at the end makes the container always exit `0` - see the [Benchmarking](#benchmarking) section for why this matters when using a config with `benchmarkTime` set

## Step 3: Monitor the job

```bash
runai training list
runai training logs namd-stmv-gpures-1gpu -p ${RUNAI_PROJECT}
```

Wait for the job to reach **Completed**. For this example (parsing the ~1.07M-atom structure plus a 180-second benchmark run), expect a total job walltime of around 3-4 minutes.

## Benchmarking

The `stmv_gpures_nve.namd` config used above sets `benchmarkTime 180`, which tells NAMD to **halt itself after 180 seconds of wall-clock time** and print a performance report - this is the standard NAMD/NVIDIA benchmarking methodology for reporting GPU throughput (ns/day), not a bug.

:::{.callout-important}
NAMD halts via an internal abort (`CmiAbort`), which exits the container with a non-zero status. Kubernetes reads that as a crash and resubmits the job - and on this cluster, the per-job `--backoff-limit` flag is **not honoured** (it always retries a fixed number of times), so an unwrapped benchmark command burns several GPU-hours re-running the same 180-second benchmark over and over before finally being marked `Failed`.

Wrapping the command as `bash -c "namd3 ...; true"` (as in [Step 2](#step-2-submit-the-job)) makes the container always exit `0`, so Run:AI marks the job `Completed` after a single run. The benchmark result itself is unaffected - NAMD still halts at 180 seconds and prints the same report either way.
:::

Recorded result for the 1-GPU case above, on 1x NVIDIA H200 (143 GB):

| Metric | Value |
|---|---|
| GPU | 1x NVIDIA H200 |
| CPU cores | 8 |
| Mode | GPU-resident (`CUDASOAintegrate on`) |
| Benchmark window | 180 s (NAMD's own `benchmarkTime`) |
| Steps completed | 31,000 |
| Performance | 29.86 ns/day (0.00579 s/step) |
| Total job walltime | 3 min 33 s (submit to `Completed`) |

:::{.callout-tip}
The same dataset also ships `stmv_gpures_nve.namd` variants of the vendor's reference scripts configured for 2, 4, and 8 GPUs (`+devices 0,1`, `0,1,2,3`, etc.) if you want to benchmark multi-GPU scaling - swap `--gpu-devices-request` and `+devices`/`+p` accordingly.
:::

## Command reference

:::{.callout-note collapse="true"}
## runai training submit flags

| Flag | Value in this guide | Purpose |
|------|---------------------|---------|
| *(job name)* | `namd-stmv-gpures-1gpu` | Unique name for this job within your project |
| `-p` | `${RUNAI_PROJECT}` | Your Run:AI project |
| `--image` | `nvcr.io/hpc/namd:3.0.1` | NVIDIA's NAMD container image (NGC) |
| `--image-pull-policy` | `IfNotPresent` | Reuses a locally cached image instead of re-downloading on every run |
| `-c` | *(flag)* | Allows you to specify the command to run inside the container |
| `--gpu-devices-request` | `1` | Requests one full GPU device, rather than a fractional share |
| `--cpu-core-request` / `--cpu-memory-request` | `8` / `32Gi` | CPU cores and RAM reserved for the job |
| `--existing-pvc` | `claimname=pvc-${RUNAI_PROJECT},...` | Mounts your persistent storage so the job can read your input files |
| `--working-dir` | `/scratch/${RUNAI_PROJECT}/namd/stmv_gpu` | Sets the container's working directory, since the `.namd` config references input files with relative paths |

## NAMD / Charm++ flags

| Flag | Purpose |
|------|---------|
| `+p8` | Number of Charm++ worker threads (PEs) |
| `+devices 0` | GPU device(s) to use, by index within the container |
| `+setcpuaffinity` | Pins worker threads to CPU cores for consistent performance |
:::

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Job shows `Failed`, but logs show `EXITING: Exceeded benchmark time limit 180 seconds` and a full performance report | Expected - NAMD's own `benchmarkTime` halts the run with a non-zero exit code | No action needed if your command is wrapped as in [Step 2](#step-2-submit-the-job) (`bash -c "...; true"`), which makes the job show `Completed` instead |
| Job retries several times (`-0-0`, `-0-1`, ... in `runai training describe`) before showing `Failed` | The `--backoff-limit` flag is not honoured on this cluster; an unwrapped NAMD command's non-zero exit is read as a crash and retried | Wrap the command as `bash -c "namd3 ...; true"` so the container always exits `0` |

## Coming soon

- **Multi-GPU scaling benchmarks** - throughput comparisons across 1, 2, 4, and 8 GPUs using the vendor's reference configs
