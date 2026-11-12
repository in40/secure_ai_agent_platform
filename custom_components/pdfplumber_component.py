import os
from typing import Any

import pdfplumber

from langflow.custom import Component
from langflow.io import BoolInput, DropdownInput, FileInput, HandleInput, IntInput, Output
from langflow.schema import Data


class PDFPlumberComponent(Component):
    display_name = "PDFPlumber"
    description = (
        "Extract text, tables or structured data from PDFs via pdfplumber. "
        "Accepts files from the widget or server paths (e.g. Uploaded File component -> File Data)."
    )
    icon = "file-text"
    name = "PDFPlumber"

    inputs = [
        HandleInput(
            name="file_path",
            display_name="Server File Path",
            info=(
                "Data object with a 'file_path' property pointing to server file"
                " or a Message object with a path to the file. Takes precedence over 'PDF Files'."
            ),
            input_types=["Data", "JSON", "Message"],
            is_list=True,
            required=False,
        ),
        FileInput(
            name="input_files",
            display_name="PDF Files",
            info="PDF file(s) to process (used when Server File Path is not connected).",
            file_types=["pdf"],
            is_list=True,
            required=False,
        ),
        DropdownInput(
            name="mode",
            display_name="Mode",
            info="Extraction mode.",
            options=["text", "tables", "full"],
            value="text",
        ),
        IntInput(
            name="max_pages",
            display_name="Max Pages",
            info="Limit number of pages (0 = all).",
            value=0,
        ),
        BoolInput(
            name="keep_tables_in_text",
            display_name="Keep Tables In Text",
            info="In 'full' mode, append table text after page text.",
            value=True,
        ),
    ]

    outputs = [
        Output(display_name="Data", name="data", method="build_output"),
    ]

    def _config_dir(self) -> str:
        return os.environ.get("LANGFLOW_CONFIG_DIR", "/app/langflow")

    def _from_handle(self) -> list[str]:
        src = self.file_path
        if src is None:
            return []
        if not isinstance(src, list):
            src = [src]
        out: list[str] = []
        for item in src:
            p = None
            if isinstance(item, Data):
                p = item.data.get("file_path") or item.data.get("path")
            elif isinstance(item, str):
                p = item
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    p = text
            if not p:
                continue
            for part in str(p).replace(",", "\n").splitlines():
                part = part.strip()
                if not part:
                    continue
                if not os.path.isabs(part):
                    cand = os.path.join(self._config_dir(), part)
                    if os.path.exists(cand):
                        part = cand
                out.append(part)
        return out

    def _resolve_paths(self) -> list[str]:
        handle = self._from_handle()
        if handle:
            return handle
        files = self.input_files
        if isinstance(files, Data):
            files = [files]
        if not isinstance(files, list):
            files = [files]
        paths = []
        for item in files:
            if isinstance(item, Data):
                p = item.data.get("file_path") or item.data.get("path")
            else:
                p = str(item)
            if p:
                paths.append(str(p))
        if not paths:
            msg = "PDFPlumber: no input file resolved (connect Server File Path or select PDF Files)"
            raise ValueError(msg)
        return paths

    def _process_pdf(self, path: str) -> Data:
        pages_text: list[str] = []
        pages_tables: list[list[Any]] = []
        with pdfplumber.open(path) as pdf:
            limit = self.max_pages or len(pdf.pages)
            for page in pdf.pages[:limit]:
                if self.mode in ("text", "full"):
                    pages_text.append(page.extract_text() or "")
                if self.mode in ("tables", "full"):
                    pages_tables.extend(page.extract_tables() or [])
        if self.mode == "text":
            payload = {"file": path, "text": "\n".join(pages_text)}
        elif self.mode == "tables":
            payload = {"file": path, "tables": pages_tables}
        else:
            text = "\n".join(pages_text)
            if self.keep_tables_in_text and pages_tables:
                table_text = "\n\n".join(
                    "\n".join(" | ".join(str(c) if c is not None else "" for c in row) for row in t)
                    for t in pages_tables
                )
                text = f"{text}\n\n--- TABLES ---\n{table_text}"
            payload = {"file": path, "text": text, "tables": pages_tables}
        return Data(data=payload)

    def build_output(self) -> list[Data]:
        results = [self._process_pdf(p) for p in self._resolve_paths()]
        self.status = f"{len(results)} file(s) processed"
        return results
