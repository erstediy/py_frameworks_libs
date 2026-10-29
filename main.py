import argparse
import hmac
import logging
import os
import secrets
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from gateways import GatewayError, TelegramNotifier, YooKassaGateway
from service import PaymentService

ROOT = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
PAYMENT_STATUSES = {
    "creating": "Выясняем результат создания", "pending": "Ожидает оплаты / обработки",
    "succeeded": "Успешно", "canceled": "Отклонён", "error": "Ошибка запроса",
    "manual_review": "Нужна ручная проверка", "waiting_for_capture": "Ожидает списания",
}


def create_app(config=None, service=None):
    load_dotenv(ROOT / ".env")
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.getenv("DATABASE_PATH", str(ROOT / "instance" / "payments.sqlite3")),
        BASE_URL=os.getenv("BASE_URL", "http://127.0.0.1:5000"),
        YOOKASSA_SHOP_ID=os.getenv("YOOKASSA_SHOP_ID", ""),
        YOOKASSA_SECRET_KEY=os.getenv("YOOKASSA_SECRET_KEY", ""),
        TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID", ""),
        ALLOW_LIVE_PAYMENTS=os.getenv("ALLOW_LIVE_PAYMENTS", "false").lower() == "true",
        RETRY_POLICY=os.getenv("RETRY_POLICY", "once"),
        WORKER_INTERVAL_SECONDS=int(os.getenv("WORKER_INTERVAL_SECONDS", "30")),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)
    if app.config["RETRY_POLICY"] not in ("once", "daily"):
        raise ValueError("RETRY_POLICY должен быть once или daily.")
    if app.config["WORKER_INTERVAL_SECONDS"] < 1:
        raise ValueError("WORKER_INTERVAL_SECONDS должен быть положительным.")
    if service is None:
        gateway = None
        if app.config["YOOKASSA_SHOP_ID"] and app.config["YOOKASSA_SECRET_KEY"]:
            gateway = YooKassaGateway(app.config["YOOKASSA_SHOP_ID"], app.config["YOOKASSA_SECRET_KEY"],
                                      allow_live=app.config["ALLOW_LIVE_PAYMENTS"])
        notifier = None
        if app.config["TELEGRAM_BOT_TOKEN"] and app.config["TELEGRAM_CHAT_ID"]:
            notifier = TelegramNotifier(app.config["TELEGRAM_BOT_TOKEN"], app.config["TELEGRAM_CHAT_ID"])
        service = PaymentService(app.config["DATABASE"], gateway, notifier, app.config["BASE_URL"],
                                 retry_policy=app.config["RETRY_POLICY"])
    app.extensions["payments"] = service
    app.extensions["worker_error"] = None

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)
        return session["csrf_token"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.filters["dt"] = lambda value: datetime.fromtimestamp(value, MSK).strftime("%d.%m.%Y %H:%M МСК") if value is not None else "—"
    app.jinja_env.filters["payment_status"] = lambda value: PAYMENT_STATUSES.get(value, value)
    app.jinja_env.filters["subscription_status"] = lambda value: {
        "pending": "Первый платёж", "active": "Активна", "paused": "Приостановлена", "canceled": "Отключена",
    }.get(value, value)
    app.jinja_env.filters["kind_label"] = lambda value: {
        "initial": "Первый платёж подписки", "one_time": "Разовый", "recurring": "Автоплатёж", "retry": "Повтор автоплатежа",
    }.get(value, value)

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            actual = request.form.get("csrf_token", "")
            if not expected or not hmac.compare_digest(expected.encode(), actual.encode()):
                abort(400, "Обновите страницу и повторите действие: недействительный CSRF-токен.")

    @app.get("/")
    def index():
        settings = {
            "gateway_configured": service.gateway is not None,
            "telegram_configured": service.notifier is not None,
            "live_mode": bool(app.config["ALLOW_LIVE_PAYMENTS"]),
            "retry_policy": service.retry_policy,
        }
        return render_template("index.html", **service.dashboard(), settings=settings,
                               worker_error=app.extensions["worker_error"])

    @app.post("/payments")
    def create_payment():
        try:
            recurring = request.form.get("recurring") == "1"
            try:
                interval = int(request.form.get("interval_days", "30")) if recurring else 30
            except ValueError:
                raise ValueError("Укажите целое число дней для интервала подписки.") from None
            payment = service.create_payment(request.form.get("customer", ""), request.form.get("amount", ""),
                                             recurring, interval, request.form.get("consent") == "1")
            if payment["confirmation_url"] and payment["status"] == "pending":
                return redirect(payment["confirmation_url"], code=303)
            flash("Платёж создан. Его результат показан в списке ниже.", "info")
        except (ValueError, GatewayError) as error:
            flash(str(error), "error")
        return redirect(url_for("index"), code=303)

    @app.get("/return")
    def payment_return():
        try:
            payment = service.refresh_payment(request.args.get("payment_id", ""))
            flash("Статус платежа: " + PAYMENT_STATUSES.get(payment["status"], payment["status"]), "info")
        except (ValueError, GatewayError) as error:
            flash(str(error), "error")
        return redirect(url_for("index"), code=303)

    @app.post("/payments/<payment_id>/refresh")
    def refresh_payment(payment_id):
        try:
            payment = service.refresh_payment(payment_id)
            flash("Статус: " + PAYMENT_STATUSES.get(payment["status"], payment["status"]), "info")
        except (ValueError, GatewayError) as error:
            flash(str(error), "error")
        return redirect(url_for("index"), code=303)

    @app.post("/payments/<payment_id>/refund")
    def refund_payment(payment_id):
        try:
            refund = service.refund_payment(payment_id)
            flash("Возврат: " + PAYMENT_STATUSES.get(refund["status"], refund["status"]), "info")
        except (ValueError, GatewayError) as error:
            flash(str(error), "error")
        return redirect(url_for("index"), code=303)

    @app.post("/subscriptions/<subscription_id>/cancel")
    def cancel_subscription(subscription_id):
        try:
            service.cancel_subscription(subscription_id)
            flash("Подписка отключена. Уже начатый платёж проверяется до финального результата.", "info")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("index"), code=303)

    return app


def start_worker(app):
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                app.extensions["payments"].tick()
                app.extensions["worker_error"] = None
            except Exception as error:
                logging.error("Фоновая проверка завершилась ошибкой %s", type(error).__name__)
                app.extensions["worker_error"] = "Фоновая проверка дала ошибку. Следующая попытка — в следующем цикле; смотрите консоль."
            stop.wait(app.config["WORKER_INTERVAL_SECONDS"])

    thread = threading.Thread(target=run, name="payment-worker", daemon=True)
    thread.start()
    return stop, thread


@contextmanager
def single_instance(database_path):
    with open(str(database_path) + ".lock", "a+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("Приложение с этой БД уже запущено. Оставьте только один процесс.") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Учебная платёжная система")
    parser.add_argument("--check-once", action="store_true", help="Один проход обработки, без веб-сервера; основной процесс должен быть остановлен")
    args = parser.parse_args()
    application = create_app()
    with single_instance(application.config["DATABASE"]):
        if args.check_once:
            application.extensions["payments"].tick()
        else:
            stop_event, worker = start_worker(application)
            try:
                application.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
            finally:
                stop_event.set()
                worker.join(timeout=30)
