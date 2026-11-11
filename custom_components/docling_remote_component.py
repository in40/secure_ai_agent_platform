import time
from pathlib import Path

import httpx

from langflow.custom import Component
from langflow.io import (
    BoolInput,
    DropdownInput,
    FileInput,
    FloatInput,
    StrInput,
    Output,
)
from langflow.schema import Data


class DoclingRemoteComponent(Component):
    display_name = "Docling Remote"
    description = "Convert documents via remote Docling Serve (OCR engines, picture classification, scale)."
    icon = "file-text"
    name = "DoclingRemote"

    inputs = [
        FileInput(
            name="input_files",
            display_name="Documents",
            info="Files to send to Docling Serve.",
            file_types=["pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg", "tiff", "bmp", "html", "md"],
            is_list=True,
            required=True,
        ),
        StrInput(
            name="base_url",
            display_name="Docling Serve URL",
            info="Base URL, e.g. http://docling-serve:5001",
            value="http://docling-serve:5001",
            required=True,
            tool_mode=False,
        ),
        StrInput(
            name="api_key",
            display_name="API Key (optional)",
            info="Bearer token, empty if not required.",
            value="",
            tool_mode=False,
        ),
        DropdownInput(
            name="ocr_engine",
            display_name="OCR Engine",
            info="OCR engine on the server side.",
            options=["auto", "easyocr", "tesseract", "rapidocr"],
            value="easyocr",
            tool_mode=False,
        ),
        StrInput(
            name="ocr_lang",
            display_name="OCR Languages",
            info="Comma-separated easyocr codes, e.g. en,ru",
            value="en,ru",
            tool_mode=False,
        ),
        FloatInput(
            name="images_scale",
            display_name="Images Scale (dpi/72)",
            info="Page rasterization scale. 2.0 (~144dpi) fits 8GB; 3.0 (~216dpi) needs ~10GB with easyocr.",
            value=2.0,
            tool_mode=False,
        ),
        BoolInput(
            name="do_picture_classification",
            display_name="Picture Classification",
            info="Enable figure classification stage.",
            value=False,
            tool_mode=False,
        ),
        BoolInput(
            name="force_ocr",
            display_name="Force OCR",
            info="Run OCR even when a text layer exists.",
            value=False,
            tool_mode=False,
        ),
        DropdownInput(
            name="output_format",
            display_name="Output Format",
            info="Text output format (Docling JSON output is always available).",
            options=["markdown", "text"],
            value="markdown",
            tool_mode=False,
        ),
        FloatInput(
            name="max_wait",
            display_name="Max Wait (sec)",
            info="Maximum time to wait per document.",
            value=900.0,
            tool_mode=False,
        ),
    ]

    outputs = [
        Output(display_name="Text", name="text_out", method="build_text", group_outputs=True),
        Output(display_name="Docling JSON", name="doc_json", method="build_doc_json", group_outputs=True),
    ]

    def _resolve_paths(self) -> list[str]:
        files = self.input_files
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
            msg = "DoclingRemote: no input files resolved"
            raise ValueError(msg)
        return paths

    def _convert(self, path: str) -> dict:
        base_url = (getattr(self, "base_url", "") or "").rstrip("/")
        headers = {}
        api_key = getattr(self, "api_key", "") or ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        langs = [x.strip() for x in (getattr(self, "ocr_lang", "") or "").split(",") if x.strip()]
        data = {
            "do_ocr": "true",
            "force_ocr": "true" if getattr(self, "force_ocr", False) else "false",
            "ocr_engine": getattr(self, "ocr_engine", "easyocr"),
            "images_scale": str(getattr(self, "images_scale", 2.0)),
            "do_picture_classification": "true" if getattr(self, "do_picture_classification", False) else "false",
            "to_formats": ["md", "json"],
            "ocr_lang": langs,
        }

        max_wait = float(getattr(self, "max_wait", 900.0))
        c = httpx.Client(timeout=60.0, headers=headers)
        try:
            with open(path, "rb") as f:
                r = c.post(
                    f"{base_url}/v1/convert/file/async",
                    files={"files": (Path(path).name, f)},
                    data=data,
                )
            if r.status_code != 200:
                msg = f"DoclingRemote submit failed: {r.status_code} {r.text[:300]}"
                raise ValueError(msg)
            task_id = r.json().get("task_id")
            if not task_id:
                msg = f"DoclingRemote: no task_id in {r.text[:200]}"
                raise ValueError(msg)

            deadline = time.time() + max_wait
            while time.time() < deadline:
                time.sleep(5)
                sr = c.get(f"{base_url}/v1/status/poll/{task_id}")
                st = sr.json().get("task_status", "")
                if st == "success":
                    rr = c.get(f"{base_url}/v1/result/{task_id}")
                    if rr.status_code != 200:
                        msg = f"DoclingRemote result fetch failed: {rr.status_code} {rr.text[:200]}"
                        raise ValueError(msg)
                    return rr.json().get("document", {})
                if st == "failure":
                    msg = f"DoclingRemote job failed on server: {sr.text[:300]}"
                    raise ValueError(msg)
            msg = f"DoclingRemote timed out after {max_wait}s"
            raise ValueError(msg)
        finally:
            c.close()

    def _all_docs(self) -> list[dict]:
        if not hasattr(self, "_cached_docs"):
            self._cached_docs = [self._convert(p) for p in self._resolve_paths()]
        return self._cached_docs

    def _doc_text(self, doc: dict) -> str:
        if getattr(self, "output_format", "markdown") == "markdown":
            return doc.get("md_content") or ""
        jc = doc.get("json_content") or {}
        return "\n\n".join(t.get("text", "") for t in jc.get("texts", []) if t.get("text"))

    def build_text(self) -> str:
        docs = self._all_docs()
        self.status = f"{len(docs)} document(s) processed"
        return "\n\n---\n\n".join(self._doc_text(d) for d in docs)

    def build_doc_json(self) -> list[Data]:
        docs = self._all_docs()
        self.status = f"{len(docs)} document(s) processed"
        return [Data(data={"doc": d.get("json_content", {}), "filename": d.get("filename", "")}) for d in docs]
