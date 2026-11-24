"""workspace_repository — извлечено из flow «Document Workspace v1.2» (6a82200a) 2026-11-24, запечено в образ 1.0.29-astra8."""

import hashlib
import json
import logging
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from urllib.parse import quote, unquote
from uuid import UUID, uuid4

from lfx.custom.custom_component.component import Component
from lfx.io import DropdownInput, HandleInput, Output, StrInput
from lfx.schema.data import Data
from lfx.schema.message import Message


class WorkspaceRepository(Component):
    display_name = "Read Document"
    name = "WorkspaceRepository"
    icon="folder-open"
    description = "Store and read the session document and successful Q&A turns."

    inputs = [
        DropdownInput(name="mode", display_name="Mode", options=["PREFLIGHT", "WRITE", "READ", "SAVE_TURN"], value="PREFLIGHT",
                      real_time_refresh=True, info="Check an upload, save Markdown, read the active document, or save an answered question."),
        StrInput(name="storage_root", display_name="Storage Root", value="", advanced=True,
                 info="Existing absolute directory. Use a Langflow global variable or DOCUMENT_WORKSPACE_ROOT."),
        StrInput(name="question_limit", display_name="Document Question Limit", value="", advanced=True,
                 info="Bind a Langflow global variable or set DOCUMENT_WORKSPACE_QUESTION_LIMIT in the environment. Default: 5. Zero disables questions."),
        HandleInput(name="chat_message", display_name="Chat Message", input_types=["Message"], required=True),
        HandleInput(name="session_id", display_name="Session ID", input_types=["JSON"], show=False,
                    info="Session ID from Request Router, without the upload message."),
        HandleInput(name="exported", display_name="Checked Markdown", input_types=["Data", "JSON"], show=False,
                    info="The Budget Guard's accepted export, with text and budget fields."),
        StrInput(name="history_turn_limit", display_name="History Turns", advanced=True, value="",
                 info="Maximum previous turns to send to the summary model; zero omits previous turns."),
        StrInput(name="history_prompt_chars", display_name="History Prompt Characters", advanced=True, value="",
                 info="Maximum characters per history excerpt (question or answer)."),
        StrInput(name="history_record_bytes", display_name="History Record Bytes", advanced=True, value="",
                 info="Maximum bytes allowed per saved Q&A turn file."),
        HandleInput(name="answer", display_name="Successful Answer", input_types=["Message"], show=False,
                    info="Final Q&A answer, before Chat Output."),
        HandleInput(name="read_request", display_name="Read Request", input_types=["Data", "JSON"], show=False,
                    info="The successful Read Document result for this question."),
    ]
    outputs = [
        Output(name="file_path", display_name="File Path", method="preflight_file", types=["JSON"], group_outputs=True, tool_mode=False),
        Output(name="receipt", display_name="Saved", method="write_receipt", types=["Message"], group_outputs=True, tool_mode=False),
        Output(name="document", display_name="Document", method="read_document", types=["JSON"], group_outputs=True, tool_mode=False),
        Output(name="saved_answer", display_name="Answer", method="saved_answer", types=["Message"], group_outputs=True, tool_mode=False),
        Output(name="error", display_name="Error", method="error_message", types=["Message"], group_outputs=True, tool_mode=False),
    ]

    _PLAYGROUND_USER = "local-dev-user"
    _NEW = ("document.md", "metadata.json", "status.json")
    _CLAIM = ".write-in-progress"
    _PORTS = {"PREFLIGHT": "file_path", "WRITE": "receipt", "READ": "document", "SAVE_TURN": "saved_answer"}
    _VIEWS = {
        "PREFLIGHT": ("Check Upload", "Upload Message", "Message with one attachment from Request Router → Upload Message."),
        "WRITE": ("Save Document", "Chat Message", "Not used when saving a document."),
        "READ": ("Read Document", "User Question", "Question message from Request Router; its session identifies the saved document."),
        "SAVE_TURN": ("Save Q&A Turn", "User Question", "The original question from Request Router."),
    }
    _ERRORS = {
        "INVALID_ID": "Некорректный идентификатор сессии.",
        "USER_IDENTITY_REQUIRED": "Хранилищу нужен подтверждённый идентификатор пользователя.",
        "INVALID_MODE": "Некорректный режим хранилища.",
        "DOCUMENT_ALREADY_EXISTS": "В этой сессии уже есть документ или идёт запись.",
        "DOCUMENT_NOT_FOUND": "В этой сессии нет документа. Сначала загрузите файл.",
        "DOCUMENT_NOT_READY": "Запись документа не завершена.",
        "STORAGE_UNAVAILABLE": "Хранилище недоступно или неверно настроено.",
        "STORAGE_WRITE_FAILED": "Не удалось сохранить документ.",
        "QUESTION_LIMIT_REACHED": "Лимит вопросов по этому документу исчерпан.",
        "QUESTION_LIMIT_INVALID": "Некорректная настройка лимита вопросов по документу.",
        "STORAGE_READ_FAILED": "Не удалось прочитать документ.",
        "INVALID_DOCUMENT": "Ожидался один корректный документ.",
    }

    def _configure_frontend(self, frontend_node: dict, mode: str) -> dict:
        if mode not in self._VIEWS:
            return frontend_node
        title, label, hint = self._VIEWS[mode]
        frontend_node["display_name"] = title
        template = frontend_node["template"]
        writing = mode == "WRITE"
        template["chat_message"].update(display_name=label, info=hint, show=not writing, required=not writing)
        template["session_id"].update(show=writing, required=writing)
        template["exported"]["show"] = writing
        for field in ("answer", "read_request", "history_record_bytes"):
            if field in template:
                template[field]["show"] = mode == "SAVE_TURN" or (field == "history_record_bytes" and mode == "READ")
        for field in ("history_turn_limit", "history_prompt_chars"):
            if field in template:
                template[field]["show"] = mode == "READ"
        # Filter the instance's complete port list, not the already filtered editor payload.
        frontend_node["outputs"] = [port.model_dump() for port in self.outputs
                                    if port.name in {self._PORTS[mode], "error"}]
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

    def _pre_run_setup(self):
        self._workspace_session_id = None
        if self.mode == "SAVE_TURN":
            # History is optional. An I/O failure must not hide a successful Q&A answer.
            self._payload, self._error = {}, None
            try:
                self._save_turn()
            except (OSError, TypeError, ValueError, RecursionError):
                logging.getLogger(__name__).warning("Workspace Repository could not persist a Q&A turn")
        else:
            self._payload, self._error = self._run_operation()
        active = "error" if self._error else self._PORTS[self.mode]
        if self._vertex is not None:
            if self.mode == "READ" and self._error == "QUESTION_LIMIT_REACHED":
                self.graph.exclude_branches_conditionally(self._vertex.id, [port.name for port in self.outputs if port.name != active])
            else:
                for port in self.outputs:
                    if port.name != active:
                        self.stop(port.name)

    def _run_operation(self):
        if self.mode not in self._PORTS:
            return {}, "INVALID_MODE"
        if self.mode == "WRITE":
            payload = self.session_id
            session_id = payload.data.get("session_id") if isinstance(payload, Data) else None
        else:
            session_id = getattr(self.chat_message, "session_id", None)
        try:
            session = self._session_key(session_id)
        except ValueError:
            return {}, "INVALID_ID"
        self._workspace_session_id = str(session_id)
        try:
            user = self._workspace_user_key()
        except ValueError:
            return {}, "USER_IDENTITY_REQUIRED"
        try:
            with self._root_directory() as root:
                if self.mode == "PREFLIGHT":
                    return self._preflight(root, user, session)
                if self.mode == "WRITE":
                    return self._write(root, user, session)
                return self._read(root, user, session)
        except (OSError, ValueError):
            return {}, "STORAGE_UNAVAILABLE"

    def _history_owner_key(self):
        # The dev document workspace is shared. Do not use its local-dev-user key for chat history.
        graph = self.graph if self._vertex is not None else None
        principal = getattr(graph, "execution_principal", None)
        actor = getattr(principal, "actor_id", None)
        if (getattr(principal, "kind", None) == "actor"
                and getattr(principal, "family", None) in {"interactive_chat", "workflow_v2"}
                and actor and actor == principal.user_id):
            return self._session_key(actor)
        return None

    def _workspace_user_key(self):
        graph = self.graph if self._vertex is not None else None
        principal = getattr(graph, "execution_principal", None)
        kind, family = getattr(principal, "kind", None), getattr(principal, "family", None)
        actor = getattr(principal, "actor_id", None)
        # The browser uses the workflow v2 route; the legacy build API uses interactive_chat.
        if kind == "actor" and family in {"interactive_chat", "workflow_v2"}:
            if not actor or actor != principal.user_id:
                raise ValueError("Missing Playground account")
            # Authenticated development Playground intentionally shares one workspace.
            # Neither this default nor the server-side override applies to LFX.
            return self._session_key(os.getenv("DOCUMENT_WORKSPACE_PLAYGROUND_USER_ID") or self._PLAYGROUND_USER)
        if (kind, family) == ("headless_operator", "lfx_headless"):
            # LFX 1.13 marks synthetic runner IDs separately. They are not authenticated users.
            if (actor and actor == principal.user_id == str(graph.user_id)
                    and hasattr(graph, "_headless_filesystem_user_id") and graph._headless_filesystem_user_id is None):
                return self._session_key(actor)
        raise ValueError("Missing trusted runtime identity")

    @staticmethod
    def _session_key(session_id):
        # Encode safe IDs without accepting separators, escapes, dot segments,
        # controls or aliases created by trimming.
        if not isinstance(session_id, (str, UUID)):
            raise ValueError("Invalid session")
        value = str(session_id)
        if (not value or len(value) > 240 or not value.isprintable() or value != value.strip()
                or value in {".", ".."} or any(char in value for char in ("/", "\\", "%", ":"))):
            raise ValueError("Invalid session")
        key = quote(value, safe=" -_.")
        if len(key) > 240:
            raise ValueError("Invalid session")
        return key

    @contextmanager
    def _root_directory(self):
        configured = (self.storage_root or "").strip() or os.getenv("DOCUMENT_WORKSPACE_ROOT", "").strip()
        root = PurePosixPath(configured)
        if os.name != "posix" or root.anchor != "/" or ".." in root.parts or "\x00" in configured:
            raise ValueError("Invalid storage root")
        with self._directories(None, ("/", *root.parts[1:])) as fd:
            yield fd

    @staticmethod
    @contextmanager
    def _directories(parent, parts, *, create=False):
        # No resolve() or exists()-then-open: walk every segment relative to an
        # already opened directory, never following a symlink (including root parents).
        fd = None
        try:
            for part in parts:
                base = parent if fd is None else fd
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=base)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
                if fd is not None:
                    os.close(fd)
                fd = child
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _has(fd, *names):
        for name in names:
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            return True  # A dangling symlink also occupies the name.
        return False

    def _preflight(self, root, user, session):
        files = self.chat_message.files or []
        if len(files) != 1 or not isinstance(files[0], str) or not files[0].strip():
            return {}, "INVALID_DOCUMENT"
        try:
            with self._directories(root, (user, session)) as fd:
                if self._has(fd, *self._NEW, self._CLAIM):
                    return {}, "DOCUMENT_ALREADY_EXISTS"
        except FileNotFoundError:
            pass
        # Router validates the attachment; Repository only passes the handle to Docling.
        return {"file_path": files[0]}, None

    @staticmethod
    def _file_metadata(record):
        result = {}
        filename = record.get("source_filename") or record.get("filename")
        if isinstance(filename, str):
            filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
            if filename.isprintable() and filename not in {".", ".."} and len(filename) <= 255:
                result["source_filename"] = filename
        mime = record.get("mimetype")
        if isinstance(mime, str) and mime.isprintable() and 0 < len(mime) <= 255:
            result["mimetype"] = mime
        return result

    @staticmethod
    def _budget_fields(record):
        budget = record.get("budget")
        if not isinstance(budget, dict):
            raise ValueError("Missing budget")
        tokens = budget.get("document_tokens")
        window = budget.get("context_window_tokens")
        if type(tokens) is not int or type(window) is not int or not 0 < tokens <= window:
            raise ValueError("Invalid budget")
        result = {"document_tokens": tokens, "context_window_tokens": window}
        maximum = budget.get("max_document_tokens")
        if maximum is not None:
            if type(maximum) is not int or not tokens <= maximum <= window:
                raise ValueError("Invalid maximum")
            result["max_document_tokens"] = maximum
        for name in ("model", "tokenizer"):
            label = budget.get(name)
            if isinstance(label, str) and label.isprintable() and 0 < len(label) <= 255:
                result[name] = label
        return result

    def _write(self, root, user, session):
        try:
            export = self.exported
            if isinstance(export, list) and len(export) == 1:
                export = export[0]
            record = export.data if isinstance(export, Data) else export
            if not isinstance(record, dict) or record.get("export_format", "Markdown") != "Markdown":
                raise ValueError("Expected Markdown export")
            text = record.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Missing Markdown")
            raw = text.encode("utf-8")
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            status = {"format_version": 1, "status": "READY", "created_at": timestamp}
            metadata = {
                **self._file_metadata(record), **self._budget_fields(record),
                "format_version": 1, "content_sha256": hashlib.sha256(raw).hexdigest(), "created_at": timestamp,
            }
        except ValueError:
            return {}, "INVALID_DOCUMENT"

        try:
            with self._directories(root, (user, session), create=True) as fd:
                if self._has(fd, *self._NEW, self._CLAIM):
                    return {}, "DOCUMENT_ALREADY_EXISTS"
                claim = os.open(self._CLAIM, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    # Recheck under an exclusive claim: another writer may have
                    # already published and released it since the first check.
                    if self._has(fd, *self._NEW):
                        return {}, "DOCUMENT_ALREADY_EXISTS"
                    self._publish(fd, "document.md", raw)
                    self._publish(fd, "metadata.json", self._encode_json(metadata))
                    os.fsync(fd)
                    self._publish(fd, "status.json", self._encode_json(status))
                finally:
                    os.close(claim)
                    self._discard(fd, self._CLAIM)
        except FileExistsError:
            return {}, "DOCUMENT_ALREADY_EXISTS"
        except OSError:
            return {}, "STORAGE_WRITE_FAILED"
        return {**status, "user_id": unquote(user), "session_id": self._workspace_session_id}, None

    @staticmethod
    def _encode_json(record):
        return (json.dumps(record, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _discard(fd, name):
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError:
            logging.getLogger(__name__).warning("Workspace Repository could not remove an internal write entry")

    def _publish(self, fd, name, payload):
        pending = f".workspace-{uuid4().hex}.tmp"
        temp_fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(temp_fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # Publish a whole file atomically, with kernel-enforced no replacement.
            os.link(pending, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        finally:
            self._discard(fd, pending)

    @staticmethod
    def _read_file(fd, name, limit=-1):
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Expected a regular file")
            data = stream.read(limit + 1 if limit >= 0 else -1)
            if limit >= 0 and len(data) > limit:
                raise ValueError("History record exceeds its size limit")
            return data

    def _history_setting(self, field):
        raw = (getattr(self, field, None) or "").strip()
        try:
            return int(raw) if raw.isascii() and raw.isdecimal() else 0
        except ValueError:
            return 0

    def _history_context(self, fd, slot, digest):
        pairs = []
        limit = self._history_setting("history_turn_limit")
        chars = self._history_setting("history_prompt_chars")
        record_bytes = self._history_setting("history_record_bytes")
        try:
            owner = self._history_owner_key()
            if owner and limit and chars and record_bytes:
                with self._directories(fd, ("history", owner)) as history:
                    # The prompt, not the file reader, resolves references.
                    indices = []
                    for name in os.listdir(history):
                        if name.startswith("turn-") and name.endswith(".json"):
                            number = name[5:-5]
                            if number.isascii() and number.isdecimal():
                                index = int(number)
                                if str(index) == number and index < slot:
                                    indices.append(index)
                    for index in sorted(indices, reverse=True):
                        try:
                            r = json.loads(self._read_file(history, f"turn-{index}.json", record_bytes))
                        except (OSError, ValueError, RecursionError):
                            continue
                        if (isinstance(r, dict) and r.get("document_sha256") == digest
                                and isinstance(r.get("question"), str) and isinstance(r.get("answer"), str)):
                            pairs.append({"question": r["question"][:chars], "answer": r["answer"][:chars]})
                            if len(pairs) == limit:
                                break
        except (OSError, ValueError):
            pass
        return json.dumps({"question": self.chat_message.text[:chars], "prior_turns": pairs[::-1]}, ensure_ascii=False)

    def _save_turn(self):
        source = self.read_request
        record = source.data if isinstance(source, Data) else source
        answer = self.answer
        owner = self._history_owner_key()
        question = self.chat_message
        record_bytes = self._history_setting("history_record_bytes")
        if (not owner or not record_bytes or not isinstance(record, dict) or not isinstance(question, Message) or question.files
                or not isinstance(answer, Message) or answer.error or not isinstance(answer.text, str)
                or not answer.text.strip() or record.get("question") != question.text
                or record.get("session_id") != str(question.session_id)):
            return
        slot = record.get("question_slot")
        if type(slot) is not int or slot < 0:
            return
        metadata = record.get("metadata")
        digest = metadata.get("content_sha256") if isinstance(metadata, dict) else None
        user, session = self._workspace_user_key(), self._session_key(question.session_id)
        with self._root_directory() as root, self._directories(root, (user, session)) as fd:
            status = json.loads(self._read_file(fd, "status.json"))
            current = json.loads(self._read_file(fd, "metadata.json"))
            if (not isinstance(status, dict) or not isinstance(current, dict)
                    or status.get("status") != "READY" or not digest or current.get("content_sha256") != digest):
                return
            with self._directories(fd, (f"question-{slot}",)):
                pass  # SAVE_TURN never consumes a question slot.
            payload = self._encode_json({"format_version": 1, "document_sha256": digest,
                                         "question": question.text, "answer": answer.text})
            if len(payload) > record_bytes:
                raise ValueError("History record exceeds its size limit")
            with self._directories(fd, ("history", owner), create=True) as history:
                try:
                    self._publish(history, f"turn-{slot}.json", payload)
                except FileExistsError:
                    pass  # Repeated delivery cannot replace an existing turn.

    def _read(self, root, user, session):
        try:
            with self._directories(root, (user, session)) as fd:
                if not self._has(fd, *self._NEW, self._CLAIM):
                    return {}, "DOCUMENT_NOT_FOUND"
                try:
                    status = json.loads(self._read_file(fd, "status.json").decode("utf-8"))
                    if (not isinstance(status, dict) or status.get("status") != "READY"
                            or type(status.get("format_version")) is not int or status["format_version"] != 1):
                        return {}, "DOCUMENT_NOT_READY"
                    raw = self._read_file(fd, "document.md")
                    metadata = json.loads(self._read_file(fd, "metadata.json").decode("utf-8"))
                except FileNotFoundError:
                    return {}, "DOCUMENT_NOT_READY"
                if (not isinstance(metadata, dict) or metadata.get("format_version") != 1
                        or metadata.get("content_sha256") != hashlib.sha256(raw).hexdigest()):
                    return {}, "STORAGE_READ_FAILED"
                text = raw.decode("utf-8")
                if not text.strip():
                    return {}, "STORAGE_READ_FAILED"
                limit = (self.question_limit or "").strip() or os.getenv("DOCUMENT_WORKSPACE_QUESTION_LIMIT", "").strip() or "5"
                if not limit.isascii() or not limit.isdecimal() or len(limit) > 9:
                    return {}, "QUESTION_LIMIT_INVALID"
                # Atomically reserve one slot before the question can reach the Agent.
                for slot in range(int(limit)):
                    try:
                        os.mkdir(f"question-{slot}", mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        continue
                    break
                else:
                    return {}, "QUESTION_LIMIT_REACHED"
                history = self._history_context(fd, slot, metadata["content_sha256"])
        except FileNotFoundError:
            return {}, "DOCUMENT_NOT_FOUND"
        except (OSError, ValueError, RecursionError):
            return {}, "STORAGE_READ_FAILED"
        return {"text": text, "metadata": metadata, "question": self.chat_message.text,
                "session_id": self._workspace_session_id, "question_slot": slot, "history_context": history}, None

    def preflight_file(self) -> Data:
        return Data(data=self._payload if self.mode == "PREFLIGHT" and not self._error else {})

    def write_receipt(self) -> Message:
        text = json.dumps(self._payload, ensure_ascii=False) if self.mode == "WRITE" and not self._error else ""
        return Message(text=text, session_id=self._workspace_session_id)

    def read_document(self) -> Data:
        return Data(data=self._payload if self.mode == "READ" and not self._error else {})

    def saved_answer(self) -> Message:
        return self.answer if isinstance(self.answer, Message) else Message(text="")

    def error_message(self) -> Message:
        text = json.dumps({"status": "ERROR", "code": self._error, "message": self._ERRORS[self._error]},
                          ensure_ascii=False) if self._error else ""
        return Message(text=text, session_id=self._workspace_session_id)
