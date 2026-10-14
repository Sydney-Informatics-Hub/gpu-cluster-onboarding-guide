# Running Apollo work with an AI coding agent

Many researchers now write and run their code with an AI coding agent. Claude Code, Codex, Cursor,
Gemini CLI and Copilot all work this way. Ask one of them to run a job on Apollo and it will
usually get it wrong. It assumes Apollo is a traditional HPC cluster, so it writes `sbatch`,
`srun` and `module load`. None of those commands exist here.

Apollo is a Kubernetes cluster scheduled by NVIDIA Run:ai. There is no SSH and no login node.
There is no Apptainer, and no `sudo` inside containers. Work is submitted as *workloads*, through
the `runai` CLI or the web interface. An agent that does not know this will waste your time and
may spend your project's GPU allocation while it guesses.

[`APOLLO_AGENT_GUIDE.md`](APOLLO_AGENT_GUIDE.md) is a runbook that tells an agent all of this
before it starts. It is plain Markdown, so any tool or human can read it. A companion,
[`GPU_EFFICIENCY_GUIDE.md`](GPU_EFFICIENCY_GUIDE.md), covers getting the most out of each GPU-hour
on any cluster. [`scripts/`](scripts/) holds small tools that both guides refer to. Everything
else in this directory is packaging and tests.

The Apollo guide covers:

- how Apollo differs from Slurm, and which habits to drop;
- how to find your project, storage and compute presets at the start of a session, rather than
  assuming values that differ per user;
- how to submit, monitor and debug a workload, with the output you should expect;
- which steps belong to you and not to the agent, such as `runai login`, anything using
  `--attach`, and every credential;
- what the agent must ask you about first, such as any GPU request, any delete, and any write
  outside a directory you have named;
- moving data with Globus, RDS or a transfer workspace, with checks before and after;
- the failure modes that cost GPU time: crash loops, pods stuck starting, lost logs, lost
  signals, maintenance windows.

The efficiency guide covers the free checks to run before spending, how to estimate a run, how to
tell from the first ten minutes whether a job is using its GPU, how to find the real bottleneck,
and the guards that keep training code from wasting time silently.

That last point matters. Apollo is a shared, fee for service facility. A GPU held overnight is a
real charge against someone's grant, and a shared PVC is readable by the whole project and is not
backed up. The guide makes an agent ask before it does anything of that kind.

Commands marked *verified* in the guide were run against the live cluster on the date given.
Those dated before 2026-10-08 used Run:ai 2.24; the key ones were re-checked on Run:ai 2.25 on
2026-10-14. The platform was upgraded to Run:ai 2.25 and the
NVIDIA 580 driver in October 2026, and the guide says which facts that affects.

## Point your agent at it

Copy the guide into the project you work in. Use the filename your tool reads at the start of a
session. Copy `GPU_EFFICIENCY_GUIDE.md` alongside it if you will run GPU jobs:

| tool | filename in your project |
| --- | --- |
| Claude Code | `CLAUDE.md`, or install the skill below |
| Codex, Cursor, Amp and other tools that follow the `AGENTS.md` convention | `AGENTS.md` |
| Gemini CLI | `GEMINI.md` |
| Cursor rules | `.cursor/rules/apollo.mdc` |
| anything else, including web chat | paste it, or attach the file |

```bash
curl -O https://raw.githubusercontent.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide/main/ai-agents/APOLLO_AGENT_GUIDE.md
curl -O https://raw.githubusercontent.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide/main/ai-agents/GPU_EFFICIENCY_GUIDE.md
```

The guides refer to scripts as `scripts/<name>`. To give your agent those, copy the
[`scripts/`](scripts/) directory into the project too, or clone this repository and point the
agent at `ai-agents/scripts/`.

Your project may already use that filename for its own instructions. In that case keep the guide
beside it, and add one line pointing to it. Most tools will read a file they are told to read.

To check it worked, ask your agent something about the cluster, such as *"how do I submit a
training job on the GPU cluster?"*. The answer should reach for `runai training submit`, not
`sbatch`.

## What an agent cannot do

Some steps are not the agent's, whichever tool you use. It is worth knowing which, before you
hand over a task:

- **Log in.** `runai login` is UniKey and Okta SSO in a browser, behind the VPN. There is no API
  token or service account to use instead. The agent should stop and ask you. It should never
  handle your password.
- **Connect the VPN, or sign in to a container registry.** The RDS password prompt during a file
  transfer is yours too.
- **Work in an interactive session.** An agent can create a workspace and give you the link, but
  anything using `--attach` needs a terminal it does not have. Batch jobs submitted with
  `runai training submit` are its natural unit of work.
- **Create or change a project.** Projects, quotas and environments are set by administrators.
  Membership comes from DashR, through your Chief Investigator.
- **Create a directory at the top of the shared storage.** The mount root is not writable, and
  there is no `sudo` in a container. An administrator has to create one for your project group.
- **List cluster hardware.** `runai node list` and `runai nodepool list` are refused at the
  standard researcher role.

When a step needs an administrator, it goes to the Research Computing support form linked from
the [Apollo wiki page](https://sydneyuni.atlassian.net/wiki/spaces/RC/pages/3579674625). The
guide tells the agent to write the request out for you to paste.

## The scripts

Each script in [`scripts/`](scripts/) has a usage header, needs only bash or Python ≥ 3.9 with the
standard library (3.12 is the version they are developed and tested on), and
reads the `cluster.env` file the guide's discovery step writes. The job scripts never request a
GPU, so they cost nothing against your allocation. The transfer scripts do nothing until given
`--yes`.

| script | what it does |
|---|---|
| `apollo_run.sh`, `apollo_script.sh` | run a command or a local script file in a throwaway CPU job, safely quoted, and clean up |
| `apollo_cleanup.sh`, `apollo_snapshot.sh` | find your leftover jobs; record and classify jobs around a maintenance window |
| `manifest.py` | list a directory's files, sizes and checksums, and check a copy against the list |
| `globus_xfer.sh` | a Globus transfer to an allowed destination, with a dry run, a wait and a result check |
| `pvc_sync_code.sh`, `pvc_upload.py`, `pvc_download.py` | code into a running workspace; laptop ↔ PVC without RDS or Globus |
| `gpu_watch.sh`, `gpu_watch_verdict.py` | sample a running job's GPU use and name the bottleneck |
| `gpu_ledger.py` | estimate a run, and record GPU-hours from the scheduler's own record |
| `train_wrapper.sh` | run a training command so signals, exit codes, logs and restarts behave |
| `step_profile.py`, `loader_bench.py` | measure a training step and the input pipeline (need PyTorch) |

```bash
ai-agents/scripts/apollo_run.sh 'du -sh "$WORKDIR"/*'
```

Running a script, or any `runai` command, needs an agent that can execute commands on your own
machine, with the `runai` CLI installed and the VPN connected. Claude Code can do this. A web chat
cannot. There the guides still supply the knowledge, and you run the commands yourself.

## The Claude skills (optional)

`skills/` packages the same material as two Claude skills: folders that Claude loads by itself
when a request looks like their kind of work. You then do not have to remember to attach the
guides. This format is specific to Claude. With any other tool, use the guides and scripts
directly as described above.

- **`apollo-gpu`**: operating Apollo. It bundles the Apollo guide and every script.
- **`gpu-efficiency`**: getting the most out of GPU time on any cluster. It bundles the
  efficiency guide and the GPU scripts.

```bash
ai-agents/build_claude_skills.sh           # writes ai-agents/dist/<skill>/ and <skill>.skill
cp -r ai-agents/dist/apollo-gpu ai-agents/dist/gpu-efficiency ~/.claude/skills/
```

There is a build step because a skill needs its guide under `references/` and its scripts under
`scripts/`. Committing second copies would let them drift apart. `dist/` is git ignored. The
sources are what gets reviewed, and anyone can rebuild the bundles from them.

## Tests

`tests/` checks the scripts and the build without a cluster or network. Fake `runai` and `globus`
CLIs and a fake copyparty server stand in for the real ones:

```bash
python3 -m unittest discover -s ai-agents/tests
```

The tests also check that the guides and skills name the same GPU verdicts, that every script a
document mentions exists and is bundled, and that nothing project-specific has crept in. CI runs
them, with `shellcheck`, on every change to `ai-agents/`.

## Keeping it current

An agent acts on these guides unattended, so a stale fact here does more damage than a stale
sentence on a documentation page. When cluster behaviour changes, such as a CLI command, a storage
rule or the support route, update the guide in the same pull request as the page it contradicts.
Tag what you write with its evidence (*verified* with a date, *observed once*, *vendor docs* or
*unverified*), and re-check verified facts after a platform upgrade.
