import os
import json
import requests


def _build_proxy_url() -> str | None:
    proxy_url = (os.environ.get("PROXY_URL") or "").strip()
    proxy_username = (os.environ.get("PROXY_USERNAME") or "").strip()
    proxy_password = (os.environ.get("PROXY_PASSWORD") or "").strip()

    if not proxy_url:
        return None
    if not proxy_username or not proxy_password or "@" in proxy_url:
        return proxy_url
    if "://" not in proxy_url:
        return proxy_url
    scheme, rest = proxy_url.split("://", 1)
    return f"{scheme}://{proxy_username}:{proxy_password}@{rest}"


_SESSION = requests.Session()
_proxy = _build_proxy_url()
if _proxy:
    _SESSION.proxies.update({"http": _proxy, "https": _proxy})


def handle_attach(attach: dict) -> str:
    match attach["_type"]:
        case "FILE":
            return attach.get("name", "FILE")
        case _:
            return attach.get("_type", "ATTACH")


def send_to_telegram(TG_BOT_TOKEN: str = "", TG_CHAT_ID: int = 0, caption: str = "", attachments: list[dict] = []):
    if not attachments:  # нет фоток — просто текст
        if caption == "":
            return
        api_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        resp = _SESSION.post(
            api_url,
            data={
                "chat_id": TG_CHAT_ID,
                "text": caption,
                "parse_mode": "HTML",
            },
        )
        print(resp.json())
        return

    if 1 <= len(attachments) <= 10:
        api_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMediaGroup"
        media = []
        not_handled_attachs = attachments.copy()
        for i, attach in enumerate(attachments):
            if attach.get("_type") == "PHOTO" and attach.get("baseUrl"):
                item = {"type": "photo", "media": attach["baseUrl"]}
                not_handled_attachs.remove(attach)
                if i == 0 and caption:
                    item["caption"] = caption
                    item["parse_mode"] = "HTML"
                media.append(item)
        if not_handled_attachs:
            if media:
                print(not_handled_attachs)
                media[0]["caption"] += "\n\nНеобработанные файлы: " + ", ".join(handle_attach(attach) for attach in not_handled_attachs)
            else:
                send_to_telegram(
                    TG_BOT_TOKEN,
                    TG_CHAT_ID,
                    caption + "\n\nНеобработанные файлы: " + ", ".join(handle_attach(attach) for attach in not_handled_attachs),
                )
                return

        payload = {
            "chat_id": TG_CHAT_ID,
            "media": json.dumps(media),
        }
        resp = _SESSION.post(api_url, data=payload)
        print(resp.json())
        return

    # если фоток больше 10 — разобьём на несколько альбомов
    for i in range(0, len(attachments), 10):
        chunk = attachments[i : i + 10]
        send_to_telegram(TG_BOT_TOKEN, TG_CHAT_ID, caption if i == 0 else "", chunk)

