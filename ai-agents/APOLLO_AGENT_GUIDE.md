# Apollo GPU cluster — agent operating guide

**What this is.** A runbook for an AI coding agent (Claude Code or similar) that has been asked
to run work on the University of Sydney **Apollo GPU cluster**. It is written to be executed, not
narrated: every step says who performs it, what it costs, what success looks like, and how it
fails. A human can read it too, but the primary reader is the agent.

**Scope.** Apollo specifically — a Kubernetes cluster scheduled by **NVIDIA Run:ai**, managed by
the Sydney Informatics Hub. Nothing here is specific to any one research project; every
project-specific value is discovered at runtime in §3 rather than hard-coded.

**Provenance.** Every claim carries its evidence:

| tag | meaning |
|---|---|
| *verified YYYY-MM-DD* | executed against the live cluster on that date |
| *observed once* | seen once, cause not established |
| *vendor docs* | stated by NVIDIA / Run:ai documentation or the CLI's own `--help`, not yet checked on Apollo |
| *unverified* | inferred; treat as a hypothesis |

**The platform changed in October 2026.** A maintenance window upgraded Apollo to **Run:ai 2.25**
(CLI `runai-cli/2.25.28` seen 2026-10-14), the NVIDIA driver from 550.163.01 to **580.82.07**,
the GPU and Network Operators to 25.10, Knative Serving to 1.19, and the nodes' operating
systems. Facts verified before 2026-10-08 were taken on Run:ai 2.24 with driver 550. Most
describe the scheduler and CLI rather than the driver, but re-check one before building on it if
the cluster behaves differently. The CLI auto-updates, so re-run §3 when a command's shape looks
wrong. Official docs: <https://sydney-informatics-hub.github.io/gpu-cluster-onboarding-guide/>.

**Companion guide.** [`GPU_EFFICIENCY_GUIDE.md`](GPU_EFFICIENCY_GUIDE.md) covers getting the
most out of each GPU-hour on any cluster. §11 below is its Apollo summary, so this file stands on
its own.

**Contents.** Read §1–§4 before acting; then jump to what the task needs.

| § | covers |
|---|---|
| 1–4 | what Apollo is; who does what; discovery; the ask-first policy |
| 5 | cost, presets, fractions, threads and shared memory |
| 6.1–6.3 | log in; run one command on the PVC (and its traps); check the GPU and CUDA |
| 6.4 | workspaces, `exec`, start-up times |
| 6.5 | real training jobs: crash loops, stuck starts, logs, signals, preemption |
| 6.6 | packages, venvs, images, and getting code onto the PVC |
| 6.7 | moving data: Globus, RDS sftp, copyparty, downloads — check before, verify after |
| 6.8–6.9 | planned maintenance; long jobs that checkpoint and resume |
| 7–8 | storage, permissions, retention; what is denied |
| 9–10 | error strings; reporting back |
| 11 | using GPU time well: gates, estimate, watch, verdicts, ledger |
| A | agent-side shell pitfalls |

**Scripts.** Paths like `scripts/apollo_run.sh` refer to `ai-agents/scripts/` in the
[onboarding-guide repository](https://github.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide),
or to `scripts/` inside the installed Claude skill. Every script has a usage header, and any
agent or person can run them.

---

## 1. Mental model — read this before running anything

Apollo is **not** a traditional HPC cluster. If you pattern-match it to Slurm you will write
commands that cannot work. Specifically:

- **There is no login node and no SSH.** You cannot `ssh` in and `cd` to a directory. All access
  is either the web UI (<https://gpu.sydney.edu.au/>) or the `runai` CLI **on the user's own
  machine**, over the University VPN.
- **There is no `sbatch`, `squeue`, `sinfo`, `module load`, or `$SCRATCH` environment variable.**
  Work is submitted as *workloads* with `runai workspace submit` or `runai training submit`.
- **There is no Apptainer/Singularity.** Workloads run **OCI/Docker images** pulled from a
  registry. A `.sif` file is not usable. System-level setup happens in a Dockerfile, because
  `sudo` does not exist inside cluster containers — it is not merely disabled, the binary is absent.
- **Containers are disposable.** Everything written inside a workload is destroyed with it. The
  only persistence is a **PVC** (Persistent Volume Claim) — shared network storage that the
  cluster mounts into the container. Anything that must survive a job goes there.
- **Workloads may be preempted, and maintenance ends them.** Submitted trainings can be evicted
  to make room for higher-priority work, so long jobs must checkpoint and resume by themselves.
  A planned maintenance window ends everything that is running, and someone has to resubmit it
  afterwards (§6.8, §6.9).

Two workload types matter:

| type | use for | behaviour |
|---|---|---|
| `workspace` | interactive sessions, Jupyter, file browsing | stays up until suspended or deleted |
| `training` | batch jobs, including anything an agent runs unattended | runs the command, then completes |

**Prefer `training` for agent work.** It needs no TTY, exits on its own, and cannot be left
running by accident. It also starts more reliably. A CPU workspace sat `Pending` for 40+ minutes
with no `Scheduled` event, while CPU trainings of the same size started in under a minute
(*observed* 2026-09-30). Workspaces were listed `Preemptible: No` and trainings preemptible. That
non-preemptible workloads must fit the project's in-quota share is the likely reason
(*unverified*). So never make unattended work depend on a workspace starting.

---

## 2. Actor contract — who performs each step

Some steps cannot be performed by an agent, for good reasons. Attempting them wastes turns and,
in the credential cases, is actively wrong. Every recipe below is tagged.

| tag | meaning |
|---|---|
| `[human]` | Only a person can do this. Ask them, then wait. |
| `[agent]` | Safe for an agent to run unattended, within the §4 policy. |
| `[human-then-agent]` | A person does it once; the agent then works in the authenticated session. |

**`[human]` — and why:**

- **`runai login`** — interactive UniKey + Okta SSO in a browser, behind the VPN. There is no API
  token or service account to substitute. Never ask the user for their password, and never type
  it anywhere; the correct action is to ask them to run `runai login` themselves.
- **Anything with `--attach`** — implies a TTY. An agent session has none.
- **`docker login`**, and any registry credential.
- **The RDS password prompt** during `sftp` transfers.
- **`globus login` and `globus session consent`** are browser sign-ins and consents. So is
  installing **Globus Connect Personal** on a laptop. Once the user has done them, the agent may
  drive `globus` CLI transfers within §4 (`[human-then-agent]`).
- **Connecting the VPN.**

Everything else — submitting, monitoring, reading logs, inspecting storage, building images — is
`[agent]` work, subject to the policy in §4.

**When authentication expires** the CLI returns an auth error on every command. Do not retry, and
do not attempt to re-authenticate. Stop and tell the user: *"the Run:ai token has expired, please
run `runai login`"*.

**When the VPN drops, `whoami` does not notice.** It answers from the cached token, and every real
command then fails with a DNS error (*verified* 2026-10-01):

```
Error: failed to submit. Error: URL is incorrect or does not exist. DNS lookup failed for '<cluster host>': no such host
```

That is not an auth problem, and `runai login` will not fix it. Ask the user to reconnect the VPN.
Check connectivity with a cheap real call (`runai project list`), not `whoami`, after any long
idle period.

---

## 3. Discovery — resolve every project-specific value

Do this first, in any new session. It replaces every placeholder in this guide. Do **not** copy
values from someone else's notes; projects, claim names and mount paths differ per user.

```bash
# The CLI is often not on the PATH of a non-interactive shell. Find it before anything else.
RUNAI=$(command -v runai || echo "$HOME/.runai/bin/runai")
"$RUNAI" whoami                 # a cached token is enough for this one...
"$RUNAI" project list           # ...so this real call is the connectivity check; also lists quota
```

If `project list` fails with `DNS lookup failed … no such host`, the VPN is down (§2).

Pick the project (ask the user if more than one is listed — some accounts can see projects that
are not theirs), then:

```bash
PROJECT=<from project list>
"$RUNAI" pvc list -p "$PROJECT"                 # claim name(s)
"$RUNAI" datasource list -p "$PROJECT"          # the project's data sources
"$RUNAI" datasource describe <name> --type pvc -p "$PROJECT" -o yaml   # claim name + mount path
"$RUNAI" compute list -p "$PROJECT"             # named resource presets
"$RUNAI" template list -p "$PROJECT"            # site-provided workload templates
"$RUNAI" template describe <name> -p "$PROJECT" -o yaml   # before using any template
```

`-o yaml` / `-o json` are available on several subcommands and are preferable to parsing tables.
The list commands take `--json` instead (vendor docs: CLI help, 2.25).

**Describe a template before you use it.** `template describe` shows the image, the arguments,
the storage rights and the priority it will submit with. That is how the data-transfer copyparty
template's default shows up: anonymous admin rights over the whole PVC (*verified* 2026-09-29;
see §4).

**Re-discover rather than reuse notes.** Presets change between sessions: `fifth-gpu` and
`two-gpus-vllm` were not in the 2026-09-22 list but appeared by 2026-09-29 (*verified*).
Re-discover after any maintenance window too. An update can change the CLI, presets, templates
and node names (*unverified* for the October 2026 update, beyond the CLI version).

Record the results so later steps and future sessions can reuse them:

```bash
cat > cluster.env <<EOF
RUNAI=$RUNAI
PROJECT=$PROJECT
PVC=<claim name>
MOUNT=<mount path from the datasource describe>
WORKDIR=\$MOUNT/<the project's own directory>
EOF
```

**Ask the user which directory under `$MOUNT` is theirs.** The mount root is typically shared
across everyone in the project and is usually **not writable** — see §7.

**Globus discovery (optional, for §6.7).** Globus can reach project PVCs through a Globus
collection, as well as RDS and other institutions. The collection names come from SIH's Globus
page on the Research Computing wiki, or from the user. Don't guess them. After the user's
`globus login` (`[human]`):

```bash
globus endpoint search "<name from the wiki or the user>"   # note the collection IDs
```

Then find where `$WORKDIR` appears inside the PVC collection, rather than assuming the paths
match. Write a marker from inside the cluster (free CPU job, §6.2), and look for it through Globus:

```bash
scripts/apollo_run.sh 'date > "$WORKDIR/.globus_probe"'
globus ls "<PVC collection ID>:/"        # walk down until .globus_probe appears
```

Add the results to `cluster.env`:

```bash
GLOBUS_PVC_COLLECTION=<id>
GLOBUS_PVC_PREFIX=<path to $WORKDIR as Globus sees it>
GLOBUS_RDS_COLLECTION=<id>             # if the project uses RDS
```

The marker is a few bytes and harmless. Mention it in the report; deleting it is a file removal,
so it falls under §4 like any other.

---

## 4. Safety policy — ask before X

An agent operating a shared research facility can spend other people's compute allocation,
overwrite other people's data, and publish things that should stay private. These rules are the
mechanism that prevents that; follow them even when the user seems to be in a hurry, because the
cost of asking is one message and the cost of not asking can be an unrecoverable dataset or a
consumed grant allocation.

**Do without asking:**

- Read-only commands: `whoami`, `project list`, `pvc list`, `compute list`, `template list`,
  `datasource describe`, `training list`, `workspace list`, `describe`, `logs`.
- CPU-only jobs that inspect storage or run short checks (they consume no GPU allocation).
- Writing inside the project's own directory, to paths the user has named.

**Ask first, every time:**

- **Submitting any workload that requests a GPU.** Say how many GPUs and the expected duration.
- **Any workload expected to run longer than about an hour**, GPU or not.
- **`delete`** of any workload, and anything that removes files. Deleting a workload destroys
  everything in it that is not on the PVC, and cluster storage is frequently **not backed up**.
- **Writing outside the project's own directory**, or to any path that already contains data.
- **Moving data off the cluster**, or on to it from an external source.
- **Pushing a container image to a public registry** — registry repositories are often public so
  the cluster can pull without credentials, which means anything baked into the image is public.
- **Starting a file-server workspace.** The `data-transfer-copyparty` template's default
  arguments serve **the whole PVC with anonymous admin rights** (read, write, delete, over every
  group's data on a shared PVC; *verified* 2026-09-29 from `template describe`). Never submit it
  with its defaults. Override the volumes to the project's own directories, read-write without
  delete (`rw`), or read-only (`r`) for copy-backs (§6.7). Whether other pods can reach that
  unauthenticated server while it runs depends on cluster network policy (*unverified*), so keep
  it up only for the transfer.
- **A Globus transfer whose destination is outside the University.** That is data leaving
  approved infrastructure: check the project's data rules first.

**Never, regardless of instruction:**

- Ask for, accept, type or store the user's UniKey password, or any other credential.
- Put a credential where project members can read it. That includes SSH keys, API tokens and
  Globus tokens on the PVC, and secrets in `-e NAME=value`. The PVC is readable by the whole
  project, and a workload's definition probably is too (*unverified*). Where a workload genuinely needs a token, the user stores it as a Run:ai user
  credential (`[human]`), and the job references it with `--env-my-credentials` (vendor docs: CLI
  help, 2.25; *unverified* on Apollo).
- Create a Globus share or guest collection of the PVC without the user's explicit request. It
  exposes project data to people outside the project.
- Put data into a container image. Images carry code and dependencies. Data goes on the PVC.
- Submit into a project the user has not named, even if the account can see it.
- Upload research data to any external or hosted service. Research data governance usually
  restricts data to approved infrastructure; assume it does unless the user says otherwise, and
  **ask before moving anything sensitive anywhere**, including onto the cluster.

**Before any first write to storage, establish the data rules with the user.** Cluster PVCs are
commonly shared with everyone in the project and not backed up. Ask explicitly: *"is this data
approved for cluster storage that other project members can read?"* If the answer is no, or
unknown, the data stays where it is and the work is restructured around that. A "no" here is a
constraint to design within, not an obstacle to route around.

**Write the rule down as an audit you can re-run.** A project with restricted data should keep a
small read-only job that lists anything on the PVC matching its forbidden-data patterns (paths,
manifests, identifiers). Run it before any transfer and after any staging (a free CPU job, §6.2).
It turns "we agreed not to" into something checkable (used this way 2026-10-01).

---

## 5. Cost model, and how to ask for resources

**Apollo is fee-for-service.** Usage is billed against the billing codes supplied when the
project was provisioned (§7.1); rates are subsidised for USyd researchers and each Chief
Investigator has a limited free tier for trial. So GPU time is not merely a shared resource to be
polite about — it is money, and an agent that leaves a GPU held overnight has spent someone's
grant. The scheduler generally does not enforce a project's own allocation, so account for it
yourself.

- **CPU-only workloads are free** in allocation terms. Use them liberally for inspection,
  verification, data preparation and file management.
- **A GPU is consumed from the moment it is allocated**, including while the container image is
  being pulled and while an interactive session sits idle.
- **First pull of a new image can take several minutes** (≈6 minutes observed for a multi-GB
  image on a cold node) with the GPU already held. Test new images CPU-only where possible.
- **Fractional presets exist.** Sites typically publish something like `small-fraction` (10 % of
  a GPU). Use the smallest preset that can run the check; reserve whole GPUs for real training.
- **Suspend interactive sessions** the moment they go idle: `runai workspace suspend <name>`.
- **A CPU-only workspace is free against the GPU allocation, but not free to the cluster.** It
  still reserves cores and RAM on a shared node. Suspend it when idle too. Suspending is quick;
  resuming can take minutes (§6.4).
- **A crashing job can hold its GPU repeatedly.** Run:ai re-creates a failed pod even with
  `--backoff-limit 0` (§6.5). One crash loop held a GPU through five restarts (*verified*
  2026-10-01).

Keep a ledger file in the project repo — date, workload, GPUs, wall time, GPU-hours, running
total — and update it after each GPU job. Nothing else will. Take the numbers from the scheduler's
own record, not from the `describe` table:
- the allocation window comes from `describe -o json` events, from the first `Scheduled` or
  `Bound` to the terminal event;
- the size comes from the JSON's GPU fields, because the table shows **GPU Compute 1.00** for a
  0.1 fraction (*verified* 2026-09-29).

Whether SIH bills a fraction as a fraction is not confirmed, so record both fraction × wall time
and whole-GPU wall time, and say which you are quoting. `scripts/gpu_ledger.py` does this (§11).

### 5.1 Compute resources — presets first, flags second

A *compute resource* in Run:ai is a named preset describing GPUs, GPU memory, CPU cores and RAM.
Site admins maintain them, so a preset is both easier and more likely to be right than
hand-rolled flags. List what exists before choosing:

```bash
"$RUNAI" compute list -p "$PROJECT"
```

The output names each preset with its GPU devices, GPU memory and CPU memory — expect a range
from fractional (a tenth of a GPU, for debugging) through one whole GPU with a large RAM
allocation (for training) to multi-GPU and CPU-only entries. Read the CPU memory column
carefully: a preset that grants a whole GPU but only a token amount of RAM will stall a
data-heavy pipeline, and that is the most common bad pick. Attach one with `--compute <name>`.

`small-fraction` itself grants only **100M** of CPU RAM (*verified* 2026-09-29). Anything beyond a
trivial check needs `--cpu-core-request` / `--cpu-memory-request` alongside it.

**What a fraction looks like from inside.** `nvidia-smi` in a fractional pod reports only the
fraction's memory: 14,378 MiB of 143,771 at 0.1 (*verified* 2026-09-29, before the driver
upgrade). That confirms you got a fraction. A fraction is time-shared, so timings measured on one
are not throughput figures. Use a fraction to test a code path, and a whole GPU to measure speed.

Use raw flags only when no preset fits, and say why when you do:

| flag | meaning |
|---|---|
| `-g`, `--gpu-devices-request <n>` | whole GPUs |
| `--gpu-portion-request <0–1>` | a fraction of one GPU |
| `--gpu-memory-request <size>` | GPU memory instead of a fraction (`--gpu-request-type memory`) |
| `--cpu-core-request <n>` / `--cpu-memory-request <size>` | CPU cores and RAM |
| `--large-shm` | a large `/dev/shm`, needed by PyTorch DataLoader workers, in CPU jobs too |

`*-request` is what the workload is guaranteed; the matching `*-limit` flags cap what it may burst
to. Requesting a **fraction** of a GPU is the cheapest way to test an image or a code path — take
a whole GPU only when the work genuinely needs one.

**`--large-shm` whenever DataLoader workers are used, GPU or not.** Without it `/dev/shm` is 64M;
with it, 2T (*verified* 2026-09-29). A CPU job with 8 DataLoader workers and no `--large-shm` died
with (*verified* 2026-10-01):

```
ERROR: Unexpected bus error encountered in worker. This might be caused by insufficient shared memory (shm).
```

**Set thread counts to the cores you requested.** Pods have no CPU limit. In a pod requesting 8
cores, `nproc` printed 224, `/sys/fs/cgroup/cpu.max` was `max 100000`, and torch defaulted to 112
intra-op threads (*verified* 2026-10-01). One single-process feature extraction ran about 4×
slower with default threading than with one thread. The request is a guarantee, not a limit, and
the node is shared, so put this in every job command:

```bash
export OMP_NUM_THREADS=<cores> MKL_NUM_THREADS=<cores> OPENBLAS_NUM_THREADS=<cores>
```

and call `torch.set_num_threads(<cores>)` in PyTorch code. DataLoader workers already run
single-threaded.

### 5.2 Environments and templates

Two other site-maintained assets save reinventing configuration:

```bash
"$RUNAI" environment list -p "$PROJECT"   # image + tooling combinations (Jupyter, RStudio, ...)
"$RUNAI" template list -p "$PROJECT"      # pre-filled workload definitions
```

An **environment** bundles a base image with the tooling that makes it work in the UI (for
example a Jupyter environment that starts the notebook server and exposes the CONNECT button). A
**template** pre-fills a whole workload — image, compute and often storage — and is attached with
`--template <name>`; CLI flags you pass alongside it override the template's values.

Prefer a site template for the things it was built for — data transfer, Jupyter, a framework
image — because it encodes choices the admins have already debugged. Check
`Mandatory fields missing` in the template listing: templates marked as incomplete need extra
flags before they will submit.

---

## 6. Recipes

Each recipe states its actor, preconditions, cost, the command, what success looks like, and how
it fails. Variables come from `cluster.env` (§3).

### 6.1 Authenticate `[human]`

**Preconditions.** VPN connected if off campus.

```bash
runai login
```

**Expected.** `Authentication was successful`, and a cluster is selected automatically.
**Verify.** `runai whoami` prints the user's email. **Agent:** ask the user to run this; do not
attempt it.

### 6.2 Run one command against cluster storage `[agent]`

The workhorse. Inspect files, check sizes, verify a path — without leaving anything behind.

**Cost.** Free (CPU-only). **Duration.** 30–60 s including container start, longer if the node
must pull the image.

**Use the scripts when you can.** They are CPU-only by construction:

```bash
scripts/apollo_run.sh 'du -sh "$WORKDIR"/*'          # one command
scripts/apollo_script.sh check_env.sh arg1 arg2       # a local multi-line script file
```

They read `cluster.env`, and their exit status is the remote command's own. They also handle the
four traps that catch hand-written submissions:

1. **The command is parsed twice:** locally, then by the pod's `bash -c`. A `>` inside a quoted
   regex became a redirection in the pod and created a stray empty file in a shared project
   directory (*verified* 2026-09-29). The scripts send the command base64-encoded and decode it in
   the pod.
2. **A failing command loops.** Run:ai re-creates a pod whose container exits non-zero, even with
   `--backoff-limit 0` (§6.5). The scripts' container records the real exit code, prints
   `[apollo] exit <rc>`, and exits 0. The script then reads the code back from the logs.
3. **Logs race the phase.** `logs` can answer `workload is not ready to stream: pod is not ready`
   for a few seconds even after the phase is terminal (*verified* 2026-09-30 and 2026-10-01). The
   scripts retry.
4. **Cleanup must happen.** The scripts delete their own job, retry once, print the name if that
   still fails, and set a 30-minute auto-deletion backstop. `scripts/apollo_cleanup.sh` lists
   anything left over.

`apollo_script.sh` also keeps a copy of the script and a log of its output under
`$WORKDIR/logs/apollo/`, because the logs of a deleted pod are gone (§6.5). Base64 and the wrapper
add overhead, so a script over about 6,500 characters no longer fits Run:ai's 10,000-character
command limit, and the script refuses it before submitting. Larger code goes to the PVC first
(§6.6) and runs from there: `scripts/apollo_run.sh 'bash "$WORKDIR/tools/<script>.sh"'`.

**By hand, when the scripts are not available:**

```bash
NAME=peek-$(date +%H%M%S)-$$-$RANDOM                        # unique under parallel submission
CMD_B64=$(printf '%s' 'ls -la "$WORKDIR"' | base64 | tr -d '\n')
"$RUNAI" training submit "$NAME" -p "$PROJECT" \
  -i <site base image> \
  --cpu-core-request 1 --cpu-memory-request 2G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" -e WORKDIR="$WORKDIR" --backoff-limit 0 \
  --command -- bash -c "echo $CMD_B64 | base64 -d > /tmp/c.sh; bash /tmp/c.sh; echo \"[apollo] exit \$?\"; exit 0"
```

**Then poll until terminal, and only then read logs:**

```bash
"$RUNAI" training standard describe "$NAME" -p "$PROJECT" | grep '^Phase:'
"$RUNAI" training standard logs     "$NAME" -p "$PROJECT"    # retry while "not ready to stream"
"$RUNAI" training standard delete   "$NAME" -p "$PROJECT"    # your own throwaway; ask for anything else
```

**Expected.** `Phase: Initializing` → `Running` → `Completed`, then the command's stdout and the
`[apollo] exit 0` line.
**Failure modes.**
- `failed to print logs. workload is not ready to stream: pod is not ready` — **not an error.**
  The container is starting, or has only just finished. Keep polling `describe`, then retry
  `logs` a few times.
- `unknown command "logs" for "runai training"` — the sub-noun is required:
  `runai training standard logs`.
- `value for Value must have at most 10000 characters` — the command is too long. Send code
  separately (§6.6).
- `Permission denied` on a write — you are probably at the mount root; see §7.

### 6.3 Verify the GPU environment `[agent, ask first]`

The first GPU workload of a session. Confirms the device, the container identity, storage and
network in one go.

**Cost.** ~0.1 GPU-hours with a whole GPU, including the first image pull; less with a fraction.
**Ask before running.** A fraction (`--gpu-portion-request 0.1`, or the `small-fraction` preset
plus CPU flags, §5.1) is enough to check the device, the driver and a code path.

```bash
"$RUNAI" training submit gpu-check-$(date +%H%M%S)-$$ -p "$PROJECT" \
  -i <site base image> \
  --gpu-portion-request 0.1 --cpu-core-request 1 --cpu-memory-request 8G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" --backoff-limit 0 \
  --command -- bash -c 'nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv; id; df -h '"$MOUNT"'; curl -sI --max-time 10 https://huggingface.co | head -1; exit 0'
```

**Expected** (Apollo, *verified* 2026-09-22 on a whole GPU — values will differ at other sites,
and the driver changed since):

```
name, memory.total [MiB], driver_version, compute_cap
NVIDIA H200, 143771 MiB, 550.163.01, 9.0
uid=<your uid> gid=10000(dgxgroup) groups=10000(dgxgroup),...
...  5.0T  1.5T  3.6T  29% <mount>
HTTP/2 200
```

Since the October 2026 maintenance the driver should read **580.82.07** (per SIH's maintenance
notice; re-check). On a 0.1 fraction, `memory.total` reads about a tenth of the full figure (§5.1).

The `compute_cap` line matters: build GPU libraries for that architecture (9.0 = Hopper/sm_90).
The `HTTP/2 200` line means **pods have outbound internet**, so public datasets and packages can
be downloaded by the cluster directly rather than staged from a laptop. Re-check it rather than
assuming — network policy can change.

**CUDA versions after the driver upgrade.** Driver 580 runs CUDA 13.0 and older natively
(vendor docs: NVIDIA's CUDA compatibility table). So CUDA 12.x and 13.0 wheels and images need
no forward-compatibility libraries. Two consequences:
- **Do not put an image's `/usr/local/cuda/compat/lib` on `LD_LIBRARY_PATH`.** That directory
  holds a forward-compatibility `libcuda` for an older driver. NVIDIA lists every CUDA 12.x
  compat package as incompatible with driver 580. Loading one over the host driver is the
  documented cause of `CUDA error 803: system has unsupported display driver / cuda driver
  combination` (vendor docs; *unverified* on Apollo). An environment built on the old driver with
  that path first on `LD_LIBRARY_PATH` needs it removed. Check that environment with a fraction
  before its next real job.
- Only CUDA 13.1 or newer would need a forward-compatibility package again, and then only one
  NVIDIA lists as compatible with 580.

### 6.4 Interactive workspaces `[mixed — see below]`

A *workspace* is the interactive workload type: it stays up until suspended or deleted, which
makes it right for exploration and wrong for anything unattended. Two flavours.

**One-off, attached — `[human]`.** An agent cannot drive this: `--attach` implies a TTY. Give the
user the command with the values filled in:

```bash
W=shell-$(date +%H%M%S)
runai workspace submit $W -p "$PROJECT" \
  -i <site base image> \
  --cpu-core-request 2 --cpu-memory-request 8G --run-as-user \
  --existing-pvc claimname=$PVC,path=$MOUNT \
  --pod-running-timeout 10m --attach ; \
runai workspace delete $W -p "$PROJECT"
```

Exit with `exit` or Ctrl-D; the trailing `delete` then cleans up. Warn them about two surprises:
the shell opens in the container's **home directory**, not the project directory (`--working-dir`
only takes effect for non-interactive `--command` jobs, so `cd` first), and everything outside
the PVC mount is destroyed on exit.

**Reserved and reconnectable — `[agent]` if CPU-only, `[ask first]` with a GPU.** A background
workspace that holds its resources until you stop it. The command keeps the container alive
without a TTY, so an agent can create one and then work inside it:

```bash
"$RUNAI" workspace submit devbox -p "$PROJECT" \
  -i <image> --compute <preset> --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --working-dir "$WORKDIR" \
  --command -- bash -c 'trap : TERM INT; sleep infinity & wait'
```

Lifecycle:

```bash
"$RUNAI" workspace list    -p "$PROJECT"
"$RUNAI" workspace bash    devbox -p "$PROJECT"          # interactive shell (needs a TTY)
"$RUNAI" workspace exec    devbox -p "$PROJECT" -- <cmd> # run one command, no TTY needed
"$RUNAI" workspace logs    devbox -p "$PROJECT"
"$RUNAI" workspace suspend devbox -p "$PROJECT"          # release resources, keep the workspace
"$RUNAI" workspace resume  devbox -p "$PROJECT"
"$RUNAI" workspace delete  devbox -p "$PROJECT"          # ask first — destroys everything not on the PVC
"$RUNAI" workspace port-forward devbox -p "$PROJECT" --port <local>:<remote>
```

`exec` is the agent's route into a running workspace: it needs no TTY, and it runs in the
workload's `--working-dir` (verified 2026-09-22). One gotcha worth knowing before you script
around it — **`exec` does not pass the remote exit code through**. A command that exits 3 makes
`runai` exit **1**, with the real code only in stderr:

```
Error: failed to exec output. command terminated with exit code 3
```

So test success/failure on `runai`'s own exit status (0 or 1) and parse stderr if the specific
code matters; do not expect `$?` to be the remote command's.

**The agent's working loop inside a workspace, without a TTY** (*verified* 2026-09-29):
- `exec` runs in the workload's `--working-dir`. Variables from your local shell are not in the
  pod. Pass them with `-e NAME=value` at submit, or inline in the command.
- Run anything long as `nohup <cmd> > "$WORKDIR/logs/<name>.log" 2>&1 &`, then poll the log with
  short `exec`s. A dropped `exec` connection then kills nothing.
- `exec -i` cannot take piped stdin without a TTY: it fails with
  `Error: failed to exec output. inappropriate ioctl for device`. Run:ai also rejects any
  `--command` or exec payload over **10,000 characters**
  (`value for Value must have at most 10000 characters`). Together these rule out pasting code
  through stdin or a long command line. `scripts/pvc_sync_code.sh` sends files as base64 chunks
  of plain `exec` calls and checks a sha256 round trip (§6.6).

`port-forward` is a **blocking** command — it holds the terminal open while the tunnel exists, so
run it in the background and kill it when done (verified 2026-09-22):

```bash
"$RUNAI" workspace port-forward <name> -p "$PROJECT" --port 8080:8090 --address localhost &
PF=$!
curl -s --retry 20 --retry-connrefused http://localhost:8080/   # local:8080 -> container:8090
kill $PF
```

It prints `port-forward started, opening ports [8080:8090]` when the tunnel is up. `--port` may
be repeated for several ports, and `--address 0.0.0.0` requires privileges — keep it on
`localhost`.

**How long things take to start.** All *verified* 2026-09-29 to 2026-10-01 on Run:ai 2.24.
Node operating systems were updated in October 2026, which may have emptied image caches
(*unverified*).

| workload | start-up observed |
|---|---|
| training, image cached on the node (event `Pulled … already present on machine`) | 12 s to ~2.5 min |
| fractional-GPU training, image cached | 2 min 23 s in `ContainerCreating` |
| CPU workspace | 20 s fresh; ~5 min on resume; 13+ min in `Init:0/1` (twice) |
| CPU workspace with `--large-shm` and an empty `-e` variable | never started in 41 min (*observed once*, cause not established) |
| any pod on one particular node, three days running | hung at start-up with no events; resubmitting with `--exclude-node` started in ~15 s (§6.5) |

Images are cached per node. The CLI cannot pin a node; it can only exclude one
(`--exclude-node`). `node list` is denied (§8). So prefer training workloads for anything that
must start promptly. Never let a long wait pass silently while a GPU is allocated (§6.5).

**Suspend and resume — what actually happens** (verified 2026-09-22). `suspend` moves the
workspace to Phase `Stopped` and its allocation drops to zero, so it genuinely stops spending.
`resume` brings it back to `Running`. But suspend **destroys the pod and resume creates a new
one** — the pod name goes from `<name>-0-0` to `<name>-0-1` — so:

- files on the **PVC survive** a suspend/resume cycle;
- **everything in the container's own filesystem is lost**, including `/tmp`, the home directory,
  anything `pip install`ed at runtime, and any process that was running;
- `exec` against a suspended workspace fails with `pod not found` — check the Phase first rather
  than treating that as a fault.

Treat a resumed workspace as a fresh machine with the same disk attached. Anything that must
survive the cycle belongs on the PVC or in the image.

**Suspend is the discipline that protects the budget.** A reserved workspace holding a GPU spends
the allocation whether or not it computes, so suspend it the moment the work pauses, and tell the
user when one is left running. A CPU-only workspace costs nothing against a GPU allocation — that
distinction is worth stating to users who assume any idle session is expensive.

For a **UI workload** (Jupyter, RStudio, a file browser), submit it from the web UI with the
matching environment and template, or with `--template`; the browser CONNECT button is how the
user reaches it. `port-forward` is the CLI equivalent when a service needs reaching from the
user's own machine.

### 6.5 Submit a real training job `[agent, ask first]`

**Cost.** Whatever you estimate (§11) — state the GPUs, the hours and the GPU-hours when asking.
**Before any GPU job,** pass the free gates in §11: a CPU dry run on real data, a resume test,
and the code path checked on a fraction.

```bash
"$RUNAI" training submit <job-name> -p "$PROJECT" \
  -i <your image> \
  --compute <a preset from `runai compute list`> \
  --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --working-dir "$WORKDIR" \
  --large-shm -e OMP_NUM_THREADS=<cores> \
  --backoff-limit 0 --pod-running-timeout 30m \
  --command -- bash "$WORKDIR/tools/train_wrapper.sh" \
      --log "$WORKDIR/logs/<job-name>.log" --done-marker "$WORKDIR/runs/<run>/DONE" \
      -- python train.py --resume auto
```

Prefer a named `--compute` preset over hand-rolled CPU/memory flags: site admins maintain them.
`train_wrapper.sh` (from `scripts/`, copied once into the project directory, §6.6) implements the
first four rules below. If you don't use it, follow them by hand.

**Rule 1 — a crashing job must not loop.** Without care, a job whose container exits non-zero is
re-created by Run:ai, and `--backoff-limit 0` does **not** stop it (*verified* 2026-10-01, twice):
- a 1-GPU job crashed on the same batch about every 2.5 min and was re-created five times (pods
  `…-0-0` to `…-0-4`), holding the GPU throughout;
- a CPU job produced four `Error` pods 12–18 s apart.

So make the **container** exit 0 after recording the real exit code:

```bash
python train.py …; rc=$?; echo "[job] exit $rc"; exit 0
```

The scheduler then ends the job instead of looping. A preemption is an eviction, not a container
exit, so it is re-queued either way. Run:ai 2.25 also has `--restart-policy Always|OnFailure|Never`
(vendor docs: CLI help). Whether `Never` with `--backoff-limit 0` ends the loop on Apollo is
*unverified*; keep the exit-0 rule regardless. How `--backoff-limit` interacts with eviction is
also *unverified*.

**Rule 2 — the job's own log goes on the PVC.** The stdout of a re-created or deleted pod is lost
(*verified* 2026-10-01). `logs --pod=<pod>` reads one pod only while it exists. `--previous`
exists (vendor docs). The 2.25 web UI can show logs from a container's earlier instances (vendor
docs; *unverified* on Apollo). So any job whose failure you may need to diagnose writes its own
log. Put this near the top of its script:

```bash
mkdir -p "$WORKDIR/logs"; exec > >(tee -a "$WORKDIR/logs/<job>_$HOSTNAME.log") 2>&1
```

Read it afterwards with a free CPU job. An append-mode training log (JSONL) on the PVC is a
record across restarts too. Repeated sequences in such a log are what revealed rule 1.

**Rule 3 — SIGTERM must reach the program.** When the command is `bash -c '…; python train.py'`,
bash is PID 1. A preemption's SIGTERM can reach bash without reaching Python, so the
checkpoint-on-SIGTERM path never runs (*verified* 2026-10-01). Use `exec python …` when Python is
the last step. Otherwise trap TERM in the wrapper, forward it, and wait again: a trapped signal
interrupts `wait` before the child has finished checkpointing:

```bash
trap 'kill -TERM "$child" 2>/dev/null' TERM
python train.py … & child=$!
wait "$child"; rc=$?
while kill -0 "$child" 2>/dev/null; do wait "$child"; rc=$?; done   # the child's real exit code
```

Exit 143 only after a real SIGTERM, and 0 otherwise (rule 1). The time between SIGTERM and the
kill is `--termination-grace-period` (vendor docs: CLI help; Kubernetes defaults to 30 s). Set it
longer than one checkpoint write.

**Rule 4 — a finished job resubmitted is a no-op.** Write a done-marker only at the real end, and
check for it at start. Then resubmitting after maintenance (§6.8) cannot retrain a finished run.

**Rule 5 — never let a stuck start pass silently.** `--pod-running-timeout` does **not** end a
pod stuck in `ContainerCreating`. A GPU pod sat there 11 minutes, GPU allocated, past an 8-minute
timeout (*verified* 2026-10-01). After submitting a GPU job:
1. Poll the phase. If it is not `Running` within ~3 minutes and the events end at
   `Bound`/`ContainerCreating`, find the node, read-only:
   `runai training standard describe <job> -p "$PROJECT" -o json` → `podsInfo.pods[].nodeName`.
2. Report the node. Ask whether to delete the job and resubmit with `--exclude-node <node>`. That
   started pods in ~15 s (*verified* 2026-10-01). Waiting costs GPU time.

Keep `--pod-running-timeout` as a backstop for `Pending` only. Node exclusions go stale. Don't
carry an `--exclude-node` list across a maintenance window, because the nodes were updated.

**Preemption.** Check whether the job is preemptible: `describe` prints `Preemptible: true|false`
and a `Priority`. Trainings on Apollo were preemptible (*verified* 2026-09-22). So any job longer
than a few minutes must checkpoint to the PVC and resume from its newest checkpoint on restart
(§6.9). If the training code cannot resume unattended, say so before submitting. That is a code
problem to fix first, not a risk to accept silently. 2.25 has `--preemptibility` (vendor docs). A
non-preemptible training probably has to fit the project's in-quota share (*unverified*).

**Monitor:**

```bash
"$RUNAI" training list -p "$PROJECT"
"$RUNAI" training standard describe <job> -p "$PROJECT"   # Phase, Pods, and an Events table
"$RUNAI" training standard logs     <job> -p "$PROJECT" --follow
"$RUNAI" training standard exec     <job> -p "$PROJECT" -- nvidia-smi   # works on a running training pod
```

**Check the GPU in the first ten minutes of every long job** (`exec … nvidia-smi` *verified*
2026-09-29). A 1-GPU run measured 35–51 % utilisation with its main Python process at 100 % of
one core — paying for an H200 to wait on the host. `scripts/gpu_watch.sh` samples it and names
the bottleneck (§11).

When a workload sits in `Pending`, the **Events** table in `describe` gives the reason (quota,
scheduling, image pull). Read it before speculating.

### 6.6 Dependencies — Python packages, environments and images `[mixed]`

Containers are disposable, so a `pip install` inside a workload disappears with it. There are
three durable answers, and the right one depends on how often the work will run.

**A. Install into the PVC — no Docker needed `[agent]`.** Fastest iteration, and the only option
when the user has no Docker or no registry account. Put the virtual environment on the PVC so it
outlives the container:

```bash
"$RUNAI" training submit setup-env-$RANDOM -p "$PROJECT" \
  -i <site base image> --cpu-core-request 4 --cpu-memory-request 16G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" --working-dir "$WORKDIR" \
  --command -- bash -c 'python3 -m venv venv && . venv/bin/activate && pip install -r requirements.txt'
```

Later workloads activate it: `--command -- bash -c '. venv/bin/activate && python train.py'`.

**Verified end to end, 2026-09-22.** One workload created the venv and `pip install`ed into it; a
second, separate workload — a different pod, no setup of its own — activated it and imported the
package successfully. `which python` resolved to the PVC copy in both. So a venv on the PVC is
genuinely reusable across workloads, which is what makes this route viable.

Three things to get right, or this bites:

- **The venv is bound to the image's Python.** It records the interpreter path (in the test the
  base image's Python 3.12.3), so changing the base image can break it. Rebuild the venv when the
  image changes, and keep the `requirements.txt` that reproduces it under version control.
- **Put the caches on the PVC too**, or every workload re-downloads multi-gigabyte model weights:

  ```bash
  -e HF_HOME="$WORKDIR/.cache/hf" -e PIP_CACHE_DIR="$WORKDIR/.cache/pip" \
  -e TORCH_HOME="$WORKDIR/.cache/torch" -e XDG_CACHE_HOME="$WORKDIR/.cache"
  ```

  (`-e` / `--environment-variable name=value`, repeatable.)
- **Imports come from a network filesystem**, which is slower than an image layer. A trivial
  pure-Python import measured 0.06 s, so the overhead is not inherently alarming — but that says
  nothing about a large framework with thousands of files, which is where the cost actually
  lands. If job startup looks slow, this is the first thing to suspect, and route B is the fix.

This route cannot install **system** packages — no `sudo` — so anything needing `apt-get` goes to B.

**Route A on an NVIDIA NGC framework image** (e.g. `nvcr.io/nvidia/pytorch:<yy.mm>-py3`). These
images ship a tuned torch, but they pin it in ways a venv inherits unless you undo them. Both
behaviours below were *verified* 2026-09-29 on `pytorch:25.06-py3`:
- **The image pins its own torch** through `PIP_CONSTRAINT=/etc/pip/constraint.txt`. A venv that
  needs a *different* torch must clear it (`PIP_CONSTRAINT=` for the install). Otherwise pip
  resolves against the image's version.
- **The image puts its torch's `lib/` first on `LD_LIBRARY_PATH`.** A venv with its own torch
  must set `LD_LIBRARY_PATH` explicitly, or it loads the image's libtorch. Do **not** include the
  image's `/usr/local/cuda/compat/lib` since the driver upgrade (§6.3).
- **Probe the image first, in a free CPU job:** Python version, torch and CUDA versions,
  `env | grep -i -e pip -e ld_library`, and which tools are missing. That image had no `ffmpeg`,
  for instance.
- **Build in a CPU job,** with the caches on the PVC. `pip freeze` into a lock file kept in the
  project's repository. Rebuild when the image changes.
- **Then test once on a fraction** (ask first): import torch and run one CUDA operation. A CPU job
  cannot test CUDA initialisation.

**B. Build a custom image `[agent builds, human logs in]`.** The durable, reproducible answer,
and the one to use for anything that will run more than a few times or that someone else must
reproduce. Start `FROM` the site's published base image so its tooling still works:

```dockerfile
FROM <site base image>:latest
# System packages must be installed here — there is no sudo at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends <packages> \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```

```bash
docker login                                  # [human], once
docker buildx build --platform linux/amd64 -t <user>/<image>:v1 . --push
```

- **`--platform linux/amd64` is not optional** when building on an Apple Silicon machine: the
  cluster is x86-64, and an arm64 image fails at pull or run.
- **Tag versions (`v1`, `v2`), not `latest`.** Nodes cache images, so a moving `latest` gives
  irreproducible runs and confusing staleness; an explicit tag also lets you roll back.
- **Registry repositories are usually public** so the cluster can pull without credentials.
  Nothing but code and dependencies goes in — no data, no credentials, no licence-restricted
  weights.
- A first pull of a large image costs minutes on each new node, **with any requested GPU already
  allocated**. Keep images lean, and test new ones CPU-only.

**C. Register it as an environment or template `[human / admin]`.** Environments and templates
are assets that make an image selectable in the web UI. The CLI is **read-only** here —
`runai environment list|describe` exist, but there is no `create` — so publishing one is a UI or
administrator action. For Jupyter, the documented pattern is to pick the site's
`jupyter-notebook` environment and **replace the image URL** with your own image built FROM their
JupyterLab base; the notebook server then still starts correctly.

**Choosing:** iterate with A, freeze into B once the dependency set settles, and ask for C only
when other people need to launch the same thing from the UI.

**Getting your own code onto the PVC.** Jobs run code that is already on the PVC, or in the image.
In order of preference:
1. **`git clone` / `git pull` inside a free CPU job,** when the repository is reachable from the
   cluster without credentials. Pods have outbound internet (§6.3). Run:ai 2.25 can also mount a
   repository with `--git-sync` (vendor docs: CLI help; *unverified* on Apollo).
2. **Globus** for a directory tree (§6.7).
3. **`scripts/pvc_sync_code.sh <workspace> <dest> <paths…>`** into a running CPU workspace. It
   takes code and config files only, sends them as base64 chunks under the 10,000-character
   limit, and checks a sha256 round trip.

A container image is the wrong carrier for code you are still changing: every change needs a new
tag and a fresh pull on each node (§6.6 B). And never paste a large script into `--command`
(§6.4).

**Keep a `tools/` directory in the project's PVC directory** with the few scripts that must run
*inside* jobs. Copy them there once by any route above: `scripts/train_wrapper.sh` (§6.5) and
`scripts/manifest.py` (§6.7).

### 6.7 Move data: choose a route, check before, verify after `[mixed — see each route]`

Moving data on to or off the cluster is ask-first, and the project's data rules decide what may
move at all (§4). Never hold a GPU during a transfer.

**Choose the route:**

| route | use when | who does what | cluster cost | integrity |
|---|---|---|---|---|
| **Globus** | the default for anything sizeable: RDS ↔ PVC, another institution ↔ PVC, a laptop with Globus Connect Personal ↔ PVC | user: `globus login` and consent once; agent: submit, wait, report | no workload needed (*unverified*) | Globus verifies checksums (vendor docs); re-runs with `--sync-level checksum` send only what changed |
| **sftp from the data-transfer workspace** | RDS when Globus isn't set up for the user, or a handful of files | agent: commands, checks, verification; user: types the RDS password | a CPU workspace: free, but suspend it afterwards | `manifest.py check` |
| **copyparty workspace** (fallback) | laptop ↔ PVC with no RDS and no Globus Connect Personal | agent end to end, ask-first | a CPU workspace, only for the transfer | `manifest.py`; resume by size |
| **download inside a pod** | public datasets and models | agent | a CPU job | the publisher's checksums |

**Before any transfer:**
1. **The data rules** (§4). For restricted data, run the project's audit job first.
2. **Free space.** Run `scripts/apollo_run.sh 'df -h "$MOUNT"; du -sh "$WORKDIR"'`. A DashR
   project's PVC defaults to 1 TB, and it is shared by the whole project.
3. **Source size and file count,** stated when asking.
4. **A source manifest,** built wherever the source is readable: the laptop, RDS mounted as a
   network drive, or a pod for PVC → elsewhere. It travels with the data, so the destination can
   be checked against it.

   ```bash
   scripts/manifest.py build <source dir> -o <name>.manifest            # path, size, sha256
   scripts/manifest.py build <source dir> --no-hash -o <name>.manifest  # sizes only: fast for huge trees
   ```

**After it:**
1. **Verify at the destination.** On the PVC, run `manifest.py` from the project's `tools/`
   directory (§6.6) in a free CPU job:
   `scripts/apollo_run.sh 'python3 "$WORKDIR/tools/manifest.py" check "$WORKDIR/data/<name>" "$WORKDIR/data/<name>.manifest"'`.
   It reports missing, changed and unexpected files, and exits non-zero on any mismatch. Its
   summary `digest` lets two sides be compared by one string.
2. **Report** the route, the Globus task ID or the commands, files and bytes moved, and the
   verification result (§10).
3. **Suspend or delete any transfer workspace** (deleting is ask-first).
4. **Results going back leave the PVC the day they are made** (§7.3).

#### (a) Globus `[human-then-agent]`

The user signs in once (`globus login`; and `globus session consent …` if the CLI asks for it).
After that the agent can drive transfers with the IDs recorded during discovery (§3):

```bash
# dry run: prints both collections' names, the paths, and the exact command; submits nothing
scripts/globus_xfer.sh "$GLOBUS_RDS_COLLECTION:/<path on RDS>/" "$GLOBUS_PVC_COLLECTION:$GLOBUS_PVC_PREFIX/data/<name>/"
# after the user approves
scripts/globus_xfer.sh --yes "$GLOBUS_RDS_COLLECTION:/<path on RDS>/" "$GLOBUS_PVC_COLLECTION:$GLOBUS_PVC_PREFIX/data/<name>/"
```

The script refuses a destination outside the collections in `cluster.env` unless
`--allow-external` is given, which needs the user's explicit OK (§4). It prints the task ID at
once, waits, and exits 0 only when the task `SUCCEEDED`. When it times out, the task keeps
running at Globus: check it with `globus task show <id>` or in the web app. Without the script,
the same transfer is:

```bash
globus transfer SRC_ID:/path/ DST_ID:/path/ --recursive --sync-level checksum --label <label>
globus task wait <task id> --timeout 3600
globus task show <task id>
```

The user can equally start the transfer from the Globus web app. The agent then does only the
checks before and after. Two things are *unverified* until checked live:
- the ownership and mode of files Globus writes on a shared PVC (check with `ls -ln` in a CPU job
  after the first transfer, §7);
- whether a Globus transfer needs any Run:ai workload at all.

#### (b) RDS by sftp from the data-transfer workspace `[agent prepares, human types the password]`

The site's `data-transfer-workspace` template starts a CPU workspace with a terminal (see the
"transfer data" page of the onboarding guide; run `template describe` first, §3). The password
prompt is the user's, so the agent writes the commands and hands them over:

```bash
# in the data-transfer workspace terminal
cd <pvc destination>
sftp -r <unikey>@research-data-ext.sydney.edu.au:/rds/PRJ-<shortcode>/<path> .
```

If a large transfer is interrupted, resume the partial files instead of starting again. OpenSSH
`sftp` has `reget` and `reput` (*unverified* against RDS):

```bash
sftp <unikey>@research-data-ext.sydney.edu.au <<< "reget -r /rds/PRJ-<shortcode>/<path> <pvc destination>"
```

Whether `rsync` works against RDS is *unverified*. If it does, `rsync -a --partial` is better for
large or repeated transfers. Never automate this by storing a password or SSH key on the PVC
(§4). Afterwards: verify with the manifest, then suspend the workspace.

#### (c) Laptop ↔ PVC through a copyparty workspace `[agent, ask first]` — fallback

Use this only when there is no RDS and no Globus Connect Personal. It needs no credentials, which
is exactly why it must be restricted:
1. `runai template describe data-transfer-copyparty -p "$PROJECT" -o yaml` shows the default
   arguments, `--chdir /tmp -v /w:/:A --theme 1`: the **whole PVC, anonymous admin** (§4).
2. Submit with the volumes overridden to the project's own directories, read-write without
   delete. The arguments after `--` are copyparty's. This shape worked on 2026-09-29:

   ```bash
   "$RUNAI" workspace submit xfer-$(date +%m%d) -p "$PROJECT" --template data-transfer-copyparty \
     --existing-pvc "claimname=$PVC,path=/w" --run-as-user \
     --cpu-core-request 4 --cpu-memory-request 8G \
     -- --chdir /tmp -v /w/<project dir>/incoming:/in:rw --theme 1
   ```

   `small-fraction`-style presets grant too little RAM for this (§5.1). Expect a slow start: the
   workspace sat in `Init:0/1` for ~13 minutes, twice (*verified* 2026-09-29).
3. Tunnel to it in the background: `"$RUNAI" workspace port-forward <name> -p "$PROJECT" --port
   <local>:<copyparty port from the template> --address localhost &` (§6.4).
4. Upload with `scripts/pvc_upload.py`. It takes a TSV of `local<TAB>remote` paths and one or
   more `--allow-root` directories, and refuses anything outside them, symlinks included. It is
   a dry run by default. Send `--one-file --yes` as a route test, then `--yes` for the rest. It
   resumes by skipping remote files of equal size.
5. Verify with the manifest, then suspend or delete the workspace. Its server is unauthenticated
   while it runs.

What copyparty does on the PVC (all *verified* 2026-09-29):
- **It never overwrites.** Without delete permission, a second PUT to an existing name stores a
  renamed copy. So `pvc_upload.py` reports a size mismatch for a human decision rather than
  re-sending.
- **It leaves a `.hist/` index in every directory it serves.** Report it; `manifest.py` ignores
  it.
- Directories it creates come out `750` and files `660` (§7).

**Results back to a laptop** use a second workspace serving read-only (`:r`), and
`scripts/pvc_download.py`. That script writes `.part` files, checks each against a `manifest.py`
manifest built on the cluster, and only then renames them; a mismatch is deleted and reported. A
read-only copyparty returned ~1 GB sha256-identical this way (*verified* 2026-09-29). A workspace
serves only the volumes fixed when it was submitted (*verified* 2026-10-01). So export every
run's artefacts *and* its training record into one `release/<run>/` tree, and one read-only
workspace can serve every later copy-back.

#### (d) Public data `[agent]`

Download from inside a CPU job, straight to the PVC. Never round-trip through the user's laptop.
Check the publisher's checksums where they exist, and keep the download command: the PVC copy is
not durable (§7.3).

### 6.8 Planned maintenance — before, during, after `[mixed]`

SIH takes the cluster down for updates; the October 2026 window upgraded Run:ai, the driver and
the nodes' OS. SIH's notice asked users to checkpoint long jobs, plan around the window, move data
they need during it to RDS, and resubmit incomplete jobs afterwards. Ask the user where SIH
announces windows. The channel is *unverified*; the October notice reached users directly.

**Before the window:**
- **Compare every submission with the window.** Take the job's expected end (duration, plus
  start-up §6.4, plus a margin). Don't start a GPU job or a long transfer that would still be
  running when the window opens, unless it checkpoints and someone will resubmit it. Say so when
  asking to submit.
- **Assume maintenance ends workloads rather than pausing them.** Whether Run:ai re-queues,
  fails or deletes running workloads during an update is *unverified*. Preemption auto-resume
  (§6.9) can't be relied on, so plan a manual resubmit.
- **Record what is in flight.** For each workload: its name, the exact submit command, the
  experiment directory, and the newest checkpoint step. `scripts/apollo_snapshot.sh save` saves
  the `training list`, the `workspace list` and each `describe` to a local, timestamped
  directory; it is read-only and free. The logs of deleted pods are lost (§6.5), so the job's own
  log on the PVC is the record.
- **Move anything out of container filesystems.** A workspace's `/tmp`, home directory and
  runtime `pip install`s go with its pod. Only the PVC survives (§6.4).

**During the window:**
- **The PVC is unreachable, not just at risk.** It can only be read from inside a workload, and
  there are none. Before the window, copy whatever the user will need during it to durable
  storage: results, the checkpoint they will evaluate, training records. Use RDS where the project
  has it (Globus, §6.7), otherwise the user's own machine. The data rules still decide what may
  leave.
- **Copy off any checkpoint that would be expensive to lose.** The PVC is not backed up, and a
  storage-level update is a plausible way to lose one (*unverified*).
- **Agent behaviour:** cluster commands fail during the window. Don't retry in a loop, and don't
  treat it as an auth problem (don't ask for `runai login`; §2). Tell the user the cluster is in
  maintenance, and stop the cluster side of the work. The exact error during a window has not
  been captured yet; record it verbatim when you see one.

**After the window:**
1. **Re-run discovery (§3),** including `runai version`, presets and templates. Drop any
   `--exclude-node` list: the nodes changed.
2. **Check storage and environment before any GPU job:**
   - the working tree is intact;
   - the data audit passes (§4);
   - the PVC venv still imports in its image, in a CPU job;
   - after a driver change, one CUDA operation runs on a fraction (§6.3, ask first).
3. **Classify every workload recorded before the window.** Run `scripts/apollo_snapshot.sh
   compare <dir>`, then check the PVC:
   - **finished:** its done-marker is present (§6.5 rule 4); nothing to do;
   - **interrupted:** no done-marker; note the newest checkpoint step;
   - **never started.**
4. **Resubmit the interrupted ones** with the same command. This is safe only if the job resumes
   from its newest checkpoint and a done-marker turns a finished run into a no-op; say so if it
   does not. Use a fresh name, or ask before deleting the old workload object. Resubmitting GPU
   jobs is still ask-first: one message listing each job, its GPUs and its remaining duration.
5. **Report** which jobs resumed from which step, and the GPU-hours lost to recomputing steps since
   the last checkpoint (§10).

### 6.9 Long jobs — checkpoint, resume, finish cleanly `[agent, ask first]`

Two things end long jobs on Apollo: **preemption** (trainings are preemptible, §6.5) and
**maintenance** (§6.8). Auto-resume covers preemption only if Run:ai re-queues the same workload;
maintenance needs a resubmit. The same discipline makes both safe. The framework-neutral detail,
with snippets, is in `GPU_EFFICIENCY_GUIDE.md` ("Guards in training code"); in short:
- **Checkpoint to the PVC** at a fixed step cadence *and* on SIGTERM. Write to a temporary file,
  then rename.
- **Resume from the newest checkpoint that loads,** skipping a truncated one.
- **Save everything that decides the next step:** model, optimiser, LR scheduler, AMP scaler,
  sampler position, every RNG state, and any module attribute that changes during training but is
  not in `state_dict()`. A resume test caught one of those (*verified* 2026-09-29). Restore the RNG
  *after* creating the DataLoader iterator, because creating it draws a seed.
- **Finish correctly.** Exit 143 only on a real SIGTERM, and 0 otherwise (§6.5 rules 1 and 3).
  Write the done-marker only at the real end. Keep a best checkpoint outside keep-last pruning.
- **Test before the first long job, on CPU, for free:**
  1. Run a tiny model uninterrupted, then the same run SIGTERM'd mid-step and resumed. The final
     weights should match to ~1e-6.
  2. Resubmit the finished run: it must exit at once without training.
  3. Do a short dry run on the real data in a CPU job.
  The first and third each caught real bugs (*verified* 2026-09-29).

`scripts/train_wrapper.sh` provides the signal forwarding, the exit codes, the log on the PVC and
the done-marker check. The checkpoint logic itself belongs in the training code.

---

## 7. Storage — where a PVC comes from, and the trap

There are two ways a project ends up with storage on Apollo. The first is the intended route and
gives the project its own PVC; the second is what you get when you join someone else's project,
and it has a permission trap that catches nearly everyone.

### 7.1 Default route — a DashR project provisions its own PVC `[human]`

Access to Apollo is granted **per DashR project**, not per person, and the storage follows
automatically. This is a Chief Investigator / project-administrator action in the Researcher
Dashboard, so an agent cannot perform it — surface these steps to the user and wait.

1. Go to <https://dashr.sydney.edu.au/>. Use an existing DashR project if one suits, or create a
   new one there — SIH explicitly suggests a new project if none of the existing ones are a good
   fit for managing a group's Apollo access.
2. In that project, open the **Data Services** tab and enable **Apollo GPU Cluster** under
   **Computing Platforms** ("Add details" after toggling it on).
3. Supply **billing codes — mandatory.** Apollo is fee-for-service; rates are subsidised for USyd
   researchers and each Chief Investigator has a limited free tier for trial. See the "Apollo –
   rates and billing" page in the Research Computing wiki.
4. Complete the **onboarding form**.

Then the provisioner does the rest: it creates a Run:ai project named after the DashR shortcode,
registers the Active Directory groups that make SSO work, adds every member of the DashR project,
and **provisions a PVC with the project** (1 TB by default per the SIH onboarding guide), which
appears as a `pvc` data source in Run:ai.

Four things to warn the user about before they start:

- **Everyone in the DashR project gets in.** All current members will be able to log into the
  cluster and use its shared compute *and storage*. Membership is the access control — decide who
  is in the project before enabling Apollo, not after.
- **Project shortcode naming.** Run:ai requires a project name beginning with a lower-case
  letter. A DashR shortcode that does not comply (for example, one consisting only of digits) can
  make the provisioner fail during project creation.
- **Sanctioned countries.** If any project member is from a sanctioned country this must be
  disclosed to SIH, and it limits the compute the project can access.
- **Provisioning is not instant.** Until it finishes, logging in at <https://gpu.sydney.edu.au/>
  may show a "you do not have the required permissions" error. That is expected — wait and retry
  rather than raising a fault.

Verify it landed with the §3 discovery commands: the project appears in `runai project list` and
its PVC in `runai pvc list -p <project>`. If no PVC data source shows up, contact SIH — one
should have been created with the project.

### 7.2 Alternative — a shared PVC in an existing project

If the user is a member of a project that already exists (a lab's or a facility's), they inherit
its PVC rather than getting their own. Expect this shape, and expect the mount root to be
**shared with everyone in the project**:

```
<mount root>            root-owned, group-readable, NOT writable by members
└── <your directory>/   created by an admin, writable by the project group
```

So `mkdir` at the mount root fails with `Permission denied`, while writing one level down
succeeds. If the project has no directory for this work yet, **an administrator must create it** —
members cannot, and there is no `sudo`. Ask the site support desk for a directory owned by the
project group and group-writable (mode `2775` keeps group ownership on files created inside it).

**What you write gets your uid and your primary group.** That holds for files written by pods
run with `--run-as-user`. Directories created by a copyparty transfer came out `750` and files
`660` (*verified* 2026-09-29): group members could read but not add to them. For working
directories other members must write to, set `umask 002` in the job's command, or fix the modes
after a transfer (`chmod -R g+w`, in a CPU job, on your own files only). The mode of files written
by Globus is *unverified*; check it with `ls -ln` after the first transfer.

Three consequences worth stating to users who expect a normal filesystem:

1. **Everything written is readable by the whole project.** Plan data placement accordingly, and
   see §4 on establishing the data rules before the first write.
2. **It is not backed up.** Treat it as working space, never as the only copy.
3. **You cannot browse it from a laptop.** It is network storage inside the cluster; the only way
   to read it is from inside a workload (§6.2) or a file-browser workspace.

If that isolation matters — one group's data invisible to another's — the answer is §7.1: a
separate DashR project, which brings a separate Run:ai project and its own PVC.

### 7.3 Retention — the 30-day rule

Apollo scratch storage is **free, temporary and not backed up**, and it is governed by an
explicit retention policy: users are **required to delete unused files within 30 days of job
completion**, and SIH reserves the right to delete inactive data after 30 days.

Two practical consequences for an agent:

- **Copy anything worth keeping to the Research Data Store the day it is produced** — trained
  models, exported artefacts, result tables. RDS is redundantly backed up and is the intended
  long-term home; the PVC is not. Globus is the usual route (§6.7). A project without RDS copies
  results to the user's own machine (§6.7 c), then on to whatever durable storage they use.
- **Do not treat a dataset staged on the PVC as durable.** A dataset that took hours to download
  may not be there next month, so keep the command that reproduces it, and re-check before
  assuming a path still exists.

When a piece of work finishes, offer to clean up its scratch data rather than leaving it — the
policy is a user obligation, not just a housekeeping suggestion.

## 8. Known limits and denied commands

Newly onboarded users typically hold a restricted researcher role. These fail by design — do not
retry them or conclude the cluster is broken:

- `runai node list`, `runai nodepool list` — **insufficient permissions**. Cluster-wide hardware
  inventory must come from the site's support desk.
- Creating or reconfiguring projects, quotas and environments — administrator-only. Membership is
  managed upstream, through DashR by the project's Chief Investigator or administrator (§7.1),
  not in Run:ai.

**Not covered by this guide:** inference serving (`runai inference …`, which runs on Knative) and
multi-node jobs over InfiniBand. Both exist in Run:ai 2.25 (vendor docs). Whether researcher
accounts may use them on Apollo is *unverified*; ask SIH before planning around either.

**Support is a request form, not an email address.** SIH takes Apollo enquiries through the
Research Computing support form linked from the
[Apollo GPU cluster wiki page](https://sydneyuni.atlassian.net/wiki/spaces/RC/pages/3579674625).
When a step needs an administrator — a directory created, a quota raised, a data source added —
tell the user to raise it there, with the exact request spelled out for them to paste.

**Acknowledge the facility in publications.** SIH asks for: *"The authors acknowledge the use of
the Apollo GPU cluster, a service provided by the Sydney Informatics Hub, a Core Research
Facility of the University of Sydney."* If the work being run leads to a paper, remind the user.

---

## 9. Error → cause → fix

| message | cause | fix |
|---|---|---|
| `runai: command not found` | the binary is not on a non-interactive shell's PATH | use the absolute path (`$HOME/.runai/bin/runai`) |
| any auth / 401 error, e.g. `Error: failed to get email and ID from token. Authentication failed. the token has expired` | the SSO token expired | **stop**; ask the user to run `runai login` |
| `Error: failed to submit. Error: URL is incorrect or does not exist. DNS lookup failed for '<host>': no such host` | the VPN is down (`whoami` still works from its cache) | **stop**; ask the user to reconnect the VPN (§2) |
| `workload is not ready to stream: pod is not ready` | container still starting — or just finished (a race seen for a few seconds after a terminal phase) | poll `describe` until Phase is terminal, then retry `logs` a few times |
| `Error: failed to submit standard training. Value: value for Value must have at most 10000 characters` | the `--command` is over Run:ai's limit | send code separately (§6.6); `apollo_script.sh` checks this first |
| `Error: failed to exec output. inappropriate ioctl for device` | `exec -i` with piped stdin and no TTY | plain `exec` calls in chunks: `pvc_sync_code.sh` (§6.4) |
| `Error: failed to exec output. command terminated with exit code N` | the remote command failed; `runai` itself exits 1 | read N from stderr (§6.4) |
| `Operation cannot be fulfilled on podgroups.scheduling.run.ai "…": the object has been modified` (PodGrouperWarning in events) | benign; seen on nearly every submission (*verified* 2026-09-29) | ignore |
| pods `<job>-0-1`, `-0-2`, … appearing after an `Error` | the container exited non-zero and Run:ai re-created it, `--backoff-limit 0` notwithstanding | stop the job (ask); make the container exit 0 after logging its code (§6.5 rule 1) |
| pod in `ContainerCreating` or `Init:0/1` for many minutes, events stop at `Bound` | a slow image pull, or a stuck node; `--pod-running-timeout` does not end it | GPU job: report the node, offer `--exclude-node` (§6.5 rule 5); CPU: wait, or use a training job (§6.4) |
| `ERROR: Unexpected bus error encountered in worker. This might be caused by insufficient shared memory (shm).` | DataLoader workers with the default 64M `/dev/shm` | resubmit with `--large-shm` (§5.1) |
| `CUDA error 803: system has unsupported display driver / cuda driver combination` | a forward-compat `libcuda` loaded over the newer host driver (vendor docs) | remove `/usr/local/cuda/compat/lib` from `LD_LIBRARY_PATH` (§6.3) |
| `unknown command "logs" for "runai training"` | missing sub-noun | `runai training standard logs` |
| `unknown flag: --project` on some subcommands | flag name varies per subcommand | use `-p`, and check `--help` for that subcommand |
| `required flag(s) "type" not set` | `datasource describe` needs `--type pvc` | add `--type pvc` |
| `Permission denied` creating a directory | writing at the shared mount root | write under the project's own directory (§7) |
| `sudo: command not found` | by design in cluster containers | move the step into the Dockerfile |
| `insufficient permissions` on `node`/`nodepool` | restricted researcher role | ask the site support desk (§8) |
| workload stuck `Pending`, no pod | quota or scheduling | read the **Events** table in `describe` |
| image pull error on a fresh image | built for the wrong architecture | rebuild with `--platform linux/amd64` |

---

## 10. Reporting back to the user

When work is done, report what was actually observed rather than what was expected:

- the workload names created and their final phase;
- **GPU-hours consumed**, and the running total against any allocation — saying how fractions
  were counted (fraction × wall, or whole-GPU wall; §5);
- the GPU utilisation observed in each long job's first minutes, and the verdict (§11);
- what was written to shared storage, and where — including anything a tool left behind, such as
  copyparty's `.hist/` index directories or a probe file;
- every transfer: the route, the Globus task ID or the commands, files and bytes moved, and the
  verification result (§6.7);
- anything still running or suspended — CPU workspaces included — and if something is holding a
  GPU, say so first.

If a step failed, quote the error. If a step was skipped, say which and why. An agent that
reports a clean run it did not verify is worse than one that reports a messy one accurately —
the user is accountable for this allocation and this data, and can only exercise that
responsibility with an honest account of what happened.

---

## 11. Using GPU time well on Apollo

Most GPU money is lost to GPUs that are **held but not computing**. A job stuck starting, a crash
loop, an idle interactive session or a job waiting on its host all count, and they cost more than
slow kernels. Every case in the October 2026 sessions was caught by a check or a measurement, not
by a guess. This section is the Apollo procedure. [`GPU_EFFICIENCY_GUIDE.md`](GPU_EFFICIENCY_GUIDE.md)
has the cluster-agnostic detail: diagnosis, the fix catalogue and guards in training code.

**Principles.**
- GPU time is money (§5).
- Measure before fixing. Two plausible bottleneck guesses were both wrong, and measurement found
  the real one.
- An unspent budget is not a reason to run.
- Decide the stop rule and the evaluation before the run, and never tune on the evaluation set.

### 11.1 Free gates before any GPU job

| gate | how | cost |
|---|---|---|
| the code runs on real data | a short dry run in a CPU job (`scripts/apollo_script.sh`) | free |
| it resumes exactly | SIGTERM a tiny CPU run mid-step and resume; weights match to ~1e-6 (§6.9) | free |
| every epoch yields batches | print batches per epoch and the padding ratio in the dry run. A bucketing sampler with `drop_last` once yielded **zero** batches per epoch, with no error (*verified* 2026-09-30) | free |
| the input pipeline can keep up | `scripts/loader_bench.py` in a CPU job: units/s the loader can deliver | free |
| the GPU code path works | one short job on a fraction — a correctness check, not a speed figure (§5.1) | ask first; ~0.05 GPU-h |
| throughput, for the estimate | ≤ 15 minutes on one whole GPU with the input cache warm | ask first |

### 11.2 Estimate, then ask

```bash
scripts/gpu_ledger.py estimate --units <per epoch> --epochs <n> --throughput <units/s measured> --gpus 1
```

Wall time = units × epochs ÷ throughput × (1 + validation share) + start-up. State GPUs, hours and
GPU-hours when asking, and for a fraction both fraction × wall and whole-GPU wall (§5).

### 11.3 Watch the first ten minutes `[agent, free]`

Run this about 5 minutes into every long job, and again at about 10. It is read-only and holds
nothing:

```bash
scripts/gpu_watch.sh -n 12 -i 5 --train-log "$WORKDIR/logs/<job>.jsonl" \
  -- "$RUNAI" training standard exec <job> -p "$PROJECT" --
```

It samples `nvidia-smi` (utilisation, memory, power) and `top` through `exec`, reads the tail of
the job's training log if there is one, and names the bottleneck. Without the script, run
`"$RUNAI" training standard exec <job> -p "$PROJECT" -- nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,power.draw --format=csv`
a few times, plus `top -bn1`, and read them against this table:

| verdict | what it looks like | action |
|---|---|---|
| `stuck-starting` | no samples: the pod is not running, or no GPU is visible | if events stop at `Bound`/`ContainerCreating`, report the node and offer `--exclude-node` (§6.5 rule 5) |
| `restart-loop` | the job has more than one pod (`-0-1`, `-0-2`, …) | stop it (ask); read its PVC log; exit-0 rule (§6.5 rule 1) |
| `nonfinite-streak` | the training log shows consecutive NaN/inf losses | stop it (ask); fix the numerics before spending more |
| `idle-held` | utilisation ≈ 0 throughout | nothing is using the GPU: suspend or stop the workload |
| `input-bound` | utilisation < 60 %, high data-wait share or busy loader workers | more workers, faster storage, precomputed inputs |
| `host-bound` | utilisation < 60 %, one process at ~100 % of a core, little data wait | remove host-device syncs, bigger batches, compile |
| `bursty` | utilisation swings between idle and full, little data wait | batch composition or per-step host work: profile the step |
| `gpu-bound` | utilisation ≥ 85 % | the GPU is busy: tune batch size and precision, then leave it |
| `unclear` | in between | measure: `step_profile.py`, `loader_bench.py` |
| `memory-headroom` (advisory) | peak memory < 25 % of the GPU | a smaller fraction, or a bigger batch |

The reference case is a 1-GPU job at 35–51 % utilisation, ~200 W of 700 W, 17 of 141 GB, with its
main Python process at 100 % of one core (*verified* 2026-09-29). That is `host-bound` with
`memory-headroom`: an H200 paid for to wait on the host. Watch whole-GPU jobs. On a fraction, the
utilisation probably includes the other workloads sharing the GPU (*unverified*).

### 11.4 Diagnose by measuring, then fix

Read the cluster-agnostic guide's "Diagnose" and "Fix catalogue" sections. On Apollo, the
measurements cost nothing when they run in CPU jobs:
- `scripts/loader_bench.py` gives input capacity, with the thread environment reported (§5.1);
- `scripts/step_profile.py` gives per-phase step time and top-level op counts;
- a data-wait share in the training log separates input-bound from host-bound.

### 11.5 Account for every hour

After each GPU job, append a ledger row from the scheduler's own record (§5):

```bash
"$RUNAI" training standard describe <job> -p "$PROJECT" -o json --pod-limit -1 --event-limit -1 > describe.json
scripts/gpu_ledger.py add runai --json describe.json --purpose "<why this ran>"
```

`--pod-limit -1 --event-limit -1` matters: `describe` shows at most 10 pods and 50 events by
default (vendor docs: CLI help), and a crash loop needs every pod counted. The ledger records both
fraction × wall and whole-GPU wall, and prints which JSON fields it used. Its field paths come
from captures made before the 2.25 upgrade and are partly *unverified*; if it reports an
unrecognised shape, fall back to `gpu_ledger.py add manual`.

### 11.6 Spending strategy

- Run the cheapest gated experiment first, and stop early on a failed gate.
- Run parallel short jobs only when the project's quota allows, and gate each one independently.
- An allocation left at the end of a grant is not a reason to run anything.

**Free:** everything in 11.1 except the last two rows, plus 11.3, 11.4 (in CPU jobs) and 11.5.
**Ask first:** any GPU request, stopping or deleting a job, and resubmitting with
`--exclude-node`.

---

## Appendix A. Agent-side pitfalls

Not Apollo's fault, but each cost a session real time (*verified* 2026-10-01 unless noted):
- **macOS's default shell is zsh.**
  - `for f in $LIST` and `set -- $pair` do not word-split (use `${=LIST}`, or put the logic in a
    bash script).
  - A word starting with `=` is expanded as a command path, so quote it.
  - `cat -A` doesn't exist; use `cat -v`.
  - There is no `timeout`; use a background job and a sleep-and-kill watchdog.
- **Never run `bash -x submit_script.sh | head` to inspect a submit script.** It runs the script,
  and the job is submitted. Read the script, or use `bash -n`.
- **Don't hide exit codes behind pipes.** `script | tail -2` reported success for a sync that had
  aborted. Capture the code first: `${PIPESTATUS[0]}` in bash, `${pipestatus[1]}` in zsh.
