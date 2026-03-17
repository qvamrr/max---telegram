class Filter:
    def __call__(self, client, message) -> bool:
        return True

    def __and__(self, other: "Filter") -> "AndFilter":
        return AndFilter(self, other)

    def __or__(self, other: "Filter") -> "OrFilter":
        return OrFilter(self, other)

    def __invert__(self) -> "NotFilter":
        return NotFilter(self)


class AndFilter(Filter):
    def __init__(self, *filters: Filter):
        self.filters = filters

    def __call__(self, client, message) -> bool:
        return all(f(client, message) for f in self.filters)


class OrFilter(Filter):
    def __init__(self, *filters: Filter):
        self.filters = filters

    def __call__(self, client, message) -> bool:
        return any(f(client, message) for f in self.filters)


class NotFilter(Filter):
    def __init__(self, filter: Filter):
        self.filter = filter

    def __call__(self, client, message) -> bool:
        return not self.filter(client, message)


class text(Filter):
    def __init__(self, text: str):
        self.text = text.lower()

    def __call__(self, client, message) -> bool:
        return message.text.lower() == self.text if message.text else False


class command(Filter):
    def __init__(self, command: str, prefix: str = "/"):
        self.command = (prefix + command).lower()

    def __call__(self, client, message) -> bool:
        return message.text.lower().startswith(self.command) if message.text else False


class user_id(Filter):
    def __init__(self, user_id: str):
        self.user_id = user_id

    def __call__(self, client, message) -> bool:
        return message.sender == self.user_id


class me(Filter):
    def __call__(self, client, message) -> bool:
        if not client.me or not client.me.contact.id:
            raise ValueError("No authenticated user found. Please authenticate first.")
        return message.sender == client.me.contact.id


class _any(Filter):
    def __call__(self, client, message) -> bool:
        return True


class user(Filter):
    def __call__(self, client, message) -> bool:
        if not client.me or not client.me.contact.id:
            raise ValueError("No authenticated user found. Please authenticate first.")
        return message.type == "USER"


class filters:
    text = text
    command = command
    user_id = user_id
    me = me
    user = user
    any = _any

