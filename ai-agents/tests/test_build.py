"""build_claude_skills.sh assembles a self-contained bundle per skill."""
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import AI_AGENTS, run


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dist = Path(tempfile.mkdtemp(prefix="ai-agents-dist-"))
        env = {"DIST": str(cls.dist), "PATH": "/usr/bin:/bin"}
        cls.result = run(["bash", str(AI_AGENTS / "build_claude_skills.sh")], env=env)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dist, ignore_errors=True)

    def skills(self):
        return sorted(p.name for p in (AI_AGENTS / "skills").iterdir() if p.is_dir())

    def test_build_succeeds(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_each_skill_has_matching_name_and_zip(self):
        for skill in self.skills():
            with self.subTest(skill=skill):
                skill_md = (self.dist / skill / "SKILL.md").read_text()
                self.assertRegex(skill_md, r"(?m)^name: {}$".format(re.escape(skill)))
                self.assertTrue(zipfile.is_zipfile(self.dist / (skill + ".skill")))

    def test_bundled_files_are_copies_of_the_sources(self):
        for skill in self.skills():
            for f in (self.dist / skill).rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(self.dist / skill)
                if rel.parts[0] == "references":
                    src = AI_AGENTS / rel.name
                elif rel.parts[0] == "scripts":
                    src = AI_AGENTS / "scripts" / rel.name
                else:
                    src = AI_AGENTS / "skills" / skill / rel
                with self.subTest(skill=skill, file=str(rel)):
                    self.assertEqual(f.read_bytes(), src.read_bytes())

    def test_scripts_named_in_a_skill_are_bundled_with_it(self):
        for skill in self.skills():
            text = (AI_AGENTS / "skills" / skill / "SKILL.md").read_text()
            for name in sorted(set(re.findall(r"scripts/([\w.-]+\.(?:sh|py))", text))):
                with self.subTest(skill=skill, script=name):
                    self.assertTrue((self.dist / skill / "scripts" / name).is_file(),
                                    "{} mentions scripts/{} but the bundle lacks it".format(
                                        skill, name))


if __name__ == "__main__":
    unittest.main()
