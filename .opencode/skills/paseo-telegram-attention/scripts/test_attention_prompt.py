"""Offline rendering and sender contract checks; no bot traffic."""

import copy
import html
import json
import unittest
from unittest.mock import patch

import attention_prompt as prompt
from telegram_attention import AttentionError, Config, send_prompt
from test_telegram_attention import Response


def example():
    return {'kind': 'clarification', 'context_label': 'hedgehoog #34',
            'context_url': 'https://github.com/singleton11/hedgehoog/issues/34',
            'question': 'How should a custom answer work?',
            'options': [{'id': 'cancel', 'label': 'Cancel this request',
                         'description': 'Stop without approving anything.'},
                        {'id': 'reply', 'label': 'Reply with details',
                         'description': 'Keep the exact question attached.'}],
            'recommended': 'reply'}


def tokens():
    return {'cancel': 'a1:cancel_random_value', 'reply': 'a1:reply_random_value',
            '__other__': 'a1:other_random_value'}


class PromptTests(unittest.TestCase):
    def test_concise_recommended_first_no_implicit_selection(self):
        original = example()
        before = copy.deepcopy(original)
        rendered = prompt.render_prompt(original, tokens())
        self.assertEqual(original, before)
        self.assertEqual(rendered['parse_mode'], 'HTML')
        self.assertIn('<b>Clarification needed</b>', rendered['text'])
        self.assertIn('<b>Reply with details (Recommended)</b>', rendered['text'])
        self.assertLess(len(rendered['text']), 700)
        rows = rendered['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0], [{'text': 'Reply with details (Recommended)', 'callback_data': tokens()['reply']}])
        self.assertEqual(rows[-1][0]['text'], 'Other / write a reply')
        self.assertTrue(all(len(row) == 1 for row in rows))
        self.assertNotIn('force_reply', rendered['reply_markup'])
        self.assertIn('use Reply', rendered['text'])
        self.assertIn('Task paused.', rendered['text'])

    def test_dynamic_values_are_escaped_not_interpreted_as_markup(self):
        data = example()
        unsafe = '<b>"A&B"</b>'
        data.update(context_label=unsafe, question=unsafe)
        data['options'][0].update(label=unsafe, description=unsafe)
        text = prompt.render_prompt(data, tokens())['text']
        self.assertNotIn(unsafe, text)
        self.assertEqual(text.count(html.escape(unsafe)), 4)
        self.assertIn('href="https://github.com/singleton11/hedgehoog/issues/34"', text)

    def test_only_expected_repository_issue_pr_urls(self):
        for suffix in ('issues/34', 'pull/42'):
            data = example() | {'context_url': 'https://github.com/singleton11/hedgehoog/' + suffix}
            prompt.validate_prompt(data)
        urls = ['http://github.com/singleton11/hedgehoog/issues/34',
                'https://github.com.evil.test/singleton11/hedgehoog/issues/34',
                'https://github.com/other/hedgehoog/issues/34',
                'https://github.com/singleton11/hedgehoog/issues/0',
                'https://github.com/singleton11/hedgehoog/issues/34?token=SECRET',
                'https://github.com/singleton11/hedgehoog/issues/34\n',
                'javascript:alert(1)', None]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(AttentionError) as error:
                prompt.validate_prompt(example() | {'context_url': url})
            self.assertNotIn('SECRET', str(error.exception))

    def test_bounds_and_malformed_data_rejected_before_http(self):
        cases = [None, [], {}, example() | {'extra': 'unexpected'}, example() | {'kind': []},
                 example() | {'kind': 'unknown'}, example() | {'recommended': 'missing'},
                 example() | {'recommended': ['reply']}, example() | {'options': []},
                 example() | {'options': [example()['options'][0]]},
                 example() | {'options': example()['options'] * 3}]
        for field, limit in [('context_label', 80), ('question', 500)]:
            for value in ('', '  ', 'line\nbreak', ' leading', 'a' * (limit + 1), '\ud800', False):
                cases.append(example() | {field: value})
        for field, value in [('id', 'bad value'), ('id', '__other__'), ('id', []),
                             ('label', 'a' * 41), ('description', 'a' * 161),
                             ('description', 'line\nbreak'), ('label', None)]:
            data = example()
            data['options'][0][field] = value
            cases.append(data)
        data = example()
        data['options'][1]['id'] = 'cancel'
        cases.append(data)
        cases.append(example() | {'options': [None, {}]})
        for value in cases:
            with self.subTest(value=value), patch('telegram_attention.urllib.request.build_opener') as network:
                with self.assertRaises(AttentionError):
                    send_prompt(Config('123:FAKE', 1, 1), value, tokens())
                network.assert_not_called()

    def test_approval_requires_decline_or_cancel_but_never_sets_authorization(self):
        data = example() | {'kind': 'approval'}
        normalized = prompt.validate_prompt(data)
        self.assertEqual(normalized['recommended'], 'reply')
        self.assertNotIn('authorized', normalized)
        data['options'][0]['id'] = 'proceed'
        with self.assertRaisesRegex(AttentionError, 'decline or cancel'):
            prompt.validate_prompt(data)

    def test_unicode_and_telegram_unit_limits(self):
        face = '\U0001f600'
        self.assertEqual(prompt.utf16_length(face), 2)
        data = example() | {'question': face * 500}
        rendered = prompt.render_prompt(data, tokens())
        self.assertIn(face * 500, rendered['text'])
        self.assertLess(prompt.utf16_length(rendered['text']), 4096)
        # The final-message guard is independent of individual field bounds.
        with patch.object(prompt, '_body', return_value=('unused', face * 2049)):
            with self.assertRaisesRegex(AttentionError, 'UTF-16'):
                prompt.validate_prompt(example())

    def test_callback_tokens_opaque_unique_versioned_and_bounded(self):
        cases = [None, {}, tokens() | {'extra': 'a1:extra'},
                 tokens() | {'reply': tokens()['cancel']},
                 tokens() | {'reply': 'a1:' + 'a' * 62},
                 tokens() | {'reply': '\U0001f600' * 32},
                 tokens() | {'reply': 'a2:unknown'}, tokens() | {'reply': False}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(AttentionError):
                prompt.render_prompt(example(), data)
        valid = tokens() | {'reply': 'a1:' + 'a' * 61}
        self.assertEqual(len(valid['reply'].encode()), 64)
        prompt.render_prompt(example(), valid)

    def test_structured_sender_uses_shared_secret_safe_boundary(self):
        captured = []
        def opener(request, timeout):
            captured.append(json.loads(request.data))
            self.assertEqual(timeout, 40)
            return Response(b'{"ok":true,"result":{"message_id":77}}')
        result = send_prompt(Config('123:FAKE', 1, 1), example(), tokens(), opener)
        self.assertEqual(result, {'chat_id': 1, 'user_id': 1, 'message_id': 77})
        self.assertEqual(captured, [{'chat_id': 1, **prompt.render_prompt(example(), tokens())}])
        def failure(*args, **kwargs):
            raise RuntimeError('123:SECRET')
        with self.assertRaises(AttentionError) as error:
            send_prompt(Config('123:SECRET', 1, 1), example(), tokens(), failure)
        self.assertNotIn('SECRET', str(error.exception))


if __name__ == '__main__':
    unittest.main()
