from typing import Any

import pdfplumber

from langflow.custom import Component
from langflow.io import BoolInput, DropdownInput, FileInput, IntInput, Output
from langflow.schema import Data


class PDFPlumberComponent(Component):
    display_name = "PDFPlumber"
    description = "Extract text, tables or structured data from PDFs via pdfplumber."
    icon = "file-text"
    name = "PDFPlumber"

    inputs = [
        FileInput(
            name="input_files",
            display_name="PDF Files",
            info="PDF file(s) to process.",
            file_types=["pdf"],
            is_list=True,
            required=True,
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

    def _resolve_paths(self) -> list[str]:
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
            msg = "PDFPlumber: no input file resolved"
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
