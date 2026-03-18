import json, time
from typing import Literal

EMOJIS = Literal[
    "❤️","👍","🤣","🔥","💯","😍","🎉","⚡",
    "🤩","🤘","😎","🙄","😐","😁","🤪","😉",
    "🤤","😇","😘","🥰","🥳","🌚","🌝","😴",
    "🫠","🤔","🫡","😳","🥱","🐈","🐶","💪",
    "🤞","👋","👏","🤝","👌","🙏","💋","👑",
    "⭐","🍷","🍑","🤷‍♀️","🤷‍♂️","👩‍❤️‍👨","🦄","👻",
    "🗿","👀","👁️","🖤","❤️‍🩹","🛑","⛄","❓",
    "❗️"
]


class Name:
    def __init__(self, **kwargs):
        self.name = kwargs.get("name")
        self.first_name = kwargs.get("firstName")
        self.last_name = kwargs.get("lastName")
        self.type = kwargs.get("type")


class Contact:
    def __init__(
        self,
        client,
        accountStatus=None,
        baseUrl=None,
        names=None,
        phone=None,
        description=None,
        options=None,
        photoId=None,
        updateTime=None,
        id=None,
        baseRawUrl=None,
        gender=None,
        link=None,
        **kwargs,
    ):
        self._client = client
        self.accountStatus = accountStatus
        self.base_url = baseUrl
        self.names = [Name(**n) for n in names] if names else []
        self.phone = phone
        self.description = description
        self.options = options
        self.photo_id = photoId
        self.update_time = updateTime
        self.id = id
        self.link = link
        self.gender = gender
        self.base_raw_url = baseRawUrl

    def add(self):
        return self._client.contact_add(self.id)

    def remove(self):
        return self._client.contact_remove(self.id)

    def block(self):
        return self._client.contact_block(self.id)

    def unblock(self):
        return self._client.contact_unblock(self.id)


class User:
    def __init__(self, client, profile, _f=0):
        self._client = client
        self.contact = Contact(client, **profile)
        _id = client.me.contact.id if client.me else profile["id"]
        if not _f:
            self.chat = Chat(self._client, profile["id"] ^ _id)


class Chat:
    def __init__(self, client, chat_id):
        if chat_id == 0:
            return
        self._client = client
        self.id: int = chat_id
        self.link = f"https://web.max.ru/{chat_id}"
        # В этом проекте история сообщений чата не нужна.
        # Важно: загрузка истории (opcode=49) конкурирует за websocket.recv() и может приводить
        # к пропуску входящих событий при высокой нагрузке. Поэтому здесь только ссылка/id.
        self.messages: list[Message] = []

    def pin(self):
        self._client.pin_chat(self.id)

    def unpin(self):
        self._client.unpin_chat(self.id)


class Message:
    def __init__(self, client, chatId: str, sender: str, id, time, text, type, _f=0, **kwargs):
        self._client = client
        self.kwargs = kwargs
        self.status = kwargs.get("status")
        if not _f:
            self.chat = Chat(client, chatId)
        self.sender = sender
        self.id = id
        self.time = time
        self.text = text
        self.type = type
        self.update_time = kwargs.get("updateTime")
        self.options = kwargs.get("options")
        self.cid = kwargs.get("cid")
        self.attaches = kwargs.get("attaches", [])
        self.reaction_info = kwargs.get("reactionInfo", {})
        self.user: User = client.get_user(id=sender, _f=1)

    def reply(self, text: str, **kwargs) -> "Message":
        return self._client.send_message(self.chat.id, text, self.id, **kwargs)

    def answer(self, text: str, **kwargs) -> "Message":
        return self._client.send_message(self.chat.id, text, **kwargs)

    def delete(self, for_me=False):
        return self._client.delete_message(self.chat.id, [self.id], for_me)

    def edit(self, text: str) -> "Message":
        return self._client.edit_message(self.chat.id, self.id, text)

    def react(self, reaction: EMOJIS):
        return self._client.set_reaction(self.chat.id, self.id, reaction)


class Reaction:
    def __init__(self, reaction: str, count: int):
        self.reaction = reaction
        self.count = count


class Reactions:
    def __init__(self, **kwargs):
        reaction_info = kwargs.get("reactionInfo", {})
        self.counters = [Reaction(**c) for c in reaction_info.get("counters", [])]
        self.your_reaction = reaction_info.get("yourReaction")
        self.total_count = reaction_info.get("totalCount")

