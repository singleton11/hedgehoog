"""Static policy regression checks; these do not execute an LLM classifier."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".opencode/skills/paseo-classify-issue/SKILL.md"


class ClassifierSkillTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text()

    def test_bot_channel_and_identity_verification(self):
        for requirement in (
            "hub.reply", "hub_reply", "executionId", "demik-paseo-bot",
            "read back the persisted comment", "exact marker and intended route",
            'user.login == "demik-paseo-bot[bot]"', 'user.type == "Bot"',
            '__typename == "Bot"',
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)

    def test_no_personal_comment_fallback(self):
        for requirement in (
            "Never post or edit classifier rationale comments",
            "personal credentials", "Do not switch", "personal-credential fallback",
            "without adding a routing label",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)

    def test_retry_and_history_safeguards(self):
        for requirement in (
            "read all issue comments (paginate)", "do not post a\nduplicate",
            "personal-authored marker is not bot evidence",
            "conflicting bot-authored routes", "never blindly resend",
            "Do not change or delete historical comments",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)

    def test_readiness_and_locking_policy_preserved(self):
        for requirement in (
            "nonblocking per-issue lock", "Never implement, create a PR",
            "If registry inspection fails", "either routing label already",
            "Never override an", "Leave `Delegated to AI` in place",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.text)


if __name__ == "__main__":
    unittest.main()
