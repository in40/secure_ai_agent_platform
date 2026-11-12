"""EPA access token для вызовов vLLM через Tyk (client_credentials, общий EPA__BASE_URL)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import TypeVar
from urllib.parse import urljoin

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict

from _vllm_tyk_lib.tyk_settings import VllmTykSettings

_T = TypeVar("_T")

_EPA_TOKEN_ENDPOINT_PATH = "passport/oauth2/token"
_EPA_GRANT_TYPE = "client_credentials"
_EPA_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}
_EPA_REQUEST_TIMEOUT_SECONDS = 10
_EPA_TOKEN_REFRESH_LAG_SECONDS = 10

_local_epa_token_lock = asyncio.Lock()
_local_epa_tokens: dict[str, _LocalEpaToken] = {}


class _LocalEpaToken(BaseModel):
    """Локально закэшированный EPA-токен процесса (кэш с упреждающим обновлением)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    access_token: str
    expires_at: datetime

    def is_valid(self) -> bool:
        """Проверяет, можно ли ещё использовать локальный EPA-токен."""
        return datetime.now(UTC) < self.expires_at


def mask_sensitive_value(value: object | None) -> str:
    """Маскирует 80% середины значения, оставляя диагностические края (токены в логах)."""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "•" * len(text)
    masked_len = max(1, round(len(text) * 0.8))
    visible_len = len(text) - masked_len
    left_len = visible_len // 2
    right_len = visible_len - left_len
    return f"{text[:left_len]}{'•' * masked_len}{text[-right_len:]}"


def _secret_to_string(value: object | None) -> str:
    """Возвращает строковое значение секрета."""
    if value is None:
        return ""
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _strip_bearer_prefix(token: str) -> str:
    """Удаляет Bearer-префикс из токена перед передачей в OpenAI-compatible клиент."""
    stripped = token.strip()
    if stripped.lower().startswith("bearer "):
        return stripped[7:].strip()
    return stripped


def _token_endpoint(base_url: str) -> str:
    """Формирует endpoint получения EPA-токена по EPA__BASE_URL."""
    if not base_url.strip():
        msg = "VLLM_EPA_AUTH=true: задайте EPA__BASE_URL"
        raise ValueError(msg)
    return urljoin(f"{base_url.strip().rstrip('/')}/", _EPA_TOKEN_ENDPOINT_PATH)


def _parse_token_response(payload: object) -> tuple[str, int]:
    """Извлекает access_token и expires_in из ответа EPA."""
    if not isinstance(payload, dict):
        msg = "EPA вернула не JSON-объект"
        raise ValueError(msg)
    access_token_raw = payload.get("access_token")
    expires_in_raw = payload.get("expires_in")
    if not isinstance(access_token_raw, str) or not access_token_raw.strip():
        msg = "EPA не вернула строковый access_token"
        raise ValueError(msg)
    if not isinstance(expires_in_raw, int) or expires_in_raw <= 0:
        msg = "EPA не вернула положительный expires_in"
        raise ValueError(msg)
    return _strip_bearer_prefix(access_token_raw), expires_in_raw


def _jwt_claims_for_log(access_token: str) -> dict[str, object]:
    """Извлекает безопасные JWT-claims для диагностики без проверки подписи."""
    token = _strip_bearer_prefix(access_token)
    parts = token.split(".")
    if len(parts) < 2:
        return {"token_format": "opaque"}
    padded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded_payload.encode("ascii")).decode("utf-8")
        parsed: object = json.loads(decoded)
    except (UnicodeDecodeError, ValueError) as exc:
        return {"token_format": "jwt", "token_claims_error": str(exc)}
    if not isinstance(parsed, dict):
        return {"token_format": "jwt", "token_payload_type": type(parsed).__name__}
    claims: dict[str, object] = {"token_format": "jwt"}
    for claim_name in ("iss", "aud", "scope", "exp", "iat"):
        if claim_name in parsed:
            claims[f"token_claim_{claim_name}"] = parsed[claim_name]
    for claim_name in ("sub", "client_id", "azp", "preferred_username"):
        if claim_name in parsed:
            claims[f"token_claim_{claim_name}"] = mask_sensitive_value(parsed[claim_name])
    return claims


def _resolve_epa_config(settings: VllmTykSettings) -> tuple[str, str, str, bool]:
    """Собирает EPA-настройки: общий EPA__BASE_URL + EPA__CLIENT_ID/SECRET или DEBUG-заголовок."""
    try:
        from epa_auth.core.epa.config import EpaConfig as _EpaConfig
    except ImportError:
        _EpaConfig = None  # noqa: N806

    if _EpaConfig is not None:
        config = _EpaConfig()
        base_url = str(config.BASE_URL or "")
        client_id = str(config.CLIENT_ID or "")
        client_secret = _secret_to_string(config.CLIENT_SECRET)
        verify_ssl = bool(config.VERIFY_SSL)
        debug_header = _secret_to_string(config.DEBUG_AUTH_HEADER)
        if debug_header:
            return base_url, client_id, client_secret, verify_ssl
        if not base_url or not client_id or not client_secret:
            msg = (
                "VLLM_EPA_AUTH=true: задайте EPA__BASE_URL, EPA__CLIENT_ID и EPA__CLIENT_SECRET "
                "или EPA__DEBUG_AUTH_HEADER для локальной отладки"
            )
            raise ValueError(msg)
        return base_url, client_id, client_secret, verify_ssl

    # Fallback: читаем EPA__* напрямую из окружения без пакета epa_auth
    import os

    base_url = os.environ.get("EPA__BASE_URL", "").strip()
    client_id = os.environ.get("EPA__CLIENT_ID", "").strip()
    client_secret = os.environ.get("EPA__CLIENT_SECRET", "").strip()
    verify_ssl = os.environ.get("EPA__VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
    debug_header = os.environ.get("EPA__DEBUG_AUTH_HEADER", "").strip()
    if debug_header:
        return base_url, client_id, client_secret, verify_ssl
    if not base_url or not client_id or not client_secret:
        msg = (
            "VLLM_EPA_AUTH=true: задайте EPA__BASE_URL, EPA__CLIENT_ID и EPA__CLIENT_SECRET "
            "или EPA__DEBUG_AUTH_HEADER для локальной отладки"
        )
        raise ValueError(msg)
    return base_url, client_id, client_secret, verify_ssl


def _safe_token_ttl(expires_in: int) -> int:
    """Возвращает TTL локального кэша с упреждающим обновлением токена."""
    return max(expires_in - _EPA_TOKEN_REFRESH_LAG_SECONDS, 0)


def _epa_debug_header() -> str:
    """Возвращает EPA__DEBUG_AUTH_HEADER (только для локальной отладки)."""
    import os

    return os.environ.get("EPA__DEBUG_AUTH_HEADER", "").strip()


def vllm_uses_epa_auth(settings: VllmTykSettings) -> bool:
    """True, если VLLM_API_BASE должен ходить через EPA/Tyk."""
    return settings.vllm_epa_auth


async def _fetch_new_epa_access_token(
    *,
    base_url: str,
    client_id: str,
    client_secret: str,
    verify_ssl: bool,
) -> str:
    """Напрямую запрашивает EPA-токен (client_credentials) и возвращает его без Bearer-префикса."""
    token_url = _token_endpoint(base_url)
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": _EPA_GRANT_TYPE,
    }
    async with httpx.AsyncClient(timeout=_EPA_REQUEST_TIMEOUT_SECONDS, verify=verify_ssl) as client:
        response = await client.post(token_url, data=data, headers=_EPA_HEADERS)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.error(
            "EPA: прямой запрос токена завершился ошибкой",
            token_endpoint=token_url,
            http_status=response.status_code,
            error_type=type(exc).__name__,
        )
        raise
    access_token, expires_in = _parse_token_response(response.json())
    _local_epa_tokens["vllm"] = _LocalEpaToken(
        access_token=access_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=_safe_token_ttl(expires_in)),
    )
    logger.info(
        "EPA-токен получен для vLLM",
        token_endpoint=token_url,
        authorization=f"Bearer {mask_sensitive_value(access_token)}",
        expires_in=expires_in,
        **_jwt_claims_for_log(access_token),
    )
    return access_token


async def fetch_vllm_epa_access_token(settings: VllmTykSettings) -> str:
    """Возвращает EPA access_token без префикса Bearer (кэш в памяти процесса)."""
    if not vllm_uses_epa_auth(settings):
        msg = "fetch_vllm_epa_access_token вызван при VLLM_EPA_AUTH=false"
        raise ValueError(msg)
    debug_header = _epa_debug_header()
    if debug_header:
        return _strip_bearer_prefix(debug_header)
    cached = _local_epa_tokens.get("vllm")
    if cached is not None and cached.is_valid():
        return cached.access_token
    async with _local_epa_token_lock:
        cached = _local_epa_tokens.get("vllm")
        if cached is not None and cached.is_valid():
            return cached.access_token
        base_url, client_id, client_secret, verify_ssl = _resolve_epa_config(settings)
        return await _fetch_new_epa_access_token(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            verify_ssl=verify_ssl,
        )


def _run_async_from_sync(coro: Coroutine[object, object, _T]) -> _T:
    """Выполняет coroutine из sync-кода, даже если текущий поток уже держит event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: list[_T] = []
    errors: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=run_in_thread)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def fetch_vllm_epa_access_token_sync(settings: VllmTykSettings) -> str:
    """Синхронная обёртка получения EPA access_token (для build_model)."""
    return _run_async_from_sync(fetch_vllm_epa_access_token(settings))


def epa_bearer_headers(access_token: str) -> dict[str, str]:
    """Заголовок Authorization для HTTP-вызовов к Tyk."""
    normalized_token = _strip_bearer_prefix(access_token)
    return {"Authorization": f"Bearer {normalized_token}"}


def chat_openai_epa_kwargs(
    settings: VllmTykSettings,
    access_token: str,
    http_client: httpx.Client | None = None,
    http_async_client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    """Дополнительные kwargs ChatOpenAI для банковского контура (EPA/Tyk)."""
    normalized_token = _strip_bearer_prefix(access_token)
    kwargs: dict[str, object] = {
        "api_key": normalized_token,
        "default_headers": epa_bearer_headers(normalized_token),
    }
    if http_client is not None:
        kwargs["http_client"] = http_client
    if http_async_client is not None:
        kwargs["http_async_client"] = http_async_client
    return kwargs
