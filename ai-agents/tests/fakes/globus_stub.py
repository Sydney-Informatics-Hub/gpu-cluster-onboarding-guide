#!/usr/bin/env python3
"""A fake `globus` CLI (globus-cli 3.x shapes) for tests. No network, no Globus account.

Supports what `globus_xfer.sh` uses: whoami, endpoint show / collection show,
`api transfer GET /endpoint/ID`, transfer, task wait, task show and task event-list. JSON output
(`-F json` / `--format json`) is what the wrapper should parse; text output is approximate.

Shapes follow globus-cli 3.x as documented at https://docs.globus.org/cli/reference/ and in its
source (github.com/globus/globus-cli), checked 2026-10-14:

- a missing login prints `MissingLoginError: Missing login for ...` on stderr and exits 4;
- `endpoint show` refuses a GCSv5 collection with exit 3 and points at `collection show`;
- with `-F json`, an API error (ConsentRequired included) is dumped as raw JSON on stderr and the
  command exits 1, because the JSON error handler runs before the consent hook; in text mode the
  consent hook prints `globus session consent '<scope>'` on stdout and exits 4;
- `task wait` exits with `--timeout-exit-code` (default 1) on timeout.

Configuration through the environment:

  FAKE_GLOBUS_STATE     directory for state (required): calls.jsonl, tasks/, counters.json
  FAKE_GLOBUS_SCENARIO  JSON file (optional):

    rules          [{"match": [tokens...], "stdout": "", "stderr": "", "rc": 0, "times": N}]
                   checked first, as in the fake runai
    logged_in      false makes every command fail as when `globus login` is missing
    collections    {id: display_name}; unknown ids still resolve to "Collection <id>"
    gcsv5          ids that are GCSv5 collections: `endpoint show` refuses them (exit 3)
    consent_scopes scopes whose consent is missing: `transfer` fails with ConsentRequired
    final_status   status task show reports once the task is done (default SUCCEEDED)
    wait_timeouts  `task wait` times out this many times before the task completes (default 0);
                   while it is timing out, `task show` reports ACTIVE
    wait_fails_on_failure  `task wait` exits 1 when the task ends unsuccessfully, as the real CLI
                   does (default true)
    task_fields    extra or overriding fields for `task show`
    error_events   list of events returned by `task event-list --filter-errors`
"""
import json
import os
import sys
from pathlib import Path


def missing_login(service):
    return "MissingLoginError: Missing login for {}.\nPlease run:\n\n  globus login\n".format(
        service)


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


def counter(state, key):
    f = state / "counters.json"
    c = json.loads(f.read_text()) if f.exists() else {}
    return c.get(key, 0)


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


def flag_value(argv, name, default=None):
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a[len(name) + 1:]
    return default


def positionals(argv):
    takes_value = {"-F", "--format", "--label", "-s", "--sync-level", "--timeout",
                   "--polling-interval", "--timeout-exit-code", "--jmespath", "--jq", "--limit",
                   "--batch", "--deadline", "--filter", "--notify", "--include", "--exclude",
                   "--submission-id", "--checksum-algorithm", "--external-checksum"}
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


def endpoint_doc(scen, cid):
    return {"DATA_TYPE": "endpoint", "id": cid, "display_name": display_name(scen, cid),
            "entity_type": "GCSv5_mapped_collection" if cid in scen.get("gcsv5", [])
            else "GCSv5_endpoint"}


def consent_required(scen, js):
    scopes = ["urn:globus:auth:scope:transfer.api.globus.org:all[*https://auth.globus.org/"
              "scopes/{}/data_access]".format(s) for s in scen["consent_scopes"]]
    if js:  # raw JSON error body on stderr, exit 1 (json_error_handler)
        body = {"code": "ConsentRequired", "message": "Missing required data_access consent",
                "request_id": "abc123", "resource": "/transfer", "required_scopes": scopes,
                "authorization_parameters": {"session_message": "Missing required data_access "
                                             "consent", "required_scopes": scopes}}
        out(stderr=json.dumps(body, indent=2), rc=1)
    out(stdout="The resource you are trying to access requires you to consent to additional "
               "access for the Globus CLI.\nPlease run:\n\n  globus session consent {}\n\n"
               "to login with the required scopes.".format(" ".join("'%s'" % s for s in scopes)),
        rc=4)


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

    pos = positionals(argv)
    js = wants_json(argv)

    if not scen.get("logged_in", True):
        out(stderr=missing_login("Globus Auth" if pos[:1] == ["whoami"] else "Globus Transfer"),
            rc=4)

    if pos[:1] == ["whoami"]:
        out(stdout="user@example.org")

    if pos[:2] == ["endpoint", "show"]:
        cid = pos[2]
        if cid in scen.get("gcsv5", []):
            out(stderr="Expected {} to be an endpoint ID.\nInstead, found it was of type "
                       "'GCSv5_mapped_collection'.\nPlease run the following command instead:\n\n"
                       "    globus collection show {}".format(cid, cid), rc=3)
        doc = endpoint_doc(scen, cid)
        out(stdout=json.dumps(doc) if js else "Display Name: " + doc["display_name"])

    if pos[:2] == ["collection", "show"]:
        cid = pos[2]
        doc = {"id": cid, "display_name": display_name(scen, cid)}
        out(stdout=json.dumps(doc) if js else "Display Name: " + doc["display_name"])

    if pos[:2] == ["api", "transfer"] and len(pos) >= 4 and pos[2].lower() == "get":
        path = pos[3].strip("/").split("/")
        if len(path) == 2 and path[0] == "endpoint":
            out(stdout=json.dumps(endpoint_doc(scen, path[1])))

    if pos[:1] == ["transfer"]:
        if scen.get("consent_scopes"):
            consent_required(scen, js)
        src, dst = pos[1], pos[2]
        n = bump(state, "tasks")
        task_id = "00000000-0000-0000-0000-{:012d}".format(n)
        task = {"task_id": task_id, "src": src, "dst": dst, "argv": argv}
        (state / "tasks" / (task_id + ".json")).write_text(json.dumps(task))
        if js:
            out(stdout=json.dumps({"DATA_TYPE": "transfer_result", "code": "Accepted",
                                   "task_id": task_id, "submission_id": "sub-" + task_id,
                                   "request_id": "req{}".format(n), "resource": "/transfer",
                                   "message": "The transfer has been accepted and a task has "
                                              "been created and queued for execution"}))
        out(stdout="Message: The transfer has been accepted and a task has been created and "
                   "queued for execution\nTask ID: " + task_id)

    if pos[:2] == ["task", "wait"]:
        if bump(state, "wait_calls") <= scen.get("wait_timeouts", 0):
            out(stderr="Task not yet complete (timeout reached)",
                rc=int(flag_value(argv, "--timeout-exit-code", "1")))
        if (scen.get("wait_fails_on_failure", True)
                and scen.get("final_status", "SUCCEEDED") != "SUCCEEDED"):
            out(rc=1)
        out()

    if pos[:2] == ["task", "show"]:
        task_id = pos[2]
        tf = state / "tasks" / (task_id + ".json")
        if not tf.exists():
            out(stderr="Globus CLI Error: A Transfer API Error Occurred.\nHTTP status: 404\n"
                       "code: TaskNotFound", rc=1)
        task = json.loads(tf.read_text())
        status = scen.get("final_status", "SUCCEEDED")
        if 0 < counter(state, "wait_calls") <= scen.get("wait_timeouts", 0):
            status = "ACTIVE"
        doc = {"task_id": task_id, "type": "TRANSFER", "status": status,
               "source_endpoint_id": task["src"].split(":")[0],
               "destination_endpoint_id": task["dst"].split(":")[0],
               "files": 3, "files_transferred": 3, "files_skipped": 0, "directories": 1,
               "bytes_transferred": 3072, "faults": 0, "verify_checksum": True,
               "nice_status": "Queued" if status == "ACTIVE" else None,
               "label": flag_value(task["argv"], "--label"),
               "fatal_error": None if status != "FAILED" else
               {"code": "FILE_NOT_FOUND", "description": "No such file or directory"}}
        doc.update(scen.get("task_fields", {}))
        out(stdout=json.dumps(doc) if js else "Status: " + doc["status"])

    if pos[:2] == ["task", "event-list"]:
        events = scen.get("error_events", []) if "--filter-errors" in argv else []
        events = events[:int(flag_value(argv, "--limit", "10"))]
        out(stdout=json.dumps({"DATA": events}) if js else "\n".join(
            e.get("description", "") for e in events))

    out(stderr="fake globus: unsupported command: " + " ".join(argv), rc=2)


if __name__ == "__main__":
    main()
