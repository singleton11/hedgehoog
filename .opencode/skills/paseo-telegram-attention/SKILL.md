---
name: paseo-telegram-attention
description: When the agent needs to ask a question, request approval, report a blocker, or otherwise needs user attention, check for hub.finish_execution. Only in a Paseo Hub session, notify the user through Telegram and wait for their reply before continuing.
---

# Paseo Hub Telegram Attention

## Activation gate

Before doing anything else, inspect the tools available in the current session.
Activate this workflow **only when `hub.finish_execution` is available**. If tools
are exposed through a tool catalog, check that catalog for the exact tool.

If that tool is absent, stop using this skill and use the session's normal
question/notification mechanism. A Paseo-looking working directory, environment
variables, or the presence of a Telegram token is not evidence of a Hub session.
Do not send Telegram messages or poll Telegram outside a confirmed Hub session.

## When To Use

In a confirmed Hub session, use Telegram whenever you need a decision,
clarification, approval, help with a blocker, or any other user attention. Send
the question before yielding or calling a tool that would wait for local input.
Do not send routine progress updates that do not require attention.

## Recipient And Credentials

- Read the bot token from `PASEO_TELEGRAM_BOT_TOKEN` in the process environment.
- Use private chat ID `169270756` and accept replies only from user ID `169270756`.
- Never embed the token in repository files, messages, logs, or command arguments.
- Use an HTTP client inside a script that reads the environment directly, such as
  Python's standard-library `urllib.request`. Never print request URLs or raw
  exceptions containing the bot token. Do not enable HTTP tracing.
- Send only the context needed to answer; do not include secrets or unnecessary
  private file contents.

## Send The Question

1. Form one concise message containing the repository/session context, the exact
   question or action needed, and any options. State that the agent is waiting.
   Ask the user to use Telegram's **Reply** action on this message so that the
   answer is routed to the correct session. For a notification without a
   question, explicitly request acknowledgment.
2. POST JSON to `https://api.telegram.org/bot<TOKEN>/sendMessage` with `chat_id`
   set to `169270756`, `text` set to that message, and `reply_markup` set to
   `{"force_reply": true, "selective": true}`. Omit `parse_mode` to send plain text.
3. Check both the HTTP status and Telegram's JSON `ok` value. Retain the returned
   `result.message_id` as the pending question's ID. Do not consider an attempted
   send to be a successful notification.

## Wait For The Answer

1. First check whether the session exposes an incoming-message mechanism that
   actually delivers this bot's Telegram replies. If it does, use that mechanism
   to wait and apply the sender/chat/reply checks below.
2. Otherwise, use Telegram's `getWebhookInfo` to check for an active webhook.
   If one exists, do not delete or replace it to enable polling. Report the
   delivery limitation in the current session and remain blocked.
3. With no webhook, long-poll `getUpdates` with a Telegram timeout of 30 seconds
   and a longer HTTP timeout (for example, 40 seconds). Start without an offset
   and maintain a local cursor while examining returned updates.
4. Do not advance Telegram's server-side update offset or use negative offsets:
   this is a shared bot, and acknowledging updates could discard replies needed
   by another session. Ignore already examined update IDs locally. If the
   unacknowledged queue is full and prevents new replies from appearing, report
   the limitation rather than clearing the queue.
5. Accept an answer only when all of these match:
   - `message.chat.id == 169270756`;
   - `message.from.id == 169270756`;
   - `message.reply_to_message.message_id` equals the pending question's ID;
   - the message contains a usable answer or acknowledgment.
6. Ignore historical messages, replies to other questions, other senders, and
   messages without a matching reply reference. Never treat them as approval.
   If an attachment is necessary to answer, inspect it using available tools or
   request clarification; do not invent its contents.
7. Keep waiting until a matching answer arrives. A shell/tool timeout is not a
   user answer: preserve the pending question ID and local cursor and resume
   waiting without sending a duplicate question. Use a supported background
   waiter if necessary, and wait for its completion rather than busy-polling.
8. On transient network failures, retry with bounded backoff. Honor Telegram's
   `retry_after` on rate limits. On a missing token, authentication failure,
   polling conflict, or unavailable receiving mechanism, report a sanitized
   error through the current session. Do not silently fall back to proceeding.

## Resume Only After A Reply

Use the matching Telegram reply as the user's response to the pending question.
If it is ambiguous, ask a follow-up on Telegram and repeat this workflow. A
response does not waive the agent's normal safety or permission requirements.

Do not continue work that depends on the answer, assume consent, mark the task
complete, or invoke `hub.finish_execution` while awaiting the reply. If the user
cancels, stop the affected work. Once the response resolves the attention request,
resume the task and use Hub lifecycle tools according to their own contracts.
