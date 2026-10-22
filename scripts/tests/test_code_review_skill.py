"""Static regression checks for the review skill's required-check outcomes."""

from pathlib import Path
import re
import unittest


SKILL = Path(__file__).resolve().parents[2] / ".opencode/skills/code-review/SKILL.md"


class CodeReviewSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = SKILL.read_text(encoding="utf-8")
        publication = text.split("## Publish Once, On The Reviewed SHA\n", 1)[1]
        cls.steps = dict(re.findall(
            r"^(\d+)\. (.*?)(?=^\d+\. |\Z)", publication, re.M | re.S
        ))

    def test_blocked_or_incomplete_review_fails(self):
        outcome = " ".join(self.steps["2"].split())
        self.assertIn(
            "`failure` for actionable defects requiring changes or an incomplete or blocked review",
            outcome,
        )
        self.assertIn("Access failures and incomplete diffs must fail the check", outcome)

    def test_success_requires_a_complete_clean_review(self):
        outcome = " ".join(self.steps["2"].split())
        self.assertIn(
            "`success` only for a complete static review with no actionable findings",
            outcome,
        )
        self.assertNotRegex(outcome, r"`(?:neutral|skipped)` for")

    def test_interruption_fails_the_original_check(self):
        interruption = " ".join(self.steps["4"].split())
        self.assertIn("your recorded check as `failure`", interruption)
        self.assertIn("Do not create a replacement check or mark it successful", interruption)
        self.assertNotIn("`neutral`", interruption)
        self.assertNotIn("`skipped`", interruption)


if __name__ == "__main__":
    unittest.main()
