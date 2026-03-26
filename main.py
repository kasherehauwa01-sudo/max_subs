import json
import logging
import os
from typing import Any, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("max-id-bot")

MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api.max.ru")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN")
MAX_TIMEOUT_SECONDS = float(os.getenv("MAX_TIMEOUT_SECONDS", "10"))

app = FastAPI(title="MAX ID Bot", version="1.0.0")


def _extract_by_paths(payload: dict[str, Any], paths: list[str]) -> Optional[Any]:
    """Возвращает первое непустое значение из списка путей вида a.b.c."""
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
    """Пытаемся достать наиболее уникальный идентификатор пользователя из события."""
    candidate_paths = [
        "message.sender.user_id",  # основной вариант из объекта Message -> sender(User)
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
    """Извлекаем chat_id, если событие пришло из группового/диалогового чата."""
    candidate_paths = [
        "message.recipient.chat_id",
        "message.chat_id",
        "chat.chat_id",
        "chat_id",
    ]
    chat_id = _extract_by_paths(payload, candidate_paths)
    return str(chat_id) if chat_id is not None else None


def extract_message_text(payload: dict[str, Any]) -> Optional[str]:
    """Пробуем извлечь текст сообщения из разных вариантов структуры body."""
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


def send_max_message(text: str, user_id: Optional[str] = None, chat_id: Optional[str] = None) -> dict[str, Any]:
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    if not user_id and not chat_id:
        raise ValueError("Нужен user_id или chat_id для отправки сообщения")

    url = f"{MAX_API_BASE_URL}/messages"
    params: dict[str, str] = {}
    if user_id:
        params["user_id"] = user_id
    elif chat_id:
        params["chat_id"] = chat_id

    headers = {
        "Authorization": MAX_BOT_TOKEN,
        "Content-Type": "application/json",
    }

    response = requests.post(
        url,
        params=params,
        headers=headers,
        json={"text": text},
        timeout=MAX_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        logger.error("MAX API error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Ошибка отправки сообщения через MAX API")

    return response.json() if response.content else {"ok": True}


def check_max_auth() -> dict[str, Any]:
    """Проверяет, что токен валиден и MAX API доступен (GET /me)."""
    if not MAX_BOT_TOKEN:
        raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения")

    url = f"{MAX_API_BASE_URL}/me"
    response = requests.get(
        url,
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


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "max-id-bot",
        "status": "ok",
        "hint": "Use POST /webhook for MAX events",
    }


@app.get("/webhook")
def webhook_get_hint() -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "message": "Webhook endpoint is alive. Send POST requests from MAX to /webhook.",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/max")
def health_max() -> JSONResponse:
    me = check_max_auth()
    return JSONResponse({"status": "ok", "max_auth": True, "me": me})


@app.post("/webhook")
async def webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        logger.exception("Некорректный JSON в webhook: %s", exc)
        raise HTTPException(status_code=400, detail="Некорректный JSON") from exc

    logger.info("Incoming MAX event: %s", json.dumps(payload, ensure_ascii=False))

    user_id = extract_user_id(payload)
    chat_id = extract_chat_id(payload)
    message_text = (extract_message_text(payload) or "").strip().lower()

    # Если пользователь написал "test" или "тест", отвечаем специальным сообщением.
    if message_text in {"test", "тест"}:
        send_result = send_max_message(text="ПРИВЕТ", user_id=user_id, chat_id=chat_id)
        return JSONResponse({"ok": True, "reply": "ПРИВЕТ", "send_result": send_result})

    if not user_id:
        logger.warning("Не удалось извлечь user_id из события")
        fallback_text = (
            "Не удалось определить ваш ID из этого события. "
            "Пожалуйста, отправьте сообщение боту в личный диалог."
        )

        if chat_id:
            send_max_message(text=fallback_text, chat_id=chat_id)
            return JSONResponse({"ok": True, "warning": "user_id_not_found"})

        raise HTTPException(status_code=422, detail="user_id не найден в webhook payload")

    reply_text = f"Ваш ID: {user_id}"
    send_result = send_max_message(text=reply_text, user_id=user_id)

    return JSONResponse({"ok": True, "user_id": user_id, "send_result": send_result})
