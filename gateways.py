import re

import requests


class GatewayError(Exception):
    def __init__(self, message, ambiguous=False):
        super().__init__(message)
        self.message = message
        self.ambiguous = ambiguous


class NotificationError(Exception):
    pass


class YooKassaGateway:
    API_URL = "https://api.yookassa.ru/v3"
    TIMEOUT = (5, 20)
    SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")

    def __init__(self, shop_id, secret_key, allow_live=False):
        if not shop_id or not secret_key:
            raise GatewayError("Укажите YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.")
        if not secret_key.startswith("test_") and not allow_live:
            raise GatewayError(
                "Разрешены только тестовые ключи ЮKassa. "
                "Для реального магазина требуется ALLOW_LIVE_PAYMENTS=true."
            )
        self.auth = (str(shop_id), secret_key)

    def create_payment(self, payload, key):
        return self._request("POST", "/payments", payload, key)

    def get_payment(self, payment_id):
        return self._request("GET", "/payments/" + self._validate_id(payment_id))

    def create_refund(self, payload, key):
        return self._request("POST", "/refunds", payload, key)

    def get_refund(self, refund_id):
        return self._request("GET", "/refunds/" + self._validate_id(refund_id))

    @classmethod
    def _validate_id(cls, value):
        if not isinstance(value, str) or not cls.SAFE_ID.fullmatch(value):
            raise GatewayError("Некорректный идентификатор операции ЮKassa.")
        return value

    def _request(self, method, path, payload=None, key=None):
        is_post = method == "POST"
        headers = {}
        if is_post:
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise GatewayError("Некорректный ключ идемпотентности.")
            headers["Idempotence-Key"] = key
        try:
            response = requests.request(
                method,
                self.API_URL + path,
                auth=self.auth,
                headers=headers,
                json=payload,
                timeout=self.TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise GatewayError(
                "Не удалось получить ответ ЮKassa. Статус операции пока неизвестен.",
                ambiguous=is_post,
            ) from None
        if response.status_code >= 400:
            raise GatewayError(
                f"ЮKassa отклонила запрос (HTTP {response.status_code}).",
                ambiguous=is_post and response.status_code >= 500,
            )
        if not 200 <= response.status_code < 300:
            raise GatewayError("Неожиданный ответ ЮKassa.", ambiguous=is_post)
        try:
            data = response.json()
        except ValueError:
            raise GatewayError("Некорректный ответ ЮKassa.", ambiguous=is_post) from None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("id"), str)
            or not self.SAFE_ID.fullmatch(data["id"])
            or data.get("status") not in {"pending", "waiting_for_capture", "succeeded", "canceled"}
        ):
            raise GatewayError("Неполный ответ ЮKassa.", ambiguous=is_post)
        return data


class TelegramNotifier:
    TIMEOUT = (5, 20)

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id

    def send(self, text):
        if not self.token or not self.chat_id:
            raise NotificationError("Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", self.token):
            raise NotificationError("Некорректный токен Telegram-бота.")
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=self.TIMEOUT,
                allow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise NotificationError(
                    f"Telegram не принял уведомление (HTTP {response.status_code})."
                )
            data = response.json()
            if not isinstance(data, dict) or data.get("ok") is not True:
                raise NotificationError("Telegram не подтвердил отправку уведомления.")
        except requests.RequestException:
            raise NotificationError("Не удалось связаться с Telegram.") from None
        except ValueError:
            raise NotificationError("Некорректный ответ Telegram.") from None
