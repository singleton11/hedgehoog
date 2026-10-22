"""Offline checks only: all HTTP responses are mocked; no real user is contacted."""

import contextlib
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import telegram_attention as attention


TOKEN = "123456:TEST_ONLY_NOT_A_REAL_TOKEN"
ENV = {"PASEO_TELEGRAM_BOT_TOKEN": TOKEN, "PASEO_TELEGRAM_USER_ID": "12345"}


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class AttentionTests(unittest.TestCase):
    def test_user_only_and_explicit_matching_chat(self):
        for extra in ({}, {"PASEO_TELEGRAM_CHAT_ID": "12345"}):
            with self.subTest(extra=extra):
                config = attention.load_config(ENV | extra)
                self.assertEqual((config.user_id, config.chat_id), (12345, 12345))
                self.assertNotIn(TOKEN, repr(config))

    def test_invalid_configuration_never_sends(self):
        cases = [{}, {"PASEO_TELEGRAM_BOT_TOKEN": TOKEN},
                 {"PASEO_TELEGRAM_USER_ID": "12345"},
                 ENV | {"PASEO_TELEGRAM_CHAT_ID": "54321"}]
        for name in ("PASEO_TELEGRAM_USER_ID", "PASEO_TELEGRAM_CHAT_ID"):
            for value in ("", "0", "-1", "+1", " 12345", "12345 ", "1.0", "abc", "01", "１２", "4503599627370496"):
                cases.append(ENV | {name: value})
        for value in ("", "bad-token", TOKEN + "/extra", TOKEN + "\n"):
            cases.append(ENV | {"PASEO_TELEGRAM_BOT_TOKEN": value})
        with patch.object(attention.urllib.request, "build_opener") as network:
            for environ in cases:
                with self.subTest(keys=list(environ)):
                    with patch.dict(attention.os.environ, environ, clear=True):
                        output = io.StringIO()
                        with contextlib.redirect_stderr(output):
                            self.assertEqual(attention.main(["send"]), 1)
                        self.assertIn("configuration:", output.getvalue())
                        self.assertNotIn(TOKEN, output.getvalue())
            network.assert_not_called()

    def test_send_request_and_pending_id(self):
        config = attention.load_config(ENV)
        def open_mock(request, timeout):
            self.assertEqual(request.method, "POST")
            self.assertEqual(timeout, 40)
            payload = json.loads(request.data)
            self.assertEqual(payload, {"chat_id": 12345, "text": "Please reply: approve?",
                                      "reply_markup": {"force_reply": True, "selective": True}})
            return Response(b'{"ok":true,"result":{"message_id":77}}')
        self.assertEqual(attention.send_question(config, "Please reply: approve?", open_mock),
                         {"chat_id": 12345, "user_id": 12345, "message_id": 77})

    def test_api_failures_are_secret_safe(self):
        config = attention.load_config(ENV)
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        failures = [urllib.error.HTTPError(url, code, TOKEN, {}, None) for code in (302, 400, 401, 403, 429, 500)]
        failures += [urllib.error.URLError(url), TimeoutError(url), RuntimeError(TOKEN)]
        for failure in failures:
            def open_mock(*args, **kwargs):
                raise failure
            with self.assertRaises(attention.AttentionError) as caught:
                attention.send_question(config, "question", open_mock)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn(url, str(caught.exception))
            self.assertIn("send failure:", str(caught.exception))

    def test_unsuccessful_or_malformed_responses(self):
        bodies = [b'not json', b'[]', b'{"ok":false}', b'{"ok":1}',
                  b'{"ok":true}', b'{"ok":true,"result":[]}',
                  b'{"ok":true,"result":{"message_id":true}}',
                  b'{"ok":true,"result":{"message_id":0}}',
                  b'{"ok":true,"result":{"message_id":"77"}}',
                  json.dumps({"ok": False, "description": TOKEN}).encode()]
        responses = [Response(body) for body in bodies] + [Response(b'{"ok":true,"result":{"message_id":77}}', 500)]
        for response in responses:
            with self.assertRaises(attention.AttentionError) as caught:
                attention.send_question(attention.load_config(ENV), "question", lambda *a, **k: response)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_text_never_sends(self):
        for text in ("", "  ", "a" * 4097):
            with patch.object(attention.urllib.request, "build_opener") as network:
                with self.assertRaises(attention.AttentionError):
                    attention.send_question(attention.load_config(ENV), text)
                network.assert_not_called()

    def test_validate_is_offline_and_does_not_print_values(self):
        with patch.dict(attention.os.environ, ENV, clear=True), patch.object(attention.urllib.request, "build_opener") as network:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(attention.main(["validate"]), 0)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertNotIn("12345", output.getvalue())
            network.assert_not_called()

    def test_cli_retains_pending_state(self):
        with patch.dict(attention.os.environ, ENV, clear=True), patch.object(attention.sys, "stdin", io.StringIO("question")), patch.object(attention.urllib.request, "build_opener") as network:
            network.return_value.open.return_value = Response(b'{"ok":true,"result":{"message_id":77}}')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(attention.main(["send"]), 0)
            self.assertEqual(json.loads(output.getvalue()), {"chat_id": 12345, "user_id": 12345, "message_id": 77})
            self.assertNotIn(TOKEN, output.getvalue())

    def test_reply_requires_private_chat_sender_and_pending_id(self):
        pending = {"chat_id": 12345, "user_id": 12345, "message_id": 77}
        valid = {"chat": {"id": 12345, "type": "private"}, "from": {"id": 12345},
                 "reply_to_message": {"message_id": 77}, "text": "yes"}
        self.assertTrue(attention.matches_reply(valid, pending))
        invalid = [None, {}, valid | {"chat": {"id": 12345, "type": "group"}},
                   valid | {"chat": {"id": 54321, "type": "private"}},
                   valid | {"from": {"id": 54321}}, valid | {"from": None},
                   valid | {"reply_to_message": {}}, valid | {"reply_to_message": {"message_id": 76}}]
        for message in invalid:
            self.assertFalse(attention.matches_reply(message, pending))


if __name__ == "__main__":
    unittest.main()
