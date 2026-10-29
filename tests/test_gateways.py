import unittest
from unittest.mock import Mock, patch

import requests

from gateways import GatewayError, NotificationError, TelegramNotifier, YooKassaGateway


class YooKassaGatewayTests(unittest.TestCase):
    def setUp(self):
        self.gateway = YooKassaGateway("12345", "test_secret")

    def response(self, status=200, data=None):
        return Mock(status_code=status, json=Mock(return_value=data))

    @patch("gateways.requests.request")
    def test_creation_sends_basic_auth_and_persisted_key(self, request):
        result = {"id": "payment-1", "status": "pending"}
        request.return_value = self.response(data=result)
        self.assertEqual(self.gateway.create_payment({"capture": True}, "stable-key"), result)
        request.assert_called_once_with(
            "POST", "https://api.yookassa.ru/v3/payments",
            auth=("12345", "test_secret"), headers={"Idempotence-Key": "stable-key"},
            json={"capture": True}, timeout=(5, 20), allow_redirects=False,
        )

    def test_live_key_requires_explicit_opt_in(self):
        with self.assertRaises(GatewayError):
            YooKassaGateway("12345", "live_secret")
        YooKassaGateway("12345", "live_secret", allow_live=True)

    def test_missing_credentials_fail(self):
        with self.assertRaises(GatewayError):
            YooKassaGateway("", "")

    @patch("gateways.requests.request")
    def test_path_injection_is_rejected_without_request(self, request):
        for value in ("../refunds", "id?query=1", "https://elsewhere", "", None):
            with self.subTest(value=value), self.assertRaises(GatewayError):
                self.gateway.get_payment(value)
        request.assert_not_called()

    @patch("gateways.requests.request", side_effect=requests.Timeout("test_secret"))
    def test_timeout_is_ambiguous_for_post_and_does_not_leak_credentials(self, request):
        with self.assertRaises(GatewayError) as raised:
            self.gateway.create_payment({}, "stable-key")
        self.assertTrue(raised.exception.ambiguous)
        self.assertNotIn("test_secret", str(raised.exception))

    @patch("gateways.requests.request", side_effect=requests.Timeout("test_secret"))
    def test_get_timeout_does_not_imply_a_new_payment(self, request):
        with self.assertRaises(GatewayError) as raised:
            self.gateway.get_payment("payment-1")
        self.assertFalse(raised.exception.ambiguous)

    @patch("gateways.requests.request")
    def test_server_error_is_ambiguous_but_rejection_is_not(self, request):
        for status, ambiguous in ((500, True), (503, True), (400, False), (401, False), (429, False)):
            with self.subTest(status=status):
                request.return_value = self.response(status=status, data={"description": "secret"})
                with self.assertRaises(GatewayError) as raised:
                    self.gateway.create_refund({}, "stable-key")
                self.assertEqual(raised.exception.ambiguous, ambiguous)
                self.assertNotIn("secret", str(raised.exception))

    @patch("gateways.requests.request")
    def test_invalid_success_response_remains_ambiguous(self, request):
        for data in ({}, [], {"id": "id", "status": "unrecognized"}):
            request.return_value = self.response(data=data)
            with self.assertRaises(GatewayError) as raised:
                self.gateway.create_payment({}, "stable-key")
            self.assertTrue(raised.exception.ambiguous)

    @patch("gateways.requests.request")
    def test_refund_lookup_has_no_mutation_key(self, request):
        request.return_value = self.response(data={"id": "refund-1", "status": "succeeded"})
        self.gateway.get_refund("refund-1")
        args, kwargs = request.call_args
        self.assertEqual(args, ("GET", "https://api.yookassa.ru/v3/refunds/refund-1"))
        self.assertEqual(kwargs["headers"], {})


class TelegramNotifierTests(unittest.TestCase):
    def setUp(self):
        self.notifier = TelegramNotifier("123456:sample_token", "-1001234")

    @patch("gateways.requests.post")
    def test_send_message_checks_telegram_success(self, post):
        post.return_value = Mock(status_code=200, json=Mock(return_value={"ok": True}))
        self.notifier.send("Платёж успешен")
        post.assert_called_once_with(
            "https://api.telegram.org/bot123456:sample_token/sendMessage",
            json={"chat_id": "-1001234", "text": "Платёж успешен"},
            timeout=(5, 20), allow_redirects=False,
        )

    @patch("gateways.requests.post")
    def test_disabled_or_invalid_telegram_does_not_report_success(self, post):
        for token, chat in (("", ""), ("123:token", ""), ("bad/token", "1")):
            with self.subTest(token=token), self.assertRaises(NotificationError):
                TelegramNotifier(token, chat).send("test")
        post.assert_not_called()

    @patch("gateways.requests.post")
    def test_telegram_api_error_is_not_silently_accepted(self, post):
        for status, data in ((403, {"description": "secret"}), (200, {"ok": False}), (200, [])):
            post.return_value = Mock(status_code=status, json=Mock(return_value=data))
            with self.assertRaises(NotificationError) as raised:
                self.notifier.send("test")
            self.assertNotIn("secret", str(raised.exception))

    @patch("gateways.requests.post", side_effect=requests.Timeout("123456:sample_token"))
    def test_network_error_does_not_leak_token(self, post):
        with self.assertRaises(NotificationError) as raised:
            self.notifier.send("test")
        self.assertNotIn("sample_token", str(raised.exception))

    @patch("gateways.requests.post")
    def test_invalid_json_is_safe_failure(self, post):
        post.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError("secret")))
        with self.assertRaises(NotificationError) as raised:
            self.notifier.send("test")
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
