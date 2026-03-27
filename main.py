import hmac
import json
import logging
import os
import random
import threading
import time
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional

import requests
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("max-id-bot")

MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api.max.ru")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_TIMEOUT_SECONDS = float(os.getenv("MAX_TIMEOUT_SECONDS", "10"))
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET")
MAX_API_MAX_RETRIES = int(os.getenv("MAX_API_MAX_RETRIES", "5"))
MAX_DEDUP_TTL_SECONDS = int(os.getenv("MAX_DEDUP_TTL_SECONDS", "3600"))
MAX_WEBHOOK_URL = os.getenv("MAX_WEBHOOK_URL")
MAX_WEBHOOK_UPDATE_TYPES = [
    item.strip()
    for item in os.getenv("MAX_WEBHOOK_UPDATE_TYPES", "message_created,bot_started,message_callback").split(",")
    if item.strip()
]
MAX_WEBHOOK_AUTO_REGISTER = os.getenv("MAX_WEBHOOK_AUTO_REGISTER", "true").lower() in {"1", "true", "yes"}
MAX_STARTUP_SELF_CHECK = os.getenv("MAX_STARTUP_SELF_CHECK", "false").lower() in {"1", "true", "yes"}
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")

app = FastAPI(title="MAX ID Bot", version="1.2.0")

# Простой in-memory dedup для повторной доставки webhook (at-least-once).
_processed_updates: dict[str, float] = {}
_dedup_lock = threading.Lock()


def _extract_by_paths(payload: dict[str, Any], paths: list[str]) -> Optional[Any]:
    for path in paths:
        value: Any = payload
        for key in path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                value = None
                break
        if value not in (None, ""):
            return value
    return None


def extract_user_id(payload: dict[str, Any]) -> Optional[str]:
    candidate_paths = [
        "message.sender.user_id",
        "message.sender.id",
        "sender.user_id",
        "sender.id",
        "user.user_id",
        "user.id",
        "profile.user_id",
        "profile.id",
        "dialog_with_user.user_id",
        "dialog_with_user.id",
    ]
    user_id = _extract_by_paths(payload, candidate_paths)
    return str(user_id) if user_id is not None else None


def extract_chat_id(payload: dict[str, Any]) -> Optional[str]:
    candidate_paths = [
        "message.recipient.chat_id",
        "message.chat_id",
        "chat.chat_id",
        "chat_id",
    ]
    chat_id = _extract_by_paths(payload, candidate_paths)
    return str(chat_id) if chat_id is not None else None


def extract_message_text(payload: dict[str, Any]) -> Optional[str]:
    text_value = _extract_by_paths(
        payload,
        [
            "message.body.text",
            "message.text",
            "body.text",
            "text",
        ],
    )
    return str(text_value) if text_value is not None else None


def normalize_incoming_text(raw_text: str) -> str:
    """
    Нормализует входной текст:
    - trim;
    - lower;
    - убирает упоминание бота в командах вида '/id@my_bot'.
    """
    text = raw_text.strip().lower()
    if text.startswith("/") and "@" in text:
        text = text.split("@", 1)[0]
    return text


def extract_dedup_key(payload: dict[str, Any]) -> Optional[str]:
    update_type = str(payload.get("update_type") or "unknown")
    mid = _extract_by_paths(payload, ["message.body.mid", "message.mid", "mid"])
    callback_id = _extract_by_paths(payload, ["callback.callback_id", "callback_id"])

    if mid:
        return f"{update_type}:mid:{mid}"
    if callback_id:
        return f"{update_type}:cb:{callback_id}"

    # Фолбэк: если нет mid/callback_id, dedup не применяем.
    return None


def _sleep_backoff(attempt: int, base: float = 0.4, cap: float = 8.0) -> None:
    delay = min(cap, base * (2**attempt))
    delay *= 0.5 + random.random()
    time.sleep(delay)


def _is_duplicate_and_mark(key: str) -> bool:
    now = time.time()
    with _dedup_lock:
        expired = [k for k, ts in _processed_updates.items() if now - ts > MAX_DEDUP_TTL_SECONDS]
        for k in expired:
            _processed_updates.pop(k, None)

        if key in _processed_updates:
            return True

        _processed_updates[key] = now
        return False


def get_coupon_barcode_and_expiry(target_date: Optional[date] = None) -> tuple[str, date]:
    current_date = target_date or datetime.utcnow().date()
    day = current_date.day
    month_last_day = monthrange(current_date.year, current_date.month)[1]

    if 1 <= day <= 10:
        barcode_value = "7123100000145"
        expiry = current_date.replace(day=10)
    elif 11 <= day <= 20:
        barcode_value = "7123100000152"
        expiry = current_date.replace(day=20)
    else:
        barcode_value = "7123100000169"
        expiry = current_date.replace(day=month_last_day)

    return barcode_value, expiry


def build_coupon_text(expiry_date: date) -> str:
    expiry_str = expiry_date.strftime("%d.%m.%Y")
    return (
        "Спасибо, что подписались 💛\n"
        "Дарим вам скидку на первую покупку — просто используйте этот купон при оформлении заказа.\n\n"
        "🛍 Покажите штрихкод на кассе и покупайте с выгодой\n\n"
        f"⏳ Купон действует до {expiry_str}"
    )


def generate_ean13_png_file(barcode_value: str, output_dir: Path) -> Path:
    """
    Генерирует PNG-файл EAN13.
    Используется ленивый импорт, чтобы модуль main.py не падал при импорте без этих зависимостей.
    """
    from barcode import EAN13  # type: ignore[import-not-found]
    from barcode.writer import ImageWriter  # type: ignore[import-not-found]

    filename = output_dir / "coupon_ean13"
    ean = EAN13(barcode_value, writer=ImageWriter())
    saved_path = Path(ean.save(str(filename)))
    return saved_path


def _extract_upload_url(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["url", "upload_url", "data.url"])


def _extract_attachment_token(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["token", "file.token", "data.token", "attachment.token"])


def upload_image_and_get_token(file_path: Path) -> str:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    response = requests.post(
        f"{MAX_API_BASE_URL}/uploads",
        params={"type": "image"},
        headers={"Authorization": MAX_BOT_TOKEN},
        timeout=MAX_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Не удалось получить upload URL: {response.status_code}")

    upload_meta = response.json() if response.content else {}
    upload_url = _extract_upload_url(upload_meta)
    if not upload_url:
        raise HTTPException(status_code=502, detail="MAX /uploads не вернул URL для загрузки")

    with file_path.open("rb") as fh:
        upload_response = requests.post(
            upload_url,
            headers={"Authorization": MAX_BOT_TOKEN},
            files={"data": (file_path.name, fh, "image/png")},
            timeout=MAX_TIMEOUT_SECONDS,
        )
    if upload_response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла: {upload_response.status_code}")

    upload_result = upload_response.json() if upload_response.content else {}
    token = _extract_attachment_token(upload_result)
    if not token:
        raise HTTPException(status_code=502, detail="Не получен token загруженного изображения")
    return str(token)


def send_max_message(
    text: str,
    user_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")
    if not user_id and not chat_id:
        raise ValueError("Нужен user_id или chat_id для отправки сообщения")

    params: dict[str, str] = {"user_id": user_id} if user_id else {"chat_id": chat_id}  # type: ignore[arg-type]
    url = f"{MAX_API_BASE_URL}/messages"

    last_error: Optional[str] = None
    for attempt in range(MAX_API_MAX_RETRIES):
        try:
            response = requests.post(
                url,
                params=params,
                headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
                json={"text": text, **({"attachments": attachments} if attachments else {})},
                timeout=MAX_TIMEOUT_SECONDS,
            )

            if response.status_code in (429, 503):
                last_error = f"retryable status={response.status_code}"
                _sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                logger.error("MAX API error %s: %s", response.status_code, response.text)
                raise HTTPException(status_code=502, detail="Ошибка отправки сообщения через MAX API")

            return response.json() if response.content else {"ok": True}
        except requests.RequestException as exc:
            last_error = str(exc)
            _sleep_backoff(attempt)

    raise HTTPException(status_code=502, detail=f"MAX API недоступен после ретраев: {last_error}")


def send_coupon(user_id: Optional[str], chat_id: Optional[str]) -> None:
    barcode_value, expiry_date = get_coupon_barcode_and_expiry()
    coupon_text = build_coupon_text(expiry_date)

    try:
        with TemporaryDirectory(prefix="coupon_ean13_") as tmp_dir:
            image_path = generate_ean13_png_file(barcode_value, Path(tmp_dir))
            token = upload_image_and_get_token(image_path)
            send_max_message(
                text=coupon_text,
                user_id=user_id,
                chat_id=chat_id,
                attachments=[{"type": "image", "payload": {"token": token}}],
            )
    except Exception as exc:
        logger.exception("Не удалось отправить изображение купона, отправляем fallback без цифрового кода: %s", exc)
        send_max_message(
            text=(
                f"{coupon_text}\n\n"
                "⚠️ Сейчас не удалось прикрепить изображение штрихкода. "
                "Попробуйте запросить купон ещё раз через минуту."
            ),
            user_id=user_id,
            chat_id=chat_id,
        )


def check_max_auth() -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    response = requests.get(
        f"{MAX_API_BASE_URL}/me",
        headers={"Authorization": MAX_BOT_TOKEN},
        timeout=MAX_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        logger.error("MAX /me auth check failed %s: %s", response.status_code, response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Проверка MAX API (/me) не прошла: status={response.status_code}",
        )

    return response.json() if response.content else {"ok": True}



def register_webhook_subscription() -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")
    webhook_url = get_effective_webhook_url()
    if not webhook_url:
        raise RuntimeError(
            "Webhook URL не определён. Задайте MAX_WEBHOOK_URL или RAILWAY_PUBLIC_DOMAIN."
        )

    payload: dict[str, Any] = {"url": webhook_url, "update_types": MAX_WEBHOOK_UPDATE_TYPES}
    if MAX_WEBHOOK_SECRET:
        payload["secret"] = MAX_WEBHOOK_SECRET

    response = requests.post(
        f"{MAX_API_BASE_URL}/subscriptions",
        headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
        json=payload,
        timeout=MAX_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        logger.error("MAX /subscriptions register failed %s: %s", response.status_code, response.text)
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось зарегистрировать webhook в MAX: status={response.status_code}",
        )

    return response.json() if response.content else {"ok": True}


def get_effective_webhook_url() -> Optional[str]:
    """
    Возвращает webhook URL в приоритете:
    1) MAX_WEBHOOK_URL
    2) RAILWAY_PUBLIC_DOMAIN -> https://<domain>/webhook
    """
    if MAX_WEBHOOK_URL:
        return MAX_WEBHOOK_URL
    if RAILWAY_PUBLIC_DOMAIN:
        return f"https://{RAILWAY_PUBLIC_DOMAIN}/webhook"
    return None


@app.on_event("startup")
def auto_register_webhook_on_startup() -> None:
    effective_webhook_url = get_effective_webhook_url()
    logger.info(
        "Startup config: token_set=%s webhook_url=%s effective_webhook_url=%s auto_register=%s update_types=%s secret_set=%s self_check=%s",
        bool(MAX_BOT_TOKEN),
        MAX_WEBHOOK_URL or "<empty>",
        effective_webhook_url or "<empty>",
        MAX_WEBHOOK_AUTO_REGISTER,
        ",".join(MAX_WEBHOOK_UPDATE_TYPES) or "<empty>",
        bool(MAX_WEBHOOK_SECRET),
        MAX_STARTUP_SELF_CHECK,
    )
    if not effective_webhook_url:
        logger.warning(
            "Webhook URL не задан. Укажите MAX_WEBHOOK_URL или включите Public Domain в Railway (RAILWAY_PUBLIC_DOMAIN)."
        )

    if MAX_STARTUP_SELF_CHECK:
        try:
            me = check_max_auth()
            logger.info("Startup MAX /me check OK: %s", json.dumps(me, ensure_ascii=False))
        except Exception as exc:
            logger.exception("Startup MAX /me check failed: %s", exc)

    if not MAX_WEBHOOK_AUTO_REGISTER:
        logger.info("Webhook auto-registration skipped: MAX_WEBHOOK_AUTO_REGISTER=false")
        return

    try:
        result = register_webhook_subscription()
        logger.info("Webhook registration success on startup: %s", result)
    except Exception as exc:
        logger.exception("Webhook auto-registration failed on startup: %s", exc)


@app.post("/setup/subscription")
def setup_subscription() -> JSONResponse:
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result})


@app.get("/subscribe")
def subscribe_get() -> JSONResponse:
    """Удобный endpoint для ручной проверки из браузера/Railway (GET)."""
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result, "hint": "Webhook subscription registered via GET /subscribe"})


@app.post("/subscribe")
def subscribe_post() -> JSONResponse:
    """Алиас на setup endpoint: регистрация webhook через POST /subscribe."""
    result = register_webhook_subscription()
    return JSONResponse({"ok": True, "subscription": result, "hint": "Webhook subscription registered via POST /subscribe"})

def process_update(payload: dict[str, Any]) -> None:
    update_type = str(payload.get("update_type") or "")
    if update_type and update_type not in {"message_created", "bot_started"}:
        logger.info("Skip unsupported update_type=%s", update_type)
        return

    dedup_key = extract_dedup_key(payload)
    if dedup_key and _is_duplicate_and_mark(dedup_key):
        logger.info("Skip duplicate update: %s", dedup_key)
        return

    user_id = extract_user_id(payload)
    chat_id = extract_chat_id(payload)
    message_text = normalize_incoming_text(extract_message_text(payload) or "")

    try:
        if message_text in {"test", "тест", "/test", "/hello", "/start"}:
            send_max_message(text="ПРИВЕТ", user_id=user_id, chat_id=chat_id)
            return
        if message_text in {"купон", "/купон", "coupon", "/coupon"}:
            send_coupon(user_id=user_id, chat_id=chat_id)
            return
        if message_text in {"id", "айди", "/id"}:
            if user_id:
                send_max_message(text=f"Ваш ID: {user_id}", user_id=user_id, chat_id=chat_id)
            elif chat_id:
                send_max_message(
                    text="Не удалось извлечь user_id. Ваш chat_id: " + chat_id,
                    chat_id=chat_id,
                )
            return

        if not user_id:
            logger.warning("Не удалось извлечь user_id из события")
            if chat_id:
                send_max_message(
                    text=(
                        "Не удалось определить ваш ID из этого события. "
                        "Пожалуйста, отправьте сообщение боту в личный диалог."
                    ),
                    chat_id=chat_id,
                )
            return

        send_max_message(text=f"Ваш ID: {user_id}", user_id=user_id)
    except Exception as exc:
        logger.exception("Ошибка обработки события: %s", exc)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "max-id-bot", "status": "ok", "hint": "Use POST /webhook for MAX events"}


@app.get("/webhook")
def webhook_get_hint() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"ok": True, "message": "Webhook endpoint is alive. Send POST requests from MAX to /webhook."},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/max")
def health_max() -> JSONResponse:
    me = check_max_auth()
    return JSONResponse({"status": "ok", "max_auth": True, "me": me})


@app.get("/health/config")
def health_config() -> JSONResponse:
    effective_webhook_url = get_effective_webhook_url()
    issues: list[str] = []
    if not MAX_BOT_TOKEN:
        issues.append("MAX_BOT_TOKEN is empty")
    if not effective_webhook_url:
        issues.append("webhook url is empty: set MAX_WEBHOOK_URL or RAILWAY_PUBLIC_DOMAIN")

    return JSONResponse(
        {
            "status": "ok",
            "config": {
                "max_api_base_url": MAX_API_BASE_URL,
                "token_set": bool(MAX_BOT_TOKEN),
                "webhook_url": MAX_WEBHOOK_URL,
                "railway_public_domain": RAILWAY_PUBLIC_DOMAIN,
                "effective_webhook_url": effective_webhook_url,
                "webhook_secret_set": bool(MAX_WEBHOOK_SECRET),
                "webhook_auto_register": MAX_WEBHOOK_AUTO_REGISTER,
                "webhook_update_types": MAX_WEBHOOK_UPDATE_TYPES,
                "startup_self_check": MAX_STARTUP_SELF_CHECK,
                "issues": issues,
            },
        }
    )


@app.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_max_bot_api_secret: Optional[str] = Header(default=None),
) -> JSONResponse:
    if MAX_WEBHOOK_SECRET:
        if not x_max_bot_api_secret or not hmac.compare_digest(x_max_bot_api_secret, MAX_WEBHOOK_SECRET):
            logger.warning("Webhook secret mismatch")
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    try:
        payload = await request.json()
    except Exception as exc:
        logger.exception("Некорректный JSON в webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Некорректный JSON") from exc

    logger.info("Incoming MAX event: %s", json.dumps(payload, ensure_ascii=False))

    background_tasks.add_task(process_update, payload)
    return JSONResponse({"ok": True, "accepted": True})


def run() -> None:
    """
    Запуск Uvicorn с логами в stdout.
    Это нужно для платформ, где stderr автоматически помечается как error.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").lower()
    port = int(os.getenv("PORT", "8000"))
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
            "access": {
                "format": '%(asctime)s %(levelname)s %(name)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "access",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": log_level.upper(), "propagate": False},
        },
    }
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=log_level, log_config=log_config)


if __name__ == "__main__":
    run()
