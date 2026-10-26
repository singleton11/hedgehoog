"""Offline integration of candidate external verifiers with the real receiver schema."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / 'scripts/automation-runtime'
RECEIVER = ROOT / '.opencode/skills/paseo-telegram-attention/scripts'
sys.path[:0] = [str(RUNTIME), str(RECEIVER)]
import attention_receiver as receiver
import verify_event as verifier
import verify_event_by_key as by_key


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'attention.sqlite3'
        self.store = receiver.Store(self.path, verifier.BOT)
        self.addCleanup(self.store.close)
        self.context = {'repository': 'singleton11/hedgehoog',
                        'issue_url': 'https://github.com/singleton11/hedgehoog/issues/42',
                        'gate': 'human approval still required'}
        self.pending = {'chat_id': verifier.USER, 'user_id': verifier.USER, 'message_id': 123}
        self.prompt = {'kind': 'approval', 'context_label': 'Issue #42',
                       'context_url': self.context['issue_url'], 'question': 'Proceed?',
                       'recommended': 'yes', 'options': [
                           {'id': 'yes', 'label': 'Proceed', 'description': 'Proceed with the approved scope.'},
                           {'id': 'decline', 'label': 'Decline', 'description': 'Do not implement.'}]}
        self.tokens = {key: 'a1:' + char * 24
                       for key, char in [('yes', 'a'), ('decline', 'b'), ('__other__', 'c')]}

    def callback_event(self, option='yes'):
        self.store.reserve('q42', self.pending, self.context, self.prompt, self.tokens)
        self.store.bind('q42', 123)
        update = {'update_id': 7, 'callback_query': {
            'id': 'click-7', 'from': {'id': verifier.USER, 'is_bot': False},
            'data': self.tokens[option], 'message': {
                'message_id': 123, 'date': 100, 'from': {'id': int(verifier.BOT), 'is_bot': True},
                'chat': {'id': verifier.USER, 'type': 'private'}}}}
        self.store.ingest([update], now=100)
        return self.store.ready(100)[0]

    def legacy_event(self):
        self.store.register('q42', self.pending, self.context)
        self.store.ingest([{'update_id': 8, 'message': {
            'message_id': 124, 'from': {'id': verifier.USER},
            'chat': {'id': verifier.USER, 'type': 'private'},
            'reply_to_message': {'message_id': 123}, 'text': 'Please revise, not approve.'}}], now=100)
        return self.store.ready(100)[0]

    def persist(self, event, status='pending'):
        self.store.db.execute('UPDATE outbox SET event=?,status=?', (json.dumps(event), status))
        self.store.db.commit()

    def test_actual_receiver_callback_and_negative_answer(self):
        event = self.callback_event('decline')
        self.assertIs(verifier.verify(event, self.store.db), event)
        self.assertEqual(by_key.load_verified(self.store.db, 'q42', event['delivery_key']), event)
        self.assertEqual(event['answer']['text'], 'Do not implement.')
        self.assertNotIn('reply', event)

    def assert_prompt_target_rejected(self, context_url):
        self.prompt['context_url'] = context_url
        event = self.callback_event()
        before = list(self.store.db.iterdump())
        with self.assertRaises(ValueError):
            verifier.verify(event, self.store.db)
        with self.assertRaises(ValueError):
            by_key.load_verified(self.store.db, 'q42', event['delivery_key'])
        self.assertEqual(list(self.store.db.iterdump()), before)

    def test_callback_prompt_context_issue_mismatch(self):
        self.assert_prompt_target_rejected('https://github.com/singleton11/hedgehoog/issues/31')

    def test_callback_prompt_context_pr_mismatch(self):
        self.assert_prompt_target_rejected('https://github.com/singleton11/hedgehoog/pull/42')

    def test_caller_mutations_rejected_by_canonical_equality(self):
        event = self.callback_event()
        for field, value in [('schema_version', True), ('answer', {'text': 'approve'}),
                             ('context', {'repository': 'attacker/repo'}), ('pending', {}),
                             ('callback_query', {}), ('update', {}), ('extra', 'forged')]:
            with self.subTest(field=field):
                forged = {**event, field: value}
                with self.assertRaises(ValueError):
                    verifier.verify(forged, self.store.db)

    def test_persisted_callback_forgery_is_independently_rejected(self):
        original = self.callback_event()
        mutations = [
            lambda e: e.update(schema_version=True),
            lambda e: e.update(schema_version=2),
            lambda e: e.pop('schema_version'),
            lambda e: e.update(reply={'text': 'fabricated'}),
            lambda e: e['answer'].update(text='Forged approval'),
            lambda e: e['answer'].update(label='Forged label'),
            lambda e: e['answer'].update(option_id='decline'),
            lambda e: e['answer'].update(source='message'),
            lambda e: e['pending'].update(message_id=124),
            lambda e: e['context'].update(issue_url='https://github.com/singleton11/hedgehoog/pull/42'),
            lambda e: e['context'].update(repository='other/repo'),
            lambda e: e.update(delivery_key='forged'),
            lambda e: e.update(update_id=True),
            lambda e: e['callback_query'].update(id='different'),
            lambda e: e['update'].update(update_id=10),
        ]
        for mutate in mutations:
            event = copy.deepcopy(original)
            mutate(event)
            self.persist(event)
            with self.subTest(event=event), self.assertRaises((ValueError, KeyError)):
                verifier.verify(event, self.store.db)

    def test_identity_checks_even_when_raw_inbox_and_outbox_agree(self):
        original = self.callback_event()
        mutations = [
            lambda c: c['from'].update(id=99),
            lambda c: c['from'].update(is_bot=True),
            lambda c: c['from'].pop('is_bot'),
            lambda c: c['message']['from'].update(id=99),
            lambda c: c['message']['from'].update(is_bot=False),
            lambda c: c['message']['chat'].update(id=99),
            lambda c: c['message']['chat'].update(type='group'),
            lambda c: c['message'].update(message_id=124),
            lambda c: c['message'].update(date=0),
            lambda c: c['message'].pop('date'),
            lambda c: c.update(inline_message_id='inline'),
            lambda c: c.update(id=''),
            lambda c: c.update(data=self.tokens['__other__']),
            lambda c: c.update(data='a1:unknown'),
            lambda c: c.update(data='a1:' + 'x' * 65),
        ]
        for mutate in mutations:
            event = copy.deepcopy(original)
            mutate(event['callback_query'])
            event['update']['callback_query'] = copy.deepcopy(event['callback_query'])
            payload = verifier.canonical(event['update'])
            digest = verifier.hashlib.sha256(payload.encode()).hexdigest()
            self.store.db.execute('INSERT OR REPLACE INTO inbox VALUES(?,?,?,?)',
                                  (verifier.BOT, digest, 7, payload))
            self.persist(event)
            with self.subTest(callback=event['callback_query']), self.assertRaises(ValueError):
                verifier.verify(event, self.store.db)

    def test_missing_or_mismatched_durable_evidence(self):
        event = self.callback_event()
        statements = [
            'DELETE FROM inbox',
            "UPDATE inbox SET payload='{}'",
            'DELETE FROM callback_tokens',
            "UPDATE callback_tokens SET description='forged'",
            'DELETE FROM prompts',
            "UPDATE prompts SET prompt='{}'",
            "UPDATE questions SET message=999",
            "UPDATE questions SET status='canceled'",
            "UPDATE questions SET context='{}'",
        ]
        for statement in statements:
            self.store.db.execute('SAVEPOINT mutation')
            self.store.db.execute(statement)
            with self.subTest(statement=statement), self.assertRaises(ValueError):
                verifier.verify(event, self.store.db)
            self.store.db.execute('ROLLBACK TO mutation')
            self.store.db.execute('RELEASE mutation')

    def test_legacy_text_unchanged_no_callback_tables_required(self):
        event = self.legacy_event()
        for table in ('prompts', 'callback_tokens', 'inbox', 'questions'):
            self.store.db.execute('DROP TABLE ' + table)
        self.assertEqual(verifier.verify(event, self.store.db), event)
        self.assertEqual(by_key.load_verified(self.store.db, 'q42', event['delivery_key']), event)
        forged = copy.deepcopy(event)
        forged['reply']['text'] = 'approve'
        with self.assertRaises(ValueError):
            verifier.verify(forged, self.store.db)

    def test_accepted_input_fix_is_only_delivery_key_exception(self):
        for callback in (False, True):
            with self.subTest(callback=callback):
                if callback:
                    # Reuse the fixture DB after removing the preceding legacy question.
                    self.store.db.execute('DELETE FROM outbox')
                    self.store.db.execute('DELETE FROM questions')
                    self.store.db.commit()
                event = self.callback_event() if callback else self.legacy_event()
                fixed = {**event, 'delivery_key': event['delivery_key'] + verifier.INPUT_FIX_SUFFIX}
                with self.assertRaises(ValueError):
                    verifier.verify(fixed, self.store.db)
                with self.assertRaises(ValueError):
                    by_key.load_verified(self.store.db, 'q42', fixed['delivery_key'])
                self.persist(event, 'accepted')
                self.assertEqual(verifier.verify(fixed, self.store.db), fixed)
                self.assertEqual(by_key.load_verified(self.store.db, 'q42', fixed['delivery_key']), fixed)
                with self.assertRaises(ValueError):
                    verifier.verify({**fixed, 'context': {}}, self.store.db)
                with self.assertRaises(ValueError):
                    by_key.load_verified(self.store.db, 'q42', fixed['delivery_key'] + verifier.INPUT_FIX_SUFFIX)

    def test_status_and_key_rejection_no_mutations(self):
        event = self.callback_event()
        for status in ('blocked', 'canceled'):
            self.persist(event, status)
            before = list(self.store.db.iterdump())
            with self.assertRaises(ValueError):
                verifier.verify(event, self.store.db)
            with self.assertRaises(ValueError):
                by_key.load_verified(self.store.db, 'q42', event['delivery_key'])
            self.assertEqual(list(self.store.db.iterdump()), before)
        self.persist(event)
        for question, key in [('other', event['delivery_key']), ('q42', 'wrong')]:
            with self.assertRaises(ValueError):
                by_key.load_verified(self.store.db, question, key)

    def test_offline_cli_read_only_and_sanitized_failures(self):
        event = self.callback_event()
        self.store.db.execute('PRAGMA wal_checkpoint(FULL)')
        home = Path(self.temp.name) / 'home'
        state = home / '.local/state/paseo-telegram-attention'
        state.mkdir(parents=True)
        (state / 'attention.sqlite3').write_bytes(self.path.read_bytes())
        import os
        env = {**os.environ, 'HOME': str(home)}
        for script, args, stdin in [('verify_event.py', [], json.dumps(event)),
                                    ('verify_event_by_key.py', ['q42', event['delivery_key']], '')]:
            result = subprocess.run([sys.executable, str(RUNTIME / script), *args],
                                    input=stdin, text=True, capture_output=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'verified': True, 'event': event})
        result = subprocess.run([sys.executable, str(RUNTIME / 'verify_event.py')],
                                input='{"private-secret":true}', text=True,
                                capture_output=True, env=env, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('private-secret', result.stderr)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
