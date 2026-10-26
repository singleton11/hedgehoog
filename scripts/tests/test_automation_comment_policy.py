"""Offline policy checks; these do not prove deployed Hub routing or identity."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / ".opencode/skills"
ROUTING = ROOT / "docs/automation-comment-routing.md"
COMMENT_SKILLS = ("issue-to-pr", "planning", "code-review", "paseo-classify-issue")
ADAPTERS = (
    "paseo-classify-issue",
    "paseo-issue-to-pr",
    "paseo-planning",
    "paseo-agent-review",
    "paseo-fix-review",
    "paseo-resolve-conflicts",
    "paseo-user-comments",
    "paseo-attention-resume",
    "paseo-telegram-attention",
)


def normalized(text):
    return " ".join(text.split())


class AutomationCommentPolicyTests(unittest.TestCase):
    def test_no_personal_comment_write_recipes_in_repository_skills(self):
        # Scan all skills so adding a new producer cannot evade this regression.
        for path in SKILLS.glob("*/SKILL.md"):
            text = path.read_text(encoding="utf-8")
            fences = re.findall(r"```[^\n]*\n(.*?)```", text, re.S)
            commands = "\n".join(fences)
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertNotRegex(commands, r"\bgh\s+(?:issue|pr)\s+comment\b")
                self.assertNotRegex(commands, r"\bgh\s+pr\s+review\b")
                self.assertNotRegex(
                    commands,
                    r"(?is)\bgh\s+api\b[^\n]*(?:--method\s+|-X\s*)(?:POST|PATCH|PUT|DELETE)"
                    r"[^\n]*(?:/comments|/reviews)",
                )
                self.assertNotRegex(
                    commands, r"\b(?:addComment|updateIssueComment|addPullRequestReview)\s*\("
                )
                # Reject inline prescriptions too, but allow prohibitions.
                for sentence in re.split(r"(?<=[.!?])\s+", normalized(text)):
                    if re.match(r"(?i)(?:never|do not)\b", sentence):
                        continue
                    self.assertNotRegex(
                        sentence,
                        r"(?i)\b(?:publish|post|send|update)[^.!?]{0,100}"
                        r"(?:with|using|via)\s+`?gh\s+(?:issue|pr)\s+comment\b",
                    )

    def test_each_comment_policy_binds_exact_target_and_requires_bot_readback(self):
        for name in COMMENT_SKILLS:
            text = normalized((SKILLS / name / "SKILL.md").read_text(encoding="utf-8"))
            with self.subTest(skill=name):
                for requirement in (
                    "docs/automation-comment-routing.md",
                    "hub.reply",
                    "hub_reply",
                    "this arrival's `executionId`",
                    "`content`",
                    "provider-native",
                    "arbitrary target",
                    "fail closed",
                    "cross-target",
                    "marker",
                    "paginate",
                    "read back",
                    "REST",
                    '"demik-paseo-bot[bot]"',
                    '"Bot"',
                ):
                    self.assertIn(requirement, text)

    def test_shared_contract_rejects_unsupported_routing_and_edits(self):
        text = normalized(ROUTING.read_text(encoding="utf-8"))
        for requirement in (
            "triggering execution's provider-native target",
            "repository, issue/PR number, and item kind",
            "Manual sessions, cross-target writes",
            "fail closed",
            "another execution's ID",
            "installation token",
            "not a supported fallback",
            "Hub comment edits are not demonstrated",
            "Stop that route before dependent edits/pushes",
            "read back before any retry; never blindly resend",
            "durable nonsecret delivery intent",
            "Paginate the exact target's conversation comments",
            'user.login == "demik-paseo-bot[bot]"',
            'user.type == "Bot"',
            "issues/{number}/comments",
            "issue_url",
            "comment ID and URL durably",
            "not proof of persistence or bot authorship",
            "Preserve historical human and bot comments",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)

    def test_inventory_covers_all_installed_adapters_without_loading_host_files(self):
        text = ROUTING.read_text(encoding="utf-8")
        for adapter in ADAPTERS:
            with self.subTest(adapter=adapter):
                self.assertRegex(text, rf"(?m)^\| `{adapter}` \|")
                self.assertRegex(
                    text, rf"(?m)^\| `{adapter}/SKILL.md` \| `[a-f0-9]{{64}}` \|$"
                )
        for marker in (
            "paseo-classification:v1",
            "paseo-issue-pr:v1",
            "paseo-plan:v1",
            "paseo-plan-revision:v1",
            "paseo-plan-approved:v1",
            "paseo-plan-blocked:v1",
            "paseo-review-findings:v1",
            "paseo-review-fix:v1",
            "paseo-review-fix-attempt:v1",
            "hedgehoog-reconcile:N:H:B",
            "paseo-user-comment:v1",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_staging_does_not_claim_deployment_or_weaken_workflow_gates(self):
        text = normalized(ROUTING.read_text(encoding="utf-8"))
        for requirement in (
            "not deployed",
            "separate operator approval",
            "all three mandatory media formats",
            "two-attempt total limit",
            "Deduplicate by source kind/ID across execution IDs",
            "shared-lock/head/base/push guards",
            "native document approval",
            "PR-wide duplicate-check rule",
            "Check output remains authoritative",
            "required comment was not verified",
            "pending rollout remain explicit gaps",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)
        review = normalized((SKILLS / "code-review/SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Publish through the check only, not duplicate PR reviews or comments", review)
        self.assertIn("If the agent has already submitted a check on the GitHub PR, do nothing", review)
        planning = normalized((SKILLS / "planning/SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Only the user may select", planning)
        self.assertIn("must be verified before a dependent label handoff", planning)


if __name__ == "__main__":
    unittest.main()
