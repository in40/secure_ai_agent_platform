"""Document Page Splitter — режет документ на страницы (pdf/office) или таб-листы (xlsx/ods/csv).

Один узел для flow: определяет формат по расширению, выдаёт list[Data] —
по записи на страницу/таб с метаданными. Office-форматы (docx/doc/pptx/odt/rtf)
конвертируются в PDF через LibreOffice (есть в образе), затем режутся по страницам.
"""

import os
import shutil
import subprocess
import tempfile

import pdfplumber
from openpyxl import load_workbook

from langflow.custom import Component
from langflow.io import BoolInput, DropdownInput, HandleInput, IntInput, Output
from langflow.schema import Data

OFFICE_TO_PDF = {".docx", ".doc", ".pptx", ".ppt", ".odt", ".rtf", ".wpd"}
SHEET_TO_XLSX = {".xls", ".ods"}
TEXT_LIKE = {".txt", ".md", ".csv", ".tsv"}


class DocumentPageSplitterComponent(Component):
    display_name = "Document Page Splitter"
    description = (
        "Split a document into per-page Data records (PDF and office formats via LibreOffice) "
        "or per-sheet records (xlsx/xls/ods/csv). Accepts paths from Uploaded File / "
        "ChatInput Router components."
    )
    icon = "scissors"
    name = "DocumentPageSplitter"

    inputs = [
        HandleInput(
            name="file_path",
            display_name="Document",
            info="File path (str), Data with 'file_path', or Message with path.",
            input_types=["Data", "JSON", "Message", "str"],
            is_list=True,
            required=True,
        ),
        IntInput(
            name="max_parts",
            display_name="Max Parts",
            info="Limit pages/sheets per document (0 = all).",
            value=0,
        ),
        DropdownInput(
            name="sheet_format",
            display_name="Sheet Format",
            info="Text rendering of tabular sheets.",
            options=["markdown", "csv"],
            value="markdown",
        ),
        BoolInput(
            name="skip_empty",
            display_name="Skip Empty Parts",
            info="Drop pages/sheets with no text.",
            value=True,
        ),
    ]

    outputs = [
        Output(display_name="Parts", name="parts", method="split_documents"),
    ]

    # ---------- path resolution ----------
    def _config_dir(self) -> str:
        return os.environ.get("LANGFLOW_CONFIG_DIR", "/app/langflow")

    def _resolve(self, p: str) -> str:
        p = str(p).strip()
        if not os.path.isabs(p):
            p = os.path.join(self._config_dir(), p)
        if os.path.exists(p):
            return p
        raise ValueError(f"DocumentPageSplitter: file not found: {p}")

    def _paths(self) -> list[str]:
        raw = self.file_path
        if not isinstance(raw, list):
            raw = [raw]
        out: list[str] = []
        for item in raw:
            val = None
            if isinstance(item, str):
                val = item
            elif isinstance(item, Data):
                val = item.get_text() or item.data.get("file_path") or item.data.get("path")
            elif hasattr(item, "get_text"):
                val = item.get_text()
            elif hasattr(item, "text"):
                val = item.text
            if isinstance(val, dict):
                val = val.get("file_path") or val.get("path")
            if not val or not str(val).strip():
                continue
            for piece in str(val).replace(",", "\n").splitlines():
                if piece.strip():
                    out.append(self._resolve(piece))
        if not out:
            msg = "DocumentPageSplitter: no usable file path in input"
            raise ValueError(msg)
        return out

    # ---------- soffice ----------
    def _soffice_convert(self, path: str, target: str) -> str:
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            msg = f"DocumentPageSplitter: LibreOffice not available to convert {path}"
            raise ValueError(msg)
        outdir = tempfile.mkdtemp(prefix="dps_")
        profile = tempfile.mkdtemp(prefix="dps_lo_")  # уникальный профиль — нет lock-конфликта между воркерами
        try:
            subprocess.run(
                [
                    soffice, "--headless", "--norestore", "--invisible",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to", target, "--outdir", outdir, path,
                ],
                check=True, capture_output=True, timeout=300,
            )
            base = os.path.splitext(os.path.basename(path))[0] + "." + target
            converted = os.path.join(outdir, base)
            if not os.path.exists(converted):
                msg = f"DocumentPageSplitter: soffice produced no {target} for {path}"
                raise ValueError(msg)
            self._tmpdirs.append(outdir)
            return converted
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    # ---------- extractors ----------
    def _pdf_parts(self, path: str) -> list[Data]:
        parts: list[Data] = []
        with pdfplumber.open(path) as pdf:
            total = len(pdf.pages)
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                parts.append(Data(data={
                    "text": text, "page_number": i, "page_count": total,
                    "part_type": "page", "part_name": f"page {i}",
                    "source_file": os.path.basename(path), "file_path": path,
                }))
        return parts

    def _sheet_to_text(self, ws) -> str:
        rows: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(c.strip() for c in cells):
                rows.append(cells)
        if not rows:
            return ""
        if self.sheet_format == "csv":
            return "\n".join(",".join(c.replace(",", ";") for c in r) for r in rows)
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(r) + " |" for r in rows]
        md.insert(1, "|" + "---|" * width)
        return "\n".join(md)

    def _xlsx_parts(self, path: str) -> list[Data]:
        wb = load_workbook(path, read_only=True, data_only=True)
        parts: list[Data] = []
        try:
            names = wb.sheetnames
            for i, name in enumerate(names, start=1):
                parts.append(Data(data={
                    "text": self._sheet_to_text(wb[name]), "page_number": i,
                    "page_count": len(names), "part_type": "sheet", "part_name": name,
                    "source_file": os.path.basename(path), "file_path": path,
                }))
        finally:
            wb.close()
        return parts

    def _text_parts(self, path: str) -> list[Data]:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        chunks = content.split("\f")
        parts: list[Data] = []
        for i, chunk in enumerate(chunks, start=1):
            parts.append(Data(data={
                "text": chunk, "page_number": i, "page_count": len(chunks),
                "part_type": "page", "part_name": f"page {i}",
                "source_file": os.path.basename(path), "file_path": path,
            }))
        return parts

    # ---------- entry ----------
    def split_documents(self) -> list[Data]:
        self._tmpdirs: list[str] = []
        try:
            all_parts: list[Data] = []
            for path in self._paths():
                ext = os.path.splitext(path)[1].lower()
                if ext == ".pdf":
                    parts = self._pdf_parts(path)
                elif ext in OFFICE_TO_PDF:
                    parts = self._pdf_parts(self._soffice_convert(path, "pdf"))
                elif ext in (".xlsx", ".xlsm"):
                    parts = self._xlsx_parts(path)
                elif ext in SHEET_TO_XLSX:
                    parts = self._xlsx_parts(self._soffice_convert(path, "xlsx"))
                elif ext in (".csv", ".tsv"):
                    parts = self._text_parts(path)
                elif ext in TEXT_LIKE:
                    parts = self._text_parts(path)
                else:
                    msg = f"DocumentPageSplitter: unsupported format '{ext}' ({path})"
                    raise ValueError(msg)
                if self.max_parts and int(self.max_parts) > 0:
                    parts = parts[: int(self.max_parts)]
                if self.skip_empty:
                    parts = [p for p in parts if (p.data.get("text") or "").strip()]
                all_parts.extend(parts)
            self.status = f"{len(all_parts)} part(s) from {len(set(p.data['file_path'] for p in all_parts)) if all_parts else 0} file(s)"
            return all_parts
        finally:
            for d in getattr(self, "_tmpdirs", []):
                shutil.rmtree(d, ignore_errors=True)
