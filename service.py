import json
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse

from gateways import GatewayError, NotificationError
from storage import Storage

DAY = 24 * 60 * 60
SAFE_REPLAY_WINDOW = 23 * 60 * 60
IN_FLIGHT = ("creating", "pending", "waiting_for_capture")
REASONS = {
    "insufficient_funds": "Недостаточно средств",
    "card_expired": "Истёк срок действия карты",
    "payment_method_restricted": "Операции этим способом оплаты ограничены",
    "permission_revoked": "Разрешение на автоплатежи отозвано",
    "canceled_by_merchant": "Платёж отменён магазином",
    "canceled_by_user": "Платёж отменён пользователем",
    "confirmation_timeout": "Истекло время подтверждения платежа",
    "general_decline": "Банк отклонил платёж",
    "issuer_unavailable": "Банк временно недоступен",
    "internal_timeout": "Истекло время обработки платежа",
    "fraud_suspected": "Платёж отклонён системой проверки безопасности",
}


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("1000000"):
            raise ValueError
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        return f"{amount:.2f}"
    except (InvalidOperation, ValueError):
        raise ValueError("Сумма должна быть от 0.01 до 1000000.00 рублей, максимум два знака после точки.") from None


class PaymentService:
    def __init__(self, db_path, gateway, notifier, base_url, clock=time.time, retry_policy="once"):
        self.store = Storage(db_path)
        self.gateway = gateway
        self.notifier = notifier
        self.base_url = base_url.rstrip("/")
        self.clock = clock
        self.retry_policy = retry_policy
        self.lock = threading.RLock()

    def now(self):
        return int(self.clock())

    def _require_gateway(self):
        if self.gateway is None:
            raise ValueError("Укажите YOOKASSA_SHOP_ID и тестовый YOOKASSA_SECRET_KEY в .env.")

    def _row(self, table, item_id):
        with self.store.connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError("Операция не найдена.")
        return dict(row)

    def create_payment(self, customer, amount, recurring=False, interval_days=30, consent=False):
        self._require_gateway()
        customer = customer.strip()
        if not customer or len(customer) > 100:
            raise ValueError("Имя пользователя должно содержать от 1 до 100 символов.")
        amount = money(amount)
        if recurring and not consent:
            raise ValueError("Для автоплатежей необходимо согласие пользователя.")
        if recurring and (not isinstance(interval_days, int) or not 1 <= interval_days <= 365):
            raise ValueError("Интервал подписки должен быть от 1 до 365 дней.")
        with self.lock:
            payment_id = str(uuid.uuid4())
            subscription_id = str(uuid.uuid4()) if recurring else None
            payload = {
                "amount": {"value": amount, "currency": "RUB"}, "capture": True,
                "description": f"Учебный платёж {payment_id}",
                "confirmation": {"type": "redirect", "return_url": self.base_url + "/return?" + urlencode({"payment_id": payment_id})},
                "save_payment_method": bool(recurring),
            }
            with self.store.connect() as db:
                if recurring:
                    db.execute("INSERT INTO subscriptions(id,customer,amount,interval_days,created_at) VALUES (?,?,?,?,?)",
                               (subscription_id, customer, amount, interval_days, self.now()))
                self._insert_payment(db, payment_id, subscription_id, customer, amount,
                                     "initial" if recurring else "one_time", payload)
            self._sync("payments", payment_id)
            self.send_notifications()
            return self._row("payments", payment_id)

    def _insert_payment(self, db, item_id, subscription_id, customer, amount, kind, payload):
        db.execute("""INSERT INTO payments
            (id,subscription_id,customer,amount,kind,idempotence_key,request_json,created_at)
            VALUES (?,?,?,?,?,?,?,?)""",
                   (item_id, subscription_id, customer, amount, kind, str(uuid.uuid4()), json.dumps(payload), self.now()))

    def refresh_payment(self, payment_id):
        self._require_gateway()
        with self.lock:
            self._sync("payments", payment_id)
            self.send_notifications()
            return self._row("payments", payment_id)

    def refund_payment(self, payment_id):
        self._require_gateway()
        with self.lock:
            payment = self._row("payments", payment_id)
            if payment["status"] != "succeeded":
                raise ValueError("Вернуть можно только успешный платёж.")
            with self.store.connect() as db:
                existing = db.execute("SELECT id FROM refunds WHERE payment_id=? AND status NOT IN ('canceled','error')",
                                      (payment_id,)).fetchone()
                if existing:
                    raise ValueError("Возврат уже создан. Его статус будет обновлён автоматически.")
                item_id = str(uuid.uuid4())
                payload = {"payment_id": payment["gateway_id"], "amount": {"value": payment["amount"], "currency": "RUB"}}
                db.execute("""INSERT INTO refunds(id,payment_id,amount,idempotence_key,request_json,created_at)
                           VALUES (?,?,?,?,?,?)""",
                           (item_id, payment_id, payment["amount"], str(uuid.uuid4()), json.dumps(payload), self.now()))
            self._sync("refunds", item_id)
            self.send_notifications()
            return self._row("refunds", item_id)

    def cancel_subscription(self, subscription_id):
        with self.lock, self.store.connect() as db:
            if db.execute("UPDATE subscriptions SET status='canceled',next_charge_at=NULL WHERE id=?",
                          (subscription_id,)).rowcount == 0:
                raise ValueError("Подписка не найдена.")

    def _notify(self, db, event_key, text):
        db.execute("INSERT OR IGNORE INTO notifications(event_key,text) VALUES (?,?)", (event_key, text))

    def _sync(self, table, item_id):
        row = self._row(table, item_id)
        if row["status"] not in IN_FLIGHT:
            return
        if not row["gateway_id"] and self.now() - row["created_at"] >= SAFE_REPLAY_WINDOW:
            self._finish(table, row, "manual_review", "Результат неизвестен. Окно безопасного повтора истекло: проверьте операцию в ЮKassa вручную.")
            return
        try:
            if row["gateway_id"]:
                method = self.gateway.get_payment if table == "payments" else self.gateway.get_refund
                result = method(row["gateway_id"])
            else:
                method = self.gateway.create_payment if table == "payments" else self.gateway.create_refund
                result = method(json.loads(row["request_json"]), row["idempotence_key"])
        except GatewayError as error:
            if row["gateway_id"] or error.ambiguous or row["reason"]:
                with self.store.connect() as db:
                    db.execute(f"UPDATE {table} SET reason=? WHERE id=?", (error.message, item_id))
            else:
                self._finish(table, row, "error", error.message)
            return
        try:
            valid = (isinstance(result["id"], str) and bool(result["id"])
                     and (not row["gateway_id"] or result["id"] == row["gateway_id"])
                     and result["amount"]["currency"] == "RUB"
                     and money(result["amount"]["value"]) == row["amount"]
                     and result["status"] in ("pending", "waiting_for_capture", "succeeded", "canceled"))
            if table == "refunds":
                valid = valid and result["payment_id"] == json.loads(row["request_json"])["payment_id"]
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            with self.store.connect() as db:
                db.execute(f"UPDATE {table} SET reason=? WHERE id=?", ("Неожиданный ответ ЮKassa; результат пока не подтверждён.", item_id))
            return
        with self.store.connect() as db:
            db.execute(f"UPDATE {table} SET gateway_id=?,status=?,reason=NULL WHERE id=?",
                       (result["id"], row["status"], item_id))
            if table == "payments":
                confirmation_url = (result.get("confirmation") or {}).get("confirmation_url")
                if confirmation_url and urlparse(confirmation_url).scheme == "https":
                    db.execute("UPDATE payments SET confirmation_url=? WHERE id=?", (confirmation_url, item_id))
        row["gateway_id"] = result["id"]
        status = result["status"]
        if status in ("succeeded", "canceled"):
            reason = None
            if status == "canceled":
                code = (result.get("cancellation_details") or {}).get("reason", "unknown")
                reason = f"{REASONS.get(code, 'Причина отказа от платёжного шлюза')}: {code}"
            self._finish(table, row, status, reason, result)
        elif status == "waiting_for_capture":
            self._finish(table, row, "manual_review", "ЮKassa ожидает подтверждения списания, хотя запрос содержит capture=true. Нужна ручная проверка.")
        else:
            with self.store.connect() as db:
                db.execute(f"UPDATE {table} SET status='pending' WHERE id=?", (item_id,))

    def _finish(self, table, row, status, reason, result=None):
        with self.store.connect() as db:
            db.execute(f"UPDATE {table} SET status=?,reason=?,completed_at=? WHERE id=?",
                       (status, reason, self.now(), row["id"]))
            extra = ""
            if table == "payments" and row["subscription_id"]:
                sub = dict(db.execute("SELECT * FROM subscriptions WHERE id=?", (row["subscription_id"],)).fetchone())
                if sub["status"] != "canceled":
                    if status == "succeeded":
                        method = (result or {}).get("payment_method") or {}
                        method_id = sub["payment_method_id"]
                        if row["kind"] == "initial":
                            method_id = method.get("id") if method.get("saved") is True else None
                        if method_id:
                            db.execute("""UPDATE subscriptions SET status='active',payment_method_id=?,
                                next_charge_at=?,retry_count=0,last_error=NULL WHERE id=?""",
                                       (method_id, self.now() + sub["interval_days"] * DAY, sub["id"]))
                        else:
                            extra = "Автоплатежи приостановлены: способ оплаты не сохранён."
                            db.execute("UPDATE subscriptions SET status='paused',next_charge_at=NULL,last_error=? WHERE id=?", (extra, sub["id"]))
                    elif (status == "canceled" and sub["payment_method_id"]
                          and ((result or {}).get("cancellation_details") or {}).get("reason") != "permission_revoked"
                          and (sub["retry_count"] == 0 or self.retry_policy == "daily")):
                        extra = "Повторное списание запланировано через 24 часа."
                        db.execute("""UPDATE subscriptions SET next_charge_at=?,retry_count=retry_count+1,
                            last_error=? WHERE id=?""", (self.now() + DAY, reason, sub["id"]))
                    else:
                        extra = "Подписка приостановлена. Для возобновления создайте новую подписку."
                        db.execute("UPDATE subscriptions SET status='paused',next_charge_at=NULL,last_error=? WHERE id=?", (reason, sub["id"]))
            title = "Платёж" if table == "payments" else "Возврат"
            label = {"succeeded": "успешно", "canceled": "отклонён", "error": "ошибка запроса", "manual_review": "нужна проверка"}[status]
            customer = row.get("customer")
            if table == "refunds":
                customer = db.execute("SELECT customer FROM payments WHERE id=?", (row["payment_id"],)).fetchone()[0]
            text = f"{title}: {label}\nПользователь: {customer}\nСумма: {row['amount']} RUB\nОперация: {row['id']}\nСтатус: {status}"
            if reason:
                text += f"\nПричина: {reason}"
            if extra:
                text += "\n" + extra
            self._notify(db, f"{table}:{row['id']}:{status}", text)

    def tick(self):
        with self.lock:
            if self.gateway is not None:
                for table in ("payments", "refunds"):
                    with self.store.connect() as db:
                        rows = db.execute(f"SELECT id FROM {table} WHERE status IN ('creating','pending','waiting_for_capture')").fetchall()
                    for row in rows:
                        self._sync(table, row["id"])
                with self.store.connect() as db:
                    due = db.execute("""SELECT * FROM subscriptions s WHERE status='active' AND next_charge_at<=?
                        AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.subscription_id=s.id
                            AND p.status IN ('creating','pending','waiting_for_capture','manual_review'))""", (self.now(),)).fetchall()
                for sub in due:
                    item_id = str(uuid.uuid4())
                    payload = {"amount": {"value": sub["amount"], "currency": "RUB"}, "capture": True,
                               "payment_method_id": sub["payment_method_id"], "description": f"Учебный автоплатёж {item_id}"}
                    with self.store.connect() as db:
                        self._insert_payment(db, item_id, sub["id"], sub["customer"], sub["amount"],
                                             "retry" if sub["retry_count"] else "recurring", payload)
                        db.execute("UPDATE subscriptions SET next_charge_at=NULL WHERE id=?", (sub["id"],))
                    self._sync("payments", item_id)
            self.send_notifications()

    def send_notifications(self):
        with self.lock:
            with self.store.connect() as db:
                rows = db.execute("SELECT * FROM notifications WHERE sent_at IS NULL ORDER BY id").fetchall()
            for row in rows:
                try:
                    if self.notifier is None:
                        raise NotificationError("Telegram не настроен: укажите токен бота и chat_id в .env.")
                    self.notifier.send(row["text"])
                except NotificationError as error:
                    with self.store.connect() as db:
                        db.execute("UPDATE notifications SET last_error=? WHERE id=?", (str(error), row["id"]))
                    break
                else:
                    with self.store.connect() as db:
                        db.execute("UPDATE notifications SET sent_at=?,last_error=NULL WHERE id=?", (self.now(), row["id"]))

    def dashboard(self):
        with self.lock, self.store.connect() as db:
            payments = [dict(row) for row in db.execute("""SELECT p.*, (status='succeeded' AND NOT EXISTS
                (SELECT 1 FROM refunds r WHERE r.payment_id=p.id AND r.status NOT IN ('canceled','error'))) AS refundable
                FROM payments p ORDER BY created_at DESC,rowid DESC LIMIT 100""")]
            return {
                "payments": payments,
                "subscriptions": [dict(row) for row in db.execute("SELECT * FROM subscriptions ORDER BY created_at DESC LIMIT 100")],
                "refunds": [dict(row) for row in db.execute("SELECT * FROM refunds ORDER BY created_at DESC LIMIT 100")],
                "notifications": [dict(row) for row in db.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 30")],
            }
