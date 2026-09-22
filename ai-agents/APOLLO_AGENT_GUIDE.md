# Apollo GPU cluster — agent operating guide

**What this is.** A runbook for an AI coding agent (Claude Code or similar) that has been asked
to run work on the University of Sydney **Apollo GPU cluster**. It is written to be executed, not
narrated: every step says who performs it, what it costs, what success looks like, and how it
fails. A human can read it too, but the primary reader is the agent.

**Scope.** Apollo specifically — a Kubernetes cluster scheduled by **NVIDIA Run:ai**, managed by
the Sydney Informatics Hub. Nothing here is specific to any one research project; every
project-specific value is discovered at runtime in §3 rather than hard-coded.

**Provenance.** Facts marked *verified* were executed against the live cluster on **2026-09-22**
with `runai-cli/2.24.65`. The CLI auto-updates, so re-run §3 if a command's shape looks wrong.
Official docs: <https://sydney-informatics-hub.github.io/gpu-cluster-onboarding-guide/>.

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
- **Workloads may be preemptible.** Submitted trainings can be evicted to make room for
  higher-priority work, so long jobs must checkpoint and resume by themselves.

Two workload types matter:

| type | use for | behaviour |
|---|---|---|
| `workspace` | interactive sessions, Jupyter, file browsing | stays up until suspended or deleted |
| `training` | batch jobs, including anything an agent runs unattended | runs the command, then completes |

**Prefer `training` for agent work.** It needs no TTY, exits on its own, and cannot be left
running by accident.

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
- **Connecting the VPN.**

Everything else — submitting, monitoring, reading logs, inspecting storage, building images — is
`[agent]` work, subject to the policy in §4.

**When authentication expires** the CLI returns an auth error on every command. Do not retry, and
do not attempt to re-authenticate. Stop and tell the user: *"the Run:ai token has expired, please
run `runai login`"*.

---

## 3. Discovery — resolve every project-specific value

Do this first, in any new session. It replaces every placeholder in this guide. Do **not** copy
values from someone else's notes; projects, claim names and mount paths differ per user.

```bash
# The CLI is often not on the PATH of a non-interactive shell. Find it before anything else.
RUNAI=$(command -v runai || echo "$HOME/.runai/bin/runai")
"$RUNAI" whoami                 # confirms an authenticated session
"$RUNAI" project list           # the project name(s) and GPU quota
```

Pick the project (ask the user if more than one is listed — some accounts can see projects that
are not theirs), then:

```bash
PROJECT=<from project list>
"$RUNAI" pvc list -p "$PROJECT"                 # claim name(s)
"$RUNAI" datasource list -p "$PROJECT"          # the project's data sources
"$RUNAI" datasource describe <name> --type pvc -p "$PROJECT" -o yaml   # claim name + mount path
"$RUNAI" compute list -p "$PROJECT"             # named resource presets
"$RUNAI" template list -p "$PROJECT"            # site-provided workload templates
```

`-o yaml` / `-o json` are available on several subcommands and are preferable to parsing tables.

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

**Never, regardless of instruction:**

- Ask for, accept, type or store the user's UniKey password, or any other credential.
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

Keep a ledger file in the project repo — date, workload, GPUs, wall time, GPU-hours, running
total — and update it after each GPU job. Nothing else will.

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

Use raw flags only when no preset fits, and say why when you do:

| flag | meaning |
|---|---|
| `-g`, `--gpu-devices-request <n>` | whole GPUs |
| `--gpu-portion-request <0–1>` | a fraction of one GPU |
| `--gpu-memory-request <size>` | GPU memory instead of a fraction (`--gpu-request-type memory`) |
| `--cpu-core-request <n>` / `--cpu-memory-request <size>` | CPU cores and RAM |
| `--large-shm` | a large `/dev/shm`, needed by many PyTorch dataloaders |

`*-request` is what the workload is guaranteed; the matching `*-limit` flags cap what it may burst
to. Requesting a **fraction** of a GPU is the cheapest way to test an image or a code path — take
a whole GPU only when the work genuinely needs one.

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

**Cost.** Free (CPU-only). **Duration.** 30–60 s including container start.

```bash
NAME=peek-$RANDOM
"$RUNAI" training submit "$NAME" -p "$PROJECT" \
  -i <site base image> \
  --cpu-core-request 1 --cpu-memory-request 2G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --command -- bash -c "ls -la $WORKDIR"
```

**Then poll until terminal, and only then read logs:**

```bash
"$RUNAI" training standard describe "$NAME" -p "$PROJECT" | grep '^Phase:'
"$RUNAI" training standard logs     "$NAME" -p "$PROJECT"
"$RUNAI" training standard delete   "$NAME" -p "$PROJECT"    # ask first if it is not your own throwaway
```

**Expected.** `Phase: Initializing` → `Running` → `Completed`, then the command's stdout.
**Failure modes.**
- `failed to print logs. workload is not ready to stream: pod is not ready` — **not an error.**
  The container is still starting. Keep polling `describe`.
- `unknown command "logs" for "runai training"` — the sub-noun is required:
  `runai training standard logs`.
- `Permission denied` on a write — you are probably at the mount root; see §7.

### 6.3 Verify the GPU environment `[agent, ask first]`

The first GPU workload of a session. Confirms the device, the container identity, storage and
network in one go.

**Cost.** ~0.1 GPU-hours including the first image pull. **Ask before running.**

```bash
"$RUNAI" training submit gpu-check-$RANDOM -p "$PROJECT" \
  -i <site base image> \
  --gpu-devices-request 1 --cpu-core-request 1 --cpu-memory-request 8G --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --command -- bash -c 'nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap --format=csv; id; df -h '"$MOUNT"'; curl -sI --max-time 10 https://huggingface.co | head -1'
```

**Expected** (Apollo, verified 2026-09-22 — values will differ at other sites):

```
name, memory.total [MiB], driver_version, compute_cap
NVIDIA H200, 143771 MiB, 550.163.01, 9.0
uid=<your uid> gid=10000(dgxgroup) groups=10000(dgxgroup),...
...  5.0T  1.5T  3.6T  29% <mount>
HTTP/2 200
```

The `compute_cap` line matters: build GPU libraries for that architecture (9.0 = Hopper/sm_90).
The `HTTP/2 200` line means **pods have outbound internet**, so public datasets and packages can
be downloaded by the cluster directly rather than staged from a laptop. Re-check it rather than
assuming — network policy can change.

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

**Cost.** Whatever you estimate — state it when asking.

```bash
"$RUNAI" training submit <job-name> -p "$PROJECT" \
  -i <your image> \
  --compute <a preset from `runai compute list`> \
  --run-as-user \
  --existing-pvc "claimname=$PVC,path=$MOUNT" \
  --working-dir "$WORKDIR" \
  --pod-running-timeout 30m \
  --command -- <your command>
```

Prefer a named `--compute` preset over hand-rolled CPU/memory flags: site admins maintain them.

**Because workloads can be preempted**, any job longer than a few minutes must write checkpoints
to the PVC and resume from the newest checkpoint automatically on restart. Check whether the job
is preemptible: `describe` prints `Preemptible: true|false` and a `Priority`. If training code
cannot resume unattended, say so before submitting — that is a code problem to fix first, not a
risk to accept silently.

**Monitor:**

```bash
"$RUNAI" training list -p "$PROJECT"
"$RUNAI" training standard describe <job> -p "$PROJECT"   # Phase, Pods, and an Events table
"$RUNAI" training standard logs     <job> -p "$PROJECT" --follow
```

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

### 6.7 Move data `[agent for public downloads, human-in-the-loop for institutional storage]`

- **Public datasets:** download from inside a cluster workload, straight to the PVC. Never
  round-trip through the user's laptop.
- **From institutional storage (e.g. the USyd Research Data Store):** the documented route is a
  CPU-only data-transfer workspace whose terminal runs `sftp` against the storage host. The
  password prompt is the user's. Never hold a GPU during a transfer.
- **Results back:** cluster scratch is not backed up. Copy anything valuable back to durable
  institutional storage the day it is produced.

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
  long-term home; the PVC is not.
- **Do not treat a dataset staged on the PVC as durable.** A corpus that took hours to download
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
| any auth / 401 error | the SSO token expired | **stop**; ask the user to run `runai login` |
| `workload is not ready to stream: pod is not ready` | container still starting | poll `describe` until Phase is terminal; first image pull can take minutes |
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
- **GPU-hours consumed**, and the running total against any allocation;
- what was written to shared storage, and where;
- anything still running or suspended — and if something is holding a GPU, say so first.

If a step failed, quote the error. If a step was skipped, say which and why. An agent that
reports a clean run it did not verify is worse than one that reports a messy one accurately —
the user is accountable for this allocation and this data, and can only exercise that
responsibility with an honest account of what happened.
