---
name: paseo-telegram-attention
description: When the agent needs to ask a question, request approval, report a blocker, or otherwise needs user attention, check for hub.finish_execution. Only in a Paseo Hub session, notify the user through Telegram and wait for their reply before continuing.
---

# Paseo Hub Telegram Attention

## Activation Gate

Before doing anything else, inspect the tools available in the current session.
Activate this workflow **only when `hub.finish_execution` is available** (including
its namespaced tool representation, such as `hub_finish_execution`). If tools are
exposed through a tool catalog, check that catalog for the exact tool.

If that tool is absent, stop using this skill and use the session's normal
question/notification mechanism. A Paseo-looking working directory, environment
variables, or the presence of a Telegram token is not evidence of a Hub session.
Do not send Telegram messages or poll Telegram outside a confirmed Hub session.
The helper cannot inspect session tools; running it does not establish this gate.

## When To Use

In a confirmed Hub session, use Telegram whenever you need a decision,
clarification, approval, help with a blocker, or any other user attention. Send
the question before yielding or calling a tool that would wait for local input.
Do not send routine progress updates that do not require attention.

## Recipient And Credentials

The helper supports exactly these process environment variables, with no aliases,
configuration-file lookup, hardcoded recipient, or discovery from incoming updates:

| Variable | Requirement and precedence |
| --- | --- |
| `PASEO_TELEGRAM_BOT_TOKEN` | Required nonempty bot token; read only from the environment. |
| `PASEO_TELEGRAM_USER_ID` | Required explicitly configured authorized user; positive decimal integer. |
| `PASEO_TELEGRAM_CHAT_ID` | Optional only when absent. If absent, use the validated user ID as `chat_id`. If present (even empty), validate it and require equality with the user ID. |

IDs must use ASCII digits without signs, whitespace, or leading zeros, and be at
most `2**52 - 1`. Zero, negative/group IDs, malformed IDs, missing user IDs, and
mismatches block sending. A chat ID alone never authorizes a recipient. The fallback
is a deterministic mapping of the operator's configured private user, not consent
inferred from an update. The user must already have started/unblocked the bot.

Check the actual session without printing values (from the repository root):

```sh
python3 .opencode/skills/paseo-telegram-attention/scripts/telegram_attention.py validate
```

This validates the actual environment offline and prints only a success message or
a sanitized configuration error naming the variable to fix. Do not run `env`,
`printenv`, shell tracing, or commands that echo credentials. Do not assume an
undocumented alias exists. The implementation session had nonempty token and user
variables and no chat variable; this is an observation, not a guarantee about future
sessions. Fresh agents must validate their own environment.

Never embed the token in repository files, messages, logs, command arguments, or
pending-state files. Do not print request URLs, raw API bodies, or raw exceptions;
Telegram's API URL contains the token. Do not enable HTTP tracing. Send only the
context needed to answer, not secrets or unnecessary private file contents.

## Send The Question

1. Confirm the Hub activation gate and validate configuration. Identify the reply
   delivery mechanism before sending where possible. If no safe receiving path is
   available, report that limitation and stay blocked, not a missing-chat-ID error.
2. Form one concise message with repository/session context, the exact question or
   action, and options. State that the agent is waiting. Ask the user to use
   Telegram's **Reply** action on this message so the answer routes to this session.
   For a notification without a question, explicitly request acknowledgment.
3. In a confirmed Hub session only, run the sender from the repository root. It
   reads the question on stdin and credentials directly from the environment:

   ```sh
   umask 077
   python3 .opencode/skills/paseo-telegram-attention/scripts/telegram_attention.py send <<'QUESTION'
   hedgehoog / <session context>: <exact question and options>. I am waiting.
   Please use Telegram Reply on this message to answer for this session.
   QUESTION
   ```

4. Replace the placeholders before sending. The standard-library helper POSTs JSON
   to `sendMessage`, uses the resolved `chat_id`, plain text (no `parse_mode`), and
   `{"force_reply": true, "selective": true}`. It refuses redirects and checks
   HTTP 200, JSON `ok: true`, and a positive integer `result.message_id`.
5. **Only exit status 0 with pending-state JSON confirms delivery.** Retain that
   JSON (`chat_id`, `user_id`, `message_id`) in session state before waiting. If
   persisting it across tool/session restarts, use a unique session-specific file
   outside the repository with mode 0600. Never overwrite another pending question.
   The JSON contains no token. Do not lose the pending ID or send another question
   merely because a tool invocation timed out. A failed/ambiguous response is not
   confirmation; a timeout can occur after Telegram accepted a message, so do not
   blindly retry and create duplicates.

The sender is not a reply receiver or production runner. Sending alone never
provides consent and never permits finishing an execution waiting on an answer.

## Wait For The Answer

1. First check whether the session exposes an incoming-message mechanism that
   actually delivers this bot's Telegram replies, including chat, sender, and reply
   metadata. Do not assume that ordinary Hub/GitHub messages are Telegram delivery.
   If available, wait through that mechanism and apply every check below.
2. Otherwise, a safe receiver must call Telegram `getWebhookInfo` (POST `{}`) and
   check HTTP success and JSON `ok`. If `result.url` is nonempty, do not delete or
   replace the webhook to enable polling. Report the delivery limitation in the
   current session and remain blocked. Never log the webhook URL or raw response.
3. With no webhook, a receiver may long-poll `getUpdates` with JSON `{"timeout":30}`
   and a longer HTTP timeout (for example, 40 seconds), checking HTTP success and
   JSON `ok` for every response. Use the same environment-only token and secret-safe
   HTTP handling as the sender. Start without an offset and maintain a local set
   of examined update IDs across waits/restarts.
4. Never send an `offset` (positive or negative), call `deleteWebhook`, or clear the
   update queue: this is a shared bot, and acknowledging updates could discard
   replies needed by another session. Ignore examined updates locally. If the
   unacknowledged queue is full and prevents new replies from appearing, report
   the limitation rather than clearing it. Do not run competing pollers; a polling
   conflict requires a supported shared receiver, not retries that steal updates.
5. Accept an answer only when all of these match the retained pending JSON:
   - `message.chat.type == "private"`;
   - `message.chat.id == pending.chat_id`;
   - `message.from.id == pending.user_id`;
   - `message.reply_to_message.message_id == pending.message_id`;
   - the message contains a usable answer or acknowledgment.
6. `matches_reply(message, pending)` in the helper implements the routing checks,
   not interpretation or approval. The receiving agent must still assess the
   answer. Ignore historical messages, replies to other questions, other senders,
   and messages without a matching reply reference. Never treat them as approval.
   Inspect necessary attachments with available tools or request clarification;
   do not invent their contents.
7. Keep waiting until a matching answer arrives. A shell/tool timeout is not a user
   answer: preserve pending JSON and local examined-update state, and resume
   waiting without a duplicate send. Use a supported background waiter if necessary
   and wait for completion rather than busy-polling. Since old unacknowledged
   updates may return immediately, use bounded delays to avoid a tight loop.
8. On transient receiving failures, retry with bounded backoff, honoring a validated
   numeric Telegram `retry_after` on rate limits. Do not log raw error bodies. If
   the session cannot safely implement or expose this receiver, report **reply
   delivery unavailable**, retain the pending ID if sent, and remain blocked.

## Failure Reporting

Report sanitized diagnostics in the current session (Hub reply when available),
without secrets, API URLs, response bodies, or exception strings:

- **Missing/invalid configuration:** name the variable and required correction;
  state that no send occurred. Never ask the user to paste a token in a reply.
- **Send failure:** state whether delivery is unconfirmed. On HTTP 401, verify the
  environment token; on 403, ask the operator to start/unblock the bot and verify
  the recipient; on 429, honor the service's retry delay if safely available.
  Network/invalid-response errors may be ambiguous; investigate before resending.
- **Reply delivery unavailable:** distinguish this from send failure. State whether
  sending succeeded and the pending ID was retained; explain the webhook, polling
  conflict, full shared queue, or missing receiving mechanism. Request a supported
  reply route without deleting webhooks or acknowledging shared updates.

Never report delivery without a successful API response. None of these errors is
an answer, authorization, or permission to resume dependent work.

## Resume Only After A Reply

Use the matching Telegram reply as the user's response to the pending question.
If it is ambiguous, ask a follow-up on Telegram and repeat this workflow. A
response does not waive the agent's normal safety or permission requirements.

Do not continue work that depends on the answer, assume consent, mark the task
complete, or invoke `hub.finish_execution` while awaiting the reply, including when
sending or receiving is blocked. If the user cancels, stop the affected work. Once
the response resolves the attention request, resume the task and use Hub lifecycle
tools according to their own contracts.

## Offline Verification

From the repository root (Python standard library only):

```sh
python3 -m unittest discover -s .opencode/skills/paseo-telegram-attention/scripts -p 'test_*.py' -v
```

These tests mock HTTP and use a fake token. They cover recipient resolution,
pre-send rejection, request construction, successful pending-ID retention, HTTP
and API failures, malformed responses, secret-safe errors, and reply routing.
They do not contact Telegram, prove live delivery, or install a runner. A live
Telegram smoke test is a separate explicitly authorized action, not mocked
validation, and must not resume unrelated work.
