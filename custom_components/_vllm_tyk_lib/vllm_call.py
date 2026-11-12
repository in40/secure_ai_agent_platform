"""Прямой HTTP-вызов vLLM /chat/completions через Tyk с EPA, retry и очисткой reasoning."""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from _vllm_tyk_lib import rate_limit
from _vllm_tyk_lib.epa import (
    epa_bearer_headers,
    fetch_vllm_epa_access_token,
    fetch_vllm_epa_access_token_sync,
    vllm_uses_epa_auth,
)
from _vllm_tyk_lib.tyk_settings import VllmTykSettings
from _vllm_tyk_lib.vllm_async import ainvoke_vllm_chat_http_async_api, invoke_vllm_chat_http_async_api

_T = TypeVar("_T")

_RETRYABLE_HTTP_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
_RETRY_BACKOFF_CAP_SECONDS = 8.0

# Qwen3: суффикс /no_think в user-сообщении отключает thinking в chat template.
_NO_THINK_TOKEN = "/no_think"
_THINK_BLOCK = re.compile(
    r"<\s*(?:think|thinking|reasoning|analysis)\b\s*>[\s\S]*?"
    r"<\s*/\s*(?:think|thinking|reasoning|analysis)\b\s*>",
    re.IGNORECASE,
)
_REDACTED_BLOCK = re.compile(r"<\s*redacted_reasoning\s*>[\s\S]*?<\s*/\s*redacted_reasoning\s*>", re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"<\s*(?:think|thinking|reasoning|analysis)\b\s*>[\s\S]*$", re.IGNORECASE)


class VllmTykChatRequest(BaseModel):
    """Запрос к vLLM /chat/completions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_message: str = Field(min_length=1)
    system_message: str = ""
    max_tokens: int = Field(default=4000, ge=1)
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    enable_thinking: bool = False
    model_name: str = ""


def apply_no_think_prompt(text: str) -> str:
    """Добавляет /no_think к user-сообщению Qwen3, чтобы ответ попал в content."""
    stripped = text.rstrip()
    if not stripped:
        return text
    if _NO_THINK_TOKEN in stripped:
        return text
    return f"{stripped} {_NO_THINK_TOKEN}"


def strip_llm_thinking(text: str) -> str:
    """Убирает блоки reasoning/thinking, включая обрезанные по max_tokens.

    Несколько удалений схлопываются в один пробел, чтобы не оставалось
    двойных пробелов после вырезанного блока.
    """
    cleaned = _THINK_BLOCK.sub(" ", text)
    cleaned = _REDACTED_BLOCK.sub(" ", cleaned)
    cleaned = _UNCLOSED_THINK.sub(" ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _resolve_model_name(settings: VllmTykSettings, request_model: str) -> str:
    """Модель: из окружения VLLM_MODEL_NAME, иначе из запроса компонента."""
    if settings.vllm_model_name:
        return settings.vllm_model_name
    if request_model.strip():
        return request_model.strip()
    msg = "VLLM_MODEL_NAME не задан и model_name в узле пуст"
    raise ValueError(msg)


def _resolve_api_base(settings: VllmTykSettings) -> str:
    """Базовый URL: VLLM_API_BASE из окружения (обязателен для Tyk-контура)."""
    api_base = settings.vllm_api_base.strip().rstrip("/")
    if not api_base:
        msg = "VLLM_API_BASE не задан"
        raise ValueError(msg)
    return api_base


def _build_headers(settings: VllmTykSettings, epa_token: str | None) -> dict[str, str]:
    """Headers запроса: EPA Bearer (при VLLM_EPA_AUTH) или VLLM_API_KEY."""
    headers = {"Content-Type": "application/json"}
    if vllm_uses_epa_auth(settings):
        token = epa_token or fetch_vllm_epa_access_token_sync(settings)
        headers.update(epa_bearer_headers(token))
        return headers
    api_key = settings.vllm_api_key.get_secret_value()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


async def _build_headers_async(settings: VllmTykSettings, epa_token: str | None) -> dict[str, str]:
    """Headers для async-вызова без sync-моста."""
    headers = {"Content-Type": "application/json"}
    if vllm_uses_epa_auth(settings):
        token = epa_token or await fetch_vllm_epa_access_token(settings)
        headers.update(epa_bearer_headers(token))
        return headers
    api_key = settings.vllm_api_key.get_secret_value()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _build_payload(settings: VllmTykSettings, request: VllmTykChatRequest) -> dict[str, object]:
    """Payload /chat/completions; enable_thinking=False по умолчанию (Qwen3)."""
    messages: list[dict[str, str]] = []
    if request.system_message.strip():
        messages.append({"role": "system", "content": request.system_message})
    user_content = apply_no_think_prompt(request.user_message)
    messages.append({"role": "user", "content": user_content})
    payload: dict[str, object] = {
        "model": _resolve_model_name(settings, request.model_name),
        "messages": messages,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    chat_template_kwargs: dict[str, bool] = {"enable_thinking": request.enable_thinking}
    payload["chat_template_kwargs"] = chat_template_kwargs
    return payload


def _extract_message_text(message: object) -> str:
    """Извлекает текст из message ответа vLLM OpenAI API."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    reasoning = message.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()
    return ""


def _parse_chat_response(data: object, *, model_name: str, allow_empty: bool) -> str:
    """Парсит ответ /chat/completions и очищает reasoning-блоки.

    Пустой ответ до очистки (allow_empty) — сигнал «страница нерелевантна»:
    чистка reasoning не применяется, чтобы не срезать легитимный текст.
    """
    if not isinstance(data, dict):
        msg = "vLLM: некорректный JSON ответа"
        raise ValueError(msg)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        msg = "vLLM: пустой choices"
        raise ValueError(msg)
    first = choices[0]
    if not isinstance(first, dict):
        msg = "vLLM: некорректный формат choices[0]"
        raise ValueError(msg)
    raw_text = _extract_message_text(first.get("message"))
    if not raw_text:
        if allow_empty:
            logger.warning("vLLM вернул пустой ответ", model=model_name)
            return ""
        msg = "vLLM вернул пустой ответ"
        raise ValueError(msg)
    cleaned = strip_llm_thinking(raw_text)
    if not cleaned:
        if allow_empty:
            logger.warning("vLLM вернул пустой ответ после очистки reasoning-блоков", model=model_name)
            return ""
        msg = "vLLM вернул пустой ответ после очистки reasoning-блоков"
        raise ValueError(msg)
    return cleaned


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def is_retryable_vllm_error(exc: BaseException) -> bool:
    """True для транзиентных ошибок vLLM/Tyk: таймаут, сеть, 429 и 5xx."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    return _http_status(exc) in _RETRYABLE_HTTP_STATUS


def _retry_backoff_seconds(failed_attempt: int) -> float:
    return min(2.0 ** (failed_attempt - 1), _RETRY_BACKOFF_CAP_SECONDS)


def _log_vllm_retry(exc: BaseException, *, attempt: int, max_attempts: int) -> None:
    logger.warning(
        "vLLM: повтор вызова после ошибки",
        attempt=attempt,
        max_attempts=max_attempts,
        error_type=type(exc).__name__,
        http_status=_http_status(exc),
    )


def _once_sync(
    settings: VllmTykSettings,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
    model_name: str,
    allow_empty: bool,
) -> str:
    """Один sync POST /chat/completions через клиент с RPS-лимитом."""
    with rate_limit.create_rate_limit_http_client(settings, settings.vllm_ssl_verify) as client:
        response = client.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        return _parse_chat_response(response.json(), model_name=model_name, allow_empty=allow_empty)


def call_vllm_chat_http(
    settings: VllmTykSettings,
    request: VllmTykChatRequest,
    *,
    timeout_seconds: float | None = None,
    allow_empty: bool = False,
) -> str:
    """Синхронный вызов vLLM chat/completions с retry по VLLM_LLM_RETRY_ATTEMPTS.

    При VLLM_ASYNC_ENABLED=true — тикетный контур (POST /async + GET ?wait=n),
    обходящий лимит Tyk ~40 с.
    """
    model_name = _resolve_model_name(settings, request.model_name)
    url = f"{_resolve_api_base(settings)}/chat/completions"
    headers = _build_headers(settings, None)
    payload = _build_payload(settings, request)
    if settings.vllm_async_enabled:
        body = invoke_vllm_chat_http_async_api(
            settings,
            chat_url=url,
            headers=headers,
            payload=payload,
        )
        return _parse_chat_response(body, model_name=model_name, allow_empty=allow_empty)

    timeout = timeout_seconds if timeout_seconds is not None else settings.vllm_tyk_timeout_seconds

    def _call() -> str:
        return _once_sync(settings, url, headers, payload, timeout, model_name, allow_empty)

    return _with_retries_sync(settings, _call)


async def _once_async(
    settings: VllmTykSettings,
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
    model_name: str,
    allow_empty: bool,
) -> str:
    """Один async POST /chat/completions через клиент с RPS-лимитом."""
    client = rate_limit.create_rate_limit_http_async_client(settings, settings.vllm_ssl_verify)
    try:
        response = await client.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        return _parse_chat_response(response.json(), model_name=model_name, allow_empty=allow_empty)
    finally:
        await client.aclose()


async def ainvoke_vllm_chat_http(
    settings: VllmTykSettings,
    request: VllmTykChatRequest,
    *,
    timeout_seconds: float | None = None,
    allow_empty: bool = False,
) -> str:
    """Асинхронный вызов vLLM chat/completions с retry по VLLM_LLM_RETRY_ATTEMPTS.

    При VLLM_ASYNC_ENABLED=true — тикетный контур (POST /async + GET ?wait=n),
    обходящий лимит Tyk ~40 с.
    """
    model_name = _resolve_model_name(settings, request.model_name)
    url = f"{_resolve_api_base(settings)}/chat/completions"
    headers = await _build_headers_async(settings, None)
    payload = _build_payload(settings, request)
    if settings.vllm_async_enabled:
        body = await ainvoke_vllm_chat_http_async_api(
            settings,
            chat_url=url,
            headers=headers,
            payload=payload,
        )
        return _parse_chat_response(body, model_name=model_name, allow_empty=allow_empty)

    timeout = timeout_seconds if timeout_seconds is not None else settings.vllm_tyk_timeout_seconds

    async def _call() -> str:
        return await _once_async(settings, url, headers, payload, timeout, model_name, allow_empty)

    return await _with_retries_async(settings, _call)


def _with_retries_sync(settings: VllmTykSettings, call: Callable[[], str]) -> str:
    """Повторяет sync-вызов vLLM согласно VLLM_LLM_RETRY_ATTEMPTS."""
    max_attempts = settings.vllm_llm_retry_attempts + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except Exception as exc:
            if not (attempt < max_attempts and is_retryable_vllm_error(exc)):
                raise
            _log_vllm_retry(exc, attempt=attempt, max_attempts=max_attempts)
            if not rate_limit.vllm_rate_limit_enabled(settings):
                time.sleep(_retry_backoff_seconds(attempt))
    msg = "vLLM: исчерпаны попытки без исключения"
    raise RuntimeError(msg)


async def _with_retries_async(settings: VllmTykSettings, call: Callable[[], Awaitable[str]]) -> str:
    """Повторяет async-вызов vLLM согласно VLLM_LLM_RETRY_ATTEMPTS."""
    import asyncio

    max_attempts = settings.vllm_llm_retry_attempts + 1
    for attempt in range(1, max_attempts + 1):
        try:
            return await call()
        except Exception as exc:
            if not (attempt < max_attempts and is_retryable_vllm_error(exc)):
                raise
            _log_vllm_retry(exc, attempt=attempt, max_attempts=max_attempts)
            if not rate_limit.vllm_rate_limit_enabled(settings):
                await asyncio.sleep(_retry_backoff_seconds(attempt))
    msg = "vLLM: исчерпаны попытки без исключения"
    raise RuntimeError(msg)
