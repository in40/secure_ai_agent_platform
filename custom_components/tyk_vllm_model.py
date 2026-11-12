"""TykVLLMModel — vLLM через Tyk/EPA с захардкоженным списком из 2 моделей.

Наследует всю обвязку _vllm_tyk_lib (EPA-токен, RPS-лимит, retry, sync/async контур).
Модели выбираются из статического dropdown MODEL_CHOICES — список /models не запрашивается.
"""

from __future__ import annotations

from _vllm_tyk_lib import vllm_call
from _vllm_tyk_lib.epa import (
    chat_openai_epa_kwargs,
    fetch_vllm_epa_access_token_sync,
    vllm_uses_epa_auth,
)
from _vllm_tyk_lib.rate_limit import create_rate_limit_http_async_client, create_rate_limit_http_client
from _vllm_tyk_lib.tyk_settings import get_vllm_tyk_settings
from langchain_openai import ChatOpenAI
from langflow.base.models import LCModelComponent
from langflow.field_typing import LanguageModel
from langflow.field_typing.range_spec import RangeSpec
from langflow.io import DropdownInput, IntInput, MessageInput, MultilineInput, SliderInput
from langflow.logging.logger import logger
from langflow.schema.message import Message

# ==== ЗАХАРКОЖЕННЫЕ МОДЕЛИ (id как в vLLM /models) ====
# Второй модели пока нет — можно добавить без пересборки:
#   VLLM_MODEL_CHOICES="qwen3.5-27b-q8-mtp-vision,вторая-модель"
MODEL_CHOICES = [
    "qwen3.5-27b-q8-mtp-vision",
]

import os as _os  # noqa: E402

_extra = [x.strip() for x in _os.environ.get("VLLM_MODEL_CHOICES", "").split(",") if x.strip()]
for _m in _extra:
    if _m not in MODEL_CHOICES:
        MODEL_CHOICES.append(_m)


def _model_needs_no_think(model_name: str) -> bool:
    return "Qwen3" in model_name


class TykVLLMModelComponent(LCModelComponent):
    display_name = "TYK vLLM Model"
    description = "vLLM через Tyk/EPA; модель выбирается из двух захардкоженных."
    icon = "brain-circuit"
    name = "TykVLLMModel"

    inputs = [
        DropdownInput(
            name="model_name",
            display_name="Model",
            options=MODEL_CHOICES,
            value=MODEL_CHOICES[0],
            advanced=False,
            info="Одна из двух моделей шлюза.",
        ),
        MessageInput(name="input_value", display_name="Input", info="Текст, передаваемый модели."),
        MultilineInput(
            name="system_message",
            display_name="System Message",
            info="Системное сообщение.",
            advanced=False,
        ),
        SliderInput(
            name="temperature",
            display_name="Temperature",
            value=0.1,
            range_spec=RangeSpec(min=0, max=2, step=0.01),
            advanced=True,
        ),
        IntInput(name="max_tokens", display_name="Max tokens", value=4000, advanced=True),
    ]

    async def text_response(self) -> Message:
        settings = get_vllm_tyk_settings()
        model_name = str(self.model_name or MODEL_CHOICES[0]).strip()
        input_text = vllm_call.apply_no_think_prompt(str(self.input_value or "").strip())
        system_text = str(self.system_message or "").strip()
        cleaned = await vllm_call.ainvoke_vllm_chat_http(
            settings,
            vllm_call.VllmTykChatRequest(
                user_message=input_text,
                system_message=system_text,
                max_tokens=int(self.max_tokens or 4000),
                temperature=self.temperature if self.temperature is not None else 0.1,
                enable_thinking=_model_needs_no_think(model_name),
            ),
        )
        result = Message(text=cleaned)
        self.status = result
        return result

    def build_model(self) -> LanguageModel:
        settings = get_vllm_tyk_settings()
        model_name = str(self.model_name or MODEL_CHOICES[0]).strip()
        kwargs: dict[str, object] = {
            "model_name": model_name,
            "base_url": settings.vllm_api_base.rstrip("/"),
            "temperature": self.temperature if self.temperature is not None else 0.1,
            "max_tokens": self.max_tokens or None,
            "max_retries": settings.vllm_llm_retry_attempts,
        }
        if _model_needs_no_think(model_name):
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        if vllm_uses_epa_auth(settings):
            token = fetch_vllm_epa_access_token_sync(settings)
            kwargs.update(chat_openai_epa_kwargs(settings, token))
        else:
            api_key = settings.vllm_api_key.get_secret_value()
            if api_key:
                kwargs["api_key"] = api_key
        kwargs["http_client"] = create_rate_limit_http_client(settings, settings.vllm_ssl_verify)
        kwargs["http_async_client"] = create_rate_limit_http_async_client(settings, settings.vllm_ssl_verify)
        return ChatOpenAI(**kwargs)
