"""request_router — извлечено из flow «Document Workspace v1.2» (6a82200a) 2026-11-24, запечено в образ 1.0.29-astra8."""

from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import UUID

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output
from lfx.schema.data import Data
from lfx.schema.image import Image
from lfx.schema.message import Message


class RequestRouter(Component):
    display_name = "Request Router"
    name = "RequestRouter"
    icon = "split"
    description = "Routes file upload requests through API v1. Only PDF (.pdf) and DOCX (.docx) are supported, with one file per request. Text requests without attachments go to the question branch, and invalid requests go to the error branch."

    inputs = [HandleInput(name="chat_message", display_name="Chat Message", input_types=["Message"], required=True)]
    outputs = [
        Output(name="upload", display_name="Upload Message", method="upload_message", types=["Message"], group_outputs=True, tool_mode=False),
        Output(name="session_id", display_name="Session ID", method="upload_session_id", types=["JSON"], group_outputs=True, tool_mode=False),
        Output(name="question", display_name="User Question", method="question_message", types=["Message"], group_outputs=True, tool_mode=False),
        Output(name="error", display_name="Error", method="error_message", types=["Message"], group_outputs=True, tool_mode=False),
    ]
    _SUPPORTED_EXTENSIONS = {".pdf", ".docx"}
    _ERRORS = {
        "INVALID_ID": "Некорректный идентификатор сессии.",
        "EMPTY_REQUEST": "Введите вопрос или прикрепите один PDF или DOCX.",
        "MULTIPLE_FILES": "Прикрепите только один файл.",
        "INVALID_ATTACHMENT": "Не удалось определить путь вложения.",
        "UNSUPPORTED_FILE_TYPE": "Поддерживаются только файлы PDF и DOCX.",
    }

    @staticmethod
    def _valid_session(value):
        # Same safe segment contract as WorkspaceRepository, without a runtime dependency on it.
        if not isinstance(value, (str, UUID)):
            return False
        value = str(value)
        return (0 < len(value) <= 240 and value.isprintable() and value == value.strip()
                and value not in {".", ".."} and not any(c in value for c in "/\\%:")
                and len(quote(value, safe=" -_.")) <= 240)

    @staticmethod
    def _attachment_path(attachment):
        if isinstance(attachment, str):
            return attachment
        if isinstance(attachment, Image):
            return attachment.path
        return None

    def _classify(self):
        msg = self.chat_message
        if not isinstance(msg, Message) or not self._valid_session(msg.session_id):
            return "error", "INVALID_ID"
        files = msg.files
        if files is None or (isinstance(files, (list, tuple)) and not files):
            if isinstance(msg.text, str) and msg.text.strip():
                return "question", msg
            return "error", "EMPTY_REQUEST"
        if not isinstance(files, (list, tuple)):
            files = [files]
        if len(files) != 1:
            return "error", "MULTIPLE_FILES"
        path = self._attachment_path(files[0])
        if not isinstance(path, str) or not path.strip() or not path.isprintable():
            return "error", "INVALID_ATTACHMENT"
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in self._SUPPORTED_EXTENSIONS:
            return "error", "UNSUPPORTED_FILE_TYPE"
        # The path is an opaque Chat Input handle, not an instruction to open bytes here.
        return "upload", msg.model_copy(update={"files": [path]})

    def _pre_run_setup(self):
        self._route, self._decision = self._classify()
        if self._vertex is not None:
            active = {"upload", "session_id"} if self._route == "upload" else {self._route}
            inactive = [port.name for port in self.outputs if port.name not in active]
            for name in inactive:
                self.stop(name)
            # The Playground scheduler can reconsider stopped vertices. Keep all
            # unselected routes excluded together for the remainder of the run.
            self.graph.exclude_branches_conditionally(self._vertex.id, inactive)

    def upload_message(self) -> Message:
        return self._decision if self._route == "upload" else Message(text="")

    def upload_session_id(self) -> Data:
        return Data(data={"session_id": str(self._decision.session_id)}) if self._route == "upload" else Data(data={})

    def question_message(self) -> Message:
        return self._decision if self._route == "question" else Message(text="")

    def error_message(self) -> Message:
        if self._route != "error":
            return Message(text="")
        code = self._decision
        session = getattr(self.chat_message, "session_id", None)
        return Message(text=f"[{code}] {self._ERRORS[code]}",
                       session_id=session if self._valid_session(session) else None)
