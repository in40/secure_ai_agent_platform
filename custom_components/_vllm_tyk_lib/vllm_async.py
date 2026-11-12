"""Асинхронный контур vLLM: тикеты Tyk (POST /async + GET ?wait=n + DELETE).

Обходит лимит шлюза ~40 с на один HTTP: POST создаёт тикет, GET long-poll
ждёт готовность (не дольше остатка бюджета), DELETE отменяет тикет по таймауту.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter
from typing import Protocol
from urllib.parse import urlparse, urlunparse

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from _vllm_tyk_lib.rate_limit import create_rate_limit_http_async_client, create_rate_limit_http_client
from _vllm_tyk_lib.tyk_settings import VllmTykSettings

_TERMINAL_OK = "succeeded"
_TERMINAL_FAIL = frozenset({"failed", "cancelled", "expired"})
_POLL_GAP_SECONDS = 0.25
_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"

_async_semaphore: asyncio.Semaphore | None = None
_async_semaphore_limit = 0
_sync_semaphore: threading.BoundedSemaphore | None = None
_sync_semaphore_limit = 0
_semaphore_guard = threading.Lock()


class TicketError(BaseModel):
    """Ошибка тикета async LLM API."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    code: str = ""
    message: str = ""


class TicketHttpResponse(BaseModel):
    """Обёртка HTTP-ответа модели внутри тикета."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: int | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, object] | None = None


class TicketMetadata(BaseModel):
    """Метаданные тикета async LLM API."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    status: str = ""
    model_name: str = ""
    endpoint_path: str = ""
    endpoint_method: str = ""
    attempts: int = 0


class AsyncTicket(BaseModel):
    """Состояние тикета POST /async и GET /async/operations/{id}."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    id: str
    done: bool
    metadata: TicketMetadata
    response: TicketHttpResponse | None = None
    error: TicketError | None = None
    created_at: str | None = Field(default=None, validation_alias="createdAt")
    modified_at: str | None = Field(default=None, validation_alias="modifiedAt")


class SyncTicketClient(Protocol):
    """Минимальный HTTP-клиент для sync poll/delete тикета."""

    def get(self, url: str, **kwargs: object) -> httpx.Response: ...

    def delete(self, url: str, **kwargs: object) -> httpx.Response: ...

    def post(self, url: str, **kwargs: object) -> httpx.Response: ...


class AsyncTicketClient(Protocol):
    """Минимальный HTTP-клиент для async poll/delete тикета."""

    async def get(self, url: str, **kwargs: object) -> httpx.Response: ...

    async def delete(self, url: str, **kwargs: object) -> httpx.Response: ...

    async def post(self, url: str, **kwargs: object) -> httpx.Response: ...


def reset_vllm_async_state() -> None:
    """Сбрасывает семафоры concurrency (только тесты)."""
    global _async_semaphore, _async_semaphore_limit, _sync_semaphore, _sync_semaphore_limit
    with _semaphore_guard:
        _async_semaphore = None
        _async_semaphore_limit = 0
        _sync_semaphore = None
        _sync_semaphore_limit = 0


def _absolute_path(path: str) -> str:
    stripped = path.strip() or "/"
    return stripped if stripped.startswith("/") else f"/{stripped}"


def _split_gateway_v1(path: str) -> tuple[str, str]:
    """Делит path на корень шлюза до первого /v1 включительно и хвост после него.

    .../inferenceLLM/v1/qwen3-32b/v1/chat/... → (.../inferenceLLM/v1, /qwen3-32b/v1/chat/...)
    /vllm/v1/chat/completions → (/vllm/v1, /chat/completions)
    """
    normalized = path if path.startswith("/") else f"/{path}"
    normalized = normalized.rstrip("/") or "/"
    marker = "/v1/"
    idx = normalized.find(marker)
    if idx >= 0:
        return normalized[: idx + len("/v1")], normalized[idx + len("/v1") :]
    if normalized.endswith("/v1"):
        return normalized, ""
    return normalized, ""


def build_async_submit_url(chat_completions_url: str, *, prefix: str) -> str:
    """Вставляет /async после первого /v1 шлюза.

    .../v1/{model}/v1/chat/... → .../v1/async/{model}/v1/chat/...
    """
    parsed = urlparse(chat_completions_url)
    async_prefix = _absolute_path(prefix).rstrip("/")
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    path = path.rstrip("/") or "/"
    if not path.endswith(_CHAT_COMPLETIONS_SUFFIX):
        msg = "async LLM: URL не заканчивается на /chat/completions"
        raise ValueError(msg)
    root, suffix = _split_gateway_v1(path)
    if suffix == async_prefix or suffix.startswith(f"{async_prefix}/"):
        new_path = f"{root}{suffix}"
    else:
        new_path = f"{root}{async_prefix}{suffix}"
    return urlunparse(parsed._replace(path=new_path))


def build_async_operations_url(api_base: str, ticket_id: str, *, operations_path: str) -> str:
    """Собирает GET/DELETE URL тикета: {gateway}/v1/async/operations/{id} (без path модели)."""
    if not ticket_id.strip():
        msg = "Пустой id тикета async LLM"
        raise ValueError(msg)
    parsed = urlparse(api_base)
    ops = _absolute_path(operations_path).rstrip("/")
    base_path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    root, suffix = _split_gateway_v1(base_path)
    if suffix == ops or suffix.startswith(f"{ops}/"):
        new_path = f"{root}{suffix.rstrip('/')}/{ticket_id.strip()}"
    elif base_path.rstrip("/").endswith(ops):
        new_path = f"{base_path.rstrip('/')}/{ticket_id.strip()}"
    else:
        new_path = f"{root}{ops}/{ticket_id.strip()}"
    return urlunparse(parsed._replace(path=new_path, query="", fragment=""))


def parse_async_ticket(payload: object) -> AsyncTicket:
    """Разбирает JSON тикета; лишние поля спецификации игнорируются."""
    if not isinstance(payload, dict):
        msg = "async LLM: ответ тикета не JSON-объект"
        raise ValueError(msg)
    try:
        return AsyncTicket.model_validate(payload)
    except ValidationError as exc:
        msg = "async LLM: некорректная схема тикета"
        raise ValueError(msg) from exc


def _ticket_fail_message(ticket: AsyncTicket) -> str:
    if ticket.error is not None and ticket.error.message:
        return ticket.error.message
    return f"async LLM: тикет {ticket.metadata.status or 'failed'}"


def extract_chat_body(ticket: AsyncTicket) -> dict[str, object]:
    """Достаёт тело chat/completions из завершённого тикета."""
    status = ticket.metadata.status.lower()
    if status in _TERMINAL_FAIL or (ticket.done and ticket.error is not None and status != _TERMINAL_OK):
        raise RuntimeError(_ticket_fail_message(ticket))
    if not ticket.done or status not in {"", _TERMINAL_OK}:
        msg = f"async LLM: тикет ещё не succeeded (status={ticket.metadata.status!r})"
        raise ValueError(msg)
    if ticket.response is None or ticket.response.body is None:
        msg = "async LLM: тикет succeeded без response.body"
        raise ValueError(msg)
    return ticket.response.body


def _new_idempotency_key() -> str:
    return str(uuid.uuid4())


def _submit_headers(base_headers: dict[str, str]) -> dict[str, str]:
    headers = dict(base_headers)
    headers["Idempotency-Key"] = _new_idempotency_key()
    return headers


def _submit_payload(payload: dict[str, object]) -> dict[str, object]:
    body = dict(payload)
    body["stream"] = False
    return body


def _async_sem(limit: int) -> asyncio.Semaphore:
    global _async_semaphore, _async_semaphore_limit
    with _semaphore_guard:
        if _async_semaphore is None or _async_semaphore_limit != limit:
            _async_semaphore = asyncio.Semaphore(limit)
            _async_semaphore_limit = limit
        return _async_semaphore


def _sync_sem(limit: int) -> threading.BoundedSemaphore:
    global _sync_semaphore, _sync_semaphore_limit
    with _semaphore_guard:
        if _sync_semaphore is None or _sync_semaphore_limit != limit:
            _sync_semaphore = threading.BoundedSemaphore(limit)
            _sync_semaphore_limit = limit
        return _sync_semaphore


@contextmanager
def _sync_concurrency(limit: int) -> Iterator[None]:
    sem = _sync_sem(limit)
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _log_async_http(event: str, **fields: object) -> None:
    """Структурированный лог одного HTTP к async LLM API (POST/GET/DELETE)."""
    logger.info(event, **fields)


def _raise_if_http_error(response: httpx.Response) -> None:
    response.raise_for_status()


def _poll_timeout_seconds(wait_seconds: int) -> float:
    return float(wait_seconds) + 10.0


async def _cancel_ticket_async(client: AsyncTicketClient, operations_url: str, headers: dict[str, str]) -> None:
    """Отменяет тикет DELETE (по таймауту ожидания)."""
    _log_async_http("async LLM: отмена тикета (DELETE)", http_method="DELETE", http_url=operations_url)
    try:
        response = await client.delete(operations_url, headers=headers, timeout=10.0)
        if response.status_code >= 400:
            logger.warning("async LLM: не удалось отменить тикет", http_status=response.status_code)
    except httpx.HTTPError:
        logger.exception("async LLM: ошибка DELETE тикета")


def _cancel_ticket_sync(client: SyncTicketClient, operations_url: str, headers: dict[str, str]) -> None:
    """Синхронная отмена тикета DELETE (по таймауту ожидания)."""
    _log_async_http("async LLM: отмена тикета (DELETE)", http_method="DELETE", http_url=operations_url)
    try:
        response = client.delete(operations_url, headers=headers, timeout=10.0)
        if response.status_code >= 400:
            logger.warning("async LLM: не удалось отменить тикет", http_status=response.status_code)
    except httpx.HTTPError:
        logger.exception("async LLM: ошибка DELETE тикета")


async def _submit_async(
    *,
    client: AsyncTicketClient,
    submit_url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
) -> AsyncTicket:
    """Создаёт тикет: POST /async с Idempotency-Key."""
    started = perf_counter()
    request_body = _submit_payload(payload)
    _log_async_http("async LLM: создание тикета (POST)", http_method="POST", http_url=submit_url)
    response = await client.post(
        submit_url,
        headers=_submit_headers(headers),
        json=request_body,
        timeout=timeout_seconds,
    )
    _raise_if_http_error(response)
    ticket = parse_async_ticket(response.json())
    _log_async_http(
        "async LLM: тикет создан",
        http_method="POST",
        http_url=submit_url,
        http_status=response.status_code,
        duration_ms=round((perf_counter() - started) * 1000),
        ticket_id=ticket.id,
    )
    return ticket


def _submit_sync(
    *,
    client: SyncTicketClient,
    submit_url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    timeout_seconds: float,
) -> AsyncTicket:
    """Синхронное создание тикета: POST /async с Idempotency-Key."""
    started = perf_counter()
    request_body = _submit_payload(payload)
    _log_async_http("async LLM: создание тикета (POST)", http_method="POST", http_url=submit_url)
    response = client.post(
        submit_url,
        headers=_submit_headers(headers),
        json=request_body,
        timeout=timeout_seconds,
    )
    _raise_if_http_error(response)
    ticket = parse_async_ticket(response.json())
    _log_async_http(
        "async LLM: тикет создан",
        http_method="POST",
        http_url=submit_url,
        http_status=response.status_code,
        duration_ms=round((perf_counter() - started) * 1000),
        ticket_id=ticket.id,
    )
    return ticket


async def _get_ticket_async(
    *,
    client: AsyncTicketClient,
    operations_url: str,
    headers: dict[str, str],
    wait_seconds: int,
) -> AsyncTicket:
    """Long-poll тикета: GET /async/operations/{id}?wait=n."""
    response = await client.get(
        operations_url,
        headers=headers,
        params={"wait": wait_seconds},
        timeout=_poll_timeout_seconds(wait_seconds),
    )
    _raise_if_http_error(response)
    return parse_async_ticket(response.json())


def _get_ticket_sync(
    *,
    client: SyncTicketClient,
    operations_url: str,
    headers: dict[str, str],
    wait_seconds: int,
) -> AsyncTicket:
    """Синхронный long-poll тикета: GET /async/operations/{id}?wait=n."""
    response = client.get(
        operations_url,
        headers=headers,
        params={"wait": wait_seconds},
        timeout=_poll_timeout_seconds(wait_seconds),
    )
    _raise_if_http_error(response)
    return parse_async_ticket(response.json())


def ticket_wait_seconds(settings: VllmTykSettings) -> int:
    """Сколько ждать один тикет (без часов задачи — потолок VLLM_ASYNC_MAX_WAIT_SECONDS)."""
    return settings.vllm_async_max_wait_seconds


async def wait_ticket_async(
    *,
    client: AsyncTicketClient,
    settings: VllmTykSettings,
    operations_url: str,
    headers: dict[str, str],
    ticket: AsyncTicket,
) -> AsyncTicket:
    """Ждёт терминальный статус тикета через GET ?wait=n; по таймауту — DELETE."""
    current = ticket
    deadline = time.monotonic() + ticket_wait_seconds(settings)
    while True:
        if current.done:
            _log_async_http(
                "async LLM: тикет завершён",
                http_method="GET",
                http_url=operations_url,
                ticket_id=current.id,
                ticket_status=current.metadata.status,
            )
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _cancel_ticket_async(client, operations_url, headers)
            msg = "async LLM: истёк бюджет ожидания тикета"
            raise TimeoutError(msg)
        wait_n = min(settings.vllm_async_wait_seconds, max(0, int(remaining)))
        current = await _get_ticket_async(
            client=client,
            operations_url=operations_url,
            headers=headers,
            wait_seconds=wait_n,
        )
        if not current.done and wait_n == 0:
            await asyncio.sleep(_POLL_GAP_SECONDS)


def wait_ticket_sync(
    *,
    client: SyncTicketClient,
    settings: VllmTykSettings,
    operations_url: str,
    headers: dict[str, str],
    ticket: AsyncTicket,
) -> AsyncTicket:
    """Синхронный аналог wait_ticket_async."""
    current = ticket
    deadline = time.monotonic() + ticket_wait_seconds(settings)
    while True:
        if current.done:
            _log_async_http(
                "async LLM: тикет завершён",
                http_method="GET",
                http_url=operations_url,
                ticket_id=current.id,
                ticket_status=current.metadata.status,
            )
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _cancel_ticket_sync(client, operations_url, headers)
            msg = "async LLM: истёк бюджет ожидания тикета"
            raise TimeoutError(msg)
        wait_n = min(settings.vllm_async_wait_seconds, max(0, int(remaining)))
        current = _get_ticket_sync(
            client=client,
            operations_url=operations_url,
            headers=headers,
            wait_seconds=wait_n,
        )
        if not current.done and wait_n == 0:
            time.sleep(_POLL_GAP_SECONDS)


def invoke_vllm_chat_http_async_api(
    settings: VllmTykSettings,
    *,
    chat_url: str,
    headers: dict[str, str],
    payload: dict[str, object],
) -> dict[str, object]:
    """Sync: создать тикет, дождаться ответа, вернуть body chat/completions."""
    submit_url = build_async_submit_url(chat_url, prefix=settings.vllm_async_prefix)
    with (
        _sync_concurrency(settings.vllm_async_max_concurrency),
        create_rate_limit_http_client(settings, settings.vllm_ssl_verify) as client,
    ):
        ticket = _submit_sync(
            client=client,
            submit_url=submit_url,
            headers=headers,
            payload=payload,
            timeout_seconds=settings.vllm_async_submit_timeout_seconds,
        )
        operations_url = build_async_operations_url(
            settings.vllm_api_base,
            ticket.id,
            operations_path=settings.vllm_async_operations_path,
        )
        finished = wait_ticket_sync(
            client=client,
            settings=settings,
            operations_url=operations_url,
            headers=headers,
            ticket=ticket,
        )
        return extract_chat_body(finished)


async def ainvoke_vllm_chat_http_async_api(
    settings: VllmTykSettings,
    *,
    chat_url: str,
    headers: dict[str, str],
    payload: dict[str, object],
) -> dict[str, object]:
    """Async: создать тикет, дождаться ответа, вернуть body chat/completions."""
    submit_url = build_async_submit_url(chat_url, prefix=settings.vllm_async_prefix)
    async with (
        _async_sem(settings.vllm_async_max_concurrency),
        create_rate_limit_http_async_client(settings, settings.vllm_ssl_verify) as client,
    ):
        ticket = await _submit_async(
            client=client,
            submit_url=submit_url,
            headers=headers,
            payload=payload,
            timeout_seconds=settings.vllm_async_submit_timeout_seconds,
        )
        operations_url = build_async_operations_url(
            settings.vllm_api_base,
            ticket.id,
            operations_path=settings.vllm_async_operations_path,
        )
        finished = await wait_ticket_async(
            client=client,
            settings=settings,
            operations_url=operations_url,
            headers=headers,
            ticket=ticket,
        )
        return extract_chat_body(finished)
