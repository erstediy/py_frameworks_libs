import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from main import create_app, single_instance
from service import PaymentService


class FakeGateway:
    def __init__(self):
        self.next_status = "pending"
        self.payments = {}
        self.created_payments = []
        self.queried_payments = []
        self.created_refunds = []

    def create_payment(self, payload, key):
        self.created_payments.append((copy.deepcopy(payload), key))
        payment_id = "gateway-" + str(len(self.created_payments))
        result = {
            "id": payment_id, "status": self.next_status,
            "amount": copy.deepcopy(payload["amount"]),
            "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/" + payment_id},
            "payment_method": {"id": "saved-method", "saved": True},
            "cancellation_details": {"reason": "insufficient_funds"},
        }
        self.payments[payment_id] = result
        return copy.deepcopy(result)

    def get_payment(self, payment_id):
        self.queried_payments.append(payment_id)
        return copy.deepcopy(self.payments[payment_id])

    def create_refund(self, payload, key):
        self.created_refunds.append((copy.deepcopy(payload), key))
        return {
            "id": "refund-" + str(len(self.created_refunds)), "status": "succeeded",
            "payment_id": payload["payment_id"], "amount": copy.deepcopy(payload["amount"]),
        }


class AppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = FakeGateway()
        self.notifier = Mock()
        self.service = PaymentService(
            Path(temporary.name) / "app.sqlite3", self.gateway, self.notifier,
            "http://127.0.0.1:5000", clock=lambda: 1_000_000,
        )
        self.app = create_app(
            {"TESTING": True, "SECRET_KEY": "test-secret", "ALLOW_LIVE_PAYMENTS": False},
            service=self.service,
        )
        self.client = self.app.test_client()
        self.client.get("/")
        with self.client.session_transaction() as session:
            self.csrf = session["csrf_token"]

    def post(self, path, **fields):
        return self.client.post(path, data={"csrf_token": self.csrf, **fields})

    def create_payment(self, **fields):
        return self.post("/payments", **{"customer": "Студент", "amount": "100.00", **fields})

    def latest_payment(self):
        return self.service.dashboard()["payments"][0]

    def test_index_renders_forms_settings_and_refunds(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for text in ("Платёжная система", "Тестовая ЮKassa", "Возвраты", "Подписки", "Telegram"):
            self.assertIn(text, html)
        self.assertIn('action="/payments"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="consent"', html)

    def test_missing_configuration_is_visible(self):
        self.service.gateway = None
        self.service.notifier = None
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("ЮKassa ещё не настроена", html)
        self.assertIn("Telegram ещё не настроен", html)
        self.assertIn("disabled", html)

    def test_post_requires_valid_csrf_including_unicode_input(self):
        for token in (None, "wrong", "неверный-токен"):
            data = {"customer": "Студент", "amount": "100.00"}
            if token is not None:
                data["csrf_token"] = token
            with self.subTest(token=token):
                self.assertEqual(self.client.post("/payments", data=data).status_code, 400)
        self.assertEqual(self.gateway.created_payments, [])

    def test_pending_creation_redirects_to_gateway(self):
        response = self.create_payment()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "https://yookassa.ru/checkout/gateway-1")
        self.assertEqual(self.latest_payment()["status"], "pending")
        self.assertEqual(len(self.gateway.created_payments), 1)

    def test_terminal_creation_does_not_redirect_to_payment_confirmation(self):
        for status in ("succeeded", "canceled"):
            self.gateway.next_status = status
            with self.subTest(status=status):
                response = self.create_payment()
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.location, "/")
                self.assertEqual(self.latest_payment()["status"], status)

    def test_invalid_amount_never_reaches_gateway(self):
        for amount in ("-1", "0", "1.001", "NaN", "сто рублей"):
            with self.subTest(amount=amount):
                response = self.create_payment(amount=amount)
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.location, "/")
                self.assertIn("Сумма должна быть", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.gateway.created_payments, [])

    def test_subscription_requires_consent_and_integer_interval(self):
        self.create_payment(recurring="1", interval_days="30")
        self.assertIn("необходимо согласие", self.client.get("/").get_data(as_text=True))
        self.create_payment(recurring="1", consent="1", interval_days="полмесяца")
        self.assertIn("целое число дней", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.gateway.created_payments, [])
        self.assertEqual(self.service.dashboard()["subscriptions"], [])

    def test_return_queries_gateway_and_ignores_claimed_success_in_url(self):
        self.create_payment()
        payment = self.latest_payment()
        response = self.client.get("/return", query_string={
            "payment_id": payment["id"], "status": "succeeded", "paid": "true",
        })
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.gateway.queried_payments, [payment["gateway_id"]])
        self.assertEqual(self.latest_payment()["status"], "pending")
        self.gateway.payments[payment["gateway_id"]]["status"] = "succeeded"
        self.client.get("/return", query_string={"payment_id": payment["id"]})
        self.assertEqual(self.latest_payment()["status"], "succeeded")
        self.assertEqual(self.notifier.send.call_count, 1)

    def test_refresh_route_queries_current_provider_status(self):
        self.create_payment()
        payment = self.latest_payment()
        self.gateway.payments[payment["gateway_id"]]["status"] = "canceled"
        response = self.post(f"/payments/{payment['id']}/refresh")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.latest_payment()["status"], "canceled")
        self.assertIn("insufficient_funds", self.client.get("/").get_data(as_text=True))

    def test_refund_route_creates_full_refund_and_blocks_duplicate(self):
        self.gateway.next_status = "succeeded"
        self.create_payment()
        payment = self.latest_payment()
        path = f"/payments/{payment['id']}/refund"
        self.assertEqual(self.post(path).status_code, 303)
        refund = self.service.dashboard()["refunds"][0]
        self.assertEqual(refund["status"], "succeeded")
        self.assertEqual(refund["amount"], "100.00")
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn(refund["id"][:12], html)
        self.assertEqual(self.post(path).status_code, 303)
        self.assertEqual(len(self.gateway.created_refunds), 1)
        self.assertIn("Возврат уже создан", self.client.get("/").get_data(as_text=True))

    def test_cancel_subscription_route_stops_future_charges(self):
        self.gateway.next_status = "succeeded"
        self.create_payment(recurring="1", consent="1", interval_days="30")
        subscription = self.service.dashboard()["subscriptions"][0]
        self.assertEqual(subscription["status"], "active")
        response = self.post(f"/subscriptions/{subscription['id']}/cancel")
        self.assertEqual(response.status_code, 303)
        subscription = self.service.dashboard()["subscriptions"][0]
        self.assertEqual(subscription["status"], "canceled")
        self.assertIsNone(subscription["next_charge_at"])

    def test_customer_name_is_escaped_in_html(self):
        customer = '<script>alert("x")</script>'
        self.create_payment(customer=customer)
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn(customer, html)
        self.assertIn("&lt;script&gt;", html)


class SingleInstanceTests(unittest.TestCase):
    def test_other_process_cannot_use_locked_database_and_release_allows_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "payments.sqlite3"
            code = "import sys\nfrom main import single_instance\nwith single_instance(sys.argv[1]):\n print('acquired')\n"
            with single_instance(database):
                result = subprocess.run(
                    [sys.executable, "-c", code, str(database)],
                    capture_output=True, text=True, timeout=10,
                    cwd=Path(__file__).resolve().parents[1],
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("acquired", result.stdout)
            with single_instance(database):
                pass

    def test_lock_is_released_after_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "payments.sqlite3"
            with self.assertRaisesRegex(RuntimeError, "test failure"):
                with single_instance(database):
                    raise RuntimeError("test failure")
            with single_instance(database):
                pass


if __name__ == "__main__":
    unittest.main()
