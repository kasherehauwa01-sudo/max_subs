import hmac
import json
import logging
import os
import random
import threading
import time
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

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
BASE_WEBHOOK_UPDATE_TYPES = [
    item.strip()
    for item in os.getenv("MAX_WEBHOOK_UPDATE_TYPES", "message_created,bot_started,message_callback").split(",")
    if item.strip()
]
MAX_WEBHOOK_AUTO_REGISTER = os.getenv("MAX_WEBHOOK_AUTO_REGISTER", "true").lower() in {"1", "true", "yes"}
MAX_STARTUP_SELF_CHECK = os.getenv("MAX_STARTUP_SELF_CHECK", "false").lower() in {"1", "true", "yes"}
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN")
MAX_CHANNEL_CHAT_ID = os.getenv("MAX_CHANNEL_CHAT_ID", "-72559954357735")
MAX_CHANNEL_URL = os.getenv("MAX_CHANNEL_URL", f"https://web.max.ru/{MAX_CHANNEL_CHAT_ID}")
MAX_CHANNEL_DEEPLINK = os.getenv("MAX_CHANNEL_DEEPLINK", f"max://chat/{MAX_CHANNEL_CHAT_ID}")
MAX_WEB_APP = os.getenv("MAX_WEB_APP")
ACTIVE_WEBHOOK_UPDATE_TYPES: list[str] = []


def get_channel_id_candidates() -> list[str]:
    """
    Возвращает варианты channel/chat id для запросов в MAX API.
    Практика показывает, что web-ссылка канала может быть со знаком "-", а API
    в некоторых методах ожидает id без знака. Поэтому пробуем оба варианта.
    """
    raw = (MAX_CHANNEL_CHAT_ID or "").strip()
    if not raw:
        return []
    variants = [raw]
    unsigned = raw.lstrip("-")
    if unsigned and unsigned not in variants:
        variants.append(unsigned)
    return variants

def _find_token_recursive(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        token_value = value.get("token")
        if token_value not in (None, ""):
            return str(token_value)
        for nested in value.values():
            found = _find_token_recursive(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token_recursive(item)
            if found:
                return found
    return None


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
    current_date = target_date or datetime.now(timezone.utc).date()
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
        "Дарим вам дополнительную скидку 5%.\n"
        "🛍 Покажите штрихкод на кассе и покупайте с выгодой\n\n"
        f"⏳ Купон действует до {expiry_str}\n"
        "_⚠ Скидка действует только на товары с белыми ценниками_"
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
    saved_path = Path(
        ean.save(
            str(filename),
            options={
                "write_text": False,
            },
        )
    )
    return saved_path


def _extract_upload_url(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["url", "upload_url", "data.url"])


def _extract_attachment_token(payload: dict[str, Any]) -> Optional[str]:
    return _extract_by_paths(payload, ["token", "file.token", "data.token", "attachment.token"]) or _find_token_recursive(
        payload
    )


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

    token_from_meta = _extract_attachment_token(upload_meta)
    with file_path.open("rb") as fh:
        upload_response = requests.post(
            upload_url,
            headers={"Authorization": MAX_BOT_TOKEN},
            files={"data": (file_path.name, fh, "image/png")},
            timeout=MAX_TIMEOUT_SECONDS,
        )
    if upload_response.status_code >= 400:
        # fallback для совместимости с возможной схемой multipart-поля "file"
        with file_path.open("rb") as fh:
            upload_response = requests.post(
                upload_url,
                headers={"Authorization": MAX_BOT_TOKEN},
                files={"file": (file_path.name, fh, "image/png")},
                timeout=MAX_TIMEOUT_SECONDS,
            )
    if upload_response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Ошибка загрузки файла: {upload_response.status_code}")

    try:
        upload_result = upload_response.json() if upload_response.content else {}
    except ValueError:
        upload_result = {}

    token = _extract_attachment_token(upload_result) or token_from_meta
    if not token:
        logger.error(
            "Upload token not found. /uploads response=%s upload response=%s",
            json.dumps(upload_meta, ensure_ascii=False),
            upload_response.text[:500],
        )
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
                text=f"\n{coupon_text}",
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


def get_miniapp_url() -> Optional[str]:
    base_url = get_public_base_url()
    if base_url:
        return f"{base_url}/miniapp"
    webhook_url = get_effective_webhook_url()
    if webhook_url:
        return webhook_url.removesuffix("/webhook") + "/miniapp"
    return None


def get_public_base_url() -> Optional[str]:
    base_url = os.getenv("PUBLIC_BASE_URL")
    if base_url:
        return base_url.rstrip("/")

    webhook_url = get_effective_webhook_url()
    if webhook_url:
        parsed = urlparse(webhook_url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return None


def build_miniapp_button_attachments() -> list[dict[str, Any]]:
    miniapp_url = get_miniapp_url()
    web_app_value = (MAX_WEB_APP or "").strip()

    # Для open_app MAX API ожидает поле web_app (snake_case) и значение
    # с username миниприложения (бота), например: "my_bot".
    # Если web_app не задан, отправляем link-кнопку как безопасный фолбэк.
    if web_app_value:
        button: dict[str, Any] = {
            "type": "open_app",
            "text": "Получить купон",
            "web_app": web_app_value,
        }
    elif miniapp_url:
        logger.warning(
            "MAX_WEB_APP не задан: отправляем link-кнопку вместо open_app. "
            "Чтобы miniapp открывался внутри MAX с контекстом пользователя, укажите MAX_WEB_APP=<bot_username>."
        )
        button = {
            "type": "link",
            "text": "Получить купон",
            "url": miniapp_url,
        }
    else:
        return []

    return [
        {
            "type": "inline_keyboard",
            "payload": {
                "buttons": [
                    [
                        button
                    ]
                ]
            },
        }
    ]


def send_miniapp_entry(user_id: Optional[str], chat_id: Optional[str]) -> None:
    try:
        send_max_message(
            text="Откройте миниприложение и нажмите «Получить купон».",
            user_id=user_id,
            chat_id=chat_id,
            attachments=build_miniapp_button_attachments(),
        )
    except HTTPException:
        # Фолбэк для нестандартных клиентов/конфигов:
        # если open_app не принялся, отправляем link-кнопку на URL miniapp.
        miniapp_url = get_miniapp_url()
        fallback_attachments: list[dict[str, Any]] = []
        if miniapp_url:
            fallback_attachments = [
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [
                            [
                                {
                                    "type": "link",
                                    "text": "Получить купон",
                                    "url": miniapp_url,
                                }
                            ]
                        ]
                    },
                }
            ]
        send_max_message(
            text="Откройте миниприложение и нажмите «Получить купон».",
            user_id=user_id,
            chat_id=chat_id,
            attachments=fallback_attachments,
        )


def is_user_subscribed_to_channel(user_id: str) -> bool:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    candidates: list[str] = []
    for channel_id in get_channel_id_candidates():
        candidates.extend(
            [
                f"{MAX_API_BASE_URL}/chats/{channel_id}/members/{user_id}",
                f"{MAX_API_BASE_URL}/chats/{channel_id}/members",
                f"{MAX_API_BASE_URL}/chats/{channel_id}/subscribers/{user_id}",
                f"{MAX_API_BASE_URL}/chats/{channel_id}/subscribers",
                f"{MAX_API_BASE_URL}/channels/{channel_id}/members/{user_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_id}/members",
                f"{MAX_API_BASE_URL}/channels/{channel_id}/subscribers/{user_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_id}/subscribers",
            ]
        )
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}

    for url in candidates:
        try:
            params = {"user_id": user_id} if url.endswith("/members") or url.endswith("/subscribers") else None
            resp = requests.get(url, params=params, headers=headers, timeout=MAX_TIMEOUT_SECONDS)
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                logger.info("Subscription check endpoint %s returned status=%s", url, resp.status_code)
                continue

            # Для endpoint вида /members/{user_id} или /subscribers/{user_id} успешный 200 обычно уже означает,
            # что пользователь найден среди участников.
            if not (url.endswith("/members") or url.endswith("/subscribers")):
                return True

            payload = resp.json() if resp.content else {}
            if payload and is_subscription_confirmed(payload, user_id):
                return True
        except Exception:
            continue
    return False


def contains_user_id(value: Any, user_id: str) -> bool:
    target = str(user_id)

    if isinstance(value, dict):
        direct_candidate = value.get("user_id")
        if direct_candidate is not None and str(direct_candidate) == target:
            return True
        direct_id = value.get("id")
        if direct_id is not None and str(direct_id) == target:
            return True

        user_obj = value.get("user")
        if user_obj is not None and contains_user_id(user_obj, user_id):
            return True

        for nested in value.values():
            if contains_user_id(nested, user_id):
                return True
        return False

    if isinstance(value, list):
        for item in value:
            if contains_user_id(item, user_id):
                return True
        return False

    return False


def is_subscription_confirmed(payload: Any, user_id: str) -> bool:
    if contains_user_id(payload, user_id):
        return True

    if isinstance(payload, dict):
        for flag_key in ("subscribed", "is_subscribed", "is_member", "member", "joined", "in_chat"):
            flag = payload.get(flag_key)
            if isinstance(flag, bool) and flag:
                return True
            if isinstance(flag, str) and flag.lower() in {"true", "yes", "member", "joined", "subscribed"}:
                return True
        for status_key in ("status", "membership", "role"):
            status_val = payload.get(status_key)
            if isinstance(status_val, str) and status_val.lower() in {"member", "subscriber", "joined", "admin", "owner"}:
                return True
    return False


def get_channel_title() -> str:
    if not MAX_BOT_TOKEN:
        return f"chat_id {MAX_CHANNEL_CHAT_ID}"

    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    candidates: list[str] = []
    for channel_id in get_channel_id_candidates():
        candidates.extend(
            [
                f"{MAX_API_BASE_URL}/chats/{channel_id}",
                f"{MAX_API_BASE_URL}/channels/{channel_id}",
            ]
        )
    for url in candidates:
        try:
            resp = requests.get(url, headers=headers, timeout=MAX_TIMEOUT_SECONDS)
            if resp.status_code >= 400:
                continue
            payload = resp.json() if resp.content else {}
            if isinstance(payload, dict):
                title = payload.get("title") or payload.get("name") or payload.get("chat_title")
                if isinstance(title, str) and title.strip():
                    return title.strip()
        except Exception:
            continue
    return f"chat_id {MAX_CHANNEL_CHAT_ID}"


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

    def _register_with_types(update_types: list[str]) -> requests.Response:
        payload: dict[str, Any] = {"url": webhook_url, "update_types": update_types}
        if MAX_WEBHOOK_SECRET:
            payload["secret"] = MAX_WEBHOOK_SECRET
        return requests.post(
            f"{MAX_API_BASE_URL}/subscriptions",
            headers={"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"},
            json=payload,
            timeout=MAX_TIMEOUT_SECONDS,
        )

    effective_update_types = get_effective_update_types()
    response = _register_with_types(effective_update_types)
    if response.status_code < 400:
        ACTIVE_WEBHOOK_UPDATE_TYPES.clear()
        ACTIVE_WEBHOOK_UPDATE_TYPES.extend(effective_update_types)
        return response.json() if response.content else {"ok": True}

    logger.error("MAX /subscriptions register failed %s: %s", response.status_code, response.text)
    raise HTTPException(
        status_code=502,
        detail=f"Не удалось зарегистрировать webhook в MAX: status={response.status_code}",
    )


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


def get_effective_update_types() -> list[str]:
    return BASE_WEBHOOK_UPDATE_TYPES


def auto_register_webhook_on_startup() -> None:
    effective_webhook_url = get_effective_webhook_url()
    logger.info(
        "Startup config: token_set=%s webhook_url=%s effective_webhook_url=%s auto_register=%s update_types=%s secret_set=%s self_check=%s",
        bool(MAX_BOT_TOKEN),
        MAX_WEBHOOK_URL or "<empty>",
        effective_webhook_url or "<empty>",
        MAX_WEBHOOK_AUTO_REGISTER,
        ",".join(get_effective_update_types()) or "<empty>",
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    auto_register_webhook_on_startup()
    yield


app.router.lifespan_context = lifespan


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
        if message_text in {"test", "тест", "/test", "/hello", "/start", "+"}:
            send_miniapp_entry(user_id=user_id, chat_id=chat_id)
            return
        if message_text in {"купон", "/купон", "coupon", "/coupon"}:
            send_miniapp_entry(user_id=user_id, chat_id=chat_id)
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


def render_miniapp_html() -> str:
    return f"""
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Купон MAX ID Bot</title>
    <script src="https://st.max.ru/js/max-web-app.js"></script>
    <style>
      body {{
        margin: 0;
        font-family: Inter, system-ui, sans-serif;
        background: linear-gradient(180deg, #f4f8ff 0%, #eef7ff 100%);
        color: #1f2937;
      }}
      .wrap {{
        max-width: 520px;
        margin: 0 auto;
        padding: 20px 14px 28px;
      }}
      .card {{
        background: #fff;
        border-radius: 18px;
        box-shadow: 0 8px 24px rgba(29, 78, 216, 0.1);
        padding: 18px;
      }}
      h2 {{
        margin: 0 0 10px;
        font-size: 22px;
      }}
      p {{
        margin: 0 0 12px;
        line-height: 1.45;
      }}
      input {{
        width: 100%;
        box-sizing: border-box;
        border: 1px solid #dbe4ff;
        border-radius: 12px;
        padding: 12px;
        font-size: 16px;
      }}
      .row {{
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        margin-top: 12px;
      }}
      button, .btn-link {{
        border: 0;
        border-radius: 12px;
        padding: 11px 14px;
        font-size: 15px;
        cursor: pointer;
        text-decoration: none;
        display: inline-flex;
        align-items: center;
        justify-content: center;
      }}
      .btn-primary {{
        background: #2563eb;
        color: #fff;
      }}
      .btn-secondary {{
        background: #eef2ff;
        color: #1e40af;
      }}
      .btn-disabled {{
        background: #e5e7eb;
        color: #9ca3af;
        cursor: not-allowed;
      }}
      .status {{
        margin-top: 12px;
        padding: 10px;
        border-radius: 10px;
        background: #f9fafb;
        font-size: 14px;
      }}
      .uid {{
        font-size: 14px;
        color: #475569;
        margin-bottom: 8px;
      }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h2>🎁 Купон на скидку</h2>
        <p>Проверьте подписку и получите купон.</p>
        <div id="uidLabel" class="uid">user_id: определяем...</div>
        <div class="row">
          <button id="checkBtn" class="btn-secondary">Проверить подписку</button>
          <button id="showCouponBtn" class="btn-disabled" disabled>Показать купон</button>
          <a id="subscribeBtn" href="{MAX_CHANNEL_DEEPLINK}" data-web-url="{MAX_CHANNEL_URL}" class="btn-link btn-primary" style="display:none;">Подписаться на канал</a>
        </div>
        <div id="status" class="status">Статус: ожидаем проверку подписки.</div>
      </div>
    </div>

    <script>
      const checkBtn = document.getElementById('checkBtn');
      const showCouponBtn = document.getElementById('showCouponBtn');
      const subscribeBtn = document.getElementById('subscribeBtn');
      const statusEl = document.getElementById('status');
      const uidLabel = document.getElementById('uidLabel');
      if (window.WebApp?.ready) {{
        window.WebApp.ready();
      }}
      const getFromInitData = () => {{
        try {{
          const initData = new URLSearchParams(window.location.search).get('initData');
          if (!initData) return '';
          const params = new URLSearchParams(initData);
          const userRaw = params.get('user');
          if (!userRaw) return '';
          const userObj = JSON.parse(userRaw);
          return (userObj.user_id || userObj.id || '').toString();
        }} catch (_e) {{
          return '';
        }}
      }};
      const getDetectedUserId = () => {{
        return (
          window.WebApp?.initDataUnsafe?.user?.user_id ||
          window.WebApp?.initDataUnsafe?.user?.id ||
          new URLSearchParams(window.location.search).get('user_id') ||
          getFromInitData() ||
          ''
        ).toString();
      }};
      const detectedUserId = getDetectedUserId();
      uidLabel.textContent = detectedUserId
        ? `user_id: ${{detectedUserId}}`
        : 'user_id: не определён (откройте miniapp кнопкой из чата с ботом)';

      const setCouponEnabled = (enabled) => {{
        showCouponBtn.disabled = !enabled;
        showCouponBtn.className = enabled ? 'btn-primary' : 'btn-disabled';
      }};

      subscribeBtn.onclick = (e) => {{
        e.preventDefault();
        const deepLink = subscribeBtn.getAttribute('href') || '{MAX_CHANNEL_DEEPLINK}';
        const webUrl = subscribeBtn.getAttribute('data-web-url') || '{MAX_CHANNEL_URL}';
        const fallbackToWeb = () => {{
          window.open(webUrl, '_blank', 'noopener,noreferrer');
        }};
        try {{
          // 1) Пробуем нативный метод MAX WebApp (если доступен).
          if (window.WebApp?.openLink) {{
            window.WebApp.openLink(deepLink);
            setTimeout(fallbackToWeb, 1200);
            return;
          }}
        }} catch (_e) {{
          // Переходим к следующей попытке
        }}

        try {{
          // 2) Фолбэк: прямой переход по deep-link.
          window.location.assign(deepLink);
          setTimeout(fallbackToWeb, 1200);
          return;
        }} catch (_e) {{
          // Финальный fallback ниже
        }}

        // 3) Если deep-link не сработал, открываем web-ссылку.
        fallbackToWeb();
      }};

      checkBtn.onclick = async () => {{
        if (!detectedUserId) {{
          statusEl.textContent = 'Не удалось определить user_id. Откройте миниприложение из чата MAX.';
          return;
        }}
        statusEl.textContent = 'Проверяем подписку...';
        const res = await fetch(`/miniapp/status?user_id=${{encodeURIComponent(detectedUserId)}}`);
        const data = await res.json();
        if (data.subscribed) {{
          setCouponEnabled(true);
          subscribeBtn.style.display = 'none';
          statusEl.textContent = `${{data.message}} ✅ Нажмите «Показать купон».`;
        }} else {{
          setCouponEnabled(false);
          subscribeBtn.style.display = 'inline-flex';
          statusEl.textContent = `${{data.message}}`;
        }}
      }};

      showCouponBtn.onclick = async () => {{
        if (!detectedUserId) {{
          statusEl.textContent = 'Не удалось определить user_id. Откройте миниприложение из чата MAX.';
          return;
        }}
        statusEl.textContent = 'Отправляем купон...';
        const res = await fetch('/miniapp/get-coupon', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ user_id: detectedUserId }})
        }});
        const data = await res.json();
        if (res.ok && data.ok) {{
          statusEl.textContent = 'Купон отправлен в чат с ботом ✅';
        }} else {{
          statusEl.textContent = 'Не удалось отправить купон. Проверьте подписку и попробуйте снова.';
        }}
      }};
    </script>
  </body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def root() -> str:
    return render_miniapp_html()


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
                "webhook_update_types": get_effective_update_types(),
                "active_webhook_update_types": ACTIVE_WEBHOOK_UPDATE_TYPES,
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


@app.get("/miniapp", response_class=HTMLResponse)
def miniapp_page() -> str:
    return render_miniapp_html()


@app.get("/miniapp/status")
def miniapp_status(user_id: str) -> JSONResponse:
    subscribed = is_user_subscribed_to_channel(user_id=user_id)
    channel_title = get_channel_title()
    message = (
        f'Вы подписаны на канал "{channel_title}"'
        if subscribed
        else f'Подписка на канал "{channel_title}" не найдена'
    )
    if not subscribed:
        logger.info("Subscription check is false for user_id=%s channel_chat_id=%s", user_id, MAX_CHANNEL_CHAT_ID)
    return JSONResponse(
        {
            "ok": True,
            "user_id": user_id,
            "subscribed": subscribed,
            "channel_chat_id": MAX_CHANNEL_CHAT_ID,
            "channel_title": channel_title,
            "message": message,
        }
    )


@app.post("/miniapp/get-coupon")
async def miniapp_get_coupon(request: Request) -> JSONResponse:
    body = await request.json()
    user_id = str(body.get("user_id") or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id обязателен")
    if not is_user_subscribed_to_channel(user_id=user_id):
        return JSONResponse({"ok": False, "reason": "not_subscribed", "channel_chat_id": MAX_CHANNEL_CHAT_ID}, status_code=403)
    send_coupon(user_id=user_id, chat_id=None)
    return JSONResponse({"ok": True, "sent": True})


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
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
            "uvicorn.access": {"handlers": ["default"], "level": log_level.upper(), "propagate": False},
        },
    }
    uvicorn.run(app, host="0.0.0.0", port=port, log_level=log_level, log_config=log_config)


if __name__ == "__main__":
    run()
