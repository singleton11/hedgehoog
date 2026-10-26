"""Static regression checks for issue-to-PR publication requirements."""

from pathlib import Path
import re
import unittest


SKILL = Path(__file__).resolve().parents[2] / ".opencode/skills/issue-to-pr/SKILL.md"


class IssueToPRSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text(encoding="utf-8")
        cls.sections = {
            title: " ".join(body.split())
            for title, body in re.findall(
                r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", cls.text, re.M | re.S
            )
        }

    def test_required_outcomes_include_published_state_and_issue_attachment(self):
        outcomes = self.sections["Required Outcomes"]
        for requirement in (
            "`isDraft: false`",
            "`closingIssuesReferences` includes the exact issue URL",
            "persisted comment linking to this PR and its validation evidence",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, outcomes)

    def test_drafts_are_not_a_blocker_fallback(self):
        outcomes = self.sections["Required Outcomes"]
        self.assertIn("Never create a draft PR or convert a PR to draft", outcomes)
        self.assertIn("do not create a PR yet", outcomes)
        self.assertIn("leave the PR non-draft", outcomes)
        # Guard the old contradictory instructions as well as the new policy.
        self.assertNotRegex(
            " ".join(self.text.split()),
            r"(?i)(use a draft if|keep the PR in draft|create a draft while)",
        )

    def test_creation_and_existing_draft_recovery(self):
        creation = self.sections["4. Create A Review-Ready PR With A Closing Link"]
        self.assertIn("gh pr create --repo", creation)
        command = re.search(r"^gh pr create .+$", self.text, re.M).group()
        self.assertNotIn("--draft", command)
        self.assertIn("Omit `--draft`", creation)
        self.assertIn("gh pr ready <pr-number> --repo <owner/repo>", creation)
        self.assertIn("do not create another PR or claim success", creation)

    def test_verification_reads_persisted_draft_and_issue_state(self):
        verification = self.sections["5. Verify The Published PR"]
        self.assertIn("Require `isDraft: false` in the read-back response", verification)
        self.assertIn("re-read until GitHub confirms false", verification)
        self.assertIn("compare issue URLs, not numbers alone", verification)
        self.assertIn("If any link is missing, correct the description", verification)
        self.assertIn("Follow pagination", verification)
        self.assertIn("Do not bypass required checks", verification)

    def test_issue_backlink_is_idempotent_and_verified(self):
        attachment = self.sections["6. Attach The PR Back To The Issue"]
        for requirement in (
            "in addition to the closing link, not a substitute",
            "Read all issue comments first (paginate if needed)",
            "exact PR URL rather than posting duplicates on retries",
            "`hub.reply` (also exposed as `hub_reply`)",
            "this arrival's `executionId`",
            "exact originating issue (repository, issue number, and issue kind)",
            "Hub reply cannot select an arbitrary target",
            "Manual sessions, cross-target requests",
            "fail closed",
            "no personal-credential fallback or account switching",
            "<!-- paseo-issue-pr:v1 issue=ISSUE_NUMBER pr=FULL_PR_URL -->",
            "verify a persisted comment contains the exact PR URL",
            '`user.login == "demik-paseo-bot[bot]"`',
            '`user.type == "Bot"`',
            "read back before any retry; never blindly resend",
            "Record the comment URL",
            "do not report this workflow as complete",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, attachment)

    def test_attention_uses_structured_prompt_and_safe_fallback(self):
        attention = self.sections["Attention Questions And Approval Gates"]
        for requirement in (
            "load `paseo-telegram-attention`",
            "confirmed Paseo Hub session",
            "`{prompt,context}`",
            "`approval`, `clarification`, or `blocker`",
            "`context_label`",
            "`context_url`",
            "2-5 explicit `options`",
            "`recommended` names exactly one option",
            "first with `(Recommended)`",
            "700 visible characters",
            "HTML escaping and 4096 UTF-16 hard limit",
            "explicit decline/cancel choice",
            "A recommendation is never consent",
            "`Other / write a reply` button gives only instructions, not an answer",
            "Telegram Reply remains the free-text fallback",
            "nonempty durable `context`",
            "legacy `{text,context}`",
            "reply.text-only",
            "Do not fake a text reply",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, attention)

    def test_attention_preserves_verification_and_workflow_gates(self):
        attention = self.sections["Attention Questions And Approval Gates"]
        for requirement in (
            "`schema_version=1`",
            '`answer.source: "callback_query"`',
            "exact option description",
            "raw update",
            "Legacy text events are unchanged",
            "canonical durable verification",
            "strict identity binding",
            "task locking",
            "delivery-key idempotency",
            "Pause the task without LLM polling or claiming completion",
            "classification labels, document approval",
            "non-draft PR, closing-link, issue-comment, and completion gates",
            "returns to human review, never directly to implementation",
            "No merge, deployment, force push, self-approval",
            "Runtime activation requires separate operator approval",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, attention)
        implementation = self.sections["2. Implement And Validate"]
        self.assertIn("load the `issue-classification` skill", implementation)
        self.assertIn("honoring any existing `planning` approval gate", implementation)
        self.assertIn("All must pass before claiming the PR is ready", implementation)

    def test_completion_gate_requires_all_outcomes(self):
        completion = self.sections["Completion Gate"]
        self.assertIn("all required outcomes and validation/mergeability checks pass", completion)
        self.assertIn("confirmed non-draft status", completion)
        self.assertIn("persisted issue comment URL", completion)
        self.assertIn("only if the auto-close setting was verified", completion)


if __name__ == "__main__":
    unittest.main()
