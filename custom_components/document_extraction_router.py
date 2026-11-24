"""document_extraction_router — извлечено из flow «Document Workspace v1.2» (6a82200a) 2026-11-24, запечено в образ 1.0.29-astra8."""

import os
from pathlib import Path

from lfx.custom.custom_component.component import Component
from lfx.io import HandleInput, Output, StrInput
from lfx.schema.data import Data
from lfx.services.deps import get_settings_service
from lfx.utils.file_path_security import component_file_access_scopes, enforce_local_file_access


class DocumentExtractionRouter(Component):
    display_name = "Document Extraction Router"
    name = "DocumentExtractionRouter"
    description = "Automatically routes simple single-column text PDFs to PDFPlumber and other documents to Docling."
    inputs = [
        HandleInput(name="file_path", display_name="Checked File Path", input_types=["Data", "JSON"], required=True),
        StrInput(name="storage_root", display_name="Storage Root", value="", advanced=True,
                 info="Base path for server files. Defaults to DOCUMENT_WORKSPACE_ROOT, then Langflow storage."),
    ]
    outputs = [
        Output(name="docling", display_name="Docling (OCR / DOCX)", method="docling", group_outputs=True),
        Output(name="pdfplumber", display_name="PDFPlumber", method="pdfplumber_path", group_outputs=True),
    ]

    def _pre_run_setup(self):
        self._use_pdfplumber = self._is_simple_pdf()
        if self._vertex is not None:
            inactive = "docling" if self._use_pdfplumber else "pdfplumber"
            # Stop updates graph dependencies; exclusion keeps the branch off for the rest of the run.
            self.stop(inactive)
            self.graph.exclude_branches_conditionally(self._vertex.id, [inactive])

    def _is_simple_pdf(self) -> bool:
        source = self.file_path
        handle = source.data.get("file_path") if isinstance(source, Data) else None
        if not isinstance(handle, str) or Path(handle).suffix.lower() not in {".pdf", ".docx"}:
            raise ValueError("Expected a checked PDF or DOCX file path.")
        if Path(handle).suffix.lower() == ".docx":
            return False

        import pdfplumber

        configured = (self.storage_root or "").strip() or os.getenv("DOCUMENT_WORKSPACE_ROOT", "").strip()
        root = Path(configured or get_settings_service().settings.config_dir).expanduser().resolve()
        path = enforce_local_file_access(root / handle, scope_ids=component_file_access_scopes(self)).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("PDF file is not available in Langflow storage.")
        has_text = False
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    try:
                        simple, has_words = self._page_layout(page)
                    finally:
                        page.close()
                    if not simple:
                        return False
                    has_text |= has_words
        except (OSError, ValueError, pdfplumber.utils.exceptions.PdfminerException):
            return False
        return has_text

    @staticmethod
    def _page_layout(page):
        words = page.extract_words()
        if not words:
            # An empty page is harmless; a page with non-text content is not.
            return not (page.chars or page.images or page.edges or page.curves), False
        if any(i["width"] * i["height"] >= 0.25 * page.width * page.height for i in page.images):
            return False, True
        if len(page.horizontal_edges) >= 2 and len(page.vertical_edges) >= 2:
            return False, True
        if any(not word["upright"] for word in words):
            return False, True

        rows = []
        for word in sorted(words, key=lambda w: w["bottom"]):
            if not rows or abs(word["bottom"] - rows[-1][0]["bottom"]) > 4:
                rows.append([])
            rows[-1].append(word)
        starts = []
        for row in rows:
            row.sort(key=lambda w: w["x0"])
            starts.append(row[0]["x0"])
            # A wide gap on one baseline can be a gutter or a borderless table.
            if any(right["x0"] - left["x1"] >= 20 for left, right in zip(row, row[1:])):
                return False, True
        # Catch staggered columns even when no two lines share a baseline.
        if max(starts) - min(starts) >= 0.15 * page.width:
            return False, True
        return any(char.isalnum() for word in words for char in word["text"]), True

    def docling(self) -> Data:
        return Data(data={}) if self._use_pdfplumber else self.file_path

    def pdfplumber_path(self) -> Data:
        return self.file_path if self._use_pdfplumber else Data(data={})
