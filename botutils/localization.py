"""Centralized per-guild translation for Discord message payloads.

Fate's cogs can continue authoring responses in English.  This module hooks the
two Discord.py transports used for bot messages and interaction responses,
translates only user-visible payload fields, and leaves protocol values such as
custom IDs, select values, mentions, URLs, and code untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import weakref
from collections import Counter
from collections.abc import Iterable
from contextlib import suppress
from copy import deepcopy
from time import monotonic
from typing import Any, MutableMapping, Optional

import aiohttp
from discord.http import HTTPClient
from discord.webhook.async_ import AsyncWebhookAdapter

LANGUAGES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "pt": "Português",
    "ar": "العربية",
    "de": "Deutsch",
    "ru": "Русский",
    "ko": "한국어",
    "it": "Italiano",
    "pl": "Polski",
    "ja": "日本語",
    "sv": "Svenska",
    "uk": "Українська",
    "nl": "Belgian (Dutch)",
}
LANGUAGE_EMOJIS = {
    "en": "🇬🇧",
    "es": "🇪🇸",
    "fr": "🇫🇷",
    "zh-CN": "🇨🇳",
    "zh-TW": "🇹🇼",
    "pt": "🇵🇹",
    "ar": "🇸🇦",
    "de": "🇩🇪",
    "ru": "🇷🇺",
    "ko": "🇰🇷",
    "it": "🇮🇹",
    "pl": "🇵🇱",
    "ja": "🇯🇵",
    "sv": "🇸🇪",
    "uk": "🇺🇦",
    "nl": "🇧🇪",
}
DEFAULT_LANGUAGE = "en"
DEFAULT_TRANSLATION_ENDPOINT = (
    "https://translate.googleapis.com/translate_a/single"
)

_PROTECTED_TEXT = re.compile(
    r"```[\s\S]*?```"
    r"|`[^`\n]*`"
    r"|https?://[^\s<>]+"
    r"|</[^:>]+:\d+>"
    r"|<(?:@!?|@&|#)\d+>"
    r"|<a?:[A-Za-z0-9_]+:\d+>"
    r"|<t:\d+(?::[A-Za-z])?>"
)
_HAS_WORDS = re.compile(r"[A-Za-z]")
_INTERACTION_TTL_SECONDS = 15 * 60

_HTTP_MANAGERS: "weakref.WeakKeyDictionary[HTTPClient, LocalizationManager]" = (
    weakref.WeakKeyDictionary()
)
_INTERACTION_CONTEXTS: dict[
    str, tuple["weakref.ReferenceType[LocalizationManager]", int, float]
] = {}
_HOOKS_INSTALLED = False


def normalize_language(value: Any) -> str:
    """Return a supported language code, falling back to English."""
    raw_language = str(value or DEFAULT_LANGUAGE).strip()
    language = _language_from_locale(raw_language)
    return language or DEFAULT_LANGUAGE


def _language_from_locale(value: Any) -> Optional[str]:
    """Map a Discord locale to a supported translation language."""
    raw_locale = getattr(value, "value", value)
    locale = str(raw_locale or "").strip().replace("_", "-").lower()
    if not locale:
        return None

    aliases = {
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "zh-sg": "zh-CN",
        "zh-tw": "zh-TW",
        "zh-hk": "zh-TW",
    }
    if locale in aliases:
        return aliases[locale]

    base_language = locale.split("-", 1)[0]
    return base_language if base_language in LANGUAGES else None


def ranked_language_codes(guilds: Iterable[Any]) -> list[str]:
    """Order languages by preferred locale across every connected guild."""
    counts: Counter[str] = Counter()
    for guild in guilds:
        language = _language_from_locale(getattr(guild, "preferred_locale", None))
        if language is not None:
            counts[language] += 1

    fallback_order = {language: index for index, language in enumerate(LANGUAGES)}
    return sorted(
        LANGUAGES,
        key=lambda language: (-counts[language], fallback_order[language]),
    )


def _cache_key(language: str, source: str) -> str:
    digest = hashlib.sha256(f"{language}\0{source}".encode("utf-8")).hexdigest()
    return f"{language}:{digest}"


def _protect_text(source: str) -> tuple[str, dict[str, str]]:
    """Replace Discord formatting targets that a translator must not alter."""
    protected: dict[str, str] = {}
    prefix = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10].upper()

    def replace(match: re.Match[str]) -> str:
        marker = f"__FATE_NT_{prefix}_{len(protected)}__"
        protected[marker] = match.group(0)
        return marker

    return _PROTECTED_TEXT.sub(replace, source), protected


def _restore_text(translated: str, protected: dict[str, str]) -> Optional[str]:
    """Restore protected fragments or reject a damaged translation."""
    for marker, original in protected.items():
        if marker not in translated:
            return None
        translated = translated.replace(marker, original)
    return translated


def _has_translatable_text(source: str) -> bool:
    return bool(_HAS_WORDS.search(_PROTECTED_TEXT.sub("", source)))


def _add_slot(slots: list[tuple[dict, str, str]], parent: Any, key: str) -> None:
    if not isinstance(parent, dict):
        return
    value = parent.get(key)
    if isinstance(value, str) and value.strip() and _HAS_WORDS.search(value):
        slots.append((parent, key, value))


def _collect_embed(slots: list[tuple[dict, str, str]], embed: Any) -> None:
    if not isinstance(embed, dict):
        return
    _add_slot(slots, embed, "title")
    _add_slot(slots, embed, "description")
    footer = embed.get("footer")
    _add_slot(slots, footer, "text")
    author = embed.get("author")
    _add_slot(slots, author, "name")
    for field in embed.get("fields", []):
        _add_slot(slots, field, "name")
        _add_slot(slots, field, "value")


def _collect_component(slots: list[tuple[dict, str, str]], component: Any) -> None:
    if not isinstance(component, dict):
        return
    for key in ("label", "placeholder", "description", "content"):
        _add_slot(slots, component, key)
    for option in component.get("options", []):
        _add_slot(slots, option, "label")
        _add_slot(slots, option, "description")
    nested = component.get("component")
    if isinstance(nested, dict):
        _collect_component(slots, nested)
    for child in component.get("components", []):
        _collect_component(slots, child)


def _collect_poll(slots: list[tuple[dict, str, str]], poll: Any) -> None:
    if not isinstance(poll, dict):
        return
    _add_slot(slots, poll.get("question"), "text")
    for answer in poll.get("answers", []):
        _add_slot(slots, answer.get("poll_media"), "text")


def _collect_message_payload(
    slots: list[tuple[dict, str, str]], payload: Any
) -> None:
    if not isinstance(payload, dict):
        return
    _add_slot(slots, payload, "content")
    for embed in payload.get("embeds", []):
        _collect_embed(slots, embed)
    for component in payload.get("components", []):
        _collect_component(slots, component)
    _collect_poll(slots, payload.get("poll"))


def _collect_payload_slots(payload: dict) -> list[tuple[dict, str, str]]:
    slots: list[tuple[dict, str, str]] = []
    callback_type = payload.get("type")
    callback_data = payload.get("data")
    if isinstance(callback_type, int) and isinstance(callback_data, dict):
        if callback_type == 8:  # Application command autocomplete
            for choice in callback_data.get("choices", []):
                _add_slot(slots, choice, "name")
        elif callback_type == 9:  # Modal
            _add_slot(slots, callback_data, "title")
            for component in callback_data.get("components", []):
                _collect_component(slots, component)
        else:
            _collect_message_payload(slots, callback_data)
    else:
        _collect_message_payload(slots, payload)
    return slots


class LocalizationManager:
    """Translate and persist exact outbound strings for a Fate instance."""

    def __init__(self, bot, *, cache: Optional[MutableMapping] = None):
        self.bot = bot
        translation_config = bot.config.get("translation", {})
        if not isinstance(translation_config, dict):
            translation_config = {}
        self.endpoint = str(
            translation_config.get("endpoint", DEFAULT_TRANSLATION_ENDPOINT)
        ).strip()
        try:
            timeout = float(translation_config.get("timeout_seconds", 1.8))
        except (TypeError, ValueError):
            timeout = 1.8
        self.timeout = min(max(timeout, 0.5), 8.0)
        self.cache = cache if cache is not None else bot.utils.cache(
            "translations", auto_sync=True
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._last_failure_log = 0.0

    def install(self) -> None:
        """Attach this manager to Fate's Discord HTTP transport."""
        global _HOOKS_INSTALLED
        _HTTP_MANAGERS[self.bot.http] = self
        if _HOOKS_INSTALLED:
            return
        HTTPClient.request = _localized_http_request  # type: ignore[method-assign]
        AsyncWebhookAdapter.request = _localized_webhook_request  # type: ignore[method-assign]
        _HOOKS_INSTALLED = True

    def register_interaction(self, interaction) -> None:
        """Remember a guild for Discord's token-only interaction webhooks."""
        guild_id = getattr(interaction, "guild_id", None)
        token = getattr(interaction, "token", None)
        if not guild_id or not token:
            return
        now = monotonic()
        _discard_expired_interactions(now)
        _INTERACTION_CONTEXTS[str(token)] = (
            weakref.ref(self),
            int(guild_id),
            now + _INTERACTION_TTL_SECONDS,
        )

    def language_for_guild(self, guild_id: Optional[int]) -> str:
        if not guild_id:
            return DEFAULT_LANGUAGE
        cog = self.bot.get_cog("Settings")
        if cog is None:
            return DEFAULT_LANGUAGE
        config = cog.config.get(int(guild_id), {})
        if not isinstance(config, dict):
            return DEFAULT_LANGUAGE
        return normalize_language(config.get("language"))

    def guild_for_route(self, route) -> Optional[int]:
        guild_id = getattr(route, "guild_id", None)
        if guild_id:
            return int(guild_id)
        channel_id = getattr(route, "channel_id", None)
        if not channel_id:
            return None
        channel = self.bot.get_channel(int(channel_id))
        guild = getattr(channel, "guild", None)
        return int(guild.id) if guild else None

    async def localize_payload(
        self, payload: Optional[dict], guild_id: Optional[int]
    ) -> Optional[dict]:
        language = self.language_for_guild(guild_id)
        if language == DEFAULT_LANGUAGE or not isinstance(payload, dict):
            return payload
        localized = deepcopy(payload)
        slots = _collect_payload_slots(localized)
        if not slots:
            return localized
        sources = [source for _parent, _key, source in slots]
        translations = await self.translate_many(sources, language)
        for (parent, key, _source), translated in zip(slots, translations):
            parent[key] = translated
        return localized

    async def localize_multipart(
        self, multipart: Any, guild_id: Optional[int]
    ) -> Any:
        language = self.language_for_guild(guild_id)
        if language == DEFAULT_LANGUAGE or not multipart:
            return multipart
        localized = [dict(part) for part in multipart]
        for part in localized:
            if part.get("name") != "payload_json":
                continue
            raw = part.get("value")
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            payload = await self.localize_payload(payload, guild_id)
            part["value"] = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            )
        return localized

    async def translate_many(self, sources: list[str], language: str) -> list[str]:
        language = normalize_language(language)
        if language == DEFAULT_LANGUAGE:
            return list(sources)
        results: dict[str, str] = {}
        tasks: dict[str, asyncio.Task[str]] = {}
        owned_tasks: list[tuple[str, asyncio.Task[str]]] = []
        batch_tasks: list[asyncio.Task[list[Optional[str]]]] = []
        missing: list[tuple[str, str]] = []

        for source in dict.fromkeys(sources):
            if not source.strip() or not _has_translatable_text(source):
                results[source] = source
                continue
            key = _cache_key(language, source)
            cached = self._cached_translation(key, source, language)
            if cached is not None:
                results[source] = cached
                continue
            task = self._inflight.get(key)
            if task is not None:
                tasks[source] = task
            else:
                missing.append((key, source))

        # Keep provider requests small enough for its normal text limit while
        # batching the many labels and fields found in a Discord payload.
        batches: list[list[tuple[str, str]]] = []
        batch: list[tuple[str, str]] = []
        batch_size = 0
        for item in missing:
            size = len(item[1]) + 64
            if batch and batch_size + size > 4000:
                batches.append(batch)
                batch = []
                batch_size = 0
            batch.append(item)
            batch_size += size
        if batch:
            batches.append(batch)

        for items in batches:
            batch_tasks.append(
                asyncio.create_task(
                    self._request_translations(
                        [source for _key, source in items], language
                    )
                )
            )
            batch_task = batch_tasks[-1]
            for index, (key, source) in enumerate(items):
                task = asyncio.create_task(
                    self._cache_batch_result(
                        batch_task, index, key, source, language
                    )
                )
                self._inflight[key] = task
                tasks[source] = task
                owned_tasks.append((key, task))

        try:
            if tasks:
                await asyncio.gather(*tasks.values())
            results.update({source: task.result() for source, task in tasks.items()})
            return [results[source] for source in sources]
        finally:
            for _key, task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await asyncio.gather(
                    *(task for _key, task in owned_tasks), return_exceptions=True
                )
            for task in batch_tasks:
                if not task.done():
                    task.cancel()
            if batch_tasks:
                await asyncio.gather(*batch_tasks, return_exceptions=True)
            for key, task in owned_tasks:
                if self._inflight.get(key) is task:
                    self._inflight.pop(key, None)

    async def translate_text(self, source: str, language: str) -> str:
        language = normalize_language(language)
        if (
            language == DEFAULT_LANGUAGE
            or not source.strip()
            or not _has_translatable_text(source)
        ):
            return source

        key = _cache_key(language, source)
        cached = self._cached_translation(key, source, language)
        if cached is not None:
            return cached

        task = self._inflight.get(key)
        owns_task = task is None
        if task is None:
            task = asyncio.create_task(
                self._translate_and_cache(key, source, language)
            )
            self._inflight[key] = task
        try:
            return await task
        finally:
            if owns_task and self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _translate_and_cache(
        self, key: str, source: str, language: str
    ) -> str:
        translated = await self._request_translation(source, language)
        if translated is None:
            return source
        self.cache[key] = {
            "language": language,
            "source": source,
            "translation": translated,
        }
        return translated

    def _cached_translation(
        self, key: str, source: str, language: str
    ) -> Optional[str]:
        cached = self.cache.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("language") == language
            and cached.get("source") == source
            and isinstance(cached.get("translation"), str)
        ):
            return cached["translation"]
        return None

    async def _cache_batch_result(
        self,
        batch_task: "asyncio.Task[list[Optional[str]]]",
        index: int,
        key: str,
        source: str,
        language: str,
    ) -> str:
        translations = await batch_task
        translated = translations[index]
        if translated is None:
            return source
        self.cache[key] = {
            "language": language,
            "source": source,
            "translation": translated,
        }
        return translated

    async def _request_translation(
        self, source: str, language: str
    ) -> Optional[str]:
        return (await self._request_translations([source], language))[0]

    async def _request_translations(
        self, sources: list[str], language: str
    ) -> list[Optional[str]]:
        if not sources:
            return []
        protected_sources: list[str] = []
        protected_values: list[dict[str, str]] = []
        for source in sources:
            protected_source, protected = _protect_text(source)
            protected_sources.append(protected_source)
            protected_values.append(protected)

        batch_digest = hashlib.sha256(
            "\0".join(sources).encode("utf-8")
        ).hexdigest()[:10].upper()
        separators = [
            f"__FATE_SPLIT_{batch_digest}_{index}__"
            for index in range(len(sources) - 1)
        ]
        combined = protected_sources[0]
        for separator, source in zip(separators, protected_sources[1:]):
            combined += f"\n{separator}\n{source}"

        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                )
            params = {
                "client": "gtx",
                "sl": DEFAULT_LANGUAGE,
                "tl": language,
                "dt": "t",
                "q": combined,
            }
            async with self._session.post(self.endpoint, data=params) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
            segments = data[0] if isinstance(data, list) and data else None
            if not isinstance(segments, list):
                raise ValueError("translation response did not contain text")
            translated = "".join(
                str(segment[0])
                for segment in segments
                if isinstance(segment, list) and segment and segment[0] is not None
            )
            parts = [translated]
            for separator in separators:
                tail = parts.pop()
                split = tail.split(separator, 1)
                if len(split) != 2:
                    raise ValueError("translation changed a batch delimiter")
                parts.extend(split)
            if len(parts) != len(sources):
                raise ValueError("translation returned the wrong batch size")

            restored_values: list[Optional[str]] = []
            for index, (part, protected) in enumerate(
                zip(parts, protected_values)
            ):
                if index:
                    part = part.removeprefix("\n")
                if index < len(parts) - 1:
                    part = part.removesuffix("\n")
                restored = _restore_text(part, protected)
                if restored is None:
                    raise ValueError(
                        "translation changed a protected Discord value"
                    )
                restored_values.append(restored)
            return restored_values
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            self._log_failure(error)
            return [None] * len(sources)

    def _log_failure(self, error: Exception) -> None:
        now = monotonic()
        if now - self._last_failure_log < 300:
            return
        self._last_failure_log = now
        log = getattr(self.bot, "log", None)
        warning = getattr(log, "warning", None)
        if warning:
            warning(
                "Automatic translation is unavailable; sending English fallback "
                f"({type(error).__name__})."
            )

    async def close(self) -> None:
        """Flush persisted translations and release the translator session."""
        _HTTP_MANAGERS.pop(self.bot.http, None)
        for token, (manager_ref, _guild_id, _expires) in list(
            _INTERACTION_CONTEXTS.items()
        ):
            if manager_ref() is self:
                _INTERACTION_CONTEXTS.pop(token, None)
        if self._session and not self._session.closed:
            await self._session.close()
        task = getattr(self.cache, "task", None)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        flush = getattr(self.cache, "flush", None)
        if flush:
            await flush()


def _discard_expired_interactions(now: Optional[float] = None) -> None:
    now = monotonic() if now is None else now
    for token, (manager_ref, _guild_id, expires) in list(
        _INTERACTION_CONTEXTS.items()
    ):
        if expires <= now or manager_ref() is None:
            _INTERACTION_CONTEXTS.pop(token, None)


def _interaction_context(token: Any) -> tuple[Optional[LocalizationManager], Optional[int]]:
    if not token:
        return None, None
    now = monotonic()
    record = _INTERACTION_CONTEXTS.get(str(token))
    if not record:
        return None, None
    manager_ref, guild_id, expires = record
    manager = manager_ref()
    if manager is None or expires <= now:
        _INTERACTION_CONTEXTS.pop(str(token), None)
        return None, None
    return manager, guild_id


_ORIGINAL_HTTP_REQUEST = getattr(
    HTTPClient.request, "__fate_localization_original__", HTTPClient.request
)
_ORIGINAL_WEBHOOK_REQUEST = getattr(
    AsyncWebhookAdapter.request,
    "__fate_localization_original__",
    AsyncWebhookAdapter.request,
)


async def _localized_http_request(
    http,
    route,
    *,
    files=None,
    form=None,
    **kwargs,
):
    manager = _HTTP_MANAGERS.get(http)
    if manager is not None:
        guild_id = manager.guild_for_route(route)
        if guild_id:
            payload = kwargs.get("json")
            if isinstance(payload, dict):
                kwargs = dict(kwargs)
                kwargs["json"] = await manager.localize_payload(payload, guild_id)
            if form:
                form = await manager.localize_multipart(form, guild_id)
    return await _ORIGINAL_HTTP_REQUEST(
        http, route, files=files, form=form, **kwargs
    )


async def _localized_webhook_request(
    adapter,
    route,
    session,
    *,
    payload=None,
    multipart=None,
    **kwargs,
):
    manager, guild_id = _interaction_context(getattr(route, "webhook_token", None))
    if manager is not None and guild_id:
        if isinstance(payload, dict):
            payload = await manager.localize_payload(payload, guild_id)
        if multipart:
            multipart = await manager.localize_multipart(multipart, guild_id)
    return await _ORIGINAL_WEBHOOK_REQUEST(
        adapter,
        route,
        session,
        payload=payload,
        multipart=multipart,
        **kwargs,
    )


_localized_http_request.__fate_localization_original__ = _ORIGINAL_HTTP_REQUEST
_localized_webhook_request.__fate_localization_original__ = (
    _ORIGINAL_WEBHOOK_REQUEST
)
