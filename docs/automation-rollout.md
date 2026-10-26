# Automation rollout: issue 42

## Scope and authorization

Approved plan: https://docs.google.com/document/d/1PGfhR6xWlqED2YYk8ypGxGl_0t20m9vxjzTRbiVfbHs/edit

Approved revision: `AHj4eMRJsnNgLm7SH4kZmDFhvjOP6JF98eatAivk4krJTdc7UM00CPSCOIWO2yXKFOApvS09ep9-CpN8eo_K4vPqAu6UHEZYC9I1_tIGKKa8`.
Binding: tab `t.0`, chip `kix.27w96x2reob2`, definition `kix.review_status`.

This is a reviewable repository bundle, not a deployment. Implementation approval
DOES NOT authorize global skill/service changes, receiver restarts, production
reply consumption, or live test messages. Request explicit operator authorization
for rollout and separate permission for live tests. Do not claim the issue's live
symptoms are resolved from these offline checks.

Reviewed upstream baseline: `ae984ab` (includes native Docs formatting, Telegram
prompts/callback routing, and classifier bot identity). Reuse those implementations;
do not recreate features or bulk-restyle existing plans.

## Read-only inventory

Run from the reviewed checkout:

```sh
python3 scripts/check_automation_rollout.py
python3 -m unittest discover -s scripts/tests -v
python3 -m unittest discover -s .opencode/skills/planning/tests -v
python3 -W error::ResourceWarning -m unittest discover -s .opencode/skills/paseo-telegram-attention/scripts -p 'test_*.py' -v
systemctl --user cat paseo-attention-receiver.service paseo-attention-receiver.timer
```

The checker never executes installed/source components, writes files, accesses
SQLite, reads credentials, sends messages, copies files, or restarts services.
Exit 0 means byte-identical listed files, 1 means drift/missing dependency, 2 means
invalid/unreadable input. Its JSON includes each source and installed SHA-256,
component and relative deployment path. Use `--home` and `--source-root` for an
isolated staged comparison. Symlinks escaping either root are rejected. Matching
hashes establish consistency, not live functionality or authorization. The manifest
is a bounded file mapping, not an installer or a complete external service export.

Initial read-only inspection on 2026-10-26 found:

| Component | Source / deployment mapping | Discrepancy |
| --- | --- | --- |
| Docs policy | `.opencode/skills/planning/SKILL.md` -> `~/.config/opencode/skills/paseo-planning/references/planning-skill.md` | Stale snapshot |
| Docs template | planning `references/create-plan.json` -> adapter same relative path | Stale plain template |
| Docs dependencies | planning `references/plan-content.json`, `scripts/build_plan.py` -> adapter same paths | Both missing |
| Telegram skill | complete repository attention skill -> global attention skill | Stale plain-text instructions; prompt module missing |
| Stable receiver | attention `scripts/{attention_receiver,telegram_attention,attention_prompt}.py` -> `~/.local/lib/paseo-telegram-attention/` | Prompt module missing; older receiver/sender |
| Canonical verifier | `scripts/automation-runtime/verify_event*.py` -> `~/services/paseo-automations/attention-runtime/` | Installed verifier text-only |
| Successor | `scripts/automation-runtime/paseo-attention-resume.SKILL.md` -> global skill | Installed policy uses `reply.text` only |
| Comment producers | See [routing audit](automation-comment-routing.md) | Mixed personal CLI and App channels |

The committed [2026-10-26 inventory](automation-inventory-2026-10-26.json)
records the nonsecret source/installed hash matrix: four missing and eleven stale
components, with no deployment performed. Regenerate it against the final reviewed
commit before rollout for a fresh source/installed hash matrix. Compare again immediately before
any authorized write; concurrent operator changes require reconciliation, not
blind overwrite. External adapter wrappers are not byte-for-byte copies of the
repository policies. Preserve their routing, registration and lifecycle contracts.

The observed receiver unit invokes only the stable bundle with `run --dispatch
hub --once`. Timer interval is 30 seconds; environment files remain private and
are never read by this checker. Do not change the units simply to install code.

## Coordinated operator procedure (requires explicit permission)

1. Record source commit, checker report, runtime baseline hashes, existing unit
   configuration and a private recoverable component snapshot. Obtain an idle
   window from the existing receiver owner. Do not create a competing poller,
   clear locks, delete a webhook, or consume the bot queue for diagnosis.
2. Back up the live SQLite database through Python's SQLite `Connection.backup`
   API to a private operator-selected destination. A raw database-file copy may
   omit WAL state. Record pending question/outbox/UI work without exposing answer
   content or credentials. Preserve existing journals, receipts and lock inodes.
3. Review the candidate verifier/successor against their recorded base hashes.
   Stage them and the complete compatible attention bundle together BEFORE any
   structured prompt is enabled. Keep `verify_event_by_key.py` as the only input
   loader; no hand-built callback-to-text conversion. Preserve issue-only routing,
   accepted-event receipt repair, per-task journals/locks and all approval gates.
4. Synchronize all manifest-listed planning dependencies into the adapter layout.
   Apply repository policy changes to installed wrappers deliberately, preserving
   their issue locks, monitor registration, recovery records and lifecycle rules.
   Apply the comment-route audit, including its unsupported-route blockers.
5. Only the operator coordinates the receiver stop/start or idle-window activation.
   Do not restart unrelated Paseo services or redeploy unrelated triggers. Verify
   the existing manual trigger remains allowlisted with scoped credentials and
   dispatches a NEW successor, never an archived execution. No secret export.
6. Re-read every installed file and compare hashes, then run the offline checks
   against the staged/installed bundle. A matching manifest is insufficient until
   the wrapper and continuation contracts have also been reviewed.
7. With separate live-test permission, execute the checks below and record exact
   artifact/receipt references and source SHA. Until then report rollout pending.

## Authorized live acceptance and evidence

- Docs: create a disposable app-generated plan, not a fabricated issue monitor
  registration. Read back native TITLE/HEADING_1, body font/spacing, bullet and
  numbered lists, clickable issue URL, monospace paths, full content, and exactly
  one bound three-option dropdown initially `review requested`. Make a targeted
  revision-guarded edit; preserve binding/status and human comments. Do not select
  either user-owned status. Inspect desktop and narrow/mobile readability. Also
  inspect a real subsequent planning round to prove the adapter path is used.
- Telegram: with harmless dedicated issue-bound context, observe escaped HTML,
  two to five options, exactly one first recommendation and Other / write a reply.
  Exercise one normal button answer and a separate Reply fallback. Other must not
  answer. Verify one canonical answer/outbox, strict verification, dispatch receipt
  and one new successor with intact approval gates. Wrong sender/chat/message,
  stale/unknown token and duplicates must not authorize work. Legacy pending
  questions survive; acknowledgement/cleanup retries cannot redispatch.
- GitHub: verify exact target, trusted marker and REST author type `Bot`, login
  `demik-paseo-bot[bot]` on representative authorized workflow replies. Never toggle
  labels solely to manufacture smoke tests; do not rewrite historical comments.
- Keep offline tests and runtime evidence distinct. Capture readable screenshots,
  an actual CLI/API or application scenario video, and an animated excerpt, inspect
  all three, and upload only to durable reviewer-accessible hosting. Link each
  individually with source SHA, steps and observed result. Do not publish private
  financial data, inbox contents, tokens or unrelated desktop content. The PR media
  gate remains blocked if capture/upload/access verification is unavailable.

## Rollback

Stop creating new structured prompts first. An old helper cannot process callback
events. Preserve additive schema and newer answers; drain or review pending
callback/outbox/UI work with compatible tooling, then roll back the entire
compatible bundle. Never restore an old database over newer responses, resend
uncertain questions, erase receipts/journals, or reset human plan review state.
