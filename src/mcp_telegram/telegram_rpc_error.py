"""Privacy-safe descriptors for Telegram RPC failures."""

from __future__ import annotations

import re
from dataclasses import dataclass

from telethon.errors import rpc_errors_dict  # type: ignore[import-untyped]

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z", re.ASCII)
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class TelegramRpcErrorDescriptor:
    """Safe, bounded identity fields for one Telegram RPC exception."""

    error_type: str
    code: int | None
    symbol: str | None


def _safe_token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN_RE.fullmatch(value) else None


def _safe_symbol(value: object) -> str | None:
    return value if isinstance(value, str) and _SYMBOL_RE.fullmatch(value) else None


def _safe_code(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:  # noqa: PLR2004
        return value
    return None


def _reverse_symbol(error_type: type[BaseException]) -> str | None:
    symbols = [
        symbol for symbol, candidate in rpc_errors_dict.items() if candidate is error_type and _safe_symbol(symbol)
    ]
    return symbols[0] if len(symbols) == 1 else None


def _error_code(exc: BaseException) -> int | None:
    try:
        own = vars(exc)
    except TypeError:
        own = {}
    sources = [own, *(vars(cls) for cls in type(exc).__mro__)]
    for source in sources:
        code = _safe_code(source.get("code"))
        if code is not None:
            return code
    return None


def describe_telegram_rpc_error(exc: BaseException) -> TelegramRpcErrorDescriptor:
    """Describe an RPC error without reading its message, request, args, or repr."""
    error_type = _safe_token(type(exc).__name__) or "UnknownError"
    try:
        own_message = vars(exc).get("message")
    except TypeError:
        own_message = None
    symbol = _safe_symbol(own_message) or _reverse_symbol(type(exc))
    return TelegramRpcErrorDescriptor(error_type=error_type, code=_error_code(exc), symbol=symbol)


__all__ = ["TelegramRpcErrorDescriptor", "describe_telegram_rpc_error"]
