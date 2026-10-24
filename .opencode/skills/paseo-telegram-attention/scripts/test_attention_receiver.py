import importlib.util
from email.message import Message
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).with_name('attention_receiver.py')


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MODULE.exists(), 'durable receiver is not implemented')
        spec = importlib.util.spec_from_file_location('receiver', MODULE)
        assert spec is not None and spec.loader is not None
        self.r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.r)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'state.sqlite3'

    def store(self):
        store = self.r.Store(self.path, '123')
        self.addCleanup(store.close)
        return store

    def update(self, uid=1, reply=10, sender=42, chat=42, text='A'):
        return {'update_id': uid, 'message': {'message_id': uid + 100,
                'chat': {'id': chat, 'type': 'private'}, 'from': {'id': sender},
                'reply_to_message': {'message_id': reply}, 'text': text}}

    def register(self, store, qid='q1', message=10):
        store.register(qid, {'chat_id': 42, 'user_id': 42, 'message_id': message},
                       {'issue_url': 'https://github.com/singleton11/hedgehoog/issues/31',
                        'execution_id': 'old', 'approval_required': True})

    def test_delayed_reply_survives_restart(self):
        store = self.store()
        self.register(store)
        store.close()
        store = self.store()
        store.ingest([self.update()])
        self.assertEqual(store.offset(), 2)
        event = store.ready(0)[0]
        self.assertEqual(event['reply']['text'], 'A')
        self.assertTrue(event['context']['approval_required'])
        self.assertEqual(event['question_id'], 'q1')

    def test_multiple_questions_foreign_users_and_duplicate_updates(self):
        store = self.store()
        self.register(store)
        self.register(store, 'q2', 20)
        store.ingest([self.update(1, sender=666), self.update(2, chat=666),
                      self.update(3, reply=999), self.update(4), self.update(4),
                      self.update(5, reply=20), self.update(6, text='later')])
        self.assertEqual({e['question_id'] for e in store.ready(0)}, {'q1', 'q2'})
        self.assertEqual(store.ready(0)[0]['update_id'], 4)
        self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 6)

    def test_registration_after_ingest_recovers_reply(self):
        store = self.store()
        store.ingest([self.update()])
        self.register(store)
        self.assertEqual(len(store.ready(0)), 1)

    def test_malformed_batch_does_not_advance_offset_or_partial_commit(self):
        store = self.store()
        with self.assertRaises(self.r.AttentionError):
            store.ingest([self.update(), {'update_id': True}])
        self.assertEqual(store.offset(), 0)
        self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)

    def test_transient_dispatch_retry_survives_restart(self):
        store = self.store()
        self.register(store)
        store.ingest([self.update()])
        calls = []
        def unavailable(event):
            calls.append(event['delivery_key'])
            raise self.r.Retryable('temporary')
        self.r.dispatch_ready(store, unavailable, 100)
        self.assertEqual(store.ready(100), [])
        store.close()
        store = self.store()
        def accepted(event):
            calls.append(event['delivery_key'])
            return {'triggerRunId': 'run', 'workflowStatus': 'running'}
        self.r.dispatch_ready(store, accepted, 1000)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(store.ready(2000), [])
        self.assertEqual(store.db.execute('SELECT status FROM outbox').fetchone()[0], 'accepted')

    def test_exclusive_bot_lock(self):
        with self.r.receiver_lock(Path(self.tmp.name), '123'):
            with self.assertRaises(self.r.AttentionError):
                with self.r.receiver_lock(Path(self.tmp.name), '123'):
                    self.fail('second receiver acquired lock')
            with self.r.receiver_lock(Path(self.tmp.name), '456'):
                pass

    def test_poll_checks_webhook_and_persists_before_next_offset(self):
        store = self.store()
        self.register(store)
        calls = []
        def bot(method, payload):
            calls.append((method, payload))
            return {'url': ''} if method == 'getWebhookInfo' else [self.update()]
        self.r.poll_once(store, bot)
        self.r.poll_once(store, bot)
        self.assertEqual(calls[1][1]['offset'], 0)
        self.assertEqual(calls[3][1]['offset'], 2)
        self.assertEqual(len(store.ready(0)), 1)

    def test_webhook_and_poll_conflict_are_fatal_without_deletion(self):
        store = self.store()
        calls = []
        def webhook(method, payload):
            calls.append(method)
            return {'url': 'https://private.invalid/secret'}
        with self.assertRaises(self.r.AttentionError):
            self.r.poll_once(store, webhook)
        self.assertEqual(calls, ['getWebhookInfo'])
        self.assertEqual(store.offset(), 0)

    def test_manual_adapter_validates_receipt_and_preserves_delivery_key(self):
        env = {'PASEO_ATTENTION_HUB_ORIGIN': 'https://hub.invalid',
               'PASEO_ATTENTION_HUB_TOKEN': 'secret', 'PASEO_ATTENTION_PROJECT': 'hedgehoog',
               'PASEO_ATTENTION_TRIGGER': 'attention-resume', 'PASEO_ATTENTION_ACTOR': 'attention'}
        calls = []
        def http(url, payload, bearer=None):
            calls.append((url, payload, bearer))
            return {'deliveryKey': payload['deliveryKey'], 'configuredTriggerName': 'attention-resume',
                    'triggerRunId': 'run', 'providerEventReceiptId': 'receipt', 'workflowStatus': 'running'}
        store = self.store()
        self.register(store)
        store.ingest([self.update()])
        event = store.ready(0)[0]
        adapter = self.r.manual_adapter(env, http)
        self.assertEqual(adapter(event)['workflowStatus'], 'running')
        self.assertEqual(calls[0][1]['deliveryKey'], event['delivery_key'])
        self.assertEqual(calls[0][1]['input'], event)
        env['PASEO_ATTENTION_HUB_ORIGIN'] = 'https://user:secret@hub.invalid'
        with self.assertRaises(self.r.AttentionError):
            self.r.manual_adapter(env, http)

    def test_failed_manual_receipt_blocks_not_fake_success(self):
        env = {'PASEO_ATTENTION_HUB_ORIGIN': 'https://hub.invalid',
               'PASEO_ATTENTION_HUB_TOKEN': 'secret', 'PASEO_ATTENTION_PROJECT': 'hedgehoog',
               'PASEO_ATTENTION_TRIGGER': 'attention-resume', 'PASEO_ATTENTION_ACTOR': 'attention'}
        adapter = self.r.manual_adapter(env, lambda *a, **kw: {'workflowStatus': 'failed'})
        store = self.store()
        self.register(store)
        store.ingest([self.update()])
        self.r.dispatch_ready(store, adapter, 0)
        self.assertEqual(store.db.execute('SELECT status FROM outbox').fetchone()[0], 'blocked')

    def test_http_errors_never_expose_tokens_and_conflict_is_not_retried(self):
        import urllib.error
        for code, kind in [(409, self.r.AttentionError), (429, self.r.Retryable),
                           (503, self.r.Retryable), (401, self.r.AttentionError)]:
            def fail(*args, **kwargs):
                raise urllib.error.HTTPError('https://secret-token.invalid', code, 'secret-token', Message(), None)
            with self.assertRaises(kind) as caught:
                self.r.json_http('https://secret-token.invalid', {}, opener=fail)
            self.assertNotIn('secret-token', str(caught.exception))
            if code == 409:
                self.assertNotIsInstance(caught.exception, self.r.Retryable)

    def test_send_intent_is_durable_and_never_blindly_retried(self):
        from telegram_attention import Config
        store = self.store()
        config = Config('123:FAKE', 42, 42)
        calls = []
        def ambiguous(config, text):
            calls.append(text)
            raise self.r.AttentionError('delivery unconfirmed')
        with self.assertRaises(self.r.AttentionError):
            self.r.ask(store, 'q1', config, 'Choose A or B', {'gate': 'approval'}, ambiguous)
        store.close()
        store = self.store()
        with self.assertRaises(self.r.AttentionError):
            self.r.ask(store, 'q1', config, 'Choose A or B', {'gate': 'approval'}, ambiguous)
        self.assertEqual(calls, ['Choose A or B'])
        self.assertEqual(store.db.execute('SELECT status FROM questions').fetchone()[0], 'sending')
        store.ingest([self.update()])
        store.bind('q1', 10)
        self.assertEqual(len(store.ready(0)), 1)

    def test_ask_binds_success_and_rejects_invalid_registration(self):
        from telegram_attention import Config
        store = self.store()
        self.r.ask(store, 'q1', Config('123:FAKE', 42, 42), 'Question?', {'gate': 'approval'},
                   lambda config, text: {'chat_id': 42, 'user_id': 42, 'message_id': 10})
        store.ingest([self.update()])
        self.assertEqual(len(store.ready(0)), 1)
        for qid, pending in [('x' * 201, {'chat_id': 42, 'user_id': 42, 'message_id': 30}),
                             ('q3', {'chat_id': 42, 'user_id': 66, 'message_id': 30})]:
            with self.assertRaises(self.r.AttentionError):
                store.register(qid, pending, {'gate': 'approval'})

    def test_cli_register_show_and_receiver_tick_offline(self):
        import contextlib
        import io
        import os
        from unittest.mock import patch
        env = {'PASEO_TELEGRAM_BOT_TOKEN': '123:FAKE', 'PASEO_TELEGRAM_USER_ID': '42'}
        with patch.dict(os.environ, env, clear=True), patch.object(self.r.Path, 'home', return_value=Path(self.tmp.name)):
            payload = {'message_id': 10, 'context': {'gate': 'approval', 'execution_id': 'old'}}
            with patch('sys.stdin', io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.r.main(['register', '--question-id', 'q1']), 0)
            with patch.object(self.r, 'bot_client', return_value=lambda method, payload:
                              {'url': ''} if method == 'getWebhookInfo' else [self.update()]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.r.main(['run', '--once']), 0)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(self.r.main(['show', '--question-id', 'q1']), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['question']['status'], 'answered')
            self.assertEqual(result['outbox']['status'], 'pending')
            self.assertEqual(result['event']['reply']['text'], 'A')
            self.assertNotIn('123:FAKE', output.getvalue())

    def test_state_root_refuses_repository_storage(self):
        root = Path(self.tmp.name)
        (root / '.git').write_text('gitdir: somewhere')
        with self.assertRaises(self.r.AttentionError):
            self.r.state_root(root)

    def test_poll_backoff_survives_restart_and_honors_retry_after(self):
        store = self.store()
        calls = []
        def limited(method, payload):
            calls.append(method)
            raise self.r.Retryable('rate limited', delay=120)
        self.assertFalse(self.r.tick(store, limited, None, 100))
        store.close()
        store = self.store()
        self.assertFalse(self.r.tick(store, limited, None, 200))
        self.assertEqual(calls, ['getWebhookInfo'])
        self.assertFalse(self.r.tick(store, limited, None, 221))
        self.assertEqual(len(calls), 2)

    def test_canceled_question_cannot_dispatch_late_answer(self):
        store = self.store()
        self.register(store)
        store.cancel('q1')
        store.ingest([self.update()])
        self.assertEqual(store.ready(0), [])
        self.register(store, 'q2', 20)
        store.ingest([self.update(2, reply=20)])
        store.cancel('q2')
        self.assertEqual(store.ready(0), [])

    def test_route_failure_rolls_back_inbox_cursor_and_outbox(self):
        from unittest.mock import patch
        store = self.store()
        self.register(store)
        with patch.object(store, '_route', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                store.ingest([self.update()])
        self.assertEqual(store.offset(), 0)
        self.assertEqual(store.ready(0), [])
        self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 0)
        store.ingest([self.update()])
        store.close()
        store = self.store()
        store.ingest([self.update()])
        self.assertEqual(len(store.ready(0)), 1)

    def test_ask_requires_checkpoint_before_send(self):
        from telegram_attention import Config
        store = self.store()
        def forbidden(*args):
            self.fail('must reject missing task context before sending')
        with self.assertRaises(self.r.AttentionError):
            self.r.ask(store, 'q1', Config('123:FAKE', 42, 42), 'Approve?', {}, forbidden)

    def test_poll_resets_prior_update_filter(self):
        store = self.store()
        calls = []
        def bot(method, payload):
            calls.append(payload)
            return {'url': ''} if method == 'getWebhookInfo' else []
        self.r.poll_once(store, bot)
        self.assertEqual(calls[1].get('allowed_updates'), [])

    def test_rate_limit_body_and_http_redirect_are_safe(self):
        import io
        import urllib.error
        from email.message import Message
        def limited(*args, **kwargs):
            raise urllib.error.HTTPError('https://secret.invalid', 429, 'secret', Message(),
                                         io.BytesIO(b'{"parameters":{"retry_after":120}}'))
        with self.assertRaises(self.r.Retryable) as caught:
            self.r.json_http('https://secret.invalid', {}, opener=limited)
        self.assertEqual(caught.exception.delay, 120)
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import threading
        calls = []
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/must-not-follow')
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), Redirect)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with self.assertRaises(self.r.AttentionError):
                self.r.json_http(f'http://127.0.0.1:{server.server_port}/initial', {})
            self.assertEqual(calls, ['/initial'])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_retry_deadline_starts_after_slow_failed_request(self):
        from unittest.mock import patch
        store = self.store()
        def limited(method, payload):
            raise self.r.Retryable('rate limited', delay=120)
        with patch.object(self.r.time, 'monotonic', side_effect=[0, 40, 40]):
            self.r.tick(store, limited, None, 100)
        self.assertEqual(store.db.execute('SELECT next_attempt FROM poll_backoff').fetchone()[0], 260)


if __name__ == '__main__':
    unittest.main()
