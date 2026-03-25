# MAX ID Bot (Webhook + FastAPI)

Бот для мессенджера MAX, который получает входящее событие по webhook и отправляет пользователю его технический идентификатор.

## Что делает бот

1. Получает POST-событие от MAX на `POST /webhook`.
2. Извлекает идентификатор пользователя из полей события (в приоритете `message.sender.user_id`).
3. Отправляет ответ через MAX API методом `POST /messages`:
   - `Ваш ID: {user_id}`

Если прямого `user_id` нет, бот пытается взять ID из альтернативных полей (`sender.id`, `user.user_id`, `profile.id` и т.д.).

---

## Почему так (по документации MAX)

- В объекте `User` основной идентификатор — `user_id`.
- Отправка сообщения выполняется через `POST /messages` с query-параметром `user_id` или `chat_id`.
- Токен должен передаваться в заголовке `Authorization`.

(Ссылки на документацию: [User](https://dev.max.ru/docs-api/objects/User), [Message](https://dev.max.ru/docs-api/objects/Message), [Отправить сообщение](https://dev.max.ru/docs-api/methods/POST/messages))

---

## Структура проекта

- `main.py` — приложение FastAPI, endpoint `/webhook`, извлечение ID, отправка ответа в MAX.
- `requirements.txt` — зависимости Python.
- `Procfile` — команда запуска для Railway.
- `.env.example` — пример переменных окружения.

---

## Установка и запуск локально

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Заполните MAX_BOT_TOKEN
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Проверка здоровья:

```bash
curl http://localhost:8000/health
```

---

## Пример входящего webhook JSON

> Реальный payload может отличаться в зависимости от типа события. Ниже — типичный пример для нового сообщения.

```json
{
  "update_type": "message_created",
  "timestamp": 1742899200000,
  "message": {
    "sender": {
      "user_id": 123456789,
      "first_name": "Ivan",
      "is_bot": false
    },
    "recipient": {
      "chat_id": 987654321
    },
    "body": {
      "text": "ID"
    }
  }
}
```

Из этого payload бот возьмёт:
- `user_id = 123456789`
- `chat_id = 987654321`

И отправит пользователю:
- `Ваш ID: 123456789`

---

## Как бот отправляет ответ через API MAX

Пример HTTP-запроса, который делает бот:

```bash
curl -X POST "https://platform-api.max.ru/messages?user_id=123456789" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"text":"Ваш ID: 123456789"}'
```

---

## Настройка webhook в MAX

1. Задеплойте приложение (например, Railway) и получите публичный URL.
2. В настройках вашего бота MAX укажите webhook:
   - `https://your-domain.com/webhook`
3. Убедитесь, что переменная `MAX_BOT_TOKEN` задана в окружении.

---

## Деплой на Railway

- Проект уже содержит `Procfile`:
  - `web: uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`
- На Railway добавьте переменные:
  - `MAX_BOT_TOKEN`
  - `MAX_API_BASE_URL` (обычно `https://platform-api.max.ru`)
  - `MAX_TIMEOUT_SECONDS` (например, `10`)
  - `LOG_LEVEL` (`INFO`)

---

## Обработка ошибок

- Если webhook пришёл с невалидным JSON → `400`.
- Если `user_id` не найден:
  - при наличии `chat_id` бот отправит текст с объяснением;
  - без `chat_id` вернёт `422`.
- Ошибки MAX API логируются, наружу отдаётся `502`.

---

## Как это работает простыми словами

1. Пользователь пишет боту в MAX.
2. MAX шлёт webhook на ваш сервер.
3. Сервер достаёт ID пользователя из JSON.
4. Сервер отправляет в MAX ответ:
   - 👉 `Ваш ID: 123456789`
