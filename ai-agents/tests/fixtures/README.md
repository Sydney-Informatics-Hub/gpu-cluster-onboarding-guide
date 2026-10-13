# Test fixtures

## `runai_describe_*.json` are synthetic

These four files are **synthetic stand-ins**, written by hand in a plausible shape. They are not
captures from a real cluster. They exist so that `gpu_ledger.py add runai` can be tested now.
Replace them with real, scrubbed captures of
`runai training standard describe JOB -p PROJECT -o json --pod-limit -1 --event-limit -1` when
those are available. Scrub workload, project, user and node names, and keep the shape and the
timestamps.

| file | what it imitates |
| --- | --- |
| `runai_describe_whole_gpu.json` | 1 whole GPU, completed: `Scheduled` 09:00 to `Completed` 11:30, so 2.5 h |
| `runai_describe_fraction.json` | a 0.1 GPU fraction, succeeded, 4 h; allocated reads 0 after completion, so the requested amount is used; nanosecond timestamps |
| `runai_describe_recreated_pods.json` | a crashing job whose pod was re-created 5 times, with per-pod start and end times (union 49 min, workload window 52 min) |
| `runai_describe_running.json` | 2 GPUs, still running, no terminal event, so `--now` is required |

## Field paths: verified and assumed

**Verified** on a live cluster:

- `events[]`, each with `reason` and `createdAt`.
- A GPU amount exists somewhere under `compute`, for example
  `compute.allocatedResources.gpuCompute`.
- `describe` lists at most 10 pods and 50 events unless `--pod-limit -1 --event-limit -1` is
  passed.

**Assumed**, and to be checked against real captures:

- `compute.allocatedResources.gpuCompute` and `compute.requestedResources.gpuCompute` hold a
  plain number, with 0.1 for a 0.1 fraction. It is not known whether the value is the
  allocated or the requested amount, or whether it reads 0 after completion. The fraction
  fixture assumes it reads 0.
- The value may instead be an object such as `{devices, portion}`, or flat submit-style fields
  such as `compute.gpuDevicesRequest` and `compute.gpuPortionRequest`. `gpu_ledger.py`
  accepts these, and its `GPU_PATHS`, `DEVICE_KEYS` and `PORTION_KEYS` list every path it
  tries.
- A top-level `phase`, such as `Running`, `Completed`, `Succeeded` or `Failed`.
- The event reasons `Scheduled` or `Bound` for the start of the allocation, and `Completed`,
  `Succeeded`, `Failed`, or any reason containing "delet", for the end.
- `podsInfo.pods[]`, with optional per-pod `startedAt` and `completedAt` timestamps. The
  alternative names tried are listed in `POD_START_KEYS` and `POD_END_KEYS`.
- A top-level `name`.
