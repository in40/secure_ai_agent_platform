"""Настройки vLLM через Tyk/EPA, читаются из переменных окружения VLLM_* и EPA__*."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class VllmTykSettings(BaseSettings):
    """Параметры подключения к vLLM через Tyk-шлюз с EPA-аутентификацией.

    Все секреты и URL берутся только из окружения (правило §1.12),
    в UI компонентов не выводятся.
    """

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    vllm_api_base: str = Field(default="", validation_alias="VLLM_API_BASE")
    vllm_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="VLLM_API_KEY")
    vllm_model_name: str = Field(default="", validation_alias="VLLM_MODEL_NAME")
    # true — VLLM_API_BASE ходит через Tyk с EPA access_token (банковский контур)
    vllm_epa_auth: bool = Field(default=False, validation_alias="VLLM_EPA_AUTH")
    # true — подробная диагностика EPA (claims токена, config, endpoint) в логах
    vllm_epa_debug: bool = Field(default=False, validation_alias="VLLM_EPA_DEBUG")
    # false — отключить проверку TLS (dev-контур)
    vllm_ssl_verify: bool = Field(default=True, validation_alias="VLLM_SSL_VERIFY")
    # Лимит исходящих HTTP к LLM API (0 — без ограничения)
    vllm_tyk_rps_limit: float = Field(default=1.0, ge=0.0, validation_alias="VLLM_TYK_RPS_LIMIT")
    # Запас к интервалу 1/RPS (мс): Tyk считает окно включительно
    vllm_tyk_rps_safety_ms: int = Field(default=50, ge=0, validation_alias="VLLM_TYK_RPS_SAFETY_MS")
    # HTTP-таймаут одного вызова vLLM через Tyk (секунды, ниже лимита шлюза)
    vllm_tyk_timeout_seconds: float = Field(default=38.0, ge=1.0, validation_alias="VLLM_TYK_TIMEOUT_SECONDS")
    # Повторные попытки после транзиентной ошибки (429/5xx/таймаут/сеть)
    vllm_llm_retry_attempts: int = Field(default=2, ge=0, le=10, validation_alias="VLLM_LLM_RETRY_ATTEMPTS")
    # true — тикетный контур: POST {gateway}/v1/async/{model}/v1/chat/completions +
    # GET {gateway}/v1/async/operations/{id}?wait=n + DELETE (обходит лимит Tyk ~40 с)
    vllm_async_enabled: bool = Field(default=False, validation_alias="VLLM_ASYNC_ENABLED")
    # Префикс async-маршрута после первого /v1 шлюза, до path модели
    vllm_async_prefix: str = Field(default="/async", validation_alias="VLLM_ASYNC_PREFIX")
    # Path операций тикета относительно корня шлюза (без path модели)
    vllm_async_operations_path: str = Field(
        default="/async/operations",
        validation_alias="VLLM_ASYNC_OPERATIONS_PATH",
    )
    # GET ?wait=n: long-poll в секундах (должен быть ниже лимита Tyk на GET ~40 с)
    vllm_async_wait_seconds: int = Field(default=25, ge=0, le=120, validation_alias="VLLM_ASYNC_WAIT_SECONDS")
    # Потолок ожидания одного тикета (с)
    vllm_async_max_wait_seconds: int = Field(
        default=300,
        ge=1,
        validation_alias="VLLM_ASYNC_MAX_WAIT_SECONDS",
    )
    # Максимум одновременных тикетов на процесс (не путать с RPS)
    vllm_async_max_concurrency: int = Field(
        default=3,
        ge=1,
        le=32,
        validation_alias="VLLM_ASYNC_MAX_CONCURRENCY",
    )
    # HTTP-таймаут POST /async (создание тикета), не генерации
    vllm_async_submit_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        validation_alias="VLLM_ASYNC_SUBMIT_TIMEOUT_SECONDS",
    )


def get_vllm_tyk_settings() -> VllmTykSettings:
    """Возвращает настройки vLLM/Tyk, перечитывая окружение при каждом вызове."""
    return VllmTykSettings()
