import unittest
from unittest.mock import patch

import test_attention_receiver as legacy


class CallbackTests(unittest.TestCase):
    setUp = legacy.DurableTests.setUp
    store = legacy.DurableTests.store
    update = legacy.DurableTests.update

    def prompt(self):
        return {'kind': 'approval', 'context_label': 'Issue #34',
                'context_url': 'https://github.com/singleton11/hedgehoog/issues/34',
                'question': 'Proceed?', 'recommended': 'approve',
                'options': [{'id': 'approve', 'label': 'Approve', 'description': 'Implement the approved plan.'},
                            {'id': 'decline', 'label': 'Do not implement', 'description': 'Do not implement the plan.'}]}

    def reserve_prompt(self, store, bind=True):
        prompt = self.prompt()
        tokens = {key: 'a1:' + char * 24 for key, char in
                  [('approve', 'a'), ('decline', 'b'), ('__other__', 'c')]}
        store.reserve('q1', {'chat_id': 42, 'user_id': 42}, {'gate': 'approval'}, prompt, tokens)
        if bind:
            store.bind('q1', 10)
        return tokens

    def callback(self, uid=1, option='approve'):
        return {'update_id': uid, 'callback_query': {
            'id': 'click-' + str(uid), 'from': {'id': 42, 'is_bot': False},
            'data': 'a1:' + {'approve': 'a', 'decline': 'b', '__other__': 'c'}[option] * 24,
            'message': {'message_id': 10, 'date': 100, 'from': {'id': 123, 'is_bot': True},
                        'chat': {'id': 42, 'type': 'private'}}}}

    def test_callback_approval_and_negative_exact_decision(self):
        for option in ('approve', 'decline'):
            with self.subTest(option=option):
                path = self.path.with_name(option + '.sqlite3')
                store = self.r.Store(path, '123')
                self.addCleanup(store.close)
                self.reserve_prompt(store)
                update = self.callback(option=option)
                store.ingest([update])
                event = store.ready(0)[0]
                self.assertEqual(event['schema_version'], 1)
                self.assertEqual(event['answer']['option_id'], option)
                self.assertEqual(event['answer']['text'], self.prompt()['options'][option == 'decline']['description'])
                self.assertEqual(event['update'], update)
                self.assertEqual(event['callback_query'], update['callback_query'])
                self.assertNotIn('reply', event)
                self.assertEqual(store.db.execute('SELECT count(*) FROM ui_tasks').fetchone()[0], 2)

    def test_callback_transport_preserves_original_hub_prompt_and_event(self):
        import json
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback(option='decline')])
        event = store.ready(0)[0]
        before = list(store.db.iterdump())
        env = {'PASEO_ATTENTION_HUB_ORIGIN': 'https://hub.invalid',
               'PASEO_ATTENTION_HUB_TOKEN': 'secret', 'PASEO_ATTENTION_PROJECT': 'hedgehoog',
               'PASEO_ATTENTION_TRIGGER': 'attention-resume', 'PASEO_ATTENTION_ACTOR': 'attention'}
        calls = []

        def http(url, payload, bearer=None):
            calls.append(payload)
            return {'deliveryKey': payload['deliveryKey'], 'configuredTriggerName': 'attention-resume',
                    'triggerRunId': 'run', 'providerEventReceiptId': 'receipt', 'workflowStatus': 'running'}

        self.r.manual_adapter(env, http)(event)
        # The original Hub's manual provider uses string input as paseo.prompt.
        prompt = calls[0]['input'] if isinstance(calls[0]['input'], str) else ''
        self.assertIsInstance(calls[0]['input'], str)
        self.assertEqual(json.loads(prompt), event)
        self.assertEqual(json.loads(prompt)['question_id'], event['question_id'])
        self.assertEqual(json.loads(prompt)['delivery_key'], event['delivery_key'])
        self.assertEqual(list(store.db.iterdump()), before)

    def test_forgery_malformed_stale_and_inaccessible_callbacks(self):
        store = self.store()
        self.reserve_prompt(store)
        mutations = [
            lambda c: c['from'].update(id=666),
            lambda c: c['from'].update(id=True),
            lambda c: c['from'].update(is_bot=True),
            lambda c: c['from'].pop('is_bot'),
            lambda c: c['message']['chat'].update(id=666),
            lambda c: c['message']['chat'].update(type='group'),
            lambda c: c['message']['from'].update(id=999),
            lambda c: c['message']['from'].update(is_bot=False),
            lambda c: c['message'].update(message_id=20),
            lambda c: c['message'].update(date=0),
            lambda c: c['message'].pop('date'),
            lambda c: c.update(inline_message_id='inline'),
            lambda c: c.update(data='approve'),
            lambda c: c.update(data='a1:' + 'z' * 24),
            lambda c: c.update(data={'bad': 'shape'}),
            lambda c: c.update(message=[]),
            lambda c: c.update(id=''),
        ]
        for uid, mutate in enumerate(mutations, 1):
            update = self.callback(uid)
            mutate(update['callback_query'])
            store.ingest([update])
            self.assertEqual(store.ready(0), [], uid)
        store.ingest([{'update_id': 100, 'callback_query': []}])
        self.assertEqual(store.offset(), 101)
        self.assertEqual(store.ready(0), [])

    def test_late_bind_restart_and_duplicate_first_answer(self):
        store = self.store()
        self.reserve_prompt(store, bind=False)
        update = self.callback(option='decline')
        store.ingest([update])
        self.assertEqual(store.ready(0), [])
        self.r.process_ui(store, lambda *a: True, 0)
        store.close()
        store = self.store()
        store.bind('q1', 10)
        store.ingest([update, self.callback(2), self.update(3)])
        self.assertEqual(len(store.ready(0)), 1)
        self.assertEqual(store.ready(0)[0]['answer']['option_id'], 'decline')
        self.assertEqual(store.offset(), 4)

    def test_other_ack_does_not_consume_then_text_cleans_keyboard(self):
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback(option='__other__')])
        self.assertEqual(store.ready(0), [])
        calls = []
        self.r.process_ui(store, lambda method, payload: calls.append((method, payload)), 0)
        self.assertEqual(calls[0][0], 'answerCallbackQuery')
        self.assertIn('Reply', calls[0][1]['text'])
        store.ingest([self.update(2, text='A custom answer')])
        event = store.ready(0)[0]
        self.assertEqual(event['reply']['text'], 'A custom answer')
        self.assertNotIn('schema_version', event)
        self.assertEqual(store.db.execute("SELECT count(*) FROM ui_tasks WHERE method='editMessageText'").fetchone()[0], 1)

    def test_cancellation_cleans_and_never_dispatches(self):
        store = self.store()
        self.reserve_prompt(store)
        store.cancel('q1')
        store.ingest([self.callback()])
        self.assertEqual(store.ready(0), [])
        self.assertEqual(store.db.execute('SELECT count(*) FROM ui_tasks').fetchone()[0], 2)

    def test_canceled_unbound_delivery_can_be_recorded_without_reopening(self):
        import json
        store = self.store()
        self.reserve_prompt(store, bind=False)
        store.cancel('q1')
        self.assertEqual(store.db.execute('SELECT count(*) FROM ui_tasks').fetchone()[0], 0)
        store.ingest([self.callback()])
        store.bind('q1', 10)
        self.assertEqual(self.r.show(store, 'q1')['question']['status'], 'canceled')
        self.assertEqual(store.ready(0), [])
        payload = json.loads(store.db.execute("SELECT payload FROM ui_tasks WHERE method='editMessageText'").fetchone()[0])
        self.assertEqual(payload['message_id'], 10)
        self.assertTrue(payload['text'].startswith('<b>Canceled</b>'))
        with self.assertRaises(self.r.AttentionError):
            store.bind('q1', 11)

    def test_ui_retry_restart_terminal_and_answer_independence(self):
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback()])
        def limited(method, payload):
            raise self.r.Retryable('limited', delay=120)
        self.r.process_ui(store, limited, 100)
        rows = store.db.execute('SELECT * FROM ui_tasks').fetchall()
        self.assertTrue(all(row['status'] == 'pending' and row['next_attempt'] >= 220 for row in rows))
        store.close()
        store = self.store()
        calls = []
        self.r.process_ui(store, lambda *args: calls.append(args), 200)
        self.assertEqual(calls, [])
        with store.db:
            store.db.execute('UPDATE ui_tasks SET attempts=7,next_attempt=0')
        self.r.process_ui(store, limited, 1000)
        self.assertEqual({row[0] for row in store.db.execute('SELECT status FROM ui_tasks')}, {'failed'})
        self.assertEqual(len(store.ready(0)), 1)
        self.assertEqual(store.offset(), 2)

    def test_ui_terminal_failure_independent_and_tick_order(self):
        store = self.store()
        self.reserve_prompt(store)
        order = []
        def bot(method, payload):
            order.append(method)
            if method == 'getWebhookInfo':
                return {'url': ''}
            if method == 'getUpdates':
                return [self.callback()]
            raise self.r.AttentionError('expired or message already edited')
        self.r.tick(store, bot, lambda event: order.append('dispatch'), 100)
        self.assertEqual(order, ['getWebhookInfo', 'getUpdates', 'answerCallbackQuery',
                                 'editMessageText', 'dispatch'])
        self.assertEqual({row[0] for row in store.db.execute('SELECT status FROM ui_tasks')}, {'failed'})

    def test_callback_ingest_rollback_and_cross_connection_race(self):
        store = self.store()
        self.reserve_prompt(store)
        with patch.object(store, '_cleanup', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                store.ingest([self.callback()])
        self.assertEqual(store.offset(), 0)
        self.assertEqual(store.ready(0), [])
        self.assertEqual(store.db.execute('SELECT count(*) FROM ui_tasks').fetchone()[0], 0)
        other = self.store()
        store.ingest([self.callback()])
        other.ingest([self.callback(2, 'decline'), self.update(3)])
        self.assertEqual(len(store.ready(0)), 1)
        self.assertEqual(store.ready(0)[0]['answer']['option_id'], 'approve')

    def test_concurrent_duplicate_callback_and_text_only_one_winner(self):
        import threading
        store = self.store()
        self.reserve_prompt(store)
        barrier = threading.Barrier(3)
        errors = []
        def receive(update):
            connection = self.r.Store(self.path, '123')
            try:
                barrier.wait(timeout=5)
                connection.ingest([update])
            except Exception as error:
                errors.append(error)
            finally:
                connection.close()
        threads = [threading.Thread(target=receive, args=(update,))
                   for update in (self.callback(), self.callback(), self.update(2))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(store.ready(0)), 1)
        self.assertEqual(store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)
        self.assertEqual(store.db.execute("SELECT count(*) FROM ui_tasks WHERE method='editMessageText'").fetchone()[0], 1)

    def test_ui_retry_can_succeed_after_restart_before_poll(self):
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback()])
        def transient(*args):
            raise self.r.Retryable('network')
        self.r.process_ui(store, transient, 100)
        store.close()
        store = self.store()
        order = []
        def bot(method, payload):
            order.append(method)
            if method == 'getWebhookInfo':
                return {'url': ''}
            if method == 'getUpdates':
                return []
            return True
        self.r.tick(store, bot, None, 200)
        self.assertEqual(order, ['answerCallbackQuery', 'editMessageText',
                                 'getWebhookInfo', 'getUpdates'])
        self.assertEqual({row[0] for row in store.db.execute('SELECT status FROM ui_tasks')}, {'done'})
        self.assertEqual(len(store.ready(0)), 1)

    def test_cleanup_marks_answered_or_canceled_and_preserves_context(self):
        import json
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback()])
        payload = json.loads(store.db.execute("SELECT payload FROM ui_tasks WHERE method='editMessageText'").fetchone()[0])
        self.assertTrue(payload['text'].startswith('<b>Answered</b>'))
        self.assertIn('Remaining approval gates still apply.', payload['text'])
        self.assertNotIn('Choose a button', payload['text'])
        self.assertEqual(payload['reply_markup'], {'inline_keyboard': []})
        self.assertEqual(self.r.show(store, 'q1')['prompt'], self.prompt())
        store.cancel('q1')
        payload = json.loads(store.db.execute("SELECT payload FROM ui_tasks WHERE method='editMessageText'").fetchone()[0])
        self.assertTrue(payload['text'].startswith('<b>Canceled</b>'))
        self.assertEqual(store.ready(0), [])

    def test_ui_outage_is_bounded_and_never_retried_twice_in_tick(self):
        store = self.store()
        self.reserve_prompt(store)
        store.ingest([self.callback(i) for i in range(1, 101)])
        clock, calls = [0], []
        def bot(method, payload):
            calls.append((method, payload))
            if method == 'getWebhookInfo':
                return {'url': ''}
            if method == 'getUpdates':
                return []
            clock[0] += 40
            raise self.r.Retryable('outage')
        dispatched = []
        with patch.object(self.r.time, 'monotonic', side_effect=lambda: clock[0]):
            self.r.tick(store, bot, lambda event: dispatched.append(event) or {'ok': True}, 100)
        ui_calls = [(method, payload) for method, payload in calls if method == 'answerCallbackQuery']
        self.assertEqual(len(ui_calls), 2)
        self.assertNotEqual(ui_calls[0][1]['callback_query_id'], ui_calls[1][1]['callback_query_id'])
        self.assertEqual(clock[0], 80)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(store.offset(), 101)

    def test_shared_token_syntax_supports_full_telegram_byte_bound(self):
        from attention_prompt import render_prompt
        store = self.store()
        tokens = {key: 'a1:' + char * 61 for key, char in
                  [('approve', 'a'), ('decline', 'b'), ('__other__', 'c')]}
        render_prompt(self.prompt(), tokens)
        store.reserve('q1', {'chat_id': 42, 'user_id': 42}, {'gate': 'approval'}, self.prompt(), tokens)
        store.bind('q1', 10)
        update = self.callback()
        update['callback_query']['data'] = tokens['decline']
        store.ingest([update])
        self.assertEqual(store.ready(0)[0]['answer']['option_id'], 'decline')

    def test_conflicting_ask_fields_rejected_without_reservation(self):
        import contextlib
        import io
        import json
        from telegram_attention import Config
        store = self.store()
        with self.assertRaises(self.r.AttentionError):
            self.r.ask(store, 'q1', Config('123:FAKE', 42, 42), 'conflicting text',
                       {'gate': 'approval'}, prompt=self.prompt())
        self.assertEqual(store.db.execute('SELECT count(*) FROM questions').fetchone()[0], 0)
        for payload in ({'prompt': None, 'context': {'gate': 'approval'}},
                        {'prompt': self.prompt(), 'text': 'conflict', 'context': {'gate': 'approval'}}):
            with patch.object(self.r, 'load_config', return_value=Config('123:FAKE', 42, 42)), \
                    patch.object(self.r, 'state_root', return_value=self.path.parent), \
                    patch.object(self.r.sys, 'stdin', io.StringIO(json.dumps(payload))), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.r.main(['ask', '--question-id', 'q2']), 1)
        self.assertEqual(store.db.execute('SELECT count(*) FROM questions').fetchone()[0], 0)

    def test_reservation_before_network_and_invalid_before_reservation(self):
        from telegram_attention import Config
        store = self.store()
        prompt = self.prompt()
        def send(config, normalized, tokens):
            observer = self.store()
            self.assertEqual(observer.db.execute('SELECT count(*) FROM callback_tokens').fetchone()[0], 3)
            self.assertEqual(observer.db.execute('SELECT status FROM questions').fetchone()[0], 'sending')
            self.assertEqual(set(tokens), {'approve', 'decline', '__other__'})
            return {'chat_id': 42, 'user_id': 42, 'message_id': 10}
        self.r.ask(store, 'q1', Config('123:FAKE', 42, 42), None, {'gate': 'approval'}, send, prompt=prompt)
        with self.assertRaises((self.r.AttentionError, ValueError)):
            self.r.ask(store, 'q2', Config('123:FAKE', 42, 42), None, {'gate': 'approval'}, send, prompt={})
        self.assertEqual(store.db.execute('SELECT count(*) FROM questions').fetchone()[0], 1)


if __name__ == '__main__':
    import unittest
    unittest.main()
