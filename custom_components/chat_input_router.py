from lfx.custom.custom_component.component import Component
from lfx.io import MessageInput, Output
from lfx.schema.message import Message
from lfx.schema.data import Data
from lfx.schema.image import get_file_paths
import time
from pathlib import Path
import json
import logging
from langflow.services.tracing.service import trace_context_var as _tc_var


def _log_timing(step_name, duration_s, extra=None, display_name=None):
    _logger = logging.getLogger("langflow.timing")
    _tc = _tc_var.get()
    _entry = {
        "event": "step_timing",
        "component": display_name or step_name,
        "operation": step_name,
        "duration_s": round(duration_s, 4),
        "session_id": _tc.session_id if _tc else "unknown",
    }
    if extra:
        _entry.update(extra)
    _logger.info(json.dumps(_entry))



class ChatInputRouter(Component):
    display_name = "ChatInput Router"
    description = "Route ChatInput message to appropriate downstream components."
    icon = "git-branch"

    inputs = [
        MessageInput(
            name="message",
            display_name="Message",
            required=True,
        ),
    ]

    outputs = [
        Output(display_name="Query", name="query", method="get_query", group_outputs=True, types=["Message"]),
        Output(display_name="Document Text", name="doc_text", method="get_doc_text", group_outputs=True, types=["Data"]),
        Output(display_name="File Paths", name="file_paths", method="get_file_paths", group_outputs=True, types=["Data"]),
        Output(display_name="Session ID", name="session_id", method="get_session_id", group_outputs=True, types=["str"]),
    ]

    def _split_at_separator(self, text: str, separator: str) -> tuple[str, str]:
        parts = text.split(separator, 1)
        doc = parts[0].strip()
        query = parts[1].strip() if len(parts) > 1 else ""
        return doc, query

    def get_query(self) -> Message:
        _t0 = time.time()
        try:
            msg: Message = self.message
            files = msg.files or []
            text = msg.text or ""
            separator = "---"
            has_files = bool(files)
            has_separator = separator in text
            if has_files:
                return Message(text=text.strip() or "")
            elif has_separator:
                _, query = self._split_at_separator(text, separator)
                return Message(text=query or text.strip())
            else:
                return Message(text=text.strip() or "")
        finally:
            _log_timing("get_query", time.time() - _t0, {
                "has_files": has_files,
                "has_separator": has_separator,
                "query_length": len(text),
                "files_count": len(files),
            }, "ChatInputRouter")

    def get_doc_text(self) -> Data:
        _t0 = time.time()
        try:
            msg: Message = self.message
            files = msg.files or []
            text = msg.text or ""
            separator = "---"
            has_files = bool(files)
            has_separator = separator in text
            if has_files:
                return Data(text="")
            elif has_separator:
                doc, _ = self._split_at_separator(text, separator)
                return Data(text=doc or "")
            else:
                return Data(text="")
        finally:
            _log_timing("get_doc_text", time.time() - _t0, {
                "has_files": has_files,
                "has_separator": has_separator,
                "doc_text_length": len(text),
            }, "ChatInputRouter")

    def get_file_paths(self) -> list[Data]:
        _t0 = time.time()
        try:
            msg: Message = self.message
            files = msg.files or []
            if not files:
                return []
            paths = get_file_paths(files)
            return [Data(data={"file_path": path}) for path in paths] if paths else []
        finally:
            _paths = locals().get("paths", [])
            _files = locals().get("files", [])
            file_extensions = [Path(p).suffix for p in _paths] if _paths else []
            _log_timing("get_file_paths", time.time() - _t0, {
                "files_count": len(_files),
                "file_extensions": file_extensions,
            }, "ChatInputRouter")
    def get_session_id(self) -> str:
        msg: Message = self.message
        return str(msg.session_id) if msg.session_id else ""

    def extract_data(self, result):
        """Extract the data from the result. Override to prevent status bleed between outputs.
        
        Returns values that downstream components can consume:
        - Message -> returned as-is
        - DataFrame/Series -> returned as-is
        - Data -> unwrap to inner dict
        - dict -> return as-is (never wrap in Data)
        - str -> return as-is
        - list -> return as-is (never wrap in Data)
        """
        import pandas as pd

        # Message objects: return as-is (extract_data sets status)
        if isinstance(result, Message):
            self.status = result.get_text()
            return result

        # Pandas objects: return as-is
        if isinstance(result, pd.DataFrame | pd.Series):
            return result

        # Data objects: unwrap to inner dict
        if isinstance(result, Data):
            inner = result.data
            if isinstance(inner, dict):
                return inner
            return inner

        # Objects with .data attribute: unwrap
        if hasattr(result, "data"):
            inner = result.data
            if isinstance(inner, dict):
                return inner
            return inner

        # Objects with model_dump: use it
        if hasattr(result, "model_dump"):
            try:
                return result.model_dump()
            except Exception:
                pass

        # Bare dicts: return as-is
        if isinstance(result, dict):
            return result

        # Strings: return as-is
        if isinstance(result, str):
            return result

        # Lists: return as-is - DO NOT wrap in Data
        if isinstance(result, list):
            return result

        return result