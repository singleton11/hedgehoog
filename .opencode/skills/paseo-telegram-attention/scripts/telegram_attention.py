#!/usr/bin/env python3
"""Secret-safe Telegram sender; invoke only after the skill's Hub activation gate."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class AttentionError(Exception):
    """An error containing only safe, actionable diagnostic text."""


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    user_id: int
    chat_id: int


def positive_id(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise AttentionError(f"configuration: {name} must be a positive decimal ID (no whitespace or sign).")
    number = int(value)
    if number > 2**52 - 1:
        raise AttentionError(f"configuration: {name} exceeds Telegram's supported ID range.")
    return number


def load_config(environ):
    token = environ.get("PASEO_TELEGRAM_BOT_TOKEN")
    if not token:
        raise AttentionError("configuration: set PASEO_TELEGRAM_BOT_TOKEN in the process environment.")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise AttentionError("configuration: PASEO_TELEGRAM_BOT_TOKEN has an invalid format; check its source.")
    user_id = positive_id(environ.get("PASEO_TELEGRAM_USER_ID"), "PASEO_TELEGRAM_USER_ID")
    # Presence, not truthiness: an explicitly empty chat ID is invalid, not a fallback.
    chat_id = positive_id(environ["PASEO_TELEGRAM_CHAT_ID"], "PASEO_TELEGRAM_CHAT_ID") if "PASEO_TELEGRAM_CHAT_ID" in environ else user_id
    if chat_id != user_id:
        raise AttentionError("configuration: PASEO_TELEGRAM_CHAT_ID must equal PASEO_TELEGRAM_USER_ID for private chats.")
    return Config(token, user_id, chat_id)


def http_failure(status):
    if status == 401:
        return "send failure: authentication rejected (HTTP 401); verify the environment token."
    if status == 403:
        return "send failure: access denied (HTTP 403); the user may need to start or unblock the bot."
    if status == 429:
        return "send failure: rate limited (HTTP 429); wait before retrying."
    return "send failure: Telegram returned a non-success HTTP status; check service availability and configuration."


def send_question(config, text, opener=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise AttentionError("configuration: provide a nonempty question of at most 4096 characters on stdin.")
    payload = {"chat_id": config.chat_id, "text": text,
               "reply_markup": {"force_reply": True, "selective": True}}
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{config.token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # Do not follow redirects: the URL contains a credential.
    if opener is None:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect()).open
    try:
        with opener(request, timeout=40) as response:
            if response.status != 200:
                raise AttentionError(http_failure(response.status))
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.close()
        raise AttentionError(http_failure(error.code)) from None
    except AttentionError:
        raise
    except Exception:
        # URLs, response bodies, and exception strings can contain the token.
        raise AttentionError("send failure: network or response error; delivery is unconfirmed. Do not blindly resend; investigate first.") from None
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise AttentionError("send failure: Telegram did not return ok=true; no delivery confirmed. Check bot access/configuration; do not log the response body.")
    result = data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if type(message_id) is not int or message_id <= 0:
        raise AttentionError("send failure: response lacks a valid message ID; delivery is unconfirmed. Investigate before resending.")
    return {"chat_id": config.chat_id, "user_id": config.user_id, "message_id": message_id}


def matches_reply(message, pending):
    """Routing check only; the agent must still assess whether the answer is usable."""
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    sender = message.get("from")
    reply = message.get("reply_to_message")
    return (
        isinstance(chat, dict) and chat.get("type") == "private"
        and type(chat.get("id")) is int and chat["id"] == pending["chat_id"]
        and isinstance(sender, dict) and type(sender.get("id")) is int
        and sender["id"] == pending["user_id"]
        and isinstance(reply, dict) and type(reply.get("message_id")) is int
        and reply["message_id"] == pending["message_id"]
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "send"))
    args = parser.parse_args(argv)
    try:
        config = load_config(os.environ)
        if args.action == "validate":
            print("configuration valid; recipient resolved; no network request made")
        else:
            pending = send_question(config, sys.stdin.read())
            print(json.dumps(pending))
    except AttentionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("attention helper failed; do not assume delivery or resume. Check local input/output and preserve pending state.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
