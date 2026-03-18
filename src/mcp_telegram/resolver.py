from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncContextManager, Callable

from anyascii import anyascii
from rapidfuzz import fuzz, process, utils

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .cache import EntityCache

CANDIDATE_THRESHOLD = 60

# t.me link pattern: optional https://, optional www., t.me/username[/message_id]
_TME_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?t\.me/([a-zA-Z_][a-zA-Z0-9_]{3,})(?:/(\d+))?$"
)


def latinize(text: str) -> str:
    """Normalize any-script text to lowercase Latin for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]+", "", anyascii(text).lower()).strip()


@dataclass(frozen=True)
class Resolved:
    """Exact match: entity uniquely identified."""
    entity_id: int
    display_name: str


@dataclass(frozen=True)
class ResolvedWithMessage(Resolved):
    """Resolution result that also carries a message_id (from t.me/channel/123 links)."""
    message_id: int | None = None


@dataclass(frozen=True)
class Candidates:
    query: str
    matches: list[dict]  # [{entity_id, display_name, score, username, entity_type}]


@dataclass(frozen=True)
class NotFound:
    """No match found for the query."""
    query: str


ResolveResult = Resolved | ResolvedWithMessage | Candidates | NotFound


def _parse_numeric_query(query: str) -> int | None:
    normalized = query.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)
    if normalized[0] in "+-" and normalized[1:].isdigit():
        return int(normalized)
    return None


def parse_exact_dialog_id(dialog: str) -> int | None:
    """Return an exact dialog id for one signed numeric selector string.

    Rejects @username queries and non-numeric strings.
    """
    selector = dialog.strip()
    if not selector or selector.startswith("@"):
        return None
    return _parse_numeric_query(selector)


def _parse_tme_link(query: str) -> tuple[str, int | None] | None:
    """Extract (username, message_id|None) from a t.me URL.

    Returns None if query is not a t.me link.
    """
    match = _TME_RE.match(query.strip())
    if not match:
        return None
    username = match.group(1)
    msg_id = int(match.group(2)) if match.group(2) else None
    return (username, msg_id)


def _build_norm_map(
    choices: dict[int, str],
    normalized_choices: dict[int, str] | None,
) -> dict[str, list[tuple[int, str]]]:
    """Build {normalized_name: [(entity_id, original_name), ...]} lookup."""
    norm_map: dict[str, list[tuple[int, str]]] = {}
    if normalized_choices is not None:
        for entity_id, norm_name in normalized_choices.items():
            original_name = choices.get(entity_id, norm_name)
            norm_map.setdefault(norm_name, []).append((entity_id, original_name))
    else:
        for entity_id, name in choices.items():
            norm_name = latinize(name)
            norm_map.setdefault(norm_name, []).append((entity_id, name))
    return norm_map


def _fuzzy_resolve(
    query: str,
    choices: dict[int, str],
    cache: EntityCache | None = None,
    *,
    normalized_choices: dict[int, str] | None = None,
) -> ResolveResult:
    """Fuzzy match query against choices in normalized (Latin) space.

    - Normalizes both query and choices via latinize()
    - Exact normalized match with multi-word query → Resolved
    - Single-word query with ≥2 hits → always Candidates (even if exact)
    - Otherwise exact normalized match → Resolved
    - No exact → all hits ≥60 as Candidates
    """
    norm_map = _build_norm_map(choices, normalized_choices)
    norm_name_to_id: dict[str, int] = {
        norm_name: entries[0][0] for norm_name, entries in norm_map.items()
    }
    norm_query = latinize(query)

    hits = process.extract(
        norm_query,
        norm_name_to_id.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=CANDIDATE_THRESHOLD,
        limit=None,
    )

    if not hits:
        return NotFound(query=query)

    is_single_word = " " not in query.strip()
    exact_entity_id: int | None = None
    exact_display_name: str | None = None
    for norm_name, _score, _idx in hits:
        if norm_name == norm_query:
            entries = norm_map[norm_name]
            exact_entity_id = entries[0][0]
            exact_display_name = entries[0][1]
            break

    if is_single_word and len(hits) >= 2:
        matches = _build_matches(hits, norm_map, cache, exact_first_id=exact_entity_id)
        return Candidates(query=query, matches=matches)

    if exact_entity_id is not None:
        return Resolved(entity_id=exact_entity_id, display_name=exact_display_name)  # type: ignore[arg-type]

    matches = _build_matches(hits, norm_map, cache)
    return Candidates(query=query, matches=matches)


def _build_matches(
    hits: list[tuple[str, float, int]],
    norm_map: dict[str, list[tuple[int, str]]],
    cache: EntityCache | None,
    exact_first_id: int | None = None,
) -> list[dict]:
    """Build match dicts from rapidfuzz hits, optionally putting exact_first_id first."""
    matches: list[dict] = []
    seen_ids: set[int] = set()

    if exact_first_id is not None:
        for norm_name, score, _idx in hits:
            for entity_id, original_name in norm_map.get(norm_name, []):
                if entity_id == exact_first_id and entity_id not in seen_ids:
                    seen_ids.add(entity_id)
                    matches.append(_make_match_info(entity_id, original_name, int(score), cache))

    for norm_name, score, _idx in hits:
        for entity_id, original_name in norm_map.get(norm_name, []):
            if entity_id in seen_ids:
                continue
            seen_ids.add(entity_id)
            matches.append(_make_match_info(entity_id, original_name, int(score), cache))

    return matches


def _make_match_info(entity_id: int, display_name: str, score: int, cache: EntityCache | None) -> dict:
    entity_info: dict = {
        "entity_id": entity_id,
        "display_name": display_name,
        "score": score,
        "username": None,
        "entity_type": None,
    }
    if cache:
        try:
            cached = cache.get(entity_id, ttl_seconds=300)
            if cached:
                entity_info["username"] = cached.get("username")
                entity_info["entity_type"] = cached.get("type")
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected cache error in fuzzy resolve for entity_id=%r", entity_id, exc_info=True)
    return entity_info


def resolve(
    query: str,
    choices: dict[int, str],
    cache: EntityCache | None = None,
    *,
    normalized_choices: dict[int, str] | None = None,
) -> ResolveResult:
    """Resolve query to entity using normalized matching (pure/sync).

    Case 1: Numeric ID query → Resolved/NotFound by id
    Case 2: @username query → lookup in cache, Resolved/NotFound (requires cache)
    Case 3-5: Fuzzy matching in latinized space with single-word caution
    """
    entity_id = _parse_numeric_query(query)
    if entity_id is not None:
        if entity_id in choices:
            return Resolved(entity_id=entity_id, display_name=choices[entity_id])
        return NotFound(query=query)

    if query.startswith("@") and cache:
        username_query = query[1:]
        try:
            result = cache.get_by_username(username_query)
            if result:
                entity_id, name = result
                return Resolved(entity_id=entity_id, display_name=name)
        except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
            pass
        except Exception:
            logger.warning("unexpected cache error in @username resolve for query=%r", query, exc_info=True)
        return NotFound(query=query)

    if query.startswith("@"):
        return NotFound(query=query)

    return _fuzzy_resolve(query, choices, cache, normalized_choices=normalized_choices)


def cache_dialog_entry(cache: EntityCache, dialog: object) -> None:
    """Persist one Telethon dialog in the local entity cache."""
    dialog_id = getattr(dialog, "id", None)
    dialog_name = getattr(dialog, "name", None)
    if not isinstance(dialog_id, int) or not isinstance(dialog_name, str):
        return

    if getattr(dialog, "is_user", False):
        dialog_type = "user"
    elif getattr(dialog, "is_group", False):
        # Supergroups (megagroups) have is_group=True — always "group" for cache
        dialog_type = "group"
    elif getattr(dialog, "is_channel", False):
        dialog_type = "channel"
    else:
        dialog_type = "unknown"

    entity = getattr(dialog, "entity", None)
    username = getattr(entity, "username", None) if entity is not None else None
    cache.upsert(dialog_id, dialog_type, dialog_name, username)


async def _resolve_by_username(
    username: str,
    cache: EntityCache,
    client_factory: Callable[[], AsyncContextManager],
) -> Resolved | NotFound:
    """Resolve @username via cache, falling back to Telegram API."""
    try:
        cached = cache.get_by_username(username)
        if cached:
            entity_id, name = cached
            return Resolved(entity_id=entity_id, display_name=name)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError):
        pass

    try:
        async with client_factory() as client:
            entity = await client.get_entity(f"@{username}")
            entity_id = getattr(entity, "id", None)
            if entity_id is None:
                return NotFound(query=f"@{username}")

            first = getattr(entity, "first_name", None) or ""
            last = getattr(entity, "last_name", None) or ""
            title = getattr(entity, "title", None) or ""
            display_name = title or f"{first} {last}".strip() or username

            from telethon.tl.types import Channel, Chat
            if isinstance(entity, Channel):
                entity_type = "channel"
            elif isinstance(entity, Chat):
                entity_type = "group"
            else:
                entity_type = "user"

            uname = getattr(entity, "username", None)
            cache.upsert(entity_id, entity_type, display_name, uname)
            return Resolved(entity_id=entity_id, display_name=display_name)
    except Exception as exc:
        logger.warning("username_api_resolve_failed username=%r error=%s", username, exc, exc_info=True)
        return NotFound(query=f"@{username}")


async def _warmup_dialogs(
    cache: EntityCache,
    client_factory: Callable[[], AsyncContextManager],
) -> dict[int, str]:
    """Iterate all dialogs and refresh cache. Returns live {id: name} map."""
    live_choices: dict[int, str] = {}
    async with client_factory() as client:
        async for dialog in client.iter_dialogs(archived=None, ignore_pinned=False):
            dialog_id = getattr(dialog, "id", None)
            dialog_name = getattr(dialog, "name", None)
            if isinstance(dialog_id, int) and isinstance(dialog_name, str):
                live_choices[dialog_id] = dialog_name
            try:
                cache_dialog_entry(cache, dialog)
            except sqlite3.Error as cache_exc:
                logger.warning(
                    "dialog_cache_refresh_failed dialog_id=%r error=%s",
                    dialog_id,
                    cache_exc,
                )
    return live_choices


async def resolve_dialog(
    query: str,
    cache: EntityCache,
    client_factory: Callable[[], AsyncContextManager],
) -> ResolveResult:
    """Resolve any dialog query: numeric ID, @username, t.me link, or fuzzy name.

    Resolution cascade:
    1. t.me link → parse username + optional message_id, resolve via @username path
    2. Numeric ID → cache lookup
    3. @username → cache lookup → Telegram API fallback (get_entity)
    4. Fuzzy name → cache → warmup (iter_dialogs) → retry
    """
    from .cache import USER_TTL, GROUP_TTL

    # 1. t.me link
    parsed_link = _parse_tme_link(query)
    if parsed_link is not None:
        username, msg_id = parsed_link
        result = await _resolve_by_username(username, cache, client_factory)
        if isinstance(result, Resolved):
            return ResolvedWithMessage(
                entity_id=result.entity_id,
                display_name=result.display_name,
                message_id=msg_id,
            )
        return result

    # 2-3. @username with API fallback
    if query.startswith("@"):
        return await _resolve_by_username(query[1:], cache, client_factory)

    # 4. Numeric ID or fuzzy name — try cache first
    choices = cache.all_names_with_ttl(USER_TTL, GROUP_TTL)
    normalized = cache.all_names_normalized_with_ttl(USER_TTL, GROUP_TTL)
    result = resolve(query, choices, cache, normalized_choices=normalized)
    if not isinstance(result, NotFound):
        return result

    logger.debug("dialog_resolve_warmup_starting query=%r", query)
    try:
        live_choices = await _warmup_dialogs(cache, client_factory)
    except Exception as exc:
        logger.warning("dialog_resolve_warmup_failed query=%r error=%s", query, exc, exc_info=True)
        return result

    choices = cache.all_names_with_ttl(USER_TTL, GROUP_TTL)
    normalized = cache.all_names_normalized_with_ttl(USER_TTL, GROUP_TTL)
    refreshed_result = resolve(query, choices, cache, normalized_choices=normalized)
    if not isinstance(refreshed_result, NotFound):
        return refreshed_result

    return resolve(query, live_choices, cache)
