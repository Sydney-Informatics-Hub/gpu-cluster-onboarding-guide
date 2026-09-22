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
before it starts. It is plain Markdown, so any tool or human can read it. Everything else in
this directory is packaging.

The guide covers:

- how Apollo differs from Slurm, and which habits to drop;
- how to find your project, storage and compute presets at the start of a session, rather than
  assuming values that differ per user;
- how to submit, monitor and debug a workload, with the output you should expect;
- which steps belong to you and not to the agent, such as `runai login`, anything using
  `--attach`, and every credential;
- what the agent must ask you about first, such as any GPU request, any delete, and any write
  outside a directory you have named.

That last point matters. Apollo is a shared, fee for service facility. A GPU held overnight is a
real charge against someone's grant, and a shared PVC is readable by the whole project and is not
backed up. The guide makes an agent ask before it does anything of that kind.

Commands marked *verified* in the guide were run against the live cluster with
`runai-cli/2.24.65`.

## Point your agent at it

Copy the guide into the project you work in. Use the filename your tool reads at the start of a
session:

| tool | filename in your project |
| --- | --- |
| Claude Code | `CLAUDE.md`, or install the skill below |
| Codex, Cursor, Amp and other tools that follow the `AGENTS.md` convention | `AGENTS.md` |
| Gemini CLI | `GEMINI.md` |
| Cursor rules | `.cursor/rules/apollo.mdc` |
| anything else, including web chat | paste it, or attach the file |

```bash
curl -O https://raw.githubusercontent.com/Sydney-Informatics-Hub/gpu-cluster-onboarding-guide/main/ai-agents/APOLLO_AGENT_GUIDE.md
```

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

## The Claude skill (optional)

`claude-skill/` packages the same guide as a Claude skill, a folder Claude loads by itself
whenever a request looks like cluster work. You then do not have to remember to attach the guide.
This format is specific to Claude. With any other tool, use the guide directly as described
above.

```bash
ai-agents/build_claude_skill.sh            # writes ai-agents/dist/apollo-gpu{,.skill}
cp -r ai-agents/dist/apollo-gpu ~/.claude/skills/
```

There is a build step because the skill needs the runbook at `references/APOLLO_AGENT_GUIDE.md`.
Committing a second copy would let the two versions drift apart. `dist/` is git ignored. The
sources are what gets reviewed, and anyone can rebuild the bundle from them.

The skill also ships `claude-skill/scripts/apollo_run.sh`, which is useful on its own. It runs a
single command inside a workload and cleans up afterwards. It never requests a GPU, so it costs
nothing against your allocation:

```bash
apollo_run.sh 'du -sh $WORKDIR/*'
```

A skill only supplies text for the agent to read. Running that script, or any `runai` command,
needs an agent that can execute commands on your own machine, with the `runai` CLI installed and
the VPN connected. Claude Code can do this. A web chat cannot. There the skill still supplies the
knowledge, and you run the commands yourself.

## Keeping it current

An agent acts on this guide unattended, so a stale fact here does more damage than a stale
sentence on a documentation page. When cluster behaviour changes, such as a CLI command, a storage
rule or the support route, update the guide in the same pull request as the page it contradicts.
