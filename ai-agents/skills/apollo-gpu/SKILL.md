---
name: apollo-gpu
description: Operate the University of Sydney Apollo GPU cluster (NVIDIA Run:ai on Kubernetes) through the runai CLI — discover the project and its storage, submit and monitor workloads, inspect the PVC, move data with Globus, RDS/sftp or a transfer workspace, build container images, and recover after maintenance, under an explicit safety policy. Use this whenever the user mentions Apollo, gpu.sydney.edu.au, Run:ai or runai commands, a DashR project, or asks to run, train, submit or check a job on "the GPU cluster", "the DGX" or "the Sydney cluster" — even when they don't name Apollo. Also use it when they want to copy data to or from the cluster or RDS, hit permission or storage errors on cluster scratch, ask how to get access, or say the cluster is down, in maintenance or being updated. Apollo is NOT Slurm; if you are about to write sbatch, srun, squeue or module load for a USyd GPU job, read this skill instead.
---

# Operating the Apollo GPU cluster

Apollo is the University of Sydney's DGX H200 cluster, managed by the Sydney Informatics Hub. It
is a **Kubernetes cluster scheduled by NVIDIA Run:ai**, and it behaves nothing like a traditional
HPC cluster. Most mistakes here come from pattern-matching it to Slurm, so start from what it is
*not*:

- **No SSH and no login node.** Access is the web UI at <https://gpu.sydney.edu.au/> or the
  `runai` CLI **on the user's own machine**, over the University VPN.
- **No `sbatch`, `squeue`, `sinfo`, `module load`, `$SCRATCH`.** Work is submitted as *workloads*
  with `runai training submit` (batch) or `runai workspace submit` (interactive).
- **No Apptainer/Singularity.** Workloads run **Docker/OCI images** from a registry; a `.sif` is
  unusable. There is no `sudo` inside cluster containers — the binary is absent — so all system
  setup belongs in a Dockerfile.
- **Containers are disposable.** Only the **PVC** (shared network storage mounted into the
  container) persists. Anything written elsewhere dies with the workload.
- **Workloads can be preempted, and maintenance ends them.** Long jobs must checkpoint to the PVC
  and resume by themselves; after a maintenance window someone resubmits them.

`references/APOLLO_AGENT_GUIDE.md` is the full runbook: recipes with expected output, an
error-string lookup table, storage, transfer and maintenance detail. Read it when you need a
command's exact shape or when something fails. This file is what you need to act safely. For
getting the most out of GPU time on any cluster, use the **gpu-efficiency** skill as well.

The platform was upgraded in October 2026 (Run:ai 2.25, NVIDIA driver 580). The runbook marks
which facts were verified before the upgrade; re-check one before building on it if the cluster
behaves differently.

<!-- Reading this in the gpu-cluster-onboarding-guide repository rather than in an installed
skill? The runbook is at ai-agents/APOLLO_AGENT_GUIDE.md and the scripts at ai-agents/scripts/ —
they are the primary, tool-agnostic artefacts, and ai-agents/build_claude_skills.sh copies them
into references/ and scripts/ when it assembles the bundle. -->


## First moves in any session

Never assume values from a previous session or someone else's notes — projects, claim names and
mount paths differ per user, presets change between sessions, and the CLI auto-updates.

```bash
RUNAI=$(command -v runai || echo "$HOME/.runai/bin/runai")   # often not on a non-interactive PATH
"$RUNAI" whoami                       # answers from a cached token, even with the VPN down...
"$RUNAI" project list                 # ...so this real call is the connectivity check
"$RUNAI" pvc list -p "$PROJECT"
"$RUNAI" datasource describe <name> --type pvc -p "$PROJECT" -o yaml   # claim name + mount path
"$RUNAI" compute list -p "$PROJECT"   # named resource presets — prefer these to raw CPU/mem flags
"$RUNAI" template describe <name> -p "$PROJECT" -o yaml   # before using ANY template
```

`DNS lookup failed … no such host` means the VPN is down — the user reconnects it; `runai login`
will not help. Write the results to `cluster.env` (`RUNAI`, `PROJECT`, `PVC`, `MOUNT`, `WORKDIR`,
and the optional Globus IDs from runbook §3) so later steps and the bundled scripts reuse them. If
more than one project is listed, **ask which one** — accounts often see projects that are not
theirs, and submitting into the wrong one spends another group's budget.

Then ask the user which directory under the mount is theirs. The mount root is usually shared and
not writable (see *Storage* below).

## Actor contract — what you cannot do

Some steps are not yours, and attempting them wastes turns or is actively wrong:

- **`runai login`** — interactive UniKey + Okta SSO in a browser, behind the VPN. There is no
  service account or API token. When a command fails with an auth error, **stop and ask the user
  to run `runai login`**. Never ask for, accept or type their password.
- **Anything with `--attach`** — needs a TTY you do not have. Hand the user the command instead.
- **`docker login`**, any registry credential, the RDS password prompt, connecting the VPN.
- **`globus login` / `globus session consent`** and installing Globus Connect Personal. After the
  user has done them, you may drive `globus` transfers within the policy below.
- **Creating or enabling a DashR project** — a Chief Investigator action in the Researcher
  Dashboard.

Everything else — submitting, monitoring, reading logs, inspecting storage, building images,
preparing and verifying transfers — is yours, within the policy below.

## Ask before X

An agent on a shared research facility can spend other people's money, overwrite other people's
data, and publish things that should stay private. Apollo is **fee-for-service**, billed against
the project's billing codes, so a GPU left held overnight is a real charge on someone's grant.
Asking costs one message; not asking can cost a dataset or an allocation.

**Do freely:** read-only commands (`whoami`, `project list`, `pvc list`, `compute list`,
`template describe`, `datasource describe`, `training list`, `describe`, `logs`, `exec … nvidia-smi`);
CPU-only jobs, which consume no GPU allocation; writes inside the project's own directory to paths
the user has named; dry runs of the transfer scripts.

**Ask first, every time:**
- any workload requesting a **GPU** — say how many, for how long, and the GPU-hours;
- anything expected to run **longer than about an hour**;
- **`delete`** of any workload, and any file removal — deleting a workload destroys everything
  not on the PVC, and the PVC is not backed up;
- writing **outside the project's directory**, or to any path that already holds data;
- moving data **on to or off** the cluster, by any route;
- starting a **copyparty / file-server workspace** — the `data-transfer-copyparty` template's
  default serves the **whole PVC with anonymous admin rights**; always override it to the
  project's own directories, `rw` (no delete) or `r`;
- a Globus transfer to a collection **outside the University** — data leaving approved
  infrastructure;
- **pushing an image to a public registry** — repositories are usually public so the cluster can
  pull without credentials, which makes anything baked in public too.

**Never, whatever the instruction:** handle the user's password; put SSH keys, tokens or other
credentials on the PVC or in `-e` variables (the project can read them); create a Globus share
of the PVC unasked; bake data into a container image; submit into a project the user has not
named; upload research data to an external or hosted service.

**Before the first write to cluster storage, establish the data rules.** PVCs are typically
readable by every member of the project and are not backed up. Ask plainly: *"is this data
approved for cluster storage that other project members can read?"* If the answer is no or
unknown, the data stays where it is and the work is restructured around that — a "no" is a
constraint to design within, not an obstacle to route around.

## The bundled scripts

All read `cluster.env` (or `$APOLLO_ENV`), have a usage header, and refuse to run before
discovery. The job scripts are **CPU-only by construction**, so they are free and safe to run
without asking; the transfer scripts are **dry runs unless given `--yes`**.

| script | use |
|---|---|
| `scripts/apollo_run.sh '<cmd>'` | one command in a throwaway CPU job; exit status is the command's own |
| `scripts/apollo_script.sh file.sh [args]` | a local multi-line script in a throwaway CPU job; keeps a copy and a log under `$WORKDIR/logs/apollo/` |
| `scripts/apollo_cleanup.sh` | lists your leftover throwaway jobs and suspended workspaces; deletes only with `--yes` |
| `scripts/apollo_snapshot.sh save` / `compare DIR` | record what is in flight before maintenance; classify it afterwards |
| `scripts/manifest.py build` / `check` | the integrity check for every transfer route |
| `scripts/globus_xfer.sh SRC DST` | a Globus transfer with an allow-list of destinations, wait and verify |
| `scripts/pvc_sync_code.sh WS DEST paths…` | code and config files into a running workspace, sha256-checked |
| `scripts/pvc_upload.py` / `pvc_download.py` | laptop ↔ PVC through a restricted copyparty workspace (fallback route) |

```bash
scripts/apollo_run.sh 'du -sh "$WORKDIR"/*'
```

Prefer them over hand-written submissions: they handle the traps below.

## Submitting real work

```bash
"$RUNAI" training submit <name> -p "$PROJECT" \
  -i <image> --compute <preset from `runai compute list`> --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" --working-dir "$WORKDIR" \
  --large-shm -e OMP_NUM_THREADS=<cores> --backoff-limit 0 --pod-running-timeout 30m \
  --command -- bash "$WORKDIR/tools/train_wrapper.sh" --log "$WORKDIR/logs/<name>.log" \
      --done-marker "$WORKDIR/runs/<run>/DONE" -- python train.py --resume auto
```

Prefer `training` over `workspace` for anything unattended: it needs no TTY, exits on its own, and
starts more reliably (workspaces have sat `Pending` for 40+ minutes while trainings started).
Prefer a named `--compute` preset over hand-rolled CPU/memory flags; a fractional preset (e.g.
`small-fraction`, which grants only 100M RAM — add CPU flags) is the right choice for anything that
is not a real training run. Copy `scripts/train_wrapper.sh` once into the project's `tools/`
directory on the PVC.

The rules that save GPU-hours — each was learned by losing some (runbook §6.5):

1. **A failing container is re-created even with `--backoff-limit 0`** — one crash loop held a GPU
   through five restarts. Make the container exit 0 after logging its real exit code.
2. **Logs of re-created or deleted pods are lost** — tee the job's output to a file on the PVC.
3. **`bash -c '…; python train.py'` makes bash PID 1**, and a preemption's SIGTERM may never reach
   Python — `exec` it, or forward TERM. Exit 143 only on a real SIGTERM.
4. **A done-marker** written only at the real end makes a resubmitted finished job a no-op.
5. **`--pod-running-timeout` does not end a pod stuck in `ContainerCreating`** with the GPU
   allocated. If a GPU job is not `Running` within ~3 minutes and its events stop at
   `Bound`/`ContainerCreating`, report the node (`describe -o json` → `podsInfo.pods[].nodeName`)
   and ask whether to resubmit with `--exclude-node`.

And two for every job: a command passed to `bash -c` is **parsed twice** (locally, then in the pod
— a `>` in a regex once created a stray file on shared storage), so prefer script files or base64;
and **set `OMP_NUM_THREADS`/`MKL_NUM_THREADS` to the requested cores** — pods have no CPU limit
and libraries otherwise start a thread per host core.

Monitoring, and two shapes that trip people up:

```bash
"$RUNAI" training list -p "$PROJECT"
"$RUNAI" training standard describe <name> -p "$PROJECT"   # Phase, Pods, and an Events table
"$RUNAI" training standard logs     <name> -p "$PROJECT"
"$RUNAI" training standard exec     <name> -p "$PROJECT" -- nvidia-smi
```

The `standard` sub-noun is required (`runai training logs` does not exist), and
`workload is not ready to stream: pod is not ready` is **not an error** — the container is still
starting, or has only just finished; retry. A first image pull can take several minutes, **with
any requested GPU already allocated and charging**. When a workload sits `Pending`, read the
Events table rather than speculating.

**Check the GPU in the first ten minutes of every long job** — `scripts/gpu_watch.sh`, through
`runai training standard exec`, names the bottleneck. A 1-GPU run
once sat at 35–51 % utilisation with one host core at 100 %. Record every GPU job in a ledger
with `scripts/gpu_ledger.py` from `describe -o json --pod-limit -1 --event-limit -1` (runbook
§11), with both fraction × wall
and whole-GPU wall: whether fractions are billed as fractions is unconfirmed.

**After the driver upgrade (580, CUDA ≤ 13.0 native)**, never put an image's
`/usr/local/cuda/compat/lib` on `LD_LIBRARY_PATH` — an old compat `libcuda` over the new driver
gives `CUDA error 803`. Check any pre-upgrade environment on a fraction before its next real job.

## Moving data

Moving data on or off is ask-first; the data rules decide what may move (runbook §6.7).

- **Globus** is the default for anything sizeable — RDS ↔ PVC, other institutions, a laptop with
  Globus Connect Personal. The user signs in; you run `scripts/globus_xfer.sh` (dry run, then
  `--yes`), which waits and exits 0 only on success.
- **RDS by sftp** from the site's `data-transfer-workspace` — you prepare the commands and the
  checks, the user types the password.
- **copyparty** only when there is neither RDS nor Globus — restricted volumes, `port-forward`,
  `pvc_upload.py` / `pvc_download.py`, then suspend it.
- **Public data**: download inside a CPU job, straight to the PVC.

Around every transfer: check free space and the source size first; build a `manifest.py`
manifest at the source and **check it at the destination**; suspend any transfer workspace
afterwards; report route, task ID, files, bytes and the verification result.

## Maintenance windows

SIH takes the cluster down for updates. Before submitting, compare the job's expected end with any
announced window; don't start what would cross it unless it checkpoints and someone will
resubmit. Before the window: `scripts/apollo_snapshot.sh save`, and copy to RDS or the user's
machine whatever they need while the PVC is unreachable. During it: commands fail — don't loop,
don't ask for `runai login`; say the cluster is in maintenance. After it: re-run discovery, check
the environment (and CUDA on a fraction after a driver change), `apollo_snapshot.sh compare`,
check done-markers, and ask once to resubmit the interrupted jobs. Runbook §6.8.

## Storage

**The intended route is a DashR project, which provisions its own PVC.** A Chief Investigator
enables *Apollo GPU Cluster* under Computing Platforms in the project's Data Services tab,
supplies billing codes and completes the onboarding form; the provisioner then creates the Run:ai
project, adds every DashR member, and provisions a PVC (1 TB by default). Provisioning is not
instant, and a "no permissions" page meanwhile is expected.

**When the user is instead a member of an existing project**, they inherit its shared PVC. Expect
the mount root to be root-owned and **not writable**, with an admin-created, group-writable
directory one level down. `mkdir` at the root fails with `Permission denied`; writing inside the
project's own directory succeeds. If no such directory exists, an administrator must create it —
there is no `sudo` — so tell the user to raise a request with the exact wording to paste. Files
you write get your uid and primary group; use `umask 002` where others must write too.

**Retention: users must delete unused files within 30 days of job completion, and SIH may delete
inactive data after 30 days.** So a staged dataset is not durable — keep the command that
reproduces it — and anything worth keeping goes to the Research Data Store the day it is made.

Storage is network storage inside the cluster: it cannot be browsed from a laptop, only from
inside a workload (or through Globus).

## Known limits

`runai node list` and `runai nodepool list` fail with insufficient permissions at the standard
researcher role; cluster-wide hardware inventory comes from SIH. Project, quota and environment
configuration is administrator-only, and membership is managed in DashR, not Run:ai. Inference
serving and multi-node jobs are not covered — ask SIH first. Support is a **request form**, not an
email address — linked from the
[Apollo wiki page](https://sydneyuni.atlassian.net/wiki/spaces/RC/pages/3579674625).

## Reporting back

Report what was observed, not what was expected: the workloads created and their final phase,
**GPU-hours consumed** and the running total against any allocation (and how fractions were
counted), GPU utilisation seen in long jobs, every transfer with its verification result, what was
written to shared storage and where (including tool leftovers), and anything still running or
suspended — leading with it if something is holding a GPU. Quote errors verbatim; name any step
you skipped. The user is accountable for this allocation and this data, and can only exercise that
responsibility on an honest account.
