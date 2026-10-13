#!/usr/bin/env python3
"""A fake `globus` CLI (globus-cli 3.x shapes) for tests. No network, no Globus account.

Supports what `globus_xfer.sh` uses: whoami, endpoint show / collection show, transfer,
task wait, task show and task event-list. JSON output (`-F json` / `--format json`) is what the
wrapper should parse; text output is approximate.

Configuration through the environment:

  FAKE_GLOBUS_STATE     directory for state (required): calls.jsonl, tasks/, counters.json
  FAKE_GLOBUS_SCENARIO  JSON file (optional):

    rules          [{"match": [tokens...], "stdout": "", "stderr": "", "rc": 0, "times": N}]
                   checked first, as in the fake runai
    logged_in      false makes every command fail as when `globus login` is missing
    collections    {id: display_name}; unknown ids still resolve to "Collection <id>"
    final_status   status task show reports once the task is done (default SUCCEEDED)
    wait_timeouts  `task wait` times out this many times before the task completes (default 0)
    task_fields    extra or overriding fields for `task show`
    error_events   list of events returned by `task event-list --filter-errors`
"""
import json
import os
import sys
from pathlib import Path

MISSING_LOGIN = ("MissingLoginError: Missing login for Globus Transfer, please run\n\n"
                 "  globus login\n")


def state_dir():
    d = os.environ.get("FAKE_GLOBUS_STATE")
    if not d:
        sys.stderr.write("fake globus: FAKE_GLOBUS_STATE is not set\n")
        sys.exit(98)
    p = Path(d)
    (p / "tasks").mkdir(parents=True, exist_ok=True)
    return p


def scenario():
    f = os.environ.get("FAKE_GLOBUS_SCENARIO")
    return json.loads(Path(f).read_text()) if f else {}


def bump(state, key):
    f = state / "counters.json"
    c = json.loads(f.read_text()) if f.exists() else {}
    c[key] = c.get(key, 0) + 1
    f.write_text(json.dumps(c))
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


def wants_json(argv):
    for i, a in enumerate(argv):
        if a in ("-F", "--format") and i + 1 < len(argv) and argv[i + 1].lower() == "json":
            return True
        if a.lower() in ("--format=json", "-fjson"):
            return True
    return False


def positionals(argv):
    takes_value = {"-F", "--format", "--label", "--sync-level", "--timeout", "--polling-interval",
                   "--jmespath", "--jq", "--limit", "--batch", "--deadline", "--filter"}
    res, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a in takes_value:
            i += 2
            continue
        if not a.startswith("-"):
            res.append(a)
        i += 1
    return res


def display_name(scen, cid):
    return scen.get("collections", {}).get(cid, "Collection " + cid)


def main():
    argv = sys.argv[1:]
    state = state_dir()
    with open(state / "calls.jsonl", "a") as f:
        f.write(json.dumps({"argv": argv}) + "\n")
    scen = scenario()

    for i, rule in enumerate(scen.get("rules", [])):
        if is_subsequence(rule.get("match", []), argv):
            times = rule.get("times")
            if times is not None and bump(state, "rule{}".format(i)) > times:
                continue
            out(rule.get("stdout", ""), rule.get("stderr", ""), rule.get("rc", 0))

    if not scen.get("logged_in", True):
        out(stderr=MISSING_LOGIN, rc=1)

    pos = positionals(argv)
    js = wants_json(argv)

    if pos[:1] == ["whoami"]:
        out(stdout="user@example.org")

    if pos[:2] in (["endpoint", "show"], ["collection", "show"]):
        cid = pos[2]
        doc = {"id": cid, "display_name": display_name(scen, cid)}
        out(stdout=json.dumps(doc) if js else "Display Name: " + doc["display_name"])

    if pos[:1] == ["transfer"]:
        src, dst = pos[1], pos[2]
        n = bump(state, "tasks")
        task_id = "00000000-0000-0000-0000-{:012d}".format(n)
        task = {"task_id": task_id, "src": src, "dst": dst, "argv": argv}
        (state / "tasks" / (task_id + ".json")).write_text(json.dumps(task))
        if js:
            out(stdout=json.dumps({"DATA_TYPE": "transfer_result", "code": "Accepted",
                                   "task_id": task_id,
                                   "message": "The transfer has been accepted and a task has "
                                              "been created and queued for execution"}))
        out(stdout="Message: The transfer has been accepted and a task has been created and "
                   "queued for execution\nTask ID: " + task_id)

    if pos[:2] == ["task", "wait"]:
        if bump(state, "wait_calls") <= scen.get("wait_timeouts", 0):
            out(stderr="Task not yet complete (timeout reached)", rc=1)
        out()

    if pos[:2] == ["task", "show"]:
        task_id = pos[2]
        tf = state / "tasks" / (task_id + ".json")
        if not tf.exists():
            out(stderr="Globus CLI Error: A Transfer API Error Occurred.\nHTTP status: 404\n"
                       "code: TaskNotFound", rc=1)
        task = json.loads(tf.read_text())
        doc = {"task_id": task_id, "type": "TRANSFER", "status": scen.get("final_status",
                                                                          "SUCCEEDED"),
               "source_endpoint_id": task["src"].split(":")[0],
               "destination_endpoint_id": task["dst"].split(":")[0],
               "files": 3, "files_transferred": 3, "files_skipped": 0, "directories": 1,
               "bytes_transferred": 3072, "faults": 0, "verify_checksum": True,
               "nice_status": None, "label": None}
        doc.update(scen.get("task_fields", {}))
        out(stdout=json.dumps(doc) if js else "Status: " + doc["status"])

    if pos[:2] == ["task", "event-list"]:
        events = scen.get("error_events", []) if "--filter-errors" in argv else []
        out(stdout=json.dumps({"DATA": events}) if js else "\n".join(
            e.get("description", "") for e in events))

    out(stderr="fake globus: unsupported command: " + " ".join(argv), rc=2)


if __name__ == "__main__":
    main()
