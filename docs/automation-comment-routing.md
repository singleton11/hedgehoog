# Automated GitHub Comment Routing

## Contract

All automated GitHub conversation comments use Hub's configured bot via
`hub.reply` (also exposed as `hub_reply`), with this arrival's `executionId` and
`content`. Never substitute a personal GitHub CLI login, switch global accounts,
copy credentials, or use direct comment-write API/CLI calls as a fallback.
Read-only API inspection remains allowed. PR descriptions, labels, and Checks
API operations are distinct operations governed by their existing workflows;
this policy grants no new authority for them.

Hub reply routes to the **triggering execution's provider-native target**. It
has no demonstrated arbitrary repository/issue/PR selector. Before any send:

1. Resolve the current execution and trusted provider context. Require the exact
   repository, issue/PR number, and item kind for the intended destination, and
   confirm the live GitHub object. Never infer a target from a URL in prose, the
   current branch, a linked issue, an old execution ID, or a Telegram checkpoint.
2. Confirm the native reply channel is the required conversation. For an inline
   review/review event, do not assume Hub posts a PR conversation comment or can
   select a particular thread. If this is not established, fail closed.
3. Manual sessions, cross-target writes, unavailable tools, or missing/ambiguous
   routing evidence fail closed. Report a blocker locally; do not invoke reply
   optimistically, use another execution's ID, or invent a target argument.
   A GitHub token, including an installation token, is not a supported fallback.
4. Preserve workflow serialization/locks and all live approval gates. Record a
   durable nonsecret delivery intent: task key, marker, intended target and
   payload, execution ID, and any prior delivery evidence. Keep it outside an
   ephemeral worktree using the workflow's existing durable journal.
5. Paginate the exact target's conversation comments before sending. Reuse only
   a matching verified bot comment with the expected marker and payload. A
   marker copied by a human or another bot is not evidence. Conflicting bot
   markers or stale payloads require reconciliation, not blind duplicates.
6. Send once through Hub and read back GitHub REST persisted state. On uncertain
   delivery, read back before any retry; never blindly resend. A successful Hub
   return alone is not proof of persistence or bot authorship. Record the verified
   comment ID and URL durably before advancing a dependent handoff.

Readback uses `GET /repos/{owner}/{repo}/issues/{number}/comments` with pagination
for both issues and PR conversations (PRs also have issue numbers). Fetch a known
comment ID through `GET /repos/{owner}/{repo}/issues/comments/{comment_id}` as
needed and verify `issue_url` belongs to the exact intended issue/PR. Require:

- `user.login == "demik-paseo-bot[bot]"` and `user.type == "Bot"` from REST.
- The exact durable marker, intended body/evidence, and correct issue/PR target.
- The required reviewed SHA, document revision/status, PR URL, or source ID.

Do not rely on display names, GraphQL-normalized logins, a gh view response that
omits type, command exit codes, or a local draft. Preserve historical human and
bot comments; do not rewrite them to simulate compliance. An existing personal
comment does not satisfy a bot-only gate; one verified bot record may be posted
if the supported exact-target route exists and no matching bot record exists.

Hub comment edits are not demonstrated here. If a workflow requires updating a
reservation in place, do not pretend reply is an edit or fall back to a direct
API patch. Stop that route before dependent edits/pushes if its mandatory
reservation/finalization contract cannot be met. A separate terminal comment or
changed marker protocol requires independent review of the adapter and its
consumers, not an agent's improvisation.

## Route Inventory And Staged Adapter Instructions

Inventory taken read-only from `/home/singleton/.config/opencode/skills/paseo-*/SKILL.md`
on 2026-10-26. This document stages operator-reviewable replacement instructions;
it is **not deployed** and does not claim installed behavior has changed. Apply
the Contract to every route below, including progress, questions, and failures.

| Adapter | Automated comment routes and durable identity | Required staged change / safe boundary |
| --- | --- | --- |
| `paseo-classify-issue` | Classification rationale: `<!-- paseo-classification:v1:ISSUE_NUMBER -->`. | Already uses Hub with bot verification. Add exact-target proof and manual/cross-target fail-closed gate from the repository skill. Verify the rationale before changing any routing label. |
| `paseo-issue-to-pr` | Issue backlink, validation, progress and blockers: `<!-- paseo-issue-pr:v1 issue=ISSUE_NUMBER pr=FULL_PR_URL -->` for backlink. Other messages need stable purpose/task markers. | Replace the direct CLI comment recipe and unsupported own-comment update instruction with repository issue-to-PR section 6. PR-triggered runs cannot use their PR execution to comment on the linked issue. Attachment remains blocked, not waived. Preserve native-chip/label gates, all three mandatory media formats, non-draft status, closure association, validation and mergeability. |
| `paseo-planning` | Initial announcement `<!-- paseo-plan:v1:ISSUE_NUMBER -->`; revision `<!-- paseo-plan-revision:v1:DOCUMENT_ID:REVISION_ID -->`; approval `<!-- paseo-plan-approved:v1:DOCUMENT_ID:REVISION_ID -->`; blockers `<!-- paseo-plan-blocked:v1:ISSUE_NUMBER:DECISION_KEY -->`. | Replace the entire manual comment fallback with the Contract. Remove direct POST instructions for comments even when a temporary App token exists. GitHub-origin runs may reply only to their verified issue. Manual monitor approval/revision and Telegram continuation runs have no verified native GitHub target; block required comments and dependent label handoffs until a separately reviewed supported route exists. Do not remove the existing App-auth checks for independently authorized manual label operations, but do not execute labels whose required comment was not verified. |
| `paseo-agent-review` | Findings comment and separate remediation command `<!-- paseo-review-fix:v1 sha=FULL_SHA check=CHECK_RUN_ID findings=FINDINGS_COMMENT_ID -->`. Add findings identity `<!-- paseo-review-findings:v1 sha=FULL_SHA check=CHECK_RUN_ID -->`. | Publish each permitted comment only through exact-target Hub reply, read back its bot ID/body/SHA, and deduplicate by check ID and marker. Use the verified findings ID in the command; its complete body must remain the existing single marker line. Keep SHA checks before conclusion and handoff, no commands for incomplete/uncertain reviews, and never repeat the command for the same check. Check output remains authoritative; a failed route cannot manufacture successful review or remediation evidence. |
| `paseo-fix-review` | Reservation and terminal outcome currently update the same `<!-- paseo-review-fix-attempt:v1 check=CHECK_ID sha=H execution=EXECUTION_ID -->` comment. | Hub can create only on the exact triggering PR; in-place update support is unverified. Fail closed before reservation-dependent code changes rather than replacing the update with a personal patch. Stage a separate reviewed finalization protocol before enabling this route. Preserve shared PR lock, check-ID deduplication, bot/source/SHA verification, and two-attempt total limit, including blocked attempts. Never echo the triggering remediation command. |
| `paseo-resolve-conflicts` | Terminal success, ambiguity question, failure/blocker with hidden key `hedgehoog-reconcile:N:H:B` (use `<!-- hedgehoog-reconcile:N:H:B -->`). | Route every comment through verified same-PR Hub reply with bot readback and exact key/payload. Retain terminal-attempt no-retry gate, shared lock and original H/B guards. Clean/no-op runs still post nothing. Unknown route blocks before comment-dependent reconciliation; never push and claim successful reporting without verification. |
| `paseo-user-comments` | Busy reply, informational answer, clarification, reservation and terminal response: `<!-- paseo-user-comment:v1 source=KIND id=ID execution=EXECUTION_ID -->`. | Deduplicate by source kind/ID across execution IDs. Busy responses also need a stable source/purpose identity, not unmarked retries. Verify source event association separately from Hub destination. Inline/review events require proof of the actual native reply channel; do not assume a PR conversation/thread selector. Mandatory same-response update is unsupported until reviewed Hub edit support or a revised protocol exists; stop before reservation-dependent work. Preserve source author/timestamp checks and all shared-lock/head/base/push guards. |
| `paseo-attention-resume` | Delegates planning revisions/approval and implementation reporting to the corresponding adapters; journal records comment intents and evidence. | A new manual execution must not reuse the old execution ID or treat checkpoint URLs as a native GitHub reply target. Preserve canonical-event verification, delivery-key idempotency and live plan gates; report unsupported GitHub writes locally and retain the pending task outcome. |
| `paseo-telegram-attention` | No direct GitHub comment writer; checkpoints/continuation delegate to the above routes. | No comment deployment change. Keep one durable receiver, verified successor, no polling or bypass through a Telegram acknowledgement. |

The repository `code-review` skill intentionally publishes **through the check
only**, not PR comments or reviews. The installed `paseo-agent-review` has a
different findings/command contract consumed by the fixer. Do not copy its
comment behavior into the repository check-only skill, remove fixer-required
commands, or erase the repository's PR-wide duplicate-check rule by copying the
installed adapter wholesale. Reconcile the two contracts explicitly during
operator review; neither a comment nor a bot identity grants review approval.

The generic repository `issue-classification` skill reports in the current
conversation and does not require a GitHub comment. Its automated adapter above
owns the rationale route; no new comment route is introduced here.

## Read-Only Baseline Hashes

All paths are relative to `/home/singleton/.config/opencode/skills/`; hashes cover
only each installed `SKILL.md`, not its dependencies. Re-read and reconcile if
any baseline differs before an operator applies a reviewed adapter update.

| Path | SHA-256 |
| --- | --- |
| `paseo-agent-review/SKILL.md` | `054ef8207ba29c854be3db026443c70bdc8c4605f703bc07bc876b57404d37b7` |
| `paseo-attention-resume/SKILL.md` | `11c3f720ea8f39768f19b1559f1112e74f6ecd4869c4b8d9a25b97c41e897af9` |
| `paseo-classify-issue/SKILL.md` | `3adfe43ba89b4f8c6638d94ba74d8d829114f9fbac58a43cc38b2563c3e8edc9` |
| `paseo-fix-review/SKILL.md` | `a83b86ad2c271d198d068dbefe13724b110ecaab3f2f76e25daae1d24c3a9085` |
| `paseo-issue-to-pr/SKILL.md` | `44af563cd6ba909dfcffbf8b4eeb3ea5464332a199cc6f2cc0a254a75d477323` |
| `paseo-planning/SKILL.md` | `817eb0576425aecd05dce5dd2ac79d811e6ad290e85f507c8a3f528eadbf03fa` |
| `paseo-resolve-conflicts/SKILL.md` | `2d24c673cef42feef7db41f2747a196f4b91c258243f609559179accd48a14d1` |
| `paseo-telegram-attention/SKILL.md` | `7456483ce3503e67361cb6bb9ef5a1b47b34edb1b286160779832d7b82748787` |
| `paseo-user-comments/SKILL.md` | `1a2040df520fef7a2592060fd20b6c58695381f7b29b696707e10e7cd5c36374` |

## Operator Review And Verification

Repository edits do not synchronize deployed adapters or services. No deployment,
trigger registration, token changes, or fabricated smoke issues are authorized.
After separate operator approval, prepare reviewed adapter replacements from the
mapping, compare baselines, and deliberately synchronize required repository
snapshots/dependencies. Preserve registration, native document approval, locks,
check ownership, media gates, source identity, and lifecycle behavior. Do not
replace a whole installed adapter with a generic repository skill.

Offline checks assert policy contracts, not live routing:

```sh
python3 -m unittest discover -s scripts/tests -p 'test_automation_comment_policy.py' -v
python3 -m unittest discover -s scripts/tests -p 'test_issue_to_pr_skill.py' -v
```

A later authorized integration check must use the actual triggering target and
record exact bot-authored REST readback with marker, ID, URL and intended body.
Exercise uncertain-send recovery without duplicate comments and verify unsupported
manual/cross-target/edit requests perform no comment writes or dependent handoffs.
Verify review/fixer command consumers and label/Docs approval transitions remain
unchanged. Unavailable live checks and pending rollout remain explicit gaps, not
successful deployment evidence.
