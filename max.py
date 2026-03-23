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
        self._heartbeat_thread = None
        self._t_stop = False
        self._connect_lock = threading.Lock()

        self.is_log_in = False
        self.me = None
        self.session_id = int(time.time() * 1000)
        self.last_connect_ts = None
        self.last_recv_ts = None
        self.last_error = None
        self.reconnect_count = 0

        self.handlers = []
        self._user_cache: dict[int, User] = {}

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
        with self._connect_lock:
            if self._connected and self.websocket is not None:
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

            ws = None
            try:
                ws = connect(
                    "wss://ws-api.oneme.ru/websocket",
                    additional_headers=headers,
                    ping_interval=None,
                    ping_timeout=None,
                )
                ws.send(self.user_agent)
                ws.recv()

                ws.send(
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

                first = json.loads(ws.recv())
                payload = first.get("payload") or {}
                if "profile" not in payload:
                    raise RuntimeError(
                        f"MAX connect: no profile in response. opcode={first.get('opcode')} payload={str(payload)[:300]}"
                    )

                self.websocket = ws
                self.me = User(self, payload["profile"])
                self._connected = True
                now = time.time()
                self.last_connect_ts = now
                self.last_recv_ts = now
                self.last_error = None

                if self._on_connect:
                    self._on_connect()
            except Exception as e:
                self.last_error = str(e)
                self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                raise

    def disconnect(self):
        ws = self.websocket
        self.websocket = None
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        self._seq = 0
        self._connected = False

    def set_token(self, token):
        self.auth_token = token

    def is_connected(self) -> bool:
        return bool(self._connected and self.websocket is not None)

    def ensure_connected(self):
        while not self._t_stop:
            if self.is_connected():
                return
            try:
                self.connect()
            except Exception as e:
                self.last_error = str(e)
                time.sleep(5)
            else:
                return

    def _hlprocessor(self, msg: Message):
        for filter_func, func in self.handlers:
            if filter_func(self, msg):
                func(self, msg)
                return

    def _heartbeat(self):
        while not self._t_stop:
            if not self.is_connected():
                time.sleep(1)
                continue
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
            except Exception as e:
                self.last_error = str(e)
                self._connected = False
            time.sleep(25)

    def _listener(self):
        while not self._t_stop:
            if not self.is_connected():
                self.ensure_connected()
                time.sleep(0.2)
                continue

            try:
                recv = json.loads(self.websocket.recv())
                self.last_recv_ts = time.time()
                self._process_message(recv)

                while True:
                    try:
                        next_msg = json.loads(self.websocket.recv(timeout=0.01))
                        self.last_recv_ts = time.time()
                        self._process_message(next_msg)
                    except TimeoutError:
                        break
                    except ConnectionClosedError:
                        break
            except (ConnectionClosedError, OSError) as e:
                self.last_error = str(e)
                self._connected = False
                try:
                    if self.websocket:
                        self.websocket.close()
                except Exception:
                    pass
                self.websocket = None
                self.reconnect_count += 1
                time.sleep(3)
                continue
            except Exception as e:
                self.last_error = str(e)
                self._connected = False
                self.websocket = None
                time.sleep(5)
                continue

    def _process_message(self, recv):
        opcode = recv.get("opcode")
        payload = recv.get("payload")

        if opcode == 128:
            try:
                msg = Message(self, payload["chatId"], **payload["message"])
                self._hlprocessor(msg)
            except Exception as e:
                print(f"MAX message parse error: {e}", flush=True)

    def run(self):
        self._t_stop = False
        self.ensure_connected()
        if self._t is None or not self._t.is_alive():
            self._t = threading.Thread(target=self._listener, name="WebMaxListener", daemon=True)
            self._t.start()
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = threading.Thread(target=self._heartbeat, name="WebMaxHeartbeat", daemon=True)
            self._heartbeat_thread.start()

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

    def get_user(self, **kwargs):
        id_ = kwargs.get("id")
        _f = kwargs.get("_f")
        try:
            cid = int(id_)
        except Exception:
            cid = None

        if cid is not None and cid in self._user_cache:
            return self._user_cache[cid]

        seq = self.seq
        payload = {"ver": 11, "cmd": 0, "seq": seq, "opcode": 32, "payload": {"contactIds": [id_]}}
        self.websocket.send(json.dumps(payload))
        while True:
            recv = json.loads(self.websocket.recv())
            self.last_recv_ts = time.time()
            if recv["seq"] == seq:
                break
        contact = recv["payload"]["contacts"][0]
        user = User(self, contact, _f)
        if cid is not None:
            self._user_cache[cid] = user
        return user
