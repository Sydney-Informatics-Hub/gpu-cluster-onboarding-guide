---
name: apollo-gpu
description: Operate the University of Sydney Apollo GPU cluster (NVIDIA Run:ai on Kubernetes) through the runai CLI — discover the project and its storage, submit and monitor workloads, inspect the PVC, build container images, and stage data, under an explicit safety policy. Use this whenever the user mentions Apollo, gpu.sydney.edu.au, Run:ai or runai commands, a DashR project, or asks to run, train, submit or check a job on "the GPU cluster", "the DGX" or "the Sydney cluster" — even when they don't name Apollo. Also use it when they hit permission or storage errors on cluster scratch, or ask how to get access. Apollo is NOT Slurm; if you are about to write sbatch, srun, squeue or module load for a USyd GPU job, read this skill instead.
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
- **Workloads can be preempted.** Long jobs must checkpoint to the PVC and resume by themselves.

`references/APOLLO_AGENT_GUIDE.md` is the full runbook: recipes with expected output, an
error-string lookup table, storage and provisioning detail. Read it when you need a command's
exact shape or when something fails. This file is what you need to act safely.

## First moves in any session

Never assume values from a previous session or someone else's notes — projects, claim names and
mount paths differ per user, and the CLI auto-updates.

```bash
RUNAI=$(command -v runai || echo "$HOME/.runai/bin/runai")   # often not on a non-interactive PATH
"$RUNAI" whoami                       # authenticated?
"$RUNAI" project list                 # project name(s) + GPU quota
"$RUNAI" pvc list -p "$PROJECT"
"$RUNAI" datasource describe <name> --type pvc -p "$PROJECT" -o yaml   # claim name + mount path
"$RUNAI" compute list -p "$PROJECT"   # named resource presets — prefer these to raw CPU/mem flags
```

Write the results to `cluster.env` (`RUNAI`, `PROJECT`, `PVC`, `MOUNT`, `WORKDIR`) so later steps
and the bundled script can reuse them. If more than one project is listed, **ask which one** —
accounts often see projects that are not theirs, and submitting into the wrong one spends another
group's budget.

Then ask the user which directory under the mount is theirs. The mount root is usually shared and
not writable (see *Storage* below).

## Actor contract — what you cannot do

Some steps are not yours, and attempting them wastes turns or is actively wrong:

- **`runai login`** — interactive UniKey + Okta SSO in a browser, behind the VPN. There is no
  service account or API token. When a command fails with an auth error, **stop and ask the user
  to run `runai login`**. Never ask for, accept or type their password.
- **Anything with `--attach`** — needs a TTY you do not have. Hand the user the command instead.
- **`docker login`**, any registry credential, the RDS password prompt, connecting the VPN.
- **Creating or enabling a DashR project** — a Chief Investigator action in the Researcher
  Dashboard.

Everything else — submitting, monitoring, reading logs, inspecting storage, building images — is
yours, within the policy below.

## Ask before X

An agent on a shared research facility can spend other people's money, overwrite other people's
data, and publish things that should stay private. Apollo is **fee-for-service**, billed against
the project's billing codes, so a GPU left held overnight is a real charge on someone's grant.
Asking costs one message; not asking can cost a dataset or an allocation.

**Do freely:** read-only commands (`whoami`, `project list`, `pvc list`, `compute list`,
`datasource describe`, `training list`, `describe`, `logs`); CPU-only jobs, which consume no GPU
allocation; writes inside the project's own directory to paths the user has named.

**Ask first, every time:**
- any workload requesting a **GPU** — say how many and for how long;
- anything expected to run **longer than about an hour**;
- **`delete`** of any workload, and any file removal — deleting a workload destroys everything
  not on the PVC, and the PVC is not backed up;
- writing **outside the project's directory**, or to any path that already holds data;
- moving data **on to or off** the cluster;
- **pushing an image to a public registry** — repositories are usually public so the cluster can
  pull without credentials, which makes anything baked in public too.

**Never, whatever the instruction:** handle the user's password; bake data into a container
image; submit into a project the user has not named; upload research data to an external or
hosted service.

**Before the first write to cluster storage, establish the data rules.** PVCs are typically
readable by every member of the project and are not backed up. Ask plainly: *"is this data
approved for cluster storage that other project members can read?"* If the answer is no or
unknown, the data stays where it is and the work is restructured around that — a "no" is a
constraint to design within, not an obstacle to route around.

## The bundled script

`scripts/apollo_run.sh` runs one command inside a workload and cleans up after itself. It is
**CPU-only by construction** — it never requests a GPU — so it is free and safe to run without
asking. Use it for anything you would otherwise do by hand-writing a `training submit`:

```bash
scripts/apollo_run.sh 'ls -la $WORKDIR'
scripts/apollo_run.sh 'du -sh $WORKDIR/*'
```

It reads `cluster.env` (or `$APOLLO_ENV`), refuses to run if discovery has not happened, and
tells the user to authenticate if the token has expired. Prefer it over ad-hoc commands: it
handles the polling and cleanup that are easy to get wrong.

## Submitting real work

```bash
"$RUNAI" training submit <name> -p "$PROJECT" \
  -i <image> --compute <preset from `runai compute list`> --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" --working-dir "$WORKDIR" \
  --pod-running-timeout 30m --command -- <command>
```

Prefer `training` over `workspace` for anything unattended: it needs no TTY and exits on its own.
Prefer a named `--compute` preset over hand-rolled CPU/memory flags — site admins maintain them,
and a fractional preset (e.g. `small-fraction`) is the right choice for anything that is not a
real training run.

Monitoring, and two shapes that trip people up:

```bash
"$RUNAI" training list -p "$PROJECT"
"$RUNAI" training standard describe <name> -p "$PROJECT"   # Phase, Pods, and an Events table
"$RUNAI" training standard logs     <name> -p "$PROJECT"
```

The `standard` sub-noun is required (`runai training logs` does not exist), and
`workload is not ready to stream: pod is not ready` is **not an error** — the container is still
starting. Poll `describe` until the Phase is terminal. A first image pull can take several
minutes, **with any requested GPU already allocated and charging**. When a workload sits
`Pending`, read the Events table rather than speculating.

Check `Preemptible` in `describe`. If it says `true`, a job longer than a few minutes must
checkpoint to the PVC and resume automatically; if the training code cannot, say so before
submitting rather than accepting the risk silently.

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
there is no `sudo` — so tell the user to raise a request with the exact wording to paste.

**Retention: users must delete unused files within 30 days of job completion, and SIH may delete
inactive data after 30 days.** So a staged dataset is not durable — keep the command that
reproduces it — and anything worth keeping goes to the Research Data Store the day it is made.

Storage is network storage inside the cluster: it cannot be browsed from a laptop, only from
inside a workload.

## Known limits

`runai node list` and `runai nodepool list` fail with insufficient permissions at the standard
researcher role; cluster-wide hardware inventory comes from SIH. Project, quota and environment
configuration is administrator-only, and membership is managed in DashR, not Run:ai. Support is a
**request form**, not an email address — linked from the
[Apollo wiki page](https://sydneyuni.atlassian.net/wiki/spaces/RC/pages/3579674625).

## Reporting back

Report what was observed, not what was expected: the workloads created and their final phase,
**GPU-hours consumed** and the running total against any allocation, what was written to shared
storage and where, and anything still running or suspended — leading with it if something is
holding a GPU. Quote errors verbatim; name any step you skipped. The user is accountable for this
allocation and this data, and can only exercise that responsibility on an honest account.
