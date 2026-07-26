"""Platform-prefixed identity and envelope id normalization."""


def qq_identity(user_id: int | str) -> str:
    return f"qq:{user_id}"


def telegram_identity(user_id: int | str) -> str:
    return f"telegram:{user_id}"


def qq_envelope_id(connection_id: str, message_id: int | str) -> str:
    return f"qq:{connection_id}:{message_id}"


def telegram_envelope_id(connection_id: str, chat_id: int | str, message_id: int | str) -> str:
    return f"telegram:{connection_id}:{chat_id}:{message_id}"
