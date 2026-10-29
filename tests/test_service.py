import copy
import json
import tempfile
import unittest
from pathlib import Path

from gateways import GatewayError, NotificationError
from service import DAY, SAFE_REPLAY_WINDOW, PaymentService, money


class FakeClock:
    def __init__(self):
        self.value = 1_800_000_000

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeGateway:
    def __init__(self):
        self.payment_responses = []
        self.refund_responses = []
        self.payment_calls = []
        self.refund_calls = []
        self.payment_lookups = []
        self.refund_lookups = []
        self.payment_lookup_error = None
        self.refund_lookup_error = None
        self.payments = {}
        self.refunds = {}
        self.keys = {}

    def _create(self, kind, payload, key):
        calls = self.payment_calls if kind == "payment" else self.refund_calls
        responses = self.payment_responses if kind == "payment" else self.refund_responses
        objects = self.payments if kind == "payment" else self.refunds
        calls.append((copy.deepcopy(payload), key))
        if key in self.keys:
            return copy.deepcopy(objects[self.keys[key]])
        outcome = responses.pop(0) if responses else {"status": "succeeded"}
        if isinstance(outcome, Exception):
            raise outcome
        result = {
            "id": f"{kind}-{len(calls)}", "amount": copy.deepcopy(payload["amount"]),
            **copy.deepcopy(outcome),
        }
        if kind == "refund":
            result.setdefault("payment_id", payload["payment_id"])
        elif payload.get("save_payment_method"):
            result.setdefault("payment_method", {"id": "saved-method", "saved": True})
        objects[result["id"]] = result
        self.keys[key] = result["id"]
        return copy.deepcopy(result)

    def create_payment(self, payload, key):
        return self._create("payment", payload, key)

    def create_refund(self, payload, key):
        return self._create("refund", payload, key)

    def get_payment(self, item_id):
        self.payment_lookups.append(item_id)
        if self.payment_lookup_error:
            raise self.payment_lookup_error
        return copy.deepcopy(self.payments[item_id])

    def get_refund(self, item_id):
        self.refund_lookups.append(item_id)
        if self.refund_lookup_error:
            raise self.refund_lookup_error
        return copy.deepcopy(self.refunds[item_id])


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.error = None
        self.calls = 0

    def send(self, text):
        self.calls += 1
        if self.error:
            raise self.error
        self.messages.append(text)


class PaymentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "payments.sqlite3"
        self.clock = FakeClock()
        self.gateway = FakeGateway()
        self.notifier = FakeNotifier()
        self.service = self.make_service()

    def make_service(self, retry_policy="once"):
        return PaymentService(
            self.db_path, self.gateway, self.notifier, "http://127.0.0.1:5000",
            clock=self.clock, retry_policy=retry_policy,
        )

    def create_subscription(self, days=7):
        payment = self.service.create_payment("Студент", "199.90", True, days, True)
        return payment, self.service._row("subscriptions", payment["subscription_id"])

    def last_payment(self):
        return self.service.dashboard()["payments"][0]

    def notification_rows(self):
        with self.service.store.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM notifications ORDER BY id")]

    def test_initial_success_activates_saved_method_and_notifies(self):
        payment, sub = self.create_subscription()
        self.assertEqual(payment["status"], "succeeded")
        self.assertEqual(sub["status"], "active")
        self.assertEqual(sub["payment_method_id"], "saved-method")
        self.assertEqual(sub["next_charge_at"], self.clock.value + 7 * DAY)
        payload, _ = self.gateway.payment_calls[0]
        self.assertTrue(payload["save_payment_method"])
        self.assertTrue(payload["capture"])
        self.assertEqual(payload["confirmation"]["type"], "redirect")
        self.assertIn(payment["id"], payload["confirmation"]["return_url"])
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertIn("Студент", self.notifier.messages[0])
        self.assertIn("199.90 RUB", self.notifier.messages[0])
        self.assertIn("succeeded", self.notifier.messages[0])

    def test_initial_refusal_pauses_subscription_with_reason(self):
        self.gateway.payment_responses.append({
            "status": "canceled", "cancellation_details": {"reason": "insufficient_funds"},
        })
        payment, sub = self.create_subscription()
        self.assertEqual(payment["status"], "canceled")
        self.assertEqual(sub["status"], "paused")
        self.assertIsNone(sub["next_charge_at"])
        self.assertIn("Недостаточно средств", payment["reason"])
        self.assertIn("insufficient_funds", self.notifier.messages[0])
        self.clock.advance(20 * DAY)
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 1)

    def test_success_without_saved_method_does_not_schedule_autopay(self):
        self.gateway.payment_responses.append({
            "status": "succeeded", "payment_method": {"id": "not-saved", "saved": False},
        })
        _, sub = self.create_subscription()
        self.assertEqual(sub["status"], "paused")
        self.assertIsNone(sub["payment_method_id"])
        self.assertIsNone(sub["next_charge_at"])

    def test_autopay_requires_explicit_consent(self):
        with self.assertRaises(ValueError):
            self.service.create_payment("Студент", "10", recurring=True, consent=False)
        self.assertEqual(self.gateway.payment_calls, [])
        self.assertEqual(self.service.dashboard()["subscriptions"], [])

    def test_pending_initial_payment_activates_only_after_confirmed_success(self):
        self.gateway.payment_responses.append({
            "status": "pending", "confirmation": {"confirmation_url": "https://yookassa.ru/checkout/test"},
        })
        payment, sub = self.create_subscription()
        self.assertEqual(sub["status"], "pending")
        self.assertIsNone(sub["next_charge_at"])
        self.assertEqual(self.notifier.messages, [])
        self.assertTrue(payment["confirmation_url"].startswith("https://"))
        self.gateway.payments[payment["gateway_id"]]["status"] = "succeeded"
        self.service.refresh_payment(payment["id"])
        self.assertEqual(self.service._row("subscriptions", sub["id"])["status"], "active")
        self.assertEqual(len(self.gateway.payment_calls), 1)
        self.assertEqual(len(self.notifier.messages), 1)

    def test_recurring_success_uses_saved_method_and_reschedules(self):
        _, sub = self.create_subscription(days=3)
        self.clock.advance(3 * DAY - 1)
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 1)
        self.clock.advance(1)
        self.service.tick()
        payment = self.last_payment()
        self.assertEqual(payment["kind"], "recurring")
        self.assertEqual(payment["status"], "succeeded")
        payload, _ = self.gateway.payment_calls[1]
        self.assertEqual(payload["payment_method_id"], "saved-method")
        self.assertNotIn("confirmation", payload)
        self.assertEqual(self.service._row("subscriptions", sub["id"])["next_charge_at"], self.clock.value + 3 * DAY)

    def test_declined_autopay_retries_exactly_after_day_with_a_new_key(self):
        _, sub = self.create_subscription()
        self.clock.advance(7 * DAY)
        self.gateway.payment_responses.append({
            "status": "canceled", "cancellation_details": {"reason": "insufficient_funds"},
        })
        self.service.tick()
        declined = self.last_payment()
        pending_sub = self.service._row("subscriptions", sub["id"])
        self.assertEqual(pending_sub["next_charge_at"], self.clock.value + DAY)
        self.assertEqual(pending_sub["retry_count"], 1)
        self.clock.advance(DAY - 1)
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 2)
        self.clock.advance(1)
        self.service.tick()
        retry = self.last_payment()
        self.assertEqual(retry["kind"], "retry")
        self.assertNotEqual(retry["idempotence_key"], declined["idempotence_key"])
        self.assertEqual(retry["status"], "succeeded")
        rescheduled = self.service._row("subscriptions", sub["id"])
        self.assertEqual(rescheduled["retry_count"], 0)
        self.assertEqual(rescheduled["next_charge_at"], self.clock.value + 7 * DAY)
        self.assertIn("24 часа", self.notifier.messages[1])

    def test_second_decline_pauses_once_policy(self):
        _, sub = self.create_subscription()
        self.gateway.payment_responses.extend([{"status": "canceled"}, {"status": "canceled"}])
        self.clock.advance(7 * DAY)
        self.service.tick()
        self.clock.advance(DAY)
        self.service.tick()
        stopped = self.service._row("subscriptions", sub["id"])
        self.assertEqual(stopped["status"], "paused")
        self.assertIsNone(stopped["next_charge_at"])
        self.clock.advance(30 * DAY)
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 3)

    def test_daily_policy_keeps_retrying_after_multiple_declines(self):
        self.service = self.make_service(retry_policy="daily")
        _, sub = self.create_subscription()
        self.gateway.payment_responses.extend([{"status": "canceled"}, {"status": "canceled"}])
        self.clock.advance(7 * DAY)
        self.service.tick()
        self.clock.advance(DAY)
        self.service.tick()
        continued = self.service._row("subscriptions", sub["id"])
        self.assertEqual(continued["status"], "active")
        self.assertEqual(continued["retry_count"], 2)
        self.assertEqual(continued["next_charge_at"], self.clock.value + DAY)

    def test_permission_revoked_pauses_both_policies_without_further_attempts(self):
        for policy in ("once", "daily"):
            with self.subTest(policy=policy):
                self.service = self.make_service(retry_policy=policy)
                _, sub = self.create_subscription()
                self.gateway.payment_responses.append({
                    "status": "canceled",
                    "cancellation_details": {"reason": "permission_revoked"},
                })
                self.clock.advance(7 * DAY)
                self.service.tick()
                stopped = self.service._row("subscriptions", sub["id"])
                self.assertEqual(stopped["status"], "paused")
                self.assertIsNone(stopped["next_charge_at"])
                self.assertIn("permission_revoked", stopped["last_error"])
                self.assertIn("permission_revoked", self.notifier.messages[-1])
                count = len(self.gateway.payment_calls)
                self.clock.advance(14 * DAY)
                self.service = self.make_service(retry_policy=policy)
                self.service.tick()
                self.assertEqual(len(self.gateway.payment_calls), count)

    def test_pending_payment_get_error_never_creates_a_replacement(self):
        self.gateway.payment_responses.append({"status": "pending"})
        payment, sub = self.create_subscription()
        self.gateway.payment_lookup_error = GatewayError("GET временно недоступен")
        self.clock.advance(30 * DAY)
        self.service = self.make_service()
        self.service.tick()
        current = self.service._row("payments", payment["id"])
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["gateway_id"], payment["gateway_id"])
        self.assertIn("GET", current["reason"])
        self.assertEqual(len(self.gateway.payment_calls), 1)
        self.assertIsNone(self.service._row("subscriptions", sub["id"])["next_charge_at"])
        self.gateway.payment_lookup_error = None
        self.gateway.payments[payment["gateway_id"]]["status"] = "succeeded"
        self.service.tick()
        self.assertEqual(self.service._row("payments", payment["id"])["status"], "succeeded")
        self.assertEqual(len(self.gateway.payment_calls), 1)

    def test_pending_refund_get_error_never_creates_a_replacement(self):
        payment = self.service.create_payment("Студент", "10")
        self.gateway.refund_responses.append({"status": "pending"})
        refund = self.service.refund_payment(payment["id"])
        self.gateway.refund_lookup_error = GatewayError("GET временно недоступен")
        self.clock.advance(30 * DAY)
        self.service = self.make_service()
        self.service.tick()
        current = self.service._row("refunds", refund["id"])
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["gateway_id"], refund["gateway_id"])
        with self.assertRaises(ValueError):
            self.service.refund_payment(payment["id"])
        self.assertEqual(len(self.gateway.refund_calls), 1)
        self.gateway.refund_lookup_error = None
        self.gateway.refunds[refund["gateway_id"]]["status"] = "succeeded"
        self.service.tick()
        self.assertEqual(self.service._row("refunds", refund["id"])["status"], "succeeded")
        self.assertEqual(len(self.gateway.refund_calls), 1)

    def test_duplicate_tick_and_restart_do_not_charge_twice(self):
        self.create_subscription()
        self.clock.advance(7 * DAY)
        self.gateway.payment_responses.append({"status": "pending"})
        self.service.tick()
        second = self.last_payment()
        self.service.tick()
        self.service = self.make_service()
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 2)
        self.assertEqual(len(self.service.dashboard()["payments"]), 2)
        self.assertEqual(self.gateway.payment_lookups, [second["gateway_id"], second["gateway_id"]])

    def test_cancel_during_initial_payment_does_not_reactivate_subscription(self):
        self.gateway.payment_responses.append({"status": "pending"})
        payment, sub = self.create_subscription()
        self.service.cancel_subscription(sub["id"])
        self.gateway.payments[payment["gateway_id"]]["status"] = "succeeded"
        self.service.tick()
        canceled = self.service._row("subscriptions", sub["id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertIsNone(canceled["next_charge_at"])
        self.clock.advance(40 * DAY)
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 1)

    def test_cancel_during_autopay_does_not_reactivate_subscription(self):
        _, sub = self.create_subscription()
        self.clock.advance(7 * DAY)
        self.gateway.payment_responses.append({"status": "pending"})
        self.service.tick()
        payment = self.last_payment()
        self.service.cancel_subscription(sub["id"])
        self.gateway.payments[payment["gateway_id"]]["status"] = "succeeded"
        self.service.tick()
        self.assertEqual(self.service._row("subscriptions", sub["id"])["status"], "canceled")
        self.assertIsNone(self.service._row("subscriptions", sub["id"])["next_charge_at"])

    def test_full_refund_success_prevents_a_second_refund(self):
        payment = self.service.create_payment("Студент", "150.25")
        refund = self.service.refund_payment(payment["id"])
        self.assertEqual(refund["status"], "succeeded")
        payload, _ = self.gateway.refund_calls[0]
        self.assertEqual(payload, {"payment_id": payment["gateway_id"], "amount": {"value": "150.25", "currency": "RUB"}})
        with self.assertRaises(ValueError):
            self.service.refund_payment(payment["id"])
        self.assertEqual(len(self.gateway.refund_calls), 1)
        self.assertFalse(self.service.dashboard()["payments"][0]["refundable"])
        self.assertIn("Возврат", self.notifier.messages[-1])

    def test_pending_refund_is_polled_and_blocks_duplicate_refund(self):
        payment = self.service.create_payment("Студент", "10")
        self.gateway.refund_responses.append({"status": "pending"})
        refund = self.service.refund_payment(payment["id"])
        with self.assertRaises(ValueError):
            self.service.refund_payment(payment["id"])
        self.gateway.refunds[refund["gateway_id"]]["status"] = "succeeded"
        self.service.tick()
        self.assertEqual(self.service._row("refunds", refund["id"])["status"], "succeeded")
        self.assertEqual(len(self.gateway.refund_calls), 1)
        self.assertEqual(self.gateway.refund_lookups, [refund["gateway_id"]])

    def test_canceled_refund_retains_reason_and_allows_new_attempt(self):
        payment = self.service.create_payment("Студент", "10")
        self.gateway.refund_responses.append({"status": "canceled", "cancellation_details": {"reason": "insufficient_funds"}})
        failed = self.service.refund_payment(payment["id"])
        self.assertEqual(failed["status"], "canceled")
        self.assertIn("insufficient_funds", failed["reason"])
        retried = self.service.refund_payment(payment["id"])
        self.assertEqual(retried["status"], "succeeded")
        self.assertNotEqual(failed["idempotence_key"], retried["idempotence_key"])

    def test_pending_or_canceled_payment_cannot_be_refunded(self):
        for status in ("pending", "canceled"):
            with self.subTest(status=status):
                self.gateway.payment_responses.append({"status": status})
                payment = self.service.create_payment("Студент", "10")
                with self.assertRaises(ValueError):
                    self.service.refund_payment(payment["id"])
        self.assertEqual(self.gateway.refund_calls, [])

    def test_ambiguous_payment_replays_exact_request_and_key_after_restart(self):
        self.gateway.payment_responses.append(GatewayError("timeout", ambiguous=True))
        payment = self.service.create_payment("Студент", "199.90", True, 7, True)
        first_payload, first_key = self.gateway.payment_calls[0]
        self.assertEqual(payment["status"], "creating")
        self.assertEqual(self.notifier.messages, [])
        self.service = self.make_service()
        self.clock.advance(60)
        self.service.tick()
        self.assertEqual(self.gateway.payment_calls[1], (first_payload, first_key))
        self.assertEqual(self.service._row("payments", payment["id"])["status"], "succeeded")
        self.assertEqual(len(self.service.dashboard()["payments"]), 1)

    def test_ambiguous_refund_replays_same_key_and_payload_after_restart(self):
        payment = self.service.create_payment("Студент", "199.90")
        self.gateway.refund_responses.append(GatewayError("timeout", ambiguous=True))
        refund = self.service.refund_payment(payment["id"])
        original = copy.deepcopy(self.gateway.refund_calls[0])
        self.service = self.make_service()
        self.service.tick()
        self.assertEqual(self.gateway.refund_calls[1], original)
        self.assertEqual(self.service._row("refunds", refund["id"])["status"], "succeeded")

    def test_auth_error_after_unknown_post_preserves_uncertainty_and_same_key(self):
        for table in ("payments", "refunds"):
            with self.subTest(table=table):
                if table == "payments":
                    responses = self.gateway.payment_responses
                    calls = self.gateway.payment_calls
                else:
                    payment = self.service.create_payment("Студент", "199.90")
                    responses = self.gateway.refund_responses
                    calls = self.gateway.refund_calls
                first_call = len(calls)
                responses.extend([
                    GatewayError("timeout", ambiguous=True),
                    GatewayError("HTTP 401", ambiguous=False),
                    {"status": "succeeded"},
                ])
                if table == "payments":
                    operation = self.service.create_payment("Студент", "199.90")
                else:
                    operation = self.service.refund_payment(payment["id"])
                original = copy.deepcopy(calls[first_call])
                self.assertEqual(operation["status"], "creating")
                notifications_before = len(self.notifier.messages)
                self.service = self.make_service()
                self.service.tick()
                uncertain = self.service._row(table, operation["id"])
                self.assertEqual(uncertain["status"], "creating")
                self.assertEqual(uncertain["reason"], "HTTP 401")
                self.assertEqual(calls[first_call + 1], original)
                self.assertEqual(len(self.notifier.messages), notifications_before)
                self.service = self.make_service()
                self.service.tick()
                self.assertEqual(self.service._row(table, operation["id"])["status"], "succeeded")
                self.assertEqual(calls[first_call + 2], original)
                self.assertEqual(len(self.notifier.messages), notifications_before + 1)

    def test_aged_unknown_payment_requires_manual_review_without_new_post(self):
        self.gateway.payment_responses.append(GatewayError("timeout", ambiguous=True))
        payment = self.service.create_payment("Студент", "199.90", True, 7, True)
        self.clock.advance(SAFE_REPLAY_WINDOW)
        self.service = self.make_service()
        self.service.tick()
        self.assertEqual(len(self.gateway.payment_calls), 1)
        self.assertEqual(self.service._row("payments", payment["id"])["status"], "manual_review")
        self.assertEqual(self.service._row("subscriptions", payment["subscription_id"])["status"], "paused")
        self.assertIn("manual_review", self.notifier.messages[-1])

    def test_aged_unknown_refund_blocks_new_refund_without_post(self):
        payment = self.service.create_payment("Студент", "10")
        self.gateway.refund_responses.append(GatewayError("timeout", ambiguous=True))
        refund = self.service.refund_payment(payment["id"])
        self.clock.advance(SAFE_REPLAY_WINDOW)
        self.service.tick()
        self.assertEqual(self.service._row("refunds", refund["id"])["status"], "manual_review")
        with self.assertRaises(ValueError):
            self.service.refund_payment(payment["id"])
        self.assertEqual(len(self.gateway.refund_calls), 1)

    def test_telegram_failure_is_persisted_and_retried_without_recharging(self):
        self.notifier.error = NotificationError("Telegram временно недоступен")
        payment = self.service.create_payment("Студент", "10")
        self.assertEqual(payment["status"], "succeeded")
        rows = self.notification_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["sent_at"])
        self.assertIn("недоступен", rows[0]["last_error"])
        self.notifier.error = None
        self.service = self.make_service()
        self.service.tick()
        self.service.tick()
        rows = self.notification_rows()
        self.assertEqual(rows[0]["sent_at"], self.clock.value)
        self.assertIsNone(rows[0]["last_error"])
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(len(self.gateway.payment_calls), 1)

    def test_repeated_terminal_refresh_does_not_duplicate_notification(self):
        payment = self.service.create_payment("Студент", "10")
        self.service.refresh_payment(payment["id"])
        self.service.tick()
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertEqual(len(self.notification_rows()), 1)
        self.assertEqual(self.gateway.payment_lookups, [])

    def test_wrong_gateway_amount_does_not_confirm_payment(self):
        self.gateway.payment_responses.append({"status": "succeeded", "amount": {"value": "1.00", "currency": "RUB"}})
        payment = self.service.create_payment("Студент", "10")
        self.assertEqual(payment["status"], "creating")
        self.assertEqual(self.notifier.messages, [])
        self.assertIn("Неожиданный ответ", payment["reason"])

    def test_wrong_payment_id_does_not_confirm_refund(self):
        payment = self.service.create_payment("Студент", "10")
        self.gateway.refund_responses.append({"status": "succeeded", "payment_id": "different-payment"})
        refund = self.service.refund_payment(payment["id"])
        self.assertEqual(refund["status"], "creating")
        self.assertIn("Неожиданный ответ", refund["reason"])

    def test_waiting_for_capture_does_not_activate_or_silently_charge(self):
        self.gateway.payment_responses.append({"status": "waiting_for_capture"})
        payment, sub = self.create_subscription()
        self.assertEqual(payment["status"], "manual_review")
        self.assertEqual(sub["status"], "paused")
        self.assertIsNone(sub["next_charge_at"])

    def test_persisted_payload_matches_original_request(self):
        payment = self.service.create_payment("Студент", "10")
        self.assertEqual(json.loads(payment["request_json"]), self.gateway.payment_calls[0][0])
        self.assertEqual(payment["idempotence_key"], self.gateway.payment_calls[0][1])


class MoneyTests(unittest.TestCase):
    def test_valid_amounts_are_normalized_without_float_arithmetic(self):
        for value, expected in ((1, "1.00"), ("1.2", "1.20"), ("0.01", "0.01"), ("1000000.00", "1000000.00")):
            with self.subTest(value=value):
                self.assertEqual(money(value), expected)

    def test_nonfinite_negative_zero_and_overprecision_are_rejected(self):
        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "0", "-1", "0.001", "1.234", "1000000.01", "abc", None, {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)


if __name__ == "__main__":
    unittest.main()
