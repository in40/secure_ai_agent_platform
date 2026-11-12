"""Ограничение исходящих HTTP к Tyk/vLLM (лимит RPS на каждый запрос)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import httpx
from loguru import logger

from _vllm_tyk_lib.tyk_settings import VllmTykSettings

_lock = threading.Lock()
_next_allowed_at = 0.0


def vllm_rate_limit_enabled(settings: VllmTykSettings) -> bool:
    """True, если VLLM_TYK_RPS_LIMIT > 0."""
    return settings.vllm_tyk_rps_limit > 0.0


def _min_interval_seconds(settings: VllmTykSettings) -> float:
    """Интервал между запросами (с) с запасом на включительное окно Tyk."""
    return (1.0 / settings.vllm_tyk_rps_limit) + (settings.vllm_tyk_rps_safety_ms / 1000.0)


def reset_vllm_rate_limit_state() -> None:
    """Сбрасывает состояние лимитера (только для тестов)."""
    global _next_allowed_at
    with _lock:
        _next_allowed_at = 0.0


def _reserve_slot(interval: float) -> float:
    """Резервирует ближайший слот и возвращает monotonic-время отправки."""
    global _next_allowed_at
    with _lock:
        now = time.monotonic()
        slot_at = now if _next_allowed_at <= now else _next_allowed_at
        _next_allowed_at = slot_at + interval
        return slot_at


def _sleep_until(deadline: float) -> None:
    """Спит до deadline, не доверяя одному time.sleep (раннее пробуждение ОС)."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(remaining)


async def _async_sleep_until(deadline: float) -> None:
    """Асинхронный аналог _sleep_until без блокировки event loop."""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(remaining)


def _log_rps_wait(settings: VllmTykSettings, wait_seconds: float) -> None:
    """Пишет ожидание слота в лог (JSON-поля, без f-строк)."""
    logger.info(
        "Tyk RPS: ожидание слота перед HTTP к LLM API",
        wait_ms=round(wait_seconds * 1000),
        rps_limit=settings.vllm_tyk_rps_limit,
        safety_ms=settings.vllm_tyk_rps_safety_ms,
    )


def acquire_vllm_rate_limit(settings: VllmTykSettings) -> None:
    """Блокирует до следующего слота исходящего вызова LLM через Tyk."""
    if not vllm_rate_limit_enabled(settings):
        return
    slot_at = _reserve_slot(_min_interval_seconds(settings))
    wait_seconds = slot_at - time.monotonic()
    if wait_seconds > 0:
        _log_rps_wait(settings, wait_seconds)
        _sleep_until(slot_at)


async def acquire_vllm_rate_limit_async(settings: VllmTykSettings) -> None:
    """Асинхронный acquire: резерв слота под lock, ожидание через asyncio.sleep."""
    if not vllm_rate_limit_enabled(settings):
        return
    slot_at = _reserve_slot(_min_interval_seconds(settings))
    wait_seconds = slot_at - time.monotonic()
    if wait_seconds > 0:
        _log_rps_wait(settings, wait_seconds)
        await _async_sleep_until(slot_at)


def create_rate_limit_http_client(settings: VllmTykSettings, verify_ssl: bool) -> httpx.Client:
    """Синхронный HTTP-клиент с RPS-лимитом к Tyk перед каждым запросом."""
    import httpx

    class _SyncTransport(httpx.BaseTransport):
        """Синхронный транспорт с ограничением RPS к Tyk."""

        def __init__(self) -> None:
            self._inner = httpx.HTTPTransport(verify=verify_ssl)

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            acquire_vllm_rate_limit(settings)
            return self._inner.handle_request(request)

        def close(self) -> None:
            self._inner.close()

    if vllm_rate_limit_enabled(settings):
        return httpx.Client(transport=_SyncTransport(), verify=verify_ssl)
    return httpx.Client(verify=verify_ssl)


def create_rate_limit_http_async_client(settings: VllmTykSettings, verify_ssl: bool) -> httpx.AsyncClient:
    """Создаёт асинхронный HTTP-клиент с RPS-лимитом к Tyk перед каждым запросом."""
    import httpx

    class _AsyncTransport(httpx.AsyncBaseTransport):
        """Асинхронный транспорт с ограничением RPS к Tyk."""

        def __init__(self) -> None:
            self._inner = httpx.AsyncHTTPTransport(verify=verify_ssl)

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await acquire_vllm_rate_limit_async(settings)
            return await self._inner.handle_async_request(request)

        async def aclose(self) -> None:
            await self._inner.aclose()

    if vllm_rate_limit_enabled(settings):
        return httpx.AsyncClient(transport=_AsyncTransport(), verify=verify_ssl)
    return httpx.AsyncClient(verify=verify_ssl)


@asynccontextmanager
async def vllm_tyk_slot(settings: VllmTykSettings) -> AsyncIterator[None]:
    """Контекст одного LLM-вызова (сам acquire делается в HTTP-транспорте)."""
    yield


@contextmanager
def vllm_tyk_slot_sync(settings: VllmTykSettings) -> Iterator[None]:
    """Синхронный аналог vllm_tyk_slot (без отдельного acquire)."""
    yield
