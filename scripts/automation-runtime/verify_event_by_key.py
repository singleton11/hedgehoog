#!/usr/bin/env python3
"""Load and verify a Telegram attention event by its durable identity."""
import json
from pathlib import Path
import sqlite3
import sys

from verify_event import BOT, INPUT_FIX_SUFFIX, verify

ROOT = Path.home() / '.local/state/paseo-telegram-attention'


def load_verified(connection, question_id, delivery_key):
    row = connection.execute(
        'SELECT event,status FROM outbox WHERE bot=? AND question=?', (BOT, question_id)
    ).fetchone()
    if row is None or row[1] not in ('pending', 'accepted'):
        raise ValueError('question is not dispatchable')
    event = json.loads(row[0])
    if event.get('question_id') != question_id:
        raise ValueError('question identity does not match durable outbox')
    if event.get('delivery_key') != delivery_key:
        if (row[1] != 'accepted' or not isinstance(event.get('delivery_key'), str)
                or delivery_key != event['delivery_key'] + INPUT_FIX_SUFFIX):
            raise ValueError('delivery key does not match durable outbox')
        event = {**event, 'delivery_key': delivery_key}
    return verify(event, connection)


def main():
    if len(sys.argv) != 3:
        print('usage: verify_event_by_key.py QUESTION_ID DELIVERY_KEY', file=sys.stderr)
        return 2
    try:
        connection = sqlite3.connect(f'file:{ROOT / "attention.sqlite3"}?mode=ro', uri=True)
        try:
            event = load_verified(connection, *sys.argv[1:])
        finally:
            connection.close()
    except Exception:
        print('attention event rejected; inspect durable state, do not mutate the task', file=sys.stderr)
        return 1
    print(json.dumps({'verified': True, 'event': event}, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
