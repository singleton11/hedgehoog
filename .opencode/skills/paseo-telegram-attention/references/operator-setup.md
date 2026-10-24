# Operator Provisioning (Not Per-Agent Setup)

Nothing in this PR installs a service, edits the current bot, or deploys a trigger.
Provision only after explicit approval and an inventory of existing integrations.

## One Receiver Owns The Bot

1. Inventory every existing webhook and getUpdates consumer across hosts/accounts.
   Do not run this receiver alongside them or steal their bot queue. Prefer a
   dedicated attention bot, or explicitly migrate all consumers into this inbox.
   Never delete a webhook to force polling. A 409 is a fatal ownership problem,
   not an invitation to restart/retry competing pollers.
2. Install the reviewed scripts from this skill into a stable directory outside
   worktrees, for example `~/.local/lib/paseo-telegram-attention/`. Keep both
   `attention_receiver.py` and its imported `telegram_attention.py` together. If
   installing a global OpenCode skill, copy the complete skill including scripts
   and references, not only SKILL.md. Keep all agents on the same reviewed version.
3. Run agents and the receiver under the same trusted Unix account/HOME. State is
   fixed at `~/.local/state/paseo-telegram-attention/`; directory mode is 0700 and
   CLI-created database/WAL/lock files use umask 077. Do not change HOME per agent.
   Restrict same-account code: this is not a sandbox against a malicious agent.
4. Supply `PASEO_TELEGRAM_BOT_TOKEN`, `PASEO_TELEGRAM_USER_ID`, and optionally
   `PASEO_TELEGRAM_CHAT_ID` through the existing private environment mechanism.
   Never inspect/print a raw Paseo execution dump to obtain credentials. For
   systemd, provision a mode-0600 environment file outside the repository and
   reference its path in the example service. Do not commit its contents.
5. Run offline `attention_receiver.py validate` using that actual environment.
   Validation does not contact the bot and cannot establish ownership or health.

The example oneshot service and timer in this directory poll every 30 seconds
without an LLM. A poll can wait 30 seconds (40-second HTTP timeout); overlapping
runs of the same unit do not execute concurrently, and the bot lock covers other
invocations. `run` without `--once` is also available for a supervised foreground
receiver. Choose one mode, not both. Transient backoff is persisted across either
mode; deferred polls exit 2, fatal errors exit 1. Observe failures and disable the
timer until webhook/conflict ownership errors are resolved. Never restart an
unrelated Paseo daemon to install this helper.

After explicit approval, adapt/copy the example units into the user's systemd
unit directory, verify the unit files, reload the user manager, and enable the
timer. Do not enable anything while merely following this skill as an agent.
Check timer activation and successful receiver execution separately. Receiver
health is not proof of actual message delivery or successor execution.

## Supported Hub Adapter

The running Hub's public `/api/openapi.json` was inspected during implementation.
It documents `POST /api/v1/manual-runs` with `runs:dispatch` bearer scope and these
fields: `projectSlug`, `trigger`, `actor`, `deliveryKey`, optional `input` and
`expectedVersionId`. The helper uses that actual HTTP API, not an invented CLI
resume command. Local Hub lifecycle source confirms idle timeout can archive an
execution. No archive revival or durable execution-pause API was verified.

Before enabling `--dispatch hub`:

1. Inspect the running Hub schema and export existing triggers through supported
   operator tools. Reuse a suitable existing integration; do not recreate one
   blindly. A normal GitHub issue-to-PR trigger is not automatically a safe
   attention-resume handler.
2. Provision/review a dedicated `manual.run` trigger with an explicit actor
   allowlist matching this adapter. Use the existing deployment schema and a
   narrowly scoped API key. Supply these process environment variables:

   - `PASEO_ATTENTION_HUB_ORIGIN`: HTTPS origin only, no credentials/path/query.
   - `PASEO_ATTENTION_HUB_TOKEN`: private bearer key with `runs:dispatch`.
   - `PASEO_ATTENTION_PROJECT`: actual project slug.
   - `PASEO_ATTENTION_TRIGGER`: installed manual trigger name.
   - `PASEO_ATTENTION_ACTOR`: actor allowed by that trigger.

3. The verified existing manual trigger pattern is `on: manual.run` with
   `filters.from_users`, an isolated `run.target.worktree`, and a `run.prompt`
   containing `${{ paseo.prompt }}` as untrusted JSON. Do NOT copy a GitHub-only
   `${{ paseo.context }}` placeholder: the inspected manual provider lacks that
   context materializer and can fail after HTTP acceptance. Do not guess YAML
   fields or handler names; validate against the installed Hub before deployment.
4. The trigger must load the installed attention skill, interpret the incoming
   `input` event as data, and reconstruct the original task from `context` plus
   live repository/document state. Preserve plan-review/implementation gates and
   task-level locking. Do not simply replay the old implementation prompt with a
   raw answer appended. Do not interpret user-provided checkpoint strings as shell
   commands. The trigger needs a stable skill path available in new worktrees.
5. Validate the adapter offline with `validate --dispatch hub`. With separate
   authorization, deploy only the intended trigger and read it back. Do not use
   production credentials in shell arguments or dump deployed secret material.
6. Only then switch the installed receiver to `--dispatch hub`. Default dispatch
   is `none`; matched answers remain in durable outbox for manual handling.

The event includes `question_id`, `bot_id`, `delivery_key`, exact `pending`
chat/user/message metadata, original `context` (including question text for new
asks), `update_id`, and the full matching `reply`. The body is sent as manual-run
`input`, not as trusted instructions. One question produces at most one outbox
row. A successor must still deduplicate its own side effects by delivery key.

A 200 response must match delivery key and configured trigger, contain run and
receipt IDs, and report running/succeeded before the local status becomes
`accepted`. This is only dispatch acceptance. Hub may subsequently fail before
launch (for example unavailable context materialization) or the successor may
fail later. Inspect the recorded run through supported Hub diagnostics. There is
no automatic success notification or execution-status monitor in this helper.

HTTP 429/5xx and ambiguous network failures retry durably with the same key;
permanent errors or failed/invalid receipts become `blocked`. After operator
inspection/fix, `retry --question-id ID` requeues blocked events. A crash after
Hub acceptance but before local receipt commit can reissue the same request;
Hub enqueue dedup plus successor idempotency are required, not exactly-once claims.
Never automatically requeue an accepted run that later failed; investigate its
actual effects and use an explicitly authorized recovery workflow.

## Recovery And Acceptance Checks

- Keep existing pending files intact. Adopt verified sent message IDs with
  `register`; do not resend old questions or consume their replies as a smoke test.
- A send crash leaves a durable `sending` intent. Verify delivery out of band,
  then `bind` the actual message ID, or cancel the unresolved intent. Never infer
  an unsent question merely from a missing local confirmation.
- Cancel superseded questions before issuing replacements. An accepted successor
  cannot be canceled by editing SQLite; coordinate through Hub controls.
- For a previously timed-out/archived execution, preserve its failure state and
  record the task as paused externally. A new manual run is a successor, not an
  unarchive operation. Save durable branch/commit/document context before yielding.
- Obtain separate approval for a dedicated live question/reply test. Verify actual
  question registration, reply ingest after process restart, matching receipt,
  new Hub run state, and successor gate-preserving behavior separately. Offline
  tests, timer activation, and an HTTP 200 are not an end-to-end test.
