#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MAX -> Telegram forwarder (WebSocket, как в maxtg-master).

Что делает:
  - Подключается к MAX по токену через библиотеку (max.py / MaxClient).
  - Слушает ВСЕ входящие сообщения в MAX.
  - Фильтрует только те, что из нужных чатов (MAX_CHAT_IDS).
  - Пересылает их в Telegram в формате: Имя + текст (+ вложения, если нужны).
  - Имеет простую админ-панель в Telegram (/admin, /stats, /set_interval, /test).

Важно:
  - Никакого собственного HTTP API для MAX не нужно.
  - Вся работа с MAX идёт через WebSocket внутри MaxClient (как в maxtg-master).
"""

import os
import time
import json
import sys
import threading
from typing import List
import html
from collections import deque


# Попробуем включить UTF-8 на stdout/stderr (Windows-консоль)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ============================================================
# Загрузка .env и конфиг
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE_PATH = os.environ.get("MAX_TG_CONFIG", os.path.join(BASE_DIR, ".env"))


def _load_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print(f"[WARN] Не удалось прочитать файл конфигурации {path}: {e}", flush=True)


_load_env_file(ENV_FILE_PATH)


# ВАЖНО: импорт модулей, которые читают PROXY_* при импорте,
# должен идти ПОСЛЕ загрузки .env.
import requests

from max import MaxClient as Client
from filters import filters
from classes import Message
from telegram import send_to_telegram as send_media_to_telegram  # из max_to_telegram_bot/telegram.py


# --- MAX ---
def _extract_max_token(raw: str) -> str:
    """
    MAX_TOKEN иногда лежит в Local Storage как JSON (например, значение ключа _oneme_auth).
    Поддерживаем варианты:
      - строка токена (An_... / eyJ...)
      - JSON со структурами token/auth_token/tokenAttrs.LOGIN.token
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    # Если это JSON — пытаемся вытащить правильное поле
    if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith('"') and raw.endswith('"')):
        try:
            data = json.loads(raw)
            if isinstance(data, str):
                return data.strip()
            if isinstance(data, dict):
                # наиболее вероятные варианты
                token = (
                    (((data.get("tokenAttrs") or {}).get("LOGIN") or {}).get("token"))
                    or data.get("auth_token")
                    or data.get("authToken")
                    or data.get("token")
                )
                return (token or "").strip()
        except Exception:
            pass

    return raw


MAX_TOKEN = _extract_max_token(os.environ.get("MAX_TOKEN") or os.environ.get("MAX_API_TOKEN") or "")
raw_chat_ids = os.environ.get("MAX_CHAT_IDS", "").strip()
single_chat_id = os.environ.get("MAX_CHAT_ID", "").strip()
MAX_CHAT_IDS: List[int] = []
try:
    if raw_chat_ids:
        MAX_CHAT_IDS = [int(x) for x in raw_chat_ids.split(",") if x.strip()]
    elif single_chat_id:
        MAX_CHAT_IDS = [int(single_chat_id)]
except ValueError:
    MAX_CHAT_IDS = []

# Файл для сохранения MAX_CHAT_IDS, чтобы можно было добавлять через админ-панель
MAX_CHAT_IDS_FILE = os.path.join(BASE_DIR, "max_chat_ids.json")
ADMIN_STATE_FILE = os.path.join(BASE_DIR, "admin_state.json")
BOT_VERSION = "1.0.0"


def load_max_chat_ids() -> List[int]:
    """
    Загружает список MAX chat_id из файла max_chat_ids.json.
    Формат: {"chat_ids":[1,2,3]}
    """
    try:
        if not os.path.exists(MAX_CHAT_IDS_FILE):
            return []
        with open(MAX_CHAT_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
        ids = data.get("chat_ids", [])
        if not isinstance(ids, list):
            return []
        out: List[int] = []
        for x in ids:
            try:
                out.append(int(x))
            except Exception:
                pass
        return sorted(list(set(out)))
    except Exception:
        return []


def save_max_chat_ids(chat_ids: List[int]) -> None:
    """Сохраняет список MAX chat_id в max_chat_ids.json (атомарно)."""
    try:
        data = {"chat_ids": sorted(list(set(int(x) for x in chat_ids)))}
        tmp = MAX_CHAT_IDS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, MAX_CHAT_IDS_FILE)
    except Exception as e:
        log(f"Не удалось сохранить {MAX_CHAT_IDS_FILE}: {e}")


# Если файл существует и не пустой — он имеет приоритет над переменными окружения
_file_ids = load_max_chat_ids()
if _file_ids:
    MAX_CHAT_IDS = _file_ids

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN") or ""
TELEGRAM_BOT_TOKEN = TELEGRAM_BOT_TOKEN.strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or ""
TELEGRAM_CHAT_ID = TELEGRAM_CHAT_ID.strip()

# --- Admin ---
try:
    POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5").strip())
except ValueError:
    POLL_INTERVAL = 5

try:
    ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0").strip())
except ValueError:
    ADMIN_TELEGRAM_ID = 0

try:
    MAX_SILENCE_RESTART_SECONDS = int(os.environ.get("MAX_SILENCE_RESTART_SECONDS", "900").strip())
except ValueError:
    MAX_SILENCE_RESTART_SECONDS = 900

# --- Proxy (Telegram only) ---
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXY_USERNAME = os.environ.get("PROXY_USERNAME", "").strip()
PROXY_PASSWORD = os.environ.get("PROXY_PASSWORD", "").strip()


# ============================================================
# Логирование и статистика
# ============================================================

STATS = {
    "forwarded_count": 0,
    "last_max_event_ts": None,
    "last_tg_http_status": None,
}


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)

def _append_event(runtime_state: dict, text: str) -> None:
    try:
        runtime_state["events"].append(f"[{_ts()}] {text}")
    except Exception:
        pass

def _append_error(runtime_state: dict, text: str) -> None:
    try:
        runtime_state["errors"].append(f"[{_ts()}] {text}")
    except Exception:
        pass

def load_admin_state() -> dict:
    """
    Загружает состояние админки (tg_chat_id, admins, muted, flags) из admin_state.json.
    Формат:
      {
        "telegram_chat_id": "-100...",
        "admins": [123, 456],
        "muted_chats": [-1, -2],
        "forwarding_enabled": true,
        "only_text": false,
        "loglevel": "info"
      }
    """
    try:
        if not os.path.exists(ADMIN_STATE_FILE):
            return {}
        with open(ADMIN_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.loads(f.read() or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def save_admin_state(runtime_state: dict) -> None:
    try:
        data = {
            "telegram_chat_id": runtime_state.get("telegram_chat_id", ""),
            "admins": sorted(list(set(int(x) for x in runtime_state.get("admins", []) if str(x).isdigit()))),
            "muted_chats": sorted(list(set(int(x) for x in runtime_state.get("muted_chats", [])))),
            "forwarding_enabled": bool(runtime_state.get("forwarding_enabled", True)),
            "only_text": bool(runtime_state.get("only_text", False)),
            "loglevel": runtime_state.get("loglevel", "info"),
        }
        tmp = ADMIN_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, ADMIN_STATE_FILE)
    except Exception as e:
        log(f"Не удалось сохранить {ADMIN_STATE_FILE}: {e}")


def build_runtime_state() -> dict:
    stored = load_admin_state()
    admins = {int(x) for x in stored.get("admins", []) if str(x).lstrip("-").isdigit()}
    if ADMIN_TELEGRAM_ID:
        admins.add(ADMIN_TELEGRAM_ID)

    return {
        "poll_interval": POLL_INTERVAL,
        "telegram_chat_id": str(stored.get("telegram_chat_id") or TELEGRAM_CHAT_ID),
        "admins": admins,
        "muted_chats": {int(x) for x in stored.get("muted_chats", [])},
        "forwarding_enabled": bool(stored.get("forwarding_enabled", True)),
        "only_text": bool(stored.get("only_text", False)),
        "loglevel": stored.get("loglevel", "info"),
        "events": deque(maxlen=200),
        "errors": deque(maxlen=100),
        "last_seen_chat_ids": deque(maxlen=50),
        "tg_backoff_sec": 0,
        "start_time": time.time(),
        "last_max_message_ts": None,
        "proxy": _build_proxy_url(),
        "client": None,
    }

# ============================================================
# Telegram helpers (для текста и админки)
# ============================================================

# Таймауты Telegram через прокси обычно должны быть больше.
# Используем раздельные таймауты: connect/read.
TG_CONNECT_TIMEOUT_SECONDS = 30.0
TG_READ_TIMEOUT_SECONDS = 90.0

# Long polling timeout для getUpdates (сек).
# Telegram будет держать запрос до этого времени, если нет обновлений.
TG_GETUPDATES_LONGPOLL_SECONDS = 25


def _build_proxy_url() -> str | None:
    if not PROXY_URL:
        return None
    if not PROXY_USERNAME or not PROXY_PASSWORD or "@" in PROXY_URL:
        return PROXY_URL
    if "://" not in PROXY_URL:
        return PROXY_URL
    scheme, rest = PROXY_URL.split("://", 1)
    return f"{scheme}://{PROXY_USERNAME}:{PROXY_PASSWORD}@{rest}"


def _build_tg_session() -> requests.Session:
    s = requests.Session()
    proxy = _build_proxy_url()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
        log(f"Telegram прокси включён: {PROXY_URL}")
    else:
        log("Telegram прокси не задан. Работа напрямую.")
    return s


def send_text_to_telegram(session: requests.Session, chat_id: str, text: str, parse_mode: str | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        log("ОШИБКА: не задан TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload: dict[str, str] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = session.post(url, data=payload, timeout=(TG_CONNECT_TIMEOUT_SECONDS, TG_READ_TIMEOUT_SECONDS))
        STATS["last_tg_http_status"] = resp.status_code
    except requests.exceptions.Timeout:
        log("Telegram: timeout при отправке.")
        return False
    except requests.exceptions.RequestException as e:
        log(f"Telegram: ошибка сети: {e}")
        return False

    if resp.status_code != 200:
        log(f"Telegram: HTTP {resp.status_code}, ответ: {(resp.text or '')[:300]}")
        return False

    try:
        data = resp.json()
    except Exception:
        log("Telegram: некорректный JSON в ответе.")
        return False

    if not data.get("ok"):
        log(f"Telegram: API ok=false. Ответ: {str(data)[:300]}")
        return False

    return True


# ============================================================
# Админ-панель (getUpdates)
# ============================================================

def process_admin_commands(session: requests.Session, last_update_id: int, runtime_state: dict) -> int:
    if not TELEGRAM_BOT_TOKEN:
        return last_update_id

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    # Long polling уменьшает количество коротких запросов через прокси
    params = {"offset": last_update_id + 1, "timeout": TG_GETUPDATES_LONGPOLL_SECONDS}

    try:
        resp = session.get(url, params=params, timeout=(TG_CONNECT_TIMEOUT_SECONDS, TG_READ_TIMEOUT_SECONDS))
    except requests.exceptions.RequestException as e:
        # Важно логировать: иначе кажется, что бот "не реагирует"
        log(f"Telegram getUpdates error: {e}")
        _append_error(runtime_state, f"getUpdates error: {e}")
        # Backoff на случай нестабильного прокси/сети
        prev = int(runtime_state.get("tg_backoff_sec", 0))
        runtime_state["tg_backoff_sec"] = 5 if prev <= 0 else min(60, prev * 2)
        if runtime_state["tg_backoff_sec"] != prev:
            log(f"Telegram backoff: {runtime_state['tg_backoff_sec']} sec")
        return last_update_id

    if resp.status_code != 200:
        log(f"Telegram getUpdates HTTP {resp.status_code}: {(resp.text or '')[:200]}")
        _append_error(runtime_state, f"getUpdates HTTP {resp.status_code}: {(resp.text or '')[:200]}")
        prev = int(runtime_state.get("tg_backoff_sec", 0))
        runtime_state["tg_backoff_sec"] = 5 if prev <= 0 else min(60, prev * 2)
        if runtime_state["tg_backoff_sec"] != prev:
            log(f"Telegram backoff: {runtime_state['tg_backoff_sec']} sec")
        return last_update_id

    try:
        data = resp.json()
    except Exception:
        log("Telegram getUpdates: invalid JSON response")
        _append_error(runtime_state, "getUpdates invalid JSON response")
        prev = int(runtime_state.get("tg_backoff_sec", 0))
        runtime_state["tg_backoff_sec"] = 5 if prev <= 0 else min(60, prev * 2)
        if runtime_state["tg_backoff_sec"] != prev:
            log(f"Telegram backoff: {runtime_state['tg_backoff_sec']} sec")
        return last_update_id

    if not data.get("ok"):
        log(f"Telegram getUpdates ok=false: {str(data)[:200]}")
        _append_error(runtime_state, f"getUpdates ok=false: {str(data)[:200]}")
        prev = int(runtime_state.get("tg_backoff_sec", 0))
        runtime_state["tg_backoff_sec"] = 5 if prev <= 0 else min(60, prev * 2)
        if runtime_state["tg_backoff_sec"] != prev:
            log(f"Telegram backoff: {runtime_state['tg_backoff_sec']} sec")
        return last_update_id

    # success -> сбрасываем backoff
    if runtime_state.get("tg_backoff_sec"):
        runtime_state["tg_backoff_sec"] = 0

    updates = data.get("result", [])
    if not updates:
        return last_update_id

    for upd in updates:
        upd_id = upd.get("update_id")
        if isinstance(upd_id, int) and upd_id > last_update_id:
            last_update_id = upd_id

        message = upd.get("message") or upd.get("edited_message")
        if not message:
            continue

        from_user = message.get("from") or {}
        user_id = from_user.get("id")
        text = (message.get("text") or "").strip()

        if not text:
            continue

        # /whoami доступен всем (чтобы узнать свой id)
        if text.startswith("/whoami"):
            send_text_to_telegram(session, str(user_id), f"Ваш Telegram ID: {user_id}")
            continue

        # Проверка прав администратора (поддержка нескольких админов)
        admins = runtime_state.get("admins", [])
        if int(user_id or 0) not in [int(x) for x in admins]:
            continue

        admin_chat_id = str(user_id)

        def _admin_help_text() -> str:
            return (
                "Админ-панель MAX->Telegram\n\n"
                "Команды:\n"
                "/admin — показать это меню\n"
                "/help — показать список команд\n"
                "/stats — статистика\n"
                "/set_interval <сек> — интервал опроса админ-панели\n"
                "/ping — проверка, что бот жив\n"
                "/health — статус MAX/Telegram/прокси\n"
                "/uptime — время работы бота\n"
                "/version — версия бота\n"
                "/pause — пауза пересылки MAX->Telegram\n"
                "/resume — возобновить пересылку\n"
                "/list_chats — список MAX чатов\n"
                "/add_chat <id> — добавить MAX чат\n"
                "/remove_chat <id> — удалить MAX чат\n"
                "/clear_chats — очистить список MAX чатов\n"
                "/set_chats <id1,id2,...> — заменить список MAX чатов\n"
                "/last_chat_ids — последние chat_id, которые бот видел\n"
                "/mute_chat <id> — временно замьютить чат (не пересылать)\n"
                "/unmute_chat <id> — снять мьют\n"
                "/only_text on|off — пересылать только текст (без медиа)\n"
                "/set_tg_chat <chat_id> — изменить целевой Telegram chat_id\n"
                "/where — показать текущие настройки (tg chat, admins)\n"
                "/loglevel info|debug — уровень логов\n"
                "/errors — последние ошибки\n"
                "/tail <n> — последние события (n=1..50)\n"
                "/allow_admin <id> — добавить админа\n"
                "/disallow_admin <id> — удалить админа\n"
                "/whoami — показать ваш Telegram ID\n"
                "/test — тестовое сообщение в Telegram\n"
            )

        if text.startswith("/admin") or text.startswith("/help") or text.startswith("/commands"):
            send_text_to_telegram(session, admin_chat_id, _admin_help_text())

        elif text.startswith("/stats"):
            stats_text = (
                "Статистика\n\n"
                f"Переслано сообщений: {STATS.get('forwarded_count', 0)}\n"
                f"Интервал опроса админ-панели: {runtime_state.get('poll_interval', POLL_INTERVAL)} сек\n"
                f"Время последнего события MAX: {STATS.get('last_max_event_ts') or 'нет'}\n"
                f"TG HTTP статус: {STATS.get('last_tg_http_status')}\n"
                f"Пересылка включена: {runtime_state.get('forwarding_enabled', True)}\n"
                f"Only text: {runtime_state.get('only_text', False)}\n"
            )
            send_text_to_telegram(session, admin_chat_id, stats_text, parse_mode="HTML")

        elif text.startswith("/set_interval"):
            parts = text.split()
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /set_interval <сек>")
                continue
            try:
                new_int = int(parts[1])
                if new_int <= 0:
                    raise ValueError
                runtime_state["poll_interval"] = new_int
                save_admin_state(runtime_state)
                send_text_to_telegram(session, admin_chat_id, f"Интервал обновлён: {new_int} сек")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Некорректное значение. Пример: /set_interval 5")

        elif text.startswith("/test"):
            ok = send_text_to_telegram(session, runtime_state.get("telegram_chat_id", TELEGRAM_CHAT_ID), "Тест: сообщение из админ-панели MAX->Telegram.")
            send_text_to_telegram(
                session,
                admin_chat_id,
                "Тест отправлен в группу." if ok else "Не удалось отправить тест в группу.",
            )

        elif text.startswith("/list_chats"):
            send_text_to_telegram(
                session,
                admin_chat_id,
                "MAX_CHAT_IDS:\n" + ("\n".join(str(x) for x in MAX_CHAT_IDS) if MAX_CHAT_IDS else "(пусто)"),
            )

        elif text.startswith("/add_chat"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /add_chat <chat_id>")
                continue
            try:
                chat_id = int(parts[1].strip())
                if chat_id not in MAX_CHAT_IDS:
                    MAX_CHAT_IDS.append(chat_id)
                    MAX_CHAT_IDS.sort()
                    save_max_chat_ids(MAX_CHAT_IDS)
                send_text_to_telegram(session, admin_chat_id, f"Добавлено. Сейчас MAX_CHAT_IDS: {', '.join(map(str, MAX_CHAT_IDS))}")
                log(f"ADMIN: add_chat {chat_id}. MAX_CHAT_IDS={MAX_CHAT_IDS}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Не удалось добавить. Пример: /add_chat -68776948203767")

        elif text.startswith("/remove_chat"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /remove_chat <chat_id>")
                continue
            try:
                chat_id = int(parts[1].strip())
                if chat_id in MAX_CHAT_IDS:
                    MAX_CHAT_IDS.remove(chat_id)
                    save_max_chat_ids(MAX_CHAT_IDS)
                send_text_to_telegram(session, admin_chat_id, f"Готово. Сейчас MAX_CHAT_IDS: {', '.join(map(str, MAX_CHAT_IDS)) if MAX_CHAT_IDS else '(пусто)'}")
                log(f"ADMIN: remove_chat {chat_id}. MAX_CHAT_IDS={MAX_CHAT_IDS}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Не удалось удалить. Пример: /remove_chat -68776948203767")

        elif text.startswith("/clear_chats"):
            MAX_CHAT_IDS.clear()
            save_max_chat_ids(MAX_CHAT_IDS)
            send_text_to_telegram(session, admin_chat_id, "Список MAX чатов очищен.")

        elif text.startswith("/set_chats"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /set_chats <id1,id2,...>")
                continue
            try:
                ids = [int(x.strip()) for x in parts[1].split(",") if x.strip()]
                MAX_CHAT_IDS[:] = sorted(list(set(ids)))
                save_max_chat_ids(MAX_CHAT_IDS)
                send_text_to_telegram(session, admin_chat_id, f"Готово. Сейчас MAX_CHAT_IDS: {', '.join(map(str, MAX_CHAT_IDS)) if MAX_CHAT_IDS else '(пусто)'}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Ошибка. Пример: /set_chats -1,-2,-3")

        elif text.startswith("/last_chat_ids"):
            seen = list(runtime_state.get("last_seen_chat_ids", []))
            send_text_to_telegram(
                session,
                admin_chat_id,
                "Последние chat_id:\n" + ("\n".join(map(str, seen)) if seen else "(пусто)"),
            )

        elif text.startswith("/mute_chat"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /mute_chat <chat_id>")
                continue
            try:
                chat_id = int(parts[1].strip())
                runtime_state["muted_chats"].add(chat_id)
                save_admin_state(runtime_state)
                send_text_to_telegram(session, admin_chat_id, f"Чат замьючен: {chat_id}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Ошибка. Пример: /mute_chat -68776948203767")

        elif text.startswith("/unmute_chat"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /unmute_chat <chat_id>")
                continue
            try:
                chat_id = int(parts[1].strip())
                runtime_state["muted_chats"].discard(chat_id)
                save_admin_state(runtime_state)
                send_text_to_telegram(session, admin_chat_id, f"Мьют снят: {chat_id}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Ошибка. Пример: /unmute_chat -68776948203767")

        elif text.startswith("/only_text"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /only_text on|off")
                continue
            val = parts[1].strip().lower()
            if val in ["on", "1", "true", "yes"]:
                runtime_state["only_text"] = True
            elif val in ["off", "0", "false", "no"]:
                runtime_state["only_text"] = False
            else:
                send_text_to_telegram(session, admin_chat_id, "Использование: /only_text on|off")
                continue
            save_admin_state(runtime_state)
            send_text_to_telegram(session, admin_chat_id, f"Only text: {runtime_state['only_text']}")

        elif text.startswith("/pause"):
            runtime_state["forwarding_enabled"] = False
            save_admin_state(runtime_state)
            send_text_to_telegram(session, admin_chat_id, "Пересылка остановлена (pause).")

        elif text.startswith("/resume"):
            runtime_state["forwarding_enabled"] = True
            save_admin_state(runtime_state)
            send_text_to_telegram(session, admin_chat_id, "Пересылка возобновлена (resume).")

        elif text.startswith("/ping"):
            send_text_to_telegram(session, admin_chat_id, "pong")

        elif text.startswith("/uptime"):
            uptime = int(time.time() - runtime_state.get("start_time", time.time()))
            send_text_to_telegram(session, admin_chat_id, f"Uptime: {uptime} sec")

        elif text.startswith("/version"):
            send_text_to_telegram(session, admin_chat_id, f"Bot version: {BOT_VERSION}")

        elif text.startswith("/where"):
            send_text_to_telegram(
                session,
                admin_chat_id,
                "Текущие настройки:\n"
                f"TELEGRAM_CHAT_ID: {runtime_state.get('telegram_chat_id', TELEGRAM_CHAT_ID)}\n"
                f"Admins: {', '.join(map(str, runtime_state.get('admins', [])))}\n",
            )

        elif text.startswith("/set_tg_chat"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /set_tg_chat <chat_id>")
                continue
            runtime_state["telegram_chat_id"] = parts[1].strip()
            save_admin_state(runtime_state)
            send_text_to_telegram(session, admin_chat_id, f"TELEGRAM_CHAT_ID обновлён: {runtime_state['telegram_chat_id']}")

        elif text.startswith("/loglevel"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /loglevel info|debug")
                continue
            lvl = parts[1].strip().lower()
            if lvl not in ["info", "debug"]:
                send_text_to_telegram(session, admin_chat_id, "Использование: /loglevel info|debug")
                continue
            runtime_state["loglevel"] = lvl
            save_admin_state(runtime_state)
            send_text_to_telegram(session, admin_chat_id, f"loglevel={lvl}")

        elif text.startswith("/errors"):
            errs = list(runtime_state.get("errors", []))[-10:]
            send_text_to_telegram(session, admin_chat_id, "Последние ошибки:\n" + ("\n".join(errs) if errs else "(нет)"))

        elif text.startswith("/tail"):
            parts = text.split(maxsplit=1)
            n = 10
            if len(parts) == 2:
                try:
                    n = int(parts[1].strip())
                except Exception:
                    n = 10
            n = max(1, min(50, n))
            ev = list(runtime_state.get("events", []))[-n:]
            send_text_to_telegram(session, admin_chat_id, "\n".join(ev) if ev else "(нет событий)")

        elif text.startswith("/allow_admin"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /allow_admin <telegram_id>")
                continue
            try:
                aid = int(parts[1].strip())
                runtime_state["admins"].add(aid)
                save_admin_state(runtime_state)
                send_text_to_telegram(session, admin_chat_id, f"Админ добавлен: {aid}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Ошибка. Пример: /allow_admin 123456789")

        elif text.startswith("/disallow_admin"):
            parts = text.split(maxsplit=1)
            if len(parts) != 2:
                send_text_to_telegram(session, admin_chat_id, "Использование: /disallow_admin <telegram_id>")
                continue
            try:
                aid = int(parts[1].strip())
                runtime_state["admins"].discard(aid)
                save_admin_state(runtime_state)
                send_text_to_telegram(session, admin_chat_id, f"Админ удалён: {aid}")
            except Exception:
                send_text_to_telegram(session, admin_chat_id, "Ошибка. Пример: /disallow_admin 123456789")

        elif text.startswith("/health"):
            client = runtime_state.get("client")
            max_ok = False
            reconnects = 0
            last_error = "-"
            last_recv_age = "n/a"
            try:
                max_ok = bool(client and client.is_connected())
                reconnects = int(getattr(client, "reconnect_count", 0))
                last_error = str(getattr(client, "last_error", "-") or "-")
                last_recv_ts = getattr(client, "last_recv_ts", None)
                if last_recv_ts:
                    last_recv_age = f"{int(time.time() - last_recv_ts)} sec"
            except Exception:
                max_ok = False
            proxy = runtime_state.get("proxy", None)
            send_text_to_telegram(
                session,
                admin_chat_id,
                "HEALTH:\n"
                f"MAX connected: {max_ok}\n"
                f"MAX reconnects: {reconnects}\n"
                f"MAX last recv age: {last_recv_age}\n"
                f"MAX last error: {last_error}\n"
                f"Proxy: {proxy or 'none'}\n"
                f"TG last status: {STATS.get('last_tg_http_status')}\n",
            )

    return last_update_id


# ============================================================
# Интеграция с MaxClient (как в maxtg-master)
# ============================================================

def setup_max_client(tg_session: requests.Session, runtime_state: dict) -> Client:
    if not MAX_TOKEN:
        raise SystemExit("В .env не задан MAX_TOKEN (или MAX_API_TOKEN).")
    if not MAX_CHAT_IDS:
        raise SystemExit("В .env не задан MAX_CHAT_IDS (список ID чатов через запятую).")

    client = Client(MAX_TOKEN)
    runtime_state["client"] = client

    @client.on_connect
    def _on_connect():
        runtime_state["last_max_message_ts"] = time.time()
        _append_event(runtime_state, "MAX connected")
        if client.me is not None:
            log(
                f"MAX подключен. Имя: {client.me.contact.names[0].name}, "
                f"Телефон: {client.me.contact.phone}, ID: {client.me.contact.id}"
            )

    @client.on_message(filters.any())
    def _on_message(c: Client, message: Message):
        try:
            # Диагностика: покажем, какие chat_id реально приходят
            chat_id_raw = None
            try:
                chat_id_raw = message.chat.id
            except Exception:
                chat_id_raw = None

            try:
                chat_id_int = int(chat_id_raw)
            except Exception:
                chat_id_int = None

            # Логируем только кратко, чтобы не спамить сильно
            preview = (message.text or "").replace("\n", " ")[:80]
            log(f"MAX message: chat_id={chat_id_raw} text='{preview}'")
            _append_event(runtime_state, f"MAX msg chat_id={chat_id_raw} text='{preview}'")

            # запоминаем последние seen chat_id
            try:
                runtime_state["last_seen_chat_ids"].append(chat_id_int if chat_id_int is not None else chat_id_raw)
            except Exception:
                pass

            if chat_id_int is None or chat_id_int not in MAX_CHAT_IDS:
                return
            if getattr(message, "status", None) == "REMOVED":
                return
            if not runtime_state.get("forwarding_enabled", True):
                return
            if chat_id_int in runtime_state.get("muted_chats", set()):
                return

            STATS["last_max_event_ts"] = _ts()
            runtime_state["last_max_message_ts"] = time.time()

            msg_text = message.text or ""
            msg_attaches = message.attaches or []
            # Имя/фамилия отправителя
            full_name = "Неизвестный отправитель"
            try:
                names = message.user.contact.names or []
                if names:
                    n0 = names[0]
                    if getattr(n0, "name", None):
                        full_name = n0.name
                    else:
                        first = (getattr(n0, "first_name", "") or "").strip()
                        last = (getattr(n0, "last_name", "") or "").strip()
                        full_name = (first + " " + last).strip() or full_name
            except Exception:
                pass

            # Откуда сообщение (только ID чата, без ссылки)
            chat_line = f"Чат ID: {chat_id_int}"

            # Обработка пересланных сообщений (как в maxtg-master)
            if "link" in message.kwargs:
                link = message.kwargs["link"]
                if isinstance(link, dict) and link.get("type") == "FORWARD":
                    fmsg = link.get("message") or {}
                    msg_text = fmsg.get("text", msg_text)
                    msg_attaches = fmsg.get("attaches", msg_attaches)
                    forwarded_author = c.get_user(id=fmsg.get("sender"), _f=1)
                    try:
                        forwarded_name = forwarded_author.contact.names[0].name
                    except Exception:
                        forwarded_name = "неизвестно"
                    full_name = f"{full_name}\n(Переслано: {forwarded_name})"

            if not msg_text and not msg_attaches:
                return

            # Готовим HTML (экранируем, чтобы Telegram не ломал разметку)
            safe_name = html.escape(full_name)
            safe_text = html.escape(msg_text) if msg_text else ""
            safe_chat_line = html.escape(chat_line)
            caption = f"<b>{safe_name}</b>\n{safe_chat_line}" + (f"\n{safe_text}" if safe_text else "")

            # Если есть вложения — используем sendMediaGroup из maxtg-master/telegram.py
            if msg_attaches and not runtime_state.get("only_text", False):
                send_media_to_telegram(
                    TG_BOT_TOKEN=TELEGRAM_BOT_TOKEN,
                    TG_CHAT_ID=int(runtime_state.get("telegram_chat_id", TELEGRAM_CHAT_ID)),
                    caption=caption,
                    attachments=msg_attaches,
                )
            else:
                send_text_to_telegram(
                    tg_session,
                    runtime_state.get("telegram_chat_id", TELEGRAM_CHAT_ID),
                    caption,
                    parse_mode="HTML",
                )

            STATS["forwarded_count"] = int(STATS.get("forwarded_count", 0)) + 1
        except Exception as e:
            log(f"Ошибка обработки входящего сообщения MAX: {e}")
            _append_error(runtime_state, f"MAX handler error: {e}")

    return client


# ============================================================
# main
# ============================================================

def main() -> None:
    # 1. Проверка базовых настроек
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise SystemExit("В .env нужно задать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")

    # 2. Инициализация сессии и состояния
    tg_session = _build_tg_session()
    runtime_state = build_runtime_state()

    try:
        client = setup_max_client(tg_session, runtime_state)
    except Exception as e:
        log(f"ОШИБКА инициализации клиента Max: {e}")
        sys.exit(1)

    client.run()

    log("Бот MAX->Telegram запущен (WebSocket + Heartbeat thread).")
    log(f"Админ Telegram ID: {ADMIN_TELEGRAM_ID}")
    log(f"MAX chat ids: {MAX_CHAT_IDS}")
    if runtime_state.get("admins"):
        log(f"Admins: {sorted(runtime_state['admins'])}")

    last_update_id = 0
    last_health_log_time = 0.0

    try:
        while True:
            last_update_id = process_admin_commands(tg_session, last_update_id, runtime_state)
            current_time = time.time()

            if current_time - last_health_log_time >= 300:
                connected = bool(getattr(client, "is_connected", lambda: False)())
                last_recv = getattr(client, "last_recv_ts", None)
                silence = int(current_time - last_recv) if last_recv else None
                log(
                    "MAX health: "
                    f"connected={connected}, "
                    f"reconnects={getattr(client, 'reconnect_count', 0)}, "
                    f"last_recv_age={silence if silence is not None else 'n/a'} sec"
                )
                last_health_log_time = current_time

            last_recv = getattr(client, "last_recv_ts", None)
            silent_for = (current_time - last_recv) if last_recv else None
            if not client.is_connected():
                log(f"CRITICAL: MAX disconnected. Last error: {getattr(client, 'last_error', 'unknown')}")
                os._exit(1)
            if silent_for is not None and silent_for > MAX_SILENCE_RESTART_SECONDS:
                log(f"CRITICAL: MAX silent for {int(silent_for)} sec. Restarting process.")
                os._exit(1)

            backoff = int(runtime_state.get("tg_backoff_sec", 0))
            sleep_for = max(1, backoff or int(runtime_state.get("poll_interval", 5)))
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        log("Остановка пользователем (Ctrl+C).")
        os._exit(0)
    except Exception as e:
        log(f"Непредвиденная ошибка в основном цикле: {e}")
        os._exit(1)


if __name__ == "__main__":
    main()

