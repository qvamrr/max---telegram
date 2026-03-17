# MAX → Telegram Bot

Бот пересылает сообщения из мессенджера **MAX** в **Telegram**.

- Подключается к MAX **напрямую по WebSocket**, без вашего API.
- Пересылает **текст** и **фото** (альбомами до 10).
- Поддерживает **несколько чатов MAX**.
- Имеет **админ‑панель** в Telegram (polling `getUpdates`).
- Поддерживает **HTTP/SOCKS5 прокси** для Telegram (актуально на VDS).

---

## Что нужно заранее

- Python 3.10+ (локально/на VDS)
- Аккаунт MAX и вход в `web.max.ru`
- Telegram‑бот (создать через `@BotFather`)

---

## 1) Получить токен MAX (из браузера)

1. Откройте `web.max.ru` и войдите в аккаунт.
2. Откройте DevTools (F12).
3. Вкладка **Application** (Chrome) / **Storage** (Firefox).
4. **Local Storage** → `https://web.max.ru`.
5. Найдите ключ авторизации (часто `_oneme_auth`).
6. Скопируйте значение:
   - если это **JSON** — можно копировать **весь JSON** целиком (бот сам извлечёт нужный токен);
   - если это **строка токена** (часто начинается с `An_` или `eyJ...`) — копируйте строку.

Вставьте в `.env` в `MAX_API_TOKEN=...` или `MAX_TOKEN=...`.

---

## 2) Получить ID чата MAX

1. Откройте `web.max.ru`.
2. Перейдите в нужный чат.
3. В адресной строке увидите: `https://web.max.ru/ID_ЧАТА`
4. `ID_ЧАТА` и есть нужный ID (часто отрицательный).

---

## 3) Настроить `.env`

Создайте файл `.env` в этой папке и заполните.

Минимальный пример:

```env
MAX_API_TOKEN=ТОКЕН_ИЛИ_JSON_ИЗ_web.max.ru
MAX_CHAT_IDS=-111,-222

TELEGRAM_BOT_TOKEN=123456:ABCDEF...
TELEGRAM_CHAT_ID=-1001234567890
ADMIN_TELEGRAM_ID=123456789
POLL_INTERVAL=5
```

### Прокси для Telegram (если Telegram заблокирован на VDS)

Рекомендуется SOCKS5 через `socks5h://`:

```env
PROXY_URL=socks5h://IP:PORT
PROXY_USERNAME=user
PROXY_PASSWORD=pass
```

---

## 4) Установка и запуск (Windows / локально)

```powershell
cd "C:\Users\...\max_to_telegram_bot"
python -m pip install -r requirements.txt
python max_to_telegram.py
```

---

## 5) Установка и запуск на VDS (Ubuntu/Debian)

### 5.1 Установка зависимостей

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

### 5.2 Развернуть проект

Скопируйте папку на сервер, например в `/root/max_to_telegram_bot`.

### 5.3 Виртуальное окружение и pip

```bash
cd /root/max_to_telegram_bot
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

### 5.4 Проверочный запуск вручную

```bash
cd /root/max_to_telegram_bot
./.venv/bin/python max_to_telegram.py
```

Остановить: `Ctrl+C`.

---

## 6) Автозапуск на VDS (systemd)

### 6.1 Unit‑файл

```bash
sudo nano /etc/systemd/system/max-to-telegram.service
```

Вставьте (путь подставьте свой):

```ini
[Unit]
Description=MAX to Telegram forwarder
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/max_to_telegram_bot
EnvironmentFile=/root/max_to_telegram_bot/.env
ExecStart=/root/max_to_telegram_bot/.venv/bin/python /root/max_to_telegram_bot/max_to_telegram.py
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 6.2 Запуск сервиса

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now max-to-telegram.service
sudo systemctl restart max-to-telegram.service
```

### 6.3 Логи

```bash
sudo journalctl -u max-to-telegram.service -n 100 --no-pager
sudo journalctl -u max-to-telegram.service -f
```

---

## Админ‑панель в Telegram

Команды отправляйте **в личку боту** с аккаунта `ADMIN_TELEGRAM_ID`.

Основные команды:
- `/admin` или `/help` — список команд
- `/test` — тестовое сообщение в Telegram
- `/stats` — статистика
- `/list_chats` — список чатов MAX
- `/add_chat <id>` — добавить чат MAX
- `/remove_chat <id>` — удалить чат MAX
- `/set_chats <id1,id2,...>` — заменить список чатов MAX
- `/clear_chats` — очистить список чатов MAX
- `/pause` / `/resume` — пауза/возобновить пересылку
- `/only_text on|off` — пересылать только текст
- `/set_tg_chat <chat_id>` — изменить целевой Telegram чат
- `/where` — показать текущие настройки
- `/errors` — последние ошибки
- `/tail <n>` — последние события
- `/whoami` — показать ваш Telegram ID

Чаты MAX, добавленные через админку, сохраняются в `max_chat_ids.json`.

---

## Частые проблемы

### Бот не отвечает на команды `/admin`/`/test`
- На VDS Telegram может быть недоступен напрямую — включите прокси в `.env` (лучше `socks5h://...`).
- Если раньше ставили webhook — отключите:
  - `https://api.telegram.org/bot<ТОКЕН>/deleteWebhook?drop_pending_updates=true`

### Не пересылает из MAX
- Проверьте `MAX_CHAT_IDS` (ID чата из URL `web.max.ru/ID`).
- Посмотрите логи: в консоли/`journalctl` есть строки `MAX message: chat_id=...`.

---

## Безопасность

Не публикуйте токены (`MAX_*`, `TELEGRAM_BOT_TOKEN`) и данные прокси.

