# MAX ID Bot (Webhook + FastAPI)

Бот для мессенджера MAX, который получает входящее событие по webhook и отправляет пользователю его технический идентификатор.

## Что делает бот

1. Получает POST-событие от MAX на `POST /webhook`.
2. Извлекает идентификатор пользователя из полей события (в приоритете `message.sender.user_id`).
3. Отправляет ответ через MAX API методом `POST /messages`:
   - если пользователь написал `test`, `тест`, `/test`, `/hello`, `/start`, `+` или `КУПОН` → бот отправляет кнопку-ссылку для открытия миниприложения
   - если пользователь написал `id`, `айди` или `/id` → `Ваш ID: {user_id}`
   - иначе → `Ваш ID: {user_id}`

Если прямого `user_id` нет, бот пытается взять ID из альтернативных полей (`sender.id`, `user.user_id`, `profile.id` и т.д.).

### Логика купонов (EAN13)

- 1–10 число месяца: `7123100000145` (действует до 10 числа).
- 11–20 число месяца: `7123100000152` (действует до 20 числа).
- С 21 числа до конца месяца: `7123100000169` (действует до последнего дня месяца).

В миниприложении:
1. `user_id` определяется автоматически из MAX WebApp (`window.WebApp.initDataUnsafe`) и fallback из `initData`/`user_id` в URL;
2. бот проверяет подписку на канал `chat_id=-72559954357735`;
3. при проверке подписки miniapp показывает сообщение вида `Вы подписаны на канал "Название канала"` или `Подписка на канал "Название канала" не найдена`;
4. если подписка есть — активна кнопка «Показать купон», и при нажатии бот отправляет купон (EAN13 + текст);
5. если подписки нет — показывается кнопка «Подписаться на канал».
   Между изображением и текстом добавляется пустая строка для читаемости.
Проверка подписки учитывает разные форматы ответа MAX API (вложенные `user_id/id`, списки `items/members/data`, флаги `is_subscribed/is_member` и endpoint-ы `members/subscribers`).
Дополнительно бот пробует оба варианта `chat_id` для API-запросов: со знаком (`-...`) и без знака (`...`), чтобы корректно обрабатывать различия между web-ссылкой канала и API endpoint-ами.

> Важно: miniapp нужно открывать кнопкой из чата с ботом. При прямом открытии URL браузером `user_id` может не определиться.
> Кнопка «Получить купон» от бота отправляется как `open_app` (а не `link`) c обязательным полем `web_app`, чтобы miniapp открывался внутри MAX с корректным WebApp-контекстом.

Если отправка изображения не удалась, бот отправляет текст купона и предупреждение (без цифрового кода).

Если MAX API вернёт нестандартный формат ответа после загрузки файла, бот попробует извлечь `token` из нескольких возможных полей и вложенных структур.
Кнопка «Подписаться на канал» в miniapp сначала пробует открыть ссылку `MAX_CHANNEL_DEEPLINK` (по умолчанию `https://max.ru/id344309962847_biz`) двумя способами: через `WebApp.openLink(...)` и через `window.location.assign(...)`. Если не сработало, используется fallback на `MAX_CHANNEL_URL` (по умолчанию `https://web.max.ru/-72559954357735`).

Endpoint'ы миниприложения:
- `GET /miniapp` — HTML-интерфейс с кнопками.
- `GET /miniapp/status?user_id=...` — проверка подписки.
- `POST /miniapp/get-coupon` — отправка купона (только если пользователь подписан).
- `GET /` — тот же интерфейс miniapp (резервный путь, если клиент открыл корень домена).

Текст купона:

```text
Спасибо, что подписались 💛
Дарим вам дополнительную скидку 5%.
🛍 Покажите штрихкод на кассе и покупайте с выгодой

⏳ Купон действует до дд.мм.гггг
_⚠ Скидка действует только на товары с белыми ценниками_
```

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

> Для Railway используется `python main.py` (см. `Procfile`), чтобы логи Uvicorn шли в stdout.

Проверка helper-логики (unit-тесты):

```bash
python -m unittest tests/test_main.py -v
```

Проверка здоровья:

```bash
curl http://localhost:8000/health
```

Проверка авторизации в MAX API (проверка токена через `GET /me`):

```bash
curl http://localhost:8000/health/max
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



### Что указывать в `MAX_API_BASE_URL`

Указывайте **базовый URL API MAX без завершающего `/`**.

Обычно это:

```env
MAX_API_BASE_URL=https://platform-api.max.ru
```

То есть:
- `https://platform-api.max.ru` ✅
- `https://platform-api.max.ru/` ⚠️ (лучше без `/` в конце)
- `https://dev.max.ru` ❌ (это портал документации, не API host)

Если вы не зададите переменную, в коде уже используется значение по умолчанию: `https://platform-api.max.ru`.

## Настройка webhook в MAX


> Требования MAX к webhook: публичный HTTPS URL (обычно 443), валидный TLS-сертификат от доверенного CA, и ответ `HTTP 200` не дольше чем за ~30 секунд.


1. Задеплойте приложение (например, Railway) и получите публичный URL.
2. В настройках вашего бота MAX укажите webhook:
   - `https://your-domain.com/webhook`
3. Убедитесь, что переменная `MAX_BOT_TOKEN` задана в окружении.

---

## Деплой на Railway

- Проект уже содержит `Procfile`:
  - `web: python main.py`
- На Railway добавьте переменные:
  - `MAX_BOT_TOKEN`
  - `MAX_API_BASE_URL` (обычно `https://platform-api.max.ru`)
  - `MAX_TIMEOUT_SECONDS` (например, `10`)
  - `MAX_API_MAX_RETRIES` (например, `5`, ретраи на 429/503/сеть)
  - `MAX_WEBHOOK_SECRET` (если указываете `secret` при создании подписки)
  - `MAX_DEDUP_TTL_SECONDS` (например, `3600`, TTL dedup ключей)
  - `MAX_WEBHOOK_UPDATE_TYPES` (например, `message_created,bot_started,message_callback`)
  - `MAX_CHANNEL_CHAT_ID` (по умолчанию `-72559954357735`, канал для проверки подписки)
  - `MAX_CHANNEL_DEEPLINK` (ссылка канала для открытия внутри приложения MAX, например `https://max.ru/id344309962847_biz`)
  - `MAX_CHANNEL_URL` (fallback web-ссылка на канал, если deep-link не сработал)
  - `MAX_WEB_APP` (опционально, username бота для поля `web_app` в кнопке `open_app`; если пусто — отправляется fallback `link` на `/miniapp`)
  - `PUBLIC_BASE_URL` (публичный URL сервиса для миниприложения)
  - `MAX_STARTUP_SELF_CHECK` (`true/false`, проверка `GET /me` при старте)
  - `LOG_LEVEL` (`INFO`)
  - `MAX_WEBHOOK_AUTO_REGISTER` (рекомендуется `true`, по умолчанию в коде теперь `true`)

`MAX_WEBHOOK_URL` можно не задавать, если в Railway включён Public Domain: тогда приложение автоматически соберёт URL как `https://$RAILWAY_PUBLIC_DOMAIN/webhook`.

---


### Обязательные настройки в Railway (чеклист)

Чтобы в логах Railway начали появляться webhook-события от MAX, проверьте:

1. **Публичный HTTPS-домен включён**
   - В Railway должен быть выдан публичный домен вида `https://<app>.up.railway.app`.
   - Бот MAX не сможет слать события на приватный/internal URL.

2. **Переменные окружения заданы**
   - `MAX_BOT_TOKEN=<токен из MAX>`
   - `MAX_API_BASE_URL=https://platform-api.max.ru`
   - `MAX_TIMEOUT_SECONDS=10`
   - `LOG_LEVEL=INFO`
   - `MAX_WEBHOOK_SECRET=<секрет>` (если использовали `secret` в `POST /subscriptions`)

3. **Команда запуска корректная**
   - Используется `Procfile`: `web: python main.py`.
   - Railway сам передаёт `PORT`, менять вручную обычно не нужно.

4. **Проверка доступности из интернета**
   - `GET https://<домен>/health` должен вернуть `200`.
   - `GET https://<домен>/health/config` должен вернуть активные настройки (без токена).
   - `GET https://<домен>/webhook` должен вернуть `200` (подсказка endpoint жив).
   - `POST` от MAX должен приходить на `https://<домен>/webhook`.

5. **Webhook в MAX указывает именно на Railway-домен**
   - В подписке должен быть URL `https://<домен>/webhook` (ровно этот путь).

6. **Если задан secret в подписке, он 1-в-1 совпадает с Railway переменной**
   - `secret` из `POST /subscriptions` == `MAX_WEBHOOK_SECRET`.
   - Любое расхождение → сервер вернёт `401`, событие не будет обработано.


### Что уже сделано для production-устойчивости

- **Fast ACK webhook**: endpoint `/webhook` быстро возвращает `200`, бизнес-логика выполняется в фоне.
- **Проверка webhook secret**: при заданном `MAX_WEBHOOK_SECRET` проверяется заголовок `X-Max-Bot-Api-Secret`.
- **Retry/backoff**: отправка сообщений в MAX API повторяется при `429` и `503` (а также сетевых сбоях) с экспоненциальной паузой и jitter.
- **Dedup входящих событий**: повторно доставленные webhook события отфильтровываются по ключам `(update_type, mid)` или `(update_type, callback_id)`.
- **Startup-диагностика**: на старте пишется сводка по env (`token_set`, `webhook_url`, `auto_register`, `update_types`), а при `MAX_STARTUP_SELF_CHECK=true` выполняется `GET /me` и результат пишется в лог.

### Быстрая диагностика, если в логах только Startup

Если в Railway видно только строки запуска (`Application startup complete`, `Uvicorn running...`) и нет `POST /webhook`, то обычно webhook ещё не доставляется из MAX. Проверьте:

1. `MAX_WEBHOOK_AUTO_REGISTER=true` (или вручную вызовите `POST /setup/subscription`).
2. `MAX_WEBHOOK_URL=https://<ваш-домен>/webhook`.
3. `MAX_WEBHOOK_UPDATE_TYPES` содержит `message_created`.
4. `GET /health/config` показывает ожидаемые значения.

Пример из логов:
- В startup-логе ориентируйтесь на `webhook_url_source` и `effective_webhook_url`:
  - `webhook_url_source=MAX_WEBHOOK_URL` — URL взят явно из переменной `MAX_WEBHOOK_URL`;
  - `webhook_url_source=RAILWAY_PUBLIC_DOMAIN` — URL собран автоматически как `https://$RAILWAY_PUBLIC_DOMAIN/webhook`;
  - `webhook_url_source=not_set` — webhook URL действительно не определён.

## Обработка ошибок

- Если webhook пришёл с невалидным JSON → `400`.
- Если `user_id` не найден:
  - при наличии `chat_id` бот отправит текст с объяснением;
  - без `chat_id` вернёт `422`.
- Ошибки MAX API логируются, наружу отдаётся `502`.

---


---


## Как проверить регистрацию webhook на стороне MAX API


### Базовая проверка токена через MAX API (`/me`)

Вы можете проверить токен напрямую (как вы и написали):

```bash
curl -X GET "https://platform-api.max.ru/me"   -H "Authorization: <MAX_BOT_TOKEN>"
```

Если ответ `200`, токен валиден и API доступно.


> Важно: проверка делается на `platform-api.max.ru`, а не на вашем домене Railway.

### 1) Посмотреть текущие подписки

```bash
curl -sS -X GET "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json"
```

Что должно быть в ответе:
- есть запись с `url` = `https://<ваш-домен>/webhook`
- в `update_types` присутствует минимум `message_created` (и при необходимости `bot_started`)

### 2) Пересоздать подписку (если нет нужной или URL неверный)

```bash
curl -sS -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<ваш-домен>/webhook",
    "update_types": ["message_created", "bot_started"]
  }'
```

### 2.1) Готовая команда именно под ваш Railway URL

Ниже команда с вашим endpoint `https://maxsubs-production.up.railway.app/webhook`.

```bash
curl -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://maxsubs-production.up.railway.app/webhook"
  }'
```

> Важно: не вставляйте реальный токен в README/скриншоты/чат с третьими лицами. Если токен уже утёк, его нужно перевыпустить в MAX.

После этого снова выполните `GET /subscriptions` и убедитесь, что подписка появилась.


### Регистрация подписки теперь есть и в коде бота

Чтобы не делать `curl` вручную, в приложении добавлены:

- `POST /setup/subscription` — сервер сам вызывает `POST https://platform-api.max.ru/subscriptions`.
- `GET /subscribe` и `POST /subscribe` — удобные алиасы для ручной регистрации (чтобы не получать `404` при проверке `/subscribe`).
- опциональная автоподписка на старте приложения через `MAX_WEBHOOK_AUTO_REGISTER=true`.

Нужные переменные окружения:

```env
MAX_WEBHOOK_URL=https://maxsubs-production.up.railway.app/webhook
MAX_WEBHOOK_AUTO_REGISTER=true
```

Ручной вызов серверного endpoint:

```bash
curl -X POST "https://maxsubs-production.up.railway.app/setup/subscription"
```

или (алиас, который можно открыть даже из браузера):

```bash
curl "https://maxsubs-production.up.railway.app/subscribe"
```

> Endpoint использует `MAX_BOT_TOKEN` из окружения и не требует передавать токен в URL/теле запроса.

### 3) Проверить, что MAX реально шлёт события

- Напишите боту сообщение в MAX.
- Откройте логи Railway: должен появиться `POST /webhook`.
- Если в логах только `GET /webhook` или вообще нет обращений, MAX не доставляет webhook на ваш URL.


### Рекомендуемая защита webhook через `secret`

При создании подписки можно передать поле `secret`, тогда MAX присылает заголовок `X-Max-Bot-Api-Secret`.
В этом проекте проверка включается автоматически, если задана переменная `MAX_WEBHOOK_SECRET`.

Пример создания подписки с секретом:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions" \
  -H "Authorization: <MAX_BOT_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://<ваш-домен>/webhook",
    "update_types": ["message_created", "bot_started"],
    "secret": "your_webhook_secret"
  }'
```

И в Railway нужно задать такую же переменную:

```env
MAX_WEBHOOK_SECRET=your_webhook_secret
```

## Шаблон запроса в техподдержку MAX

Ниже готовый текст, который можно отправить почти без изменений:

```text
Тема: Не приходят webhook-события на URL бота MAX

Здравствуйте.

Бот: <имя_бота или bot_id>
Дата/время проблемы (UTC): <YYYY-MM-DD HH:MM>
Webhook URL: https://<ваш-домен>/webhook

Описание:
- Сервис доступен извне, /health возвращает 200.
- В MAX API подписка создана и видна через GET /subscriptions.
- В подписке указан URL: https://<ваш-домен>/webhook
- update_types: ["message_created", "bot_started"]
- При отправке сообщений боту POST-запросы на /webhook не приходят (по логам Railway).

Что уже проверили:
1) Токен валиден (сообщения через POST /messages отправляются успешно / или код ошибки: <код>).
2) SSL-сертификат домена валиден.
3) Повторно создавали подписку через POST /subscriptions.

Просьба:
Проверьте, пожалуйста, доставку webhook-событий для этого бота и сообщите, есть ли ошибки маршрутизации/доставки на стороне MAX.

Дополнительно можем предоставить:
- response от GET /subscriptions,
- фрагменты логов Railway за период <время>,
- correlation/request id (если есть).
```

## Как проверить, что бот реально связан с MAX (чеклист)

Если бот «молчит» после деплоя на Railway, проверьте по шагам:

1. **Проверка сервиса Railway**
   - Откройте `https://<ваш-домен>/health` — должен вернуться JSON `{"status":"ok"}`.
   - В логах Railway должны быть входящие запросы на `/webhook` после вашего сообщения боту.

2. **Проверка, что webhook действительно подписан в MAX**

```bash
curl -X GET "https://platform-api.max.ru/subscriptions"   -H "Authorization: <MAX_BOT_TOKEN>"
```

В ответе должен быть ваш URL `https://<ваш-домен>/webhook`.

Если подписки нет — создайте её:

```bash
curl -X POST "https://platform-api.max.ru/subscriptions"   -H "Authorization: <MAX_BOT_TOKEN>"   -H "Content-Type: application/json"   -d '{
    "url": "https://<ваш-домен>/webhook",
    "update_types": ["message_created", "bot_started"]
  }'
```

3. **Проверка, что токен рабочий (бот может отправлять сообщения)**

```bash
curl -X POST "https://platform-api.max.ru/messages?user_id=<YOUR_USER_ID>"   -H "Authorization: <MAX_BOT_TOKEN>"   -H "Content-Type: application/json"   -d '{"text":"Проверка отправки из Railway"}'
```

Если этот запрос не проходит, проблема в токене/правах/ID, а не в webhook.

4. **Проверка входящих событий без webhook (диагностика)**

```bash
curl -X GET "https://platform-api.max.ru/updates"   -H "Authorization: <MAX_BOT_TOKEN>"
```

Если здесь приходят события, но на `/webhook` ничего нет — проблема в подписке webhook (URL, SSL, доступность).


### Что означают типовые логи Railway

- `GET /` → `404` (раньше) или `200` (после добавления root endpoint) — это просто проверка браузером.
- `GET /webhook` → `405 Method Not Allowed` означает, что endpoint живой, но ждёт **POST** (это нормально для webhook).
- `GET /subscriptions` → `404` на вашем сервере — тоже нормально, потому что `/subscriptions` это метод **MAX API**, а не вашего FastAPI-приложения.

Проверять подписки нужно запросом на `https://platform-api.max.ru/subscriptions`, а не на ваш домен Railway.

### Частые причины, почему бот молчит

- Не создана подписка `POST /subscriptions`.
- Подписка создана, но без типа `message_created`.
- Неверный `MAX_BOT_TOKEN` в Railway Variables.
- Webhook URL недоступен извне или указан не тот путь (нужно ровно `/webhook`).
- Вы пишете боту в чате, где у бота нет прав (для групп бот должен быть админом).

## Как это работает простыми словами

1. Пользователь пишет боту в MAX.
2. MAX шлёт webhook на ваш сервер.
3. Сервер достаёт ID пользователя из JSON.
4. Сервер сразу возвращает `200 OK` в webhook (чтобы уложиться в тайм-аут доставки), а обработку делает в фоне.
5. Сервер отправляет в MAX ответ:
   - 👉 `Ваш ID: 123456789`
