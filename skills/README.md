# Agent skills

Skills for AI coding agents (Claude Code and similar) working on the Apollo GPU cluster.

A *skill* is a folder with a `SKILL.md` at its root: a short set of operating instructions that
an agent loads when the work matches its description, plus any reference documents and scripts it
needs. It is the agent-facing counterpart to this site — the onboarding guide teaches a person,
a skill equips an agent.

| skill | what it covers |
| --- | --- |
| [`apollo-gpu`](apollo-gpu/) | Operating Apollo through the `runai` CLI: discovering the project and its storage, submitting and monitoring workloads, inspecting the PVC, building images, staging data — under an explicit safety policy. |

## Why this exists

Agents pattern-match Apollo to Slurm and write `sbatch`, `srun` and `module load` commands that
cannot work — Apollo is Kubernetes scheduled by NVIDIA Run:ai, with no SSH, no login node and no
Apptainer. The skill front-loads that correction, and pairs it with a policy for a facility where
a wrong command spends someone else's allocation or overwrites a shared PVC that is not backed up.

`apollo-gpu/references/APOLLO_AGENT_GUIDE.md` is the full runbook behind the skill: recipes with
expected output, an error-string lookup table, and storage and provisioning detail. Commands in it
marked *verified* were executed against the live cluster with `runai-cli/2.24.65`.

## Installing `apollo-gpu`

**Claude Code** — copy the folder to either location and restart the session:

```bash
# available in every project, for you
cp -r skills/apollo-gpu ~/.claude/skills/

# or committed alongside a single project, shared with everyone who clones it
cp -r skills/apollo-gpu <your-project>/.claude/skills/
```

**Apps that accept a skill upload** (e.g. Claude's Settings → Capabilities → Skills) — build the
bundle and upload it. It is a plain zip; rename the extension to `.zip` if the upload control
insists on one:

```bash
skills/build_skill.sh apollo-gpu   # writes skills/dist/apollo-gpu.skill
```

Either way, check it loaded by asking the agent something Apollo-shaped — *"how do I submit a
training job on the GPU cluster?"* — and confirming it reaches for `runai training submit` rather
than `sbatch`.

## What the skill will not do

By design, and worth knowing before you hand an agent a cluster task. It will not run
`runai login` (interactive UniKey and Okta SSO in a browser — there is no service account or API
token), touch your password, use `--attach`, or run `docker login`. It asks before requesting any
GPU, before deleting a workload, and before writing outside a directory you have named. Anything
read-only, and anything CPU-only, it does unattended.

`apollo-gpu/scripts/apollo_run.sh` runs a single command inside a workload and cleans up after
itself. It is CPU-only by construction — it never requests a GPU — so it costs nothing against a
project allocation.

## Packaging and contributing

`build_skill.sh` zips a skill directory into `dist/<name>.skill`; run it with no arguments to
package every skill here. `dist/` is git-ignored — the source folders are what gets reviewed, and
the bundle is a build artefact anyone can reproduce.

Skills are excluded from the Quarto render in `_quarto.yml`: they are agent assets, not pages of
the onboarding site.

When cluster behaviour changes — a CLI shape, a storage rule, a support route — update `SKILL.md`
and the runbook in the same pull request as the corresponding site page, so the two do not drift.
