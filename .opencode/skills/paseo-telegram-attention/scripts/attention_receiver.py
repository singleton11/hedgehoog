#!/usr/bin/env python3
"""Durable private-chat attention inbox. No LLM or per-execution Telegram polling."""

from contextlib import contextmanager
import argparse
import os
import sys
import time
import fcntl
import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from telegram_attention import AttentionError, load_config, matches_reply, send_question


def validate_question(qid, pending, context):
    if not isinstance(qid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', qid):
        raise AttentionError('question ID must be 1-64 ASCII letters, digits, underscores or hyphens.')
    if (not isinstance(pending, dict) or any(type(pending.get(k)) is not int or pending[k] <= 0
            for k in ('chat_id', 'user_id', 'message_id')) or pending['chat_id'] != pending['user_id']):
        raise AttentionError('pending metadata must identify the authorized private chat and message.')
    if not isinstance(context, dict) or not context:
        raise AttentionError('provide nonempty durable task context and approval gates.')


def ask(store, qid, config, text, context, sender=send_question):
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise AttentionError('provide a nonempty question of at most 4096 characters.')
    pending_metadata = {'chat_id': config.chat_id, 'user_id': config.user_id, 'message_id': 1}
    validate_question(qid, pending_metadata, context)
    context = {**context, 'question_text': text}
    store.reserve(qid, pending_metadata, context)
    pending = sender(config, text)
    store.bind(qid, pending['message_id'])
    return pending


class Store:
    def __init__(self, path, bot):
        self.bot = bot
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS poll_backoff(
                bot TEXT PRIMARY KEY, next_attempt REAL NOT NULL, attempts INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors(
                bot TEXT PRIMARY KEY, offset INTEGER NOT NULL, last_update_at REAL);
            CREATE TABLE IF NOT EXISTS questions(
                bot TEXT, id TEXT, chat INTEGER, user INTEGER, message INTEGER,
                context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
                PRIMARY KEY(bot,id), UNIQUE(bot,chat,message));
            CREATE TABLE IF NOT EXISTS inbox(
                bot TEXT, digest TEXT, id INTEGER, payload TEXT NOT NULL, PRIMARY KEY(bot,digest));
            CREATE TABLE IF NOT EXISTS outbox(
                bot TEXT, question TEXT, event TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT,
                PRIMARY KEY(bot,question));
        ''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if 'last_update_at' not in {row['name'] for row in self.db.execute('PRAGMA table_info(cursors)')}:
                self.db.execute('ALTER TABLE cursors ADD COLUMN last_update_at REAL')
            if 'digest' not in {row['name'] for row in self.db.execute('PRAGMA table_info(inbox)')}:
                self.db.execute('ALTER TABLE inbox RENAME TO inbox_legacy')
                self.db.execute('CREATE TABLE inbox(bot TEXT,digest TEXT,id INTEGER,payload TEXT NOT NULL,PRIMARY KEY(bot,digest))')
                for row in self.db.execute('SELECT * FROM inbox_legacy').fetchall():
                    payload = json.dumps(json.loads(row['payload']), sort_keys=True, separators=(',', ':'))
                    digest = hashlib.sha256(payload.encode()).hexdigest()
                    self.db.execute('INSERT INTO inbox VALUES(?,?,?,?)', (row['bot'], digest, row['id'], payload))
                self.db.execute('DROP TABLE inbox_legacy')

    def close(self):
        self.db.close()

    def offset(self):
        row = self.db.execute('SELECT offset FROM cursors WHERE bot=?', (self.bot,)).fetchone()
        return row[0] if row else 0

    def poll_offset(self, now):
        row = self.db.execute('SELECT offset,last_update_at FROM cursors WHERE bot=?', (self.bot,)).fetchone()
        # Telegram can randomize IDs after a week. Leave margin for its 24h queue
        # retention and delayed ingest; never send a stale offset on restart.
        if (row is None or row['last_update_at'] is None or now < row['last_update_at']
                or now - row['last_update_at'] >= 5 * 86400):
            return None
        return row['offset']

    def register(self, qid, pending, context):
        validate_question(qid, pending, context)
        try:
            with self.db:
                self.db.execute('INSERT INTO questions(bot,id,chat,user,message,context) VALUES(?,?,?,?,?,?)',
                                (self.bot, qid, pending['chat_id'], pending['user_id'],
                                 pending['message_id'], json.dumps(context)))
                self._route()
        except sqlite3.IntegrityError:
            raise AttentionError('question already registered; inspect state, do not resend.') from None

    def reserve(self, qid, pending, context):
        validate_question(qid, {**pending, 'message_id': 1}, context)
        try:
            with self.db:
                self.db.execute('''INSERT INTO questions(bot,id,chat,user,context,status)
                                   VALUES(?,?,?,?,?,'sending')''',
                                (self.bot, qid, pending['chat_id'], pending['user_id'], json.dumps(context)))
        except sqlite3.IntegrityError:
            raise AttentionError('question already registered; delivery may be ambiguous; do not resend.') from None

    def bind(self, qid, message_id):
        if type(message_id) is not int or message_id <= 0:
            raise AttentionError('message ID must be a positive integer.')
        with self.db:
            result = self.db.execute("UPDATE questions SET message=?,status='waiting' WHERE bot=? AND id=? AND status='sending'",
                                     (message_id, self.bot, qid))
            if result.rowcount != 1:
                raise AttentionError('only an unresolved sending intent may be bound; inspect state.')
            self._route()

    def ingest(self, updates, now=None, reset=False):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT last_update_at FROM cursors WHERE bot=?', (self.bot,)).fetchone()
            last_update_at = row[0] if row else None
            offset = 0 if reset else self.offset()
            for update in updates:
                if (not isinstance(update, dict) or type(update.get('update_id')) is not int
                        or update['update_id'] < 0):
                    raise AttentionError('receiver: invalid update batch; cursor not advanced.')
                uid = update['update_id']
                payload = json.dumps(update, sort_keys=True, separators=(',', ':'))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                inserted = self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?,?,?)',
                                           (self.bot, digest, uid, payload))
                if inserted.rowcount or reset:
                    last_update_at = now
                offset = max(offset, uid + 1)
            self._route()
            if updates:
                self.db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?)',
                                (self.bot, offset, last_update_at))

    def _route(self):
        questions = self.db.execute("SELECT * FROM questions WHERE bot=? AND status='waiting'",
                                    (self.bot,)).fetchall()
        updates = self.db.execute('SELECT payload FROM inbox WHERE bot=? ORDER BY id',
                                  (self.bot,)).fetchall()
        for question in questions:
            pending = {'chat_id': question['chat'], 'user_id': question['user'],
                       'message_id': question['message']}
            for row in updates:
                update = json.loads(row[0])
                message = update.get('message')
                if not matches_reply(message, pending):
                    continue
                if not isinstance(message.get('text'), str) or not message['text'].strip():
                    continue
                event = {'question_id': question['id'], 'bot_id': self.bot,
                         'delivery_key': f"telegram:{self.bot}:{question['id']}:{update['update_id']}",
                         'pending': pending, 'context': json.loads(question['context']),
                         'update_id': update['update_id'], 'reply': message}
                self.db.execute('INSERT OR IGNORE INTO outbox(bot,question,event) VALUES(?,?,?)',
                                (self.bot, question['id'], json.dumps(event)))
                self.db.execute("UPDATE questions SET status='answered' WHERE bot=? AND id=?",
                                (self.bot, question['id']))
                break

    def cancel(self, qid):
        with self.db:
            accepted = self.db.execute("SELECT 1 FROM outbox WHERE bot=? AND question=? AND status='accepted'",
                                       (self.bot, qid)).fetchone()
            if accepted:
                raise AttentionError('dispatch already accepted; inspect Hub run before cancellation.')
            result = self.db.execute("UPDATE questions SET status='canceled' WHERE bot=? AND id=?",
                                     (self.bot, qid))
            if result.rowcount != 1:
                raise AttentionError('question not found for this bot.')
            self.db.execute("UPDATE outbox SET status='canceled' WHERE bot=? AND question=?",
                            (self.bot, qid))

    def ready(self, now):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT event FROM outbox WHERE bot=? AND status='pending' AND next_attempt<=?",
            (self.bot, now))]


class Retryable(AttentionError):
    """A sanitized transient failure; retry with durable backoff."""

    def __init__(self, message, delay=30):
        super().__init__(message)
        self.delay = delay if type(delay) is int and 0 < delay <= 86400 else 30


def tick(store, bot, adapter, now):
    started = time.monotonic()
    backoff = store.db.execute('SELECT * FROM poll_backoff WHERE bot=?', (store.bot,)).fetchone()
    polled = False
    if backoff is None or backoff['next_attempt'] <= now:
        try:
            poll_once(store, bot, now=now)
        except Retryable as error:
            attempts = backoff['attempts'] + 1 if backoff else 1
            delay = max(error.delay, min(3600, 30 * 2 ** min(attempts - 1, 7)))
            with store.db:
                store.db.execute('INSERT OR REPLACE INTO poll_backoff VALUES(?,?,?)',
                                 (store.bot, now + time.monotonic() - started + delay, attempts))
        else:
            with store.db:
                store.db.execute('DELETE FROM poll_backoff WHERE bot=?', (store.bot,))
            polled = True
    if adapter:
        dispatch_ready(store, adapter, now + time.monotonic() - started)
    return polled


@contextmanager
def receiver_lock(root, bot):
    # Never unlink the lock: a second inode would permit concurrent owners.
    with (Path(root) / f'bot-{bot}.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AttentionError('receiver: another process owns this bot; refusing polling.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def dispatch_ready(store, adapter, now):
    started = time.monotonic()
    for event in store.ready(now):
        key = (store.bot, event['question_id'])
        try:
            receipt = adapter(event)
        except Retryable as error:
            with store.db:
                row = store.db.execute('SELECT attempts FROM outbox WHERE bot=? AND question=?', key).fetchone()
                attempts = row[0] + 1
                delay = max(error.delay, min(3600, 30 * 2 ** min(attempts - 1, 7)))
                store.db.execute('UPDATE outbox SET attempts=?,next_attempt=? WHERE bot=? AND question=?',
                                 (attempts, now + time.monotonic() - started + delay, *key))
        except AttentionError:
            with store.db:
                store.db.execute("UPDATE outbox SET status='blocked' WHERE bot=? AND question=?", key)
        else:
            with store.db:
                store.db.execute("UPDATE outbox SET status='accepted',receipt=? WHERE bot=? AND question=?",
                                 (json.dumps(receipt), *key))


def json_http(url, payload, bearer=None, opener=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    headers = {'Content-Type': 'application/json'}
    if bearer:
        headers['Authorization'] = f'Bearer {bearer}'
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method='POST')
    if opener is None:
        opener = urllib.request.build_opener(NoRedirect()).open
    try:
        with opener(request, timeout=40) as response:
            if response.status != 200:
                raise AttentionError('HTTP request rejected; delivery unconfirmed.')
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        delay = 30
        try:
            header = error.headers.get('Retry-After', '')
            if re.fullmatch(r'[0-9]{1,5}', header):
                delay = int(header)
            data = json.loads(error.read())
            candidate = data.get('parameters', {}).get('retry_after')
            if type(candidate) is int and 0 < candidate <= 86400:
                delay = candidate
        except Exception:
            pass
        finally:
            error.close()
        if error.code == 429 or error.code >= 500:
            raise Retryable('HTTP service temporarily unavailable; retry deferred.', delay=delay) from None
        raise AttentionError('HTTP request rejected; check credentials, webhook or competing receiver (409).') from None
    except AttentionError:
        raise
    except Exception:
        raise Retryable('HTTP network/response failure; delivery unconfirmed.') from None


def bot_client(config, http=json_http):
    def call(method, payload):
        data = http(f'https://api.telegram.org/bot{config.token}/{method}', payload)
        if not isinstance(data, dict) or data.get('ok') is not True:
            if isinstance(data, dict) and (data.get('error_code') == 429 or
                                          type(data.get('error_code')) is int and data['error_code'] >= 500):
                parameters = data.get('parameters')
                delay = parameters.get('retry_after', 30) if isinstance(parameters, dict) else 30
                raise Retryable('Telegram temporarily unavailable; retry deferred.', delay=delay)
            raise AttentionError('Telegram rejected request; check webhook, conflict and configuration.')
        return data.get('result')
    return call


def poll_once(store, bot, now=None):
    now = time.time() if now is None else now
    webhook = bot('getWebhookInfo', {})
    if not isinstance(webhook, dict) or webhook.get('url') != '':
        raise AttentionError('receiver: existing webhook or invalid webhook response; polling refused.')
    offset = store.poll_offset(now)
    payload = {'timeout': 30, 'limit': 100, 'allowed_updates': []}
    if offset is not None:
        payload['offset'] = offset
    updates = bot('getUpdates', payload)
    if not isinstance(updates, list):
        raise AttentionError('receiver: invalid updates response; cursor not advanced.')
    # The next request acknowledges only this fully committed batch.
    store.ingest(updates, now=now, reset=offset is None)


def manual_adapter(environ, http=json_http):
    def required(suffix):
        name = 'PASEO_ATTENTION_' + suffix
        value = environ.get(name)
        if not isinstance(value, str) or not value.strip():
            raise AttentionError(f'configuration: set {name} in the process environment.')
        return value
    origin = required('HUB_ORIGIN')
    parsed = urllib.parse.urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise AttentionError('configuration: HUB_ORIGIN must be an HTTPS origin without credentials, path or query.')
    token, project, trigger, actor = (required(k) for k in ('HUB_TOKEN', 'PROJECT', 'TRIGGER', 'ACTOR'))

    def dispatch(event):
        payload = {'projectSlug': project, 'trigger': trigger, 'actor': actor,
                   'deliveryKey': event['delivery_key'], 'input': event}
        receipt = http(origin.rstrip('/') + '/api/v1/manual-runs', payload, bearer=token)
        if (not isinstance(receipt, dict) or receipt.get('deliveryKey') != event['delivery_key']
                or receipt.get('configuredTriggerName') != trigger
                or not receipt.get('triggerRunId') or not receipt.get('providerEventReceiptId')
                or receipt.get('workflowStatus') not in ('running', 'succeeded')):
            raise AttentionError('Hub dispatch rejected or failed; operator inspection required.')
        return receipt
    return dispatch


def state_root(home=None):
    root = ((home or Path.home()) / '.local/state/paseo-telegram-attention').resolve()
    if any((parent / '.git').exists() for parent in (root, *root.parents)):
        raise AttentionError('state directory must be outside every git worktree.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid():
        raise AttentionError('state directory must be owned by the receiver account.')
    root.chmod(0o700)
    return root


def show(store, qid):
    question = store.db.execute('SELECT * FROM questions WHERE bot=? AND id=?', (store.bot, qid)).fetchone()
    outbox = store.db.execute('SELECT * FROM outbox WHERE bot=? AND question=?', (store.bot, qid)).fetchone()
    if question is None:
        raise AttentionError('question not found for this bot.')
    result = {'question': dict(question), 'outbox': dict(outbox) if outbox else None}
    result['question']['context'] = json.loads(result['question']['context'])
    result['event'] = json.loads(result['outbox'].pop('event')) if outbox else None
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('validate', 'run'):
        command = sub.add_parser(action)
        command.add_argument('--dispatch', choices=('none', 'hub'), default='none')
        if action == 'run':
            command.add_argument('--once', action='store_true', help='one bounded poll; suitable for a timer')
    for action in ('ask', 'register', 'bind', 'show', 'retry', 'cancel'):
        command = sub.add_parser(action)
        command.add_argument('--question-id', required=True)
        if action == 'bind':
            command.add_argument('--message-id', required=True, type=int)
    args = parser.parse_args(argv)
    store = None
    old_umask = os.umask(0o077)
    try:
        config = load_config(os.environ)
        adapter = manual_adapter(os.environ) if getattr(args, 'dispatch', 'none') == 'hub' else None
        if args.action == 'validate':
            print('configuration valid; no network request made; runner/trigger readiness not verified')
            return 0
        root = state_root()
        bot_id = str(int(config.token.split(':', 1)[0]))
        store = Store(root / 'attention.sqlite3', bot_id)
        if args.action == 'ask':
            payload = json.load(sys.stdin)
            pending = ask(store, args.question_id, config, payload['text'], payload['context'])
            print(json.dumps({'question_id': args.question_id, **pending}))
        elif args.action == 'register':
            payload = json.load(sys.stdin)
            store.register(args.question_id, {'chat_id': config.chat_id, 'user_id': config.user_id,
                           'message_id': payload['message_id']}, payload['context'])
            print('question registered; no network request made')
        elif args.action == 'bind':
            store.bind(args.question_id, args.message_id)
            print('sending intent bound; no network request made')
        elif args.action == 'show':
            print(json.dumps(show(store, args.question_id)))
        elif args.action == 'retry':
            with store.db:
                result = store.db.execute("UPDATE outbox SET status='pending',next_attempt=0 WHERE bot=? AND question=? AND status='blocked'",
                                          (store.bot, args.question_id))
                if result.rowcount != 1:
                    raise AttentionError('only blocked dispatches may be requeued; inspect accepted runs separately.')
            print('blocked dispatch requeued; no network request made')
        elif args.action == 'cancel':
            with receiver_lock(root, bot_id):
                store.cancel(args.question_id)
            print('question canceled locally; no network request made')
        elif args.action == 'run':
            with receiver_lock(root, bot_id):
                bot = bot_client(config)
                while True:
                    polled = tick(store, bot, adapter, time.time())
                    if args.once:
                        print('poll committed' if polled else 'poll deferred by durable backoff')
                        return 0 if polled else 2
                    time.sleep(5)
    except AttentionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('attention helper failed; preserve durable state and inspect configuration; no resume assumed.', file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()
        os.umask(old_umask)
    return 0


if __name__ == '__main__':
    sys.exit(main())
