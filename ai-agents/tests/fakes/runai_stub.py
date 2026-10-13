#!/usr/bin/env python3
"""A fake `runai` CLI for tests. No cluster, no network.

It behaves like the parts of runai-cli 2.25 the scripts use, closely enough to test them:

- `training [standard] submit NAME ... -- ARGV` records the job and, unless the scenario says
  otherwise, *simulates the pod*: it runs ARGV locally (cwd = --working-dir if it exists, else the
  sandbox) with the `-e` variables set, and keeps its output and exit code;
- `training standard describe NAME [-o json]` reports `Running` for the first few polls, then
  `Completed` or `Failed` from the simulated exit code;
- `training standard logs NAME` prints the simulated output, optionally after a few
  "not ready to stream" errors;
- `training standard delete`, `exec`, `workspace exec`, `training list`, `workspace list`,
  `whoami` and `project list` do what the scripts expect.

Configuration, all through the environment:

  FAKE_RUNAI_STATE     directory for state (required): calls.jsonl, jobs/, counters.json
  FAKE_RUNAI_SCENARIO  JSON file (optional), see Scenario below
  FAKE_RUNAI_SANDBOX   directory standing in for the pod's filesystem (default: the state dir)

Scenario keys (all optional):

  rules           [{"match": [tokens...], "stdout": "", "stderr": "", "rc": 0, "times": N}]
                  checked first, in order; a rule fires when its tokens appear in argv in that
                  order; `times` limits how often it fires (default: always)
  simulate        run submitted commands locally (default true)
  running_polls   describe answers Running this many times before the terminal phase (default 1)
  pods            pods to report per job; more than 1 imitates re-created pods (default 1)
  logs_not_ready  logs fails with "not ready to stream" this many times per job (default 0)
  delete_fail     delete fails this many times in total (default 0)
  list_training   JSON returned by `training list --json` (default: built from known jobs)
  list_workspace  JSON returned by `workspace list --json` (default: [])
  describe_json   {name: JSON} returned by `describe NAME -o json` (default: synthesised)
  user            email printed by whoami (default user@example.org)
"""
import json
import os
import subprocess
import sys
from pathlib import Path

COMMAND_CAP = 10000
TERMINAL_OK, TERMINAL_FAIL = "Completed", "Failed"


def state_dir():
    d = os.environ.get("FAKE_RUNAI_STATE")
    if not d:
        sys.stderr.write("fake runai: FAKE_RUNAI_STATE is not set\n")
        sys.exit(98)
    p = Path(d)
    (p / "jobs").mkdir(parents=True, exist_ok=True)
    return p


def load_scenario():
    f = os.environ.get("FAKE_RUNAI_SCENARIO")
    return json.loads(Path(f).read_text()) if f else {}


def counters(state):
    f = state / "counters.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_counters(state, c):
    (state / "counters.json").write_text(json.dumps(c))


def bump(state, key):
    c = counters(state)
    c[key] = c.get(key, 0) + 1
    save_counters(state, c)
    return c[key]


def is_subsequence(tokens, argv):
    it = iter(argv)
    return all(any(t == a for a in it) for t in tokens)


def out(stdout="", stderr="", rc=0):
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if stderr:
        sys.stderr.write(stderr if stderr.endswith("\n") else stderr + "\n")
    sys.exit(rc)


def flag_values(argv, *names):
    vals = []
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                vals.append(argv[i + 1])
            elif a.startswith(n + "="):
                vals.append(a[len(n) + 1:])
    return vals


def split_dashdash(argv):
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def positional_after(argv, words):
    """The first non-flag token after the command words, e.g. the workload name."""
    i = 0
    for w in words:
        i = argv.index(w, i) + 1
    flags_with_value = {"-p", "--project", "-i", "--image", "-o", "--output", "--pod", "-c",
                        "--container", "--compute", "-e", "--environment-variable"}
    while i < len(argv):
        a = argv[i]
        if a in flags_with_value:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def job_path(state, name):
    return state / "jobs" / (name + ".json")


def load_job(state, name):
    p = job_path(state, name)
    return json.loads(p.read_text()) if p.exists() else None


def save_job(state, job):
    job_path(state, job["name"]).write_text(json.dumps(job))


def sandbox(state):
    return Path(os.environ.get("FAKE_RUNAI_SANDBOX", str(state)))


def run_locally(state, argv_cmd, env_pairs, workdir):
    env = dict(os.environ)
    for pair in env_pairs:
        k, _, v = pair.partition("=")
        env[k] = v
    cwd = workdir if workdir and Path(workdir).is_dir() else str(sandbox(state))
    p = subprocess.run(argv_cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.stdout, p.returncode


def cmd_submit(state, scen, argv, words):
    head, cmd = split_dashdash(argv)
    name = positional_after(head, words)
    if not name:
        out(stderr="Error: workload name is required", rc=1)
    if len(" ".join(cmd)) > COMMAND_CAP:
        out(stderr="Error: failed to submit standard training. Value: value for Value must have "
                   "at most 10000 characters", rc=1)
    job = {"name": name, "argv": argv, "cmd": cmd, "polls": 0, "logs_tries": 0,
           "env": flag_values(head, "-e", "--environment-variable"),
           "workdir": (flag_values(head, "--working-dir") or [None])[0],
           "large_shm": "--large-shm" in head,
           "exclude_node": flag_values(head, "--exclude-node"),
           "deleted": False}
    if scen.get("simulate", True) and cmd:
        job["output"], job["rc"] = run_locally(state, cmd, job["env"], job["workdir"])
    else:
        job["output"], job["rc"] = "", 0
    save_job(state, job)
    out(stdout="Creating standard training {} ...\nThe workload '{}' has been submitted "
               "successfully".format(name, name))


def phase_of(scen, job):
    if job["polls"] <= scen.get("running_polls", 1):
        return "Running"
    return TERMINAL_OK if job["rc"] == 0 else TERMINAL_FAIL


def cmd_describe(state, scen, argv):
    name = positional_after(argv, ["describe"])
    job = load_job(state, name)
    if job is None or job["deleted"]:
        out(stderr="Error: workload '{}' not found".format(name), rc=1)
    job["polls"] += 1
    save_job(state, job)
    phase = phase_of(scen, job)
    if "json" in flag_values(argv, "-o", "--output"):
        custom = scen.get("describe_json", {}).get(name)
        if custom is not None:
            out(stdout=json.dumps(custom))
        pods = [{"name": "{}-0-{}".format(name, i), "phase": phase, "nodeName": "node-a"}
                for i in range(scen.get("pods", 1))]
        out(stdout=json.dumps({"name": name, "phase": phase, "podsInfo": {"pods": pods}}))
    out(stdout="Name: {}\nPhase: {}\nPreemptible: true\n".format(name, phase))


def cmd_logs(state, scen, argv):
    name = positional_after(argv, ["logs"])
    job = load_job(state, name)
    if job is None or job["deleted"]:
        out(stderr="Error: workload '{}' not found".format(name), rc=1)
    job["logs_tries"] += 1
    save_job(state, job)
    if job["logs_tries"] <= scen.get("logs_not_ready", 0):
        out(stderr="Error: failed to print logs. workload is not ready to stream: pod is not ready",
            rc=1)
    out(stdout=job["output"])


def cmd_delete(state, scen, argv):
    name = positional_after(argv, ["delete"])
    if bump(state, "delete_attempts") <= scen.get("delete_fail", 0):
        out(stderr="Error: failed to delete workload '{}': connection reset".format(name), rc=1)
    job = load_job(state, name)
    if job is not None:
        job["deleted"] = True
        save_job(state, job)
    out(stdout="Successfully deleted workload '{}'".format(name))


def cmd_exec(state, scen, argv):
    head, cmd = split_dashdash(argv)
    if not cmd:
        out(stderr="Error: no command given", rc=1)
    if "-i" in head or "--stdin" in head:
        if not sys.stdin.isatty() and "-t" not in head and "--tty" not in head:
            out(stderr="Error: failed to exec output. inappropriate ioctl for device", rc=1)
    text, rc = run_locally(state, cmd, [], str(sandbox(state)))
    sys.stdout.write(text)
    if rc != 0:
        out(stderr="Error: failed to exec output. command terminated with exit code {}".format(rc),
            rc=1)
    sys.exit(0)


def cmd_list(state, scen, argv, kind):
    key = "list_" + kind
    if key in scen:
        data = scen[key]
    elif kind == "training":
        data = []
        for p in sorted((state / "jobs").glob("*.json")):
            job = json.loads(p.read_text())
            if not job["deleted"]:
                data.append({"name": job["name"], "phase": phase_of(scen, job)})
    else:
        data = []
    if "--json" in argv:
        out(stdout=json.dumps(data))
    rows = ["{:<40} {}".format(d.get("name", ""), d.get("phase", "")) for d in data]
    out(stdout="\n".join(["NAME                                     PHASE"] + rows))


def main():
    argv = sys.argv[1:]
    state = state_dir()
    with open(state / "calls.jsonl", "a") as f:
        f.write(json.dumps({"argv": argv}) + "\n")
    scen = load_scenario()

    for i, rule in enumerate(scen.get("rules", [])):
        if is_subsequence(rule.get("match", []), argv):
            times = rule.get("times")
            if times is not None and bump(state, "rule{}".format(i)) > times:
                continue
            out(rule.get("stdout", ""), rule.get("stderr", ""), rule.get("rc", 0))

    words = [a for a in argv if not a.startswith("-")]
    if words[:1] == ["whoami"]:
        out(stdout="User {} logged in with ID 00000000-1111-2222-3333-444444444444".format(
            scen.get("user", "user@example.org")))
    if words[:2] == ["project", "list"]:
        out(stdout="PROJECT    GPU QUOTA\ntest-proj  1")
    if words[:2] == ["training", "submit"]:
        cmd_submit(state, scen, argv, ["submit"])
    if words[:3] == ["training", "standard", "submit"]:
        cmd_submit(state, scen, argv, ["submit"])
    if words[:3] == ["training", "standard", "describe"]:
        cmd_describe(state, scen, argv)
    if words[:3] == ["training", "standard", "logs"]:
        cmd_logs(state, scen, argv)
    if words[:3] == ["training", "standard", "delete"]:
        cmd_delete(state, scen, argv)
    if words[:3] == ["training", "standard", "exec"] or words[:2] == ["workspace", "exec"]:
        cmd_exec(state, scen, argv)
    if words[:2] == ["training", "list"]:
        cmd_list(state, scen, argv, "training")
    if words[:2] == ["workspace", "list"]:
        cmd_list(state, scen, argv, "workspace")
    # Anything else (workspace submit/suspend/delete, template describe, ...) succeeds quietly;
    # tests assert on the recorded call instead.
    sys.exit(0)


if __name__ == "__main__":
    main()
