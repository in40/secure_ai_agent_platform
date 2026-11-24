"""context_budget_guard — извлечено из flow «Document Workspace v1.2» (6a82200a) 2026-11-24, запечено в образ 1.0.29-astra8."""

import json

import httpx
from lfx.custom import Component
from lfx.io import DropdownInput, HandleInput, MessageTextInput, Output, SecretStrInput, StrInput
from lfx.schema.data import Data
from lfx.schema.message import Message


class ContextBudgetGuard(Component):
    display_name = "Context Budget Guard"
    name = "ContextBudgetGuard"
    description = "Check the rendered system prompt and token reserves before saving Markdown or calling the Agent."

    _UPLOAD = "Upload"
    _DOCUMENT_QUESTIONS = "Document Q&A"

    inputs = [
        DropdownInput(name="mode", display_name="Mode", options=[_UPLOAD, _DOCUMENT_QUESTIONS], value=_UPLOAD,
                      real_time_refresh=True, info="Check an upload before saving, or check a document question before calling the Agent."),
        HandleInput(name="exported", display_name="Extracted Document", input_types=["Data", "JSON"], is_list=True,
                    info="Connect PDFPlumber and the Docling Markdown export. Exactly one active result is required."),
        HandleInput(name="request", display_name="Stored Document and Question", input_types=["Data", "JSON"], show=False,
                    info="Connect Read Document → Document, with separate text, question and session_id fields."),
        MessageTextInput(name="prompt", display_name="Rendered System Prompt", required=True, value="",
                         info="Connect Prompt Template.prompt. Its rendered text must already include the exact Markdown and the current summary, if any."),
        MessageTextInput(name="history_text", display_name="Rendered History Summary", value="", advanced=True, show=False,
                         info="Leave blank in P0. In Document Q&A, provide the exact summary inserted into the prompt when one is available."),
        StrInput(name="model_base", display_name="Model API Base URL", required=True, value="", advanced=True,
                 info="Set the same trusted llama.cpp server base URL used by the Agent model; an optional trailing /v1 is removed before calling /tokenize."),
        SecretStrInput(name="api_key", display_name="Model API Key", value="", advanced=True,
                       info="Optional API key for the model server's /tokenize endpoint. Select a Langflow credential variable if the server requires authentication; otherwise leave blank."),
        StrInput(name="expected_model", display_name="Expected Tokenizer Model ID", value="", advanced=True,
                 info="Optional exact ID to compare with a model ID reported by /tokenize. A response without an ID cannot verify the model."),
        StrInput(name="window", display_name="Context Window (Tokens)", required=True, value="", advanced=True,
                 info="Set the model server's effective context window as a positive integer, shared by both modes."),
        StrInput(name="history", display_name="History Reserve (Tokens)", required=True, value="2048", advanced=True,
                 info="Set 2048 in both modes, even when the rendered history is empty and Agent history is disabled."),
        StrInput(name="question", display_name="Future Question Reserve (Tokens)", value="", advanced=True,
                 info="Set a positive integer for the estimated length of a future question. Document Q&A counts the actual question."),
        StrInput(name="answer", display_name="Answer Reserve (Tokens)", required=True, value="4096", advanced=True,
                 info="Set at least 4096 in both modes to cover Agent.max_tokens; increase for a larger generation limit."),
        StrInput(name="overhead", display_name="Chat and Tool Overhead (Tokens)", required=True, value="", advanced=True,
                 info="Set the same positive allowance in both modes for chat formatting, Calculator tool schema and Agent tool-call/result context."),
    ]
    outputs = [
        Output(name="accepted", display_name="Checked Markdown", method="accepted_export", types=["JSON"],
               group_outputs=True, tool_mode=False, info="Send the unchanged Markdown and Docling fields, plus budget metrics, to Save Document."),
        Output(name="accepted_question", display_name="Checked User Question", method="checked_question", types=["Message"],
               group_outputs=True, tool_mode=False, info="Connect to Agent.input_value. Contains only the original question and its session ID."),
        Output(name="accepted_prompt", display_name="Checked System Prompt", method="checked_prompt", types=["Message"],
               group_outputs=True, tool_mode=False, info="Connect to Agent.system_prompt. Contains exactly the rendered system text checked by the guard."),
        Output(name="rejected", display_name="Error", method="budget_error", types=["Message"],
               group_outputs=True, tool_mode=False, info="On error, connect to Chat Output for a JSON code and a Russian diagnostic."),
    ]
    _VIEWS = {
        _UPLOAD: ("Upload Budget Check", frozenset({"accepted"})),
        _DOCUMENT_QUESTIONS: ("Document Q&A Budget Check", frozenset({"accepted_question", "accepted_prompt"})),
    }
    _HISTORY_TOKENS = 2048
    _MIN_ANSWER_TOKENS = 4096
    _ERRORS = {
        "CONTEXT_BUDGET_INVALID": "Проверьте настройки окна, резервов и адреса токенизатора.",
        "DOCUMENT_TEXT_MISSING": "Не удалось получить Markdown одного документа.",
        "PROMPT_INVALID": "Системный промпт не содержит точный текст документа или указанного саммари.",
        "QUESTION_INVALID": "Не удалось получить исходный вопрос и сессию.",
        "HISTORY_TOO_LARGE": "Саммари истории превышает резерв в 2048 токенов.",
        "TOKENIZER_UNAVAILABLE": "Не удалось получить корректный результат от токенизатора модели.",
        "MODEL_TOKENIZER_MISMATCH": "Токенизатор сообщил идентификатор модели, отличный от ожидаемого.",
        "DOCUMENT_TOO_LARGE": "Документ и резервы не помещаются в контекст модели. Файл не сохранён.",
        "CONTEXT_BUDGET_EXCEEDED": "Документ, вопрос и резервы не помещаются в контекст модели.",
    }

    def _configure_frontend(self, frontend_node: dict, mode: str) -> dict:
        if mode not in self._VIEWS:
            return frontend_node
        title, success = self._VIEWS[mode]
        frontend_node["display_name"] = title
        template = frontend_node["template"]
        upload = mode == self._UPLOAD
        template["exported"]["show"] = upload
        template["request"]["show"] = not upload
        template["history_text"]["show"] = not upload
        template["question"].update(show=upload, required=upload)
        # Always start from the complete port list, including when switching back to a mode.
        frontend_node["outputs"] = [port.model_dump() for port in self.outputs
                                    if port.name in success or port.name == "rejected"]
        frontend_node["base_classes"] = list(dict.fromkeys(
            output_type for port in frontend_node["outputs"] for output_type in port["types"]
        ))
        return frontend_node

    def _update_template(self, frontend_node: dict) -> dict:
        return self._configure_frontend(frontend_node, frontend_node["template"]["mode"]["value"])

    def update_outputs(self, frontend_node: dict, field_name: str, field_value) -> dict:
        mode_field = frontend_node["template"]["mode"]
        if field_name == "mode":
            mode_field["value"] = field_value
        return self._configure_frontend(frontend_node, mode_field["value"])

    @classmethod
    def _failure(cls, code, **details):
        return {"status": "ERROR", "code": code, "message": cls._ERRORS[code], **details}

    @staticmethod
    def _integer(value):
        if type(value) is int:
            return value
        if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
            return int(value.strip())
        raise ValueError("Expected a non-negative integer")

    def _settings(self):
        window, history, answer, overhead = map(self._integer, (self.window, self.history, self.answer, self.overhead))
        upload = self.mode == self._UPLOAD
        question = self._integer(self.question) if upload else 0
        if (window <= 0 or history != self._HISTORY_TOKENS or (upload and question <= 0)
                or answer < self._MIN_ANSWER_TOKENS or overhead <= 0):
            raise ValueError("Invalid budget limits")
        base = (self.model_base or "").strip()
        url = httpx.URL(base)
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or "?" in base or "#" in base):
            raise ValueError("Invalid model base URL")
        endpoint = url.copy_with(raw_path=url.raw_path.rstrip(b"/").removesuffix(b"/v1") + b"/tokenize")
        return window, history, question, answer, overhead, endpoint

    @staticmethod
    def _record(value):
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if isinstance(value, Data) and not isinstance(value, Message):
            value = value.data
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _text(value):
        return value.text if isinstance(value, Message) else value

    @staticmethod
    def _tokenize(client, endpoint, text):
        response = client.post(endpoint, json={"content": text, "add_special": False, "parse_special": False})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid tokenizer response")
        tokens = payload.get("tokens")
        if not isinstance(tokens, list) or not tokens or any(type(token) is not int or token < 0 for token in tokens):
            raise ValueError("Invalid tokens")
        model = payload.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("Invalid tokenizer model ID")
        return len(tokens), model

    def _check(self):
        if self.mode not in self._VIEWS:
            return self._failure("CONTEXT_BUDGET_INVALID")
        upload = self.mode == self._UPLOAD
        source = self.exported if upload else self.request
        if upload and isinstance(source, list):
            source = source[0] if len(source) == 1 else None
        record = self._record(source)
        if not upload:
            session = record.get("session_id")
            if isinstance(session, str) and session.strip():
                self._guard_session_id = session
        document = record.get("text")
        if (not isinstance(document, str) or not document.strip()
                or (upload and record.get("export_format", "Markdown") != "Markdown")):
            return self._failure("DOCUMENT_TEXT_MISSING")
        question = record.get("question") if not upload else None
        if not upload and (not self._guard_session_id or not isinstance(question, str) or not question.strip()):
            return self._failure("QUESTION_INVALID")
        prompt = self._text(self.prompt)
        summary = self._text(self.history_text)
        if summary is None:
            summary = ""
        if (not isinstance(prompt, str) or document not in prompt or not isinstance(summary, str)
                or (summary and (upload or summary not in prompt))):
            return self._failure("PROMPT_INVALID")
        try:
            window, history, future_question, answer, overhead, endpoint = self._settings()
            expected_model = (self.expected_model or "").strip()
        except (AttributeError, TypeError, ValueError, httpx.InvalidURL):
            return self._failure("CONTEXT_BUDGET_INVALID")

        try:
            key = self.api_key
            if hasattr(key, "get_secret_value"):
                key = key.get_secret_value()
            key = (key or "").strip()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            observed_models = set()
            with httpx.Client(timeout=30, trust_env=False, headers=headers) as client:
                def count(value):
                    tokens, model = self._tokenize(client, endpoint, value)
                    if model:
                        observed_models.add(model)
                    return tokens

                prompt_tokens = count(prompt)
                summary_tokens = count(summary) if summary else 0
                question_tokens = future_question if upload else count(question)
                document_tokens = count(document) if upload else None
        except (httpx.HTTPError, TypeError, ValueError, AttributeError, RecursionError):
            return self._failure("TOKENIZER_UNAVAILABLE")

        if len(observed_models) > 1 or (expected_model and observed_models - {expected_model}):
            return self._failure("MODEL_TOKENIZER_MISMATCH")
        if summary_tokens > history:
            return self._failure("HISTORY_TOO_LARGE", history_tokens=summary_tokens, history_reserve_tokens=history)
        unused_history = history - summary_tokens
        # The rendered prompt already includes the Markdown and any supplied summary.
        required = prompt_tokens + question_tokens + unused_history + answer + overhead
        if required > window:
            code = "DOCUMENT_TOO_LARGE" if upload else "CONTEXT_BUDGET_EXCEEDED"
            return self._failure(code, required_tokens=required, context_window_tokens=window)
        if upload:
            # Standalone document count is for repository metadata, not another term in required.
            # Repository's schema can only represent document_tokens <= context_window_tokens.
            if document_tokens > window:
                return self._failure("DOCUMENT_TOO_LARGE", document_tokens=document_tokens, context_window_tokens=window)
            budget = {"document_tokens": document_tokens, "context_window_tokens": window,
                      "rendered_prompt_tokens": prompt_tokens, "question_reserve_tokens": future_question,
                      "history_reserve_tokens": history, "answer_reserve_tokens": answer,
                      "overhead_tokens": overhead, "required_tokens": required}
            if observed_models:
                budget["model"] = next(iter(observed_models))
            self._accepted = Data(data={**record, "budget": budget})
        else:
            self._question, self._prompt = question, prompt
        return None

    def _pre_run_setup(self):
        self._accepted = None
        self._question = self._prompt = ""
        self._guard_session_id = None
        self._error = self._check()
        active = {"rejected"} if self._error else self._VIEWS[self.mode][1]
        if self._vertex is not None:
            inactive = [port.name for port in self.outputs if port.name not in active]
            # stop() rebuilds dependencies and can reschedule already completed inputs.
            self.graph.exclude_branches_conditionally(self._vertex.id, inactive)

    def accepted_export(self) -> Data:
        return self._accepted if self._accepted is not None else Data(data={})

    def checked_question(self) -> Message:
        return Message(text=self._question, session_id=self._guard_session_id)

    def checked_prompt(self) -> Message:
        return Message(text=self._prompt, session_id=self._guard_session_id)

    def budget_error(self) -> Message:
        text = json.dumps(self._error, ensure_ascii=False) if self._error else ""
        return Message(text=text, session_id=self._guard_session_id)
