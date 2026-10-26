#!/usr/bin/env python3
"""Validate a Hub input against the durable receiver outbox, without network I/O."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path.home() / '.local/state/paseo-telegram-attention'
BOT = '8848707126'
USER = 169270756
INPUT_FIX_SUFFIX = ':input-fix-v1'


def canonical(value):
    # JSON equality must not conflate True with 1 (including schema/Telegram IDs).
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def verify_callback(event, connection):
    if type(event.get('schema_version')) is not int or event['schema_version'] != 1 or 'reply' in event:
        raise ValueError('unsupported callback schema')
    pending, context = event['pending'], event['context']
    question = connection.execute(
        'SELECT chat,user,message,context,status FROM questions WHERE bot=? AND id=?',
        (BOT, event['question_id']),
    ).fetchone()
    if (question is None or question[4] != 'answered'
            or canonical(pending) != canonical(dict(zip(('chat_id', 'user_id', 'message_id'), question[:3])))
            or canonical(context) != canonical(json.loads(question[3]))):
        raise ValueError('callback question mismatch')
    if (any(type(pending.get(k)) is not int or pending[k] <= 0
            for k in ('chat_id', 'user_id', 'message_id'))
            or pending['chat_id'] != USER or pending['user_id'] != USER):
        raise ValueError('callback pending identity mismatch')
    update, callback = event.get('update'), event.get('callback_query')
    if (not isinstance(update, dict) or not isinstance(callback, dict)
            or type(event.get('update_id')) is not int or event['update_id'] < 0
            or type(update.get('update_id')) is not int or update['update_id'] != event['update_id']
            or canonical(update.get('callback_query')) != canonical(callback)
            or 'inline_message_id' in callback):
        raise ValueError('raw callback update mismatch')
    payload = canonical(update)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    inbox = connection.execute('SELECT id,payload FROM inbox WHERE bot=? AND digest=?', (BOT, digest)).fetchone()
    if (inbox is None or inbox[0] != event['update_id']
            or canonical(json.loads(inbox[1])) != payload):
        raise ValueError('callback missing from raw inbox')
    actor, message = callback.get('from'), callback.get('message')
    if (not isinstance(callback.get('id'), str) or not callback['id']
            or not isinstance(actor, dict) or type(actor.get('id')) is not int
            or actor['id'] != USER or actor.get('is_bot') is not False
            or not isinstance(message, dict)):
        raise ValueError('callback sender mismatch')
    chat, author = message.get('chat'), message.get('from')
    if (not isinstance(chat, dict) or chat.get('type') != 'private'
            or type(chat.get('id')) is not int or chat['id'] != USER
            or type(message.get('message_id')) is not int or message['message_id'] != pending['message_id']
            or type(message.get('date')) is not int or message['date'] <= 0
            or not isinstance(author, dict) or author.get('is_bot') is not True
            or type(author.get('id')) is not int or str(author['id']) != BOT):
        raise ValueError('callback question message mismatch')
    token = callback.get('data')
    if (not isinstance(token, str) or not re.fullmatch(r'a1:[A-Za-z0-9_-]+', token)
            or not 1 <= len(token.encode()) <= 64):
        raise ValueError('invalid callback token')
    option = connection.execute(
        'SELECT option_id,label,description FROM callback_tokens WHERE bot=? AND question=? AND token=?',
        (BOT, event['question_id'], token),
    ).fetchone()
    if option is None or option[0] == '__other__':
        raise ValueError('callback token not bound to an answer')
    expected = {'source': 'callback_query', 'option_id': option[0], 'label': option[1], 'text': option[2]}
    if (not isinstance(option[2], str) or not option[2].strip()
            or canonical(event.get('answer')) != canonical(expected)):
        raise ValueError('callback answer mapping mismatch')
    prompt = connection.execute('SELECT prompt FROM prompts WHERE bot=? AND question=?',
                                (BOT, event['question_id'])).fetchone()
    if prompt is None:
        raise ValueError('callback prompt missing')
    stored_prompt = json.loads(prompt[0])
    if not isinstance(stored_prompt, dict) or stored_prompt.get('context_url') != context['issue_url']:
        raise ValueError('callback prompt task mismatch')
    options = stored_prompt.get('options', [])
    matches = [item for item in options if isinstance(item, dict) and item.get('id') == option[0]]
    if len(matches) != 1 or matches[0].get('label') != option[1] or matches[0].get('description') != option[2]:
        raise ValueError('callback prompt mapping mismatch')


def verify(event, connection):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    row = connection.execute(
        'SELECT event,status FROM outbox WHERE bot=? AND question=?',
        (event.get('bot_id'), event.get('question_id')),
    ).fetchone()
    persisted = json.loads(row[0]) if row is not None else None
    normalized = event
    if (isinstance(persisted, dict) and row[1] == 'accepted'
            and isinstance(persisted.get('delivery_key'), str)
            and event.get('delivery_key') == persisted['delivery_key'] + INPUT_FIX_SUFFIX):
        normalized = {**event, 'delivery_key': persisted['delivery_key']}
    if row is None or row[1] not in ('pending', 'accepted') or canonical(normalized) != canonical(persisted):
        raise ValueError('event does not match a dispatchable durable outbox row')
    context = event.get('context', {})
    if context.get('repository') != 'singleton11/hedgehoog':
        raise ValueError('repository not allowlisted')
    url = context.get('issue_url', '')
    if not re.fullmatch(r'https://github\.com/singleton11/hedgehoog/issues/[1-9][0-9]*', url):
        raise ValueError('an allowlisted issue URL is required')
    if event.get('bot_id') != BOT:
        raise ValueError('bot identity mismatch')
    if any(key in event for key in ('schema_version', 'callback_query', 'update', 'answer')):
        if normalized['delivery_key'] != f"telegram:{BOT}:{event['question_id']}:{event['update_id']}":
            raise ValueError('callback delivery identity mismatch')
        verify_callback(event, connection)
    else:
        # Keep the installed legacy text-reply contract unchanged.
        pending = event['pending']
        reply = event['reply']
        if (pending.get('user_id') != USER or pending.get('chat_id') != USER
                or reply.get('from', {}).get('id') != USER
                or reply.get('chat', {}).get('id') != USER
                or reply.get('chat', {}).get('type') != 'private'
                or reply.get('reply_to_message', {}).get('message_id') != pending.get('message_id')
                or not isinstance(reply.get('text'), str) or not reply['text'].strip()):
            raise ValueError('reply identity mismatch')
    return event


def main():
    try:
        event = json.load(sys.stdin)
        connection = sqlite3.connect(f'file:{ROOT / "attention.sqlite3"}?mode=ro', uri=True)
        try:
            result = verify(event, connection)
        finally:
            connection.close()
        print(json.dumps({'verified': True, 'event': result}))
        return 0
    except Exception:
        print('attention event rejected; inspect durable state, do not mutate the task', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
