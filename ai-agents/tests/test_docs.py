"""The guides, skills and scripts must agree with each other, and stay project-agnostic."""
import importlib.util
import re
import unittest

from helpers import AI_AGENTS, SCRIPTS

REPO = AI_AGENTS.parent
APOLLO_GUIDE = AI_AGENTS / "APOLLO_AGENT_GUIDE.md"
EFFICIENCY_GUIDE = AI_AGENTS / "GPU_EFFICIENCY_GUIDE.md"
EFFICIENCY_SKILL = AI_AGENTS / "skills" / "gpu-efficiency" / "SKILL.md"
HUMAN_PAGES = [REPO / "notebooks" / "data_transfer.md",
               REPO / "notebooks" / "long_jobs_and_gpu_time.md"]

# Words that would mean project-, cohort- or domain-specific material leaked into shared guidance.
DENYLIST = ["casvar", "cohort", "wavlm", "icefall", "lhotse", "zipformer", "zipa", "fbank",
            "utterance", "specaugment", "audio", r"\bk2\b", "corpora", "corpus"]


def docs():
    files = sorted(AI_AGENTS.glob("*.md")) + sorted((AI_AGENTS / "skills").glob("*/SKILL.md"))
    return files + [p for p in HUMAN_PAGES if p.exists()]


def verdict_labels():
    spec = importlib.util.spec_from_file_location("gpu_watch_verdict",
                                                  SCRIPTS / "gpu_watch_verdict.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(mod.VERDICTS)


def table_labels(text):
    """Backticked verdict-looking labels in the first column of markdown table rows."""
    return set(re.findall(r"(?m)^\|\s*`([a-z]+(?:-[a-z]+)*)`(?:\s*\(advisory\))?\s*\|", text))


class VerdictsAgree(unittest.TestCase):
    def test_every_verdict_is_documented_and_no_extra_ones(self):
        labels = set(verdict_labels()) | {"memory-headroom"}
        apollo_s11 = APOLLO_GUIDE.read_text().split("## 11.", 1)[1]
        for name, text in [("GPU_EFFICIENCY_GUIDE.md", EFFICIENCY_GUIDE.read_text()),
                           ("gpu-efficiency SKILL.md", EFFICIENCY_SKILL.read_text()),
                           ("APOLLO_AGENT_GUIDE.md §11", apollo_s11)]:
            with self.subTest(doc=name):
                documented = table_labels(text)
                self.assertEqual(documented, labels,
                                 "{}: documented {} vs script {}".format(
                                     name, sorted(documented), sorted(labels)))


class ScriptsExist(unittest.TestCase):
    def test_every_script_a_document_names_exists(self):
        for doc in docs() + [AI_AGENTS / "README.md"]:
            for name in sorted(set(re.findall(r"scripts/([\w.-]+\.(?:sh|py))", doc.read_text()))):
                with self.subTest(doc=doc.name, script=name):
                    self.assertTrue((SCRIPTS / name).is_file(), "{} names scripts/{}".format(
                        doc.name, name))

    def test_every_script_has_a_usage_header(self):
        # The house style (see apollo_run.sh): a header that names the script and shows how to
        # call it, so an agent can use it from the first lines alone.
        for script in sorted(SCRIPTS.glob("*")):
            if script.suffix not in (".sh", ".py") or script.name.startswith("_"):
                continue
            head = "\n".join(script.read_text().splitlines()[:60])
            with self.subTest(script=script.name):
                self.assertGreaterEqual(head.count(script.name), 2,
                                        "{} header shows no example call".format(script.name))


class ProjectAgnostic(unittest.TestCase):
    def test_no_project_or_domain_terms(self):
        files = docs() + [p for p in SCRIPTS.glob("*") if p.is_file()]
        files += [p for p in (AI_AGENTS / "tests").rglob("*")
                  if p.is_file() and p.suffix in (".py", ".json", ".md", ".txt")
                  and p.name != "test_docs.py"]
        for f in files:
            text = f.read_text(errors="replace").lower()
            for term in DENYLIST:
                with self.subTest(file=str(f.relative_to(REPO)), term=term):
                    self.assertIsNone(re.search(term, text),
                                      "{} contains '{}'".format(f.relative_to(REPO), term))


if __name__ == "__main__":
    unittest.main()
