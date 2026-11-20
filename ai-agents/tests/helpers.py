"""Shared test helpers: paths, a sandbox with fake CLIs on PATH, and a subprocess wrapper.

Tests use only the standard library (unittest), so `python3 -m unittest discover -s ai-agents/tests`
runs them on any Python >= 3.9 with nothing installed (3.12 is the reference version). pytest
runs them too.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
AI_AGENTS = TESTS.parent
SCRIPTS = AI_AGENTS / "scripts"
FAKES = TESTS / "fakes"
FIXTURES = TESTS / "fixtures"

if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


class Sandbox:
    """A temporary directory with `bin/` (fake CLIs, first on PATH), `state/` and `pvc/`.

    `pvc/` stands in for the PVC mount, with `pvc/work` as WORKDIR. `env()` returns an
    environment that points the scripts and the fakes at all of this; no cluster.env is read.
    """

    def __init__(self, runai_scenario=None, globus_scenario=None):
        self.root = Path(tempfile.mkdtemp(prefix="ai-agents-test-"))
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        self.gstate = self.root / "gstate"
        self.pvc = self.root / "pvc"
        self.workdir = self.pvc / "work"
        for d in (self.bin, self.state, self.gstate, self.workdir):
            d.mkdir(parents=True)
        self._wrap("runai", FAKES / "runai_stub.py")
        self._wrap("globus", FAKES / "globus_stub.py")
        self.runai_scenario_file = self.root / "runai_scenario.json"
        self.globus_scenario_file = self.root / "globus_scenario.json"
        self.set_runai_scenario(runai_scenario or {})
        self.set_globus_scenario(globus_scenario or {})

    def _wrap(self, name, target):
        p = self.bin / name
        p.write_text('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, target))
        p.chmod(0o755)

    def add_bin(self, name, script):
        """Put an executable shell script named `name` on the sandbox PATH (e.g. a fake nvidia-smi)."""
        p = self.bin / name
        p.write_text(script)
        p.chmod(0o755)
        return p

    def set_runai_scenario(self, scen):
        self.runai_scenario_file.write_text(json.dumps(scen))

    def set_globus_scenario(self, scen):
        self.globus_scenario_file.write_text(json.dumps(scen))

    def env(self, **extra):
        e = dict(os.environ)
        e.update({
            "PATH": "{}:{}".format(self.bin, os.environ.get("PATH", "/usr/bin:/bin")),
            "APOLLO_ENV": str(self.root / "no-cluster.env"),
            "RUNAI": str(self.bin / "runai"),
            "PROJECT": "test-proj",
            "PVC": "test-pvc",
            "MOUNT": str(self.pvc),
            "WORKDIR": str(self.workdir),
            "FAKE_RUNAI_STATE": str(self.state),
            "FAKE_RUNAI_SCENARIO": str(self.runai_scenario_file),
            "FAKE_RUNAI_SANDBOX": str(self.workdir),
            "FAKE_GLOBUS_STATE": str(self.gstate),
            "FAKE_GLOBUS_SCENARIO": str(self.globus_scenario_file),
            "POLL_S": "0",
            "LOG_RETRY_S": "0",
        })
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def runai_calls(self):
        return _read_calls(self.state / "calls.jsonl")

    def globus_calls(self):
        return _read_calls(self.gstate / "calls.jsonl")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _read_calls(path):
    if not path.exists():
        return []
    return [json.loads(line)["argv"] for line in path.read_text().splitlines() if line.strip()]


def run(argv, env=None, cwd=None, input=None, timeout=120):
    """Run a command and return the CompletedProcess, with text stdout and stderr."""
    return subprocess.run([str(a) for a in argv], env=env, cwd=cwd, input=input,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          timeout=timeout)


def calls_matching(calls, *words):
    """The recorded argvs whose leading non-flag words start with `words`."""
    res = []
    for argv in calls:
        lead = [a for a in argv if not a.startswith("-")]
        if lead[:len(words)] == list(words):
            res.append(argv)
    return res
