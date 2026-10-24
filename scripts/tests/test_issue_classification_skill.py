"""Static contract checks; these do not prove an LLM's classification judgment."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".opencode/skills/issue-classification/SKILL.md"


class IssueClassificationSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.flat = " ".join(cls.text.split())

    def test_discoverable_skill(self):
        self.assertTrue(self.text.startswith("---\nname: issue-classification\n"))
        self.assertRegex(self.text, r"(?m)^description: .+complexity.+$")
        self.assertEqual(SKILL.parent.name, "issue-classification")

    def test_both_exact_label_definitions(self):
        for label in ("Requires planning with AI", "Ready for implementation with AI"):
            with self.subTest(label=label):
                self.assertIn(f'gh label create "{label}"', self.text)
                self.assertRegex(self.text, rf"(?m)^\| {re.escape(label)} \|")

    def test_live_context_and_conservative_complexity_criteria(self):
        for requirement in (
            "all comments (paginate)", "relevant code/tests and dependencies",
            "not a PR", "acceptance criteria", "Multiple viable approaches",
            "Data/schema migrations", "security/authentication", "financial calculations",
            "When uncertain, choose **Requires planning with AI**",
            "No existing planning/review approval gate is pending",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.flat)

    def test_label_mutations_are_narrow_idempotent_and_verified(self):
        for requirement in (
            "Create either missing label", "Reuse existing definitions",
            "Never use `--force`", "Immediately before mutation, re-read",
            "remove only the opposite classification label if present",
            "Preserve all unrelated labels, including `Delegated to AI`",
            "Never replace the complete label list",
            "If already correct, make no mutation",
            "Read back the persisted issue labels", "Require exactly one of this pair",
            "Permission/API failures are blockers",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.flat)

    def test_existing_plan_cannot_be_self_approved(self):
        for requirement in (
            "load the `planning` skill", "native status dropdown",
            "Never self-approve or change the dropdown",
            "verified user-approved plan covering the current scope",
            "Material scope changes require renewed planning/review",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.flat)

    def test_handoff_does_not_install_automation_or_authorize_implementation(self):
        self.assertIn("does not install a watcher, webhook, or Paseo trigger", self.flat)
        self.assertIn("stop implementation and hand off to the `planning`", self.flat)
        self.assertIn("only if implementation was requested", self.flat)
        self.assertIn("A label alone does not authorize coding", self.flat)
        self.assertIn("Do not post duplicate classification comments", self.flat)

    def test_issue_to_pr_invokes_classification_before_implementation(self):
        text = (ROOT / ".opencode/skills/issue-to-pr/SKILL.md").read_text()
        section = text.split("## 2. Implement And Validate\n", 1)[1].split("## 3.", 1)[0]
        self.assertLess(section.index("load the `issue-classification` skill"),
                        section.index("Use a dedicated non-default branch"))
        self.assertIn("stop implementation", section)
        self.assertIn("Ready for\n  implementation with AI", section)
        self.assertIn("existing `planning` approval", section)


if __name__ == "__main__":
    unittest.main()
