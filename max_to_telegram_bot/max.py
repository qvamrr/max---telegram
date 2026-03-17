from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosedError
import json
import threading
import time
from uuid import uuid4

from classes import User, Message


class MaxClient:
    def __init__(self, token: str | None = None, phone: str | None = None):
        self._seq = 0
        self.phone_number = phone
        self.auth_token = token
        self.user_agent = self._generate_user_agent()

        self.websocket = None
        self._on_connect = None
        self._connected = False
        self._t = None
        self._t_stop = False

        self.is_log_in = False
        self.me = None
        self.session_id = int(time.time() * 1000)

        self.handlers = []

    @property
    def seq(self):
        current_seq = self._seq
        self._seq += 1
        return current_seq

    @property
    def cid(self):
        return int(time.time() * 1000)

    def _generate_user_agent(self) -> str:
        return json.dumps(
            {
                "ver": 11,
                "cmd": 0,
                "seq": self.seq,
                "opcode": 6,
                "payload": {
                    "userAgent": {
                        "deviceType": "WEB",
                        "locale": "en",
                        "osVersion": "Windows",
                        "deviceName": "WebMax Lib",
                        "headerUserAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
                        "deviceLocale": "en",
                        "appVersion": "4.8.42",
                        "screen": "1920x1080 1.0x",
                        "timezone": "UTC",
                    },
                    "deviceId": str(uuid4()),
                },
            }
        )

    def connect(self):
        if self._connected:
            return

        headers = [
            ("Origin", "https://web.oneme.ru"),
            ("Pragma", "no-cache"),
            ("Cache-Control", "no-cache"),
            (
                "User-Agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            ),
        ]
        self.websocket = connect("wss://ws-api.oneme.ru/websocket", additional_headers=headers)
        self.websocket.send(self.user_agent)
        self.websocket.recv()

        self.websocket.send(
            json.dumps(
                {
                    "ver": 11,
                    "cmd": 0,
                    "seq": self.seq,
                    "opcode": 19,
                    "payload": {
                        "interactive": True,
                        "token": self.auth_token,
                        "chatsSync": 0,
                        "contactsSync": 0,
                        "presenceSync": 0,
                        "draftsSync": 0,
                        "chatsCount": 40,
                    },
                }
            )
        )

        first = json.loads(self.websocket.recv())
        p = first.get("payload") or {}
        # Иногда сервер может вернуть ошибку/другой payload без profile
        if "profile" not in p:
            raise RuntimeError(f"MAX connect: нет profile в ответе. opcode={first.get('opcode')} payload={str(p)[:300]}")
        usr = User(self, p["profile"])
        self.me = usr
        self._connected = True

        if self._on_connect:
            self._on_connect()

    def disconnect(self):
        if not self._connected:
            return
        if self.websocket:
            self.websocket.close()
            self._seq = 0
        self._connected = False
        self.websocket = None

    def set_token(self, token):
        self.auth_token = token

    def _hlprocessor(self, msg: Message):
        for filter_func, func in self.handlers:
            if filter_func(self, msg):
                func(self, msg)
                return

    def _heartbeat(self):
        while self._connected and not self._t_stop:
            try:
                self.websocket.send(
                    json.dumps(
                        {
                            "ver": 11,
                            "cmd": 0,
                            "seq": self.seq,
                            "opcode": 1,
                            "payload": {"interactive": False},
                        }
                    )
                )
            except Exception:
                pass
            time.sleep(25)

    def _listener(self):
        while not self._t_stop:
            try:
                recv = json.loads(self.websocket.recv())
                self._process_message(recv)

                while True:
                    try:
                        next_msg = json.loads(self.websocket.recv(timeout=0.01))
                        self._process_message(next_msg)
                    except TimeoutError:
                        break
                    except ConnectionClosedError:
                        break
            except ConnectionClosedError:
                self._connected = False
                try:
                    if self.websocket:
                        self.websocket.close()
                except Exception:
                    pass
                time.sleep(3)
                try:
                    self.connect()
                except Exception:
                    time.sleep(5)
                else:
                    break
            except Exception:
                self._connected = False
                time.sleep(5)
                continue

    def _process_message(self, recv):
        opcode = recv.get("opcode")
        payload = recv.get("payload")

        if opcode == 128:
            try:
                msg = Message(self, payload["chatId"], **payload["message"])
                self._hlprocessor(msg)
            except Exception:
                pass

    def run(self):
        self.connect()
        self._t = threading.Thread(target=self._listener, name="WebMaxListener", daemon=True)
        self._t.start()
        threading.Thread(target=self._heartbeat, name="WebMaxHeartbeat", daemon=True).start()

    def stop(self):
        self._t_stop = True
        self.disconnect()

    def on_message(self, filter_func):
        def decorator(func):
            self.handlers.append((filter_func, func))
            return func

        return decorator

    def on_connect(self, func):
        self._on_connect = func
        return func

    # Минимально нужное для нашего проекта
    def get_user(self, **kwargs):
        id_ = kwargs.get("id")
        _f = kwargs.get("_f")
        seq = self.seq
        j = {"ver": 11, "cmd": 0, "seq": seq, "opcode": 32, "payload": {"contactIds": [id_]}}
        self.websocket.send(json.dumps(j))
        while True:
            recv = json.loads(self.websocket.recv())
            if recv["seq"] == seq:
                break
        payload = recv["payload"]
        contact = payload["contacts"][0]
        return User(self, contact, _f)

