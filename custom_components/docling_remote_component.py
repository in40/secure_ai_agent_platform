import base64
import time
from typing import Any

import httpx

from langflow.custom import Component
from langflow.io import DropdownInput, FloatInput, StrInput, Output
from langflow.schema import Data


class DoclingRemoteComponent(Component):
    display_name = "Docling Remote"
    description = "Extract structured data from documents via remote Docling Serve API."
    icon = "file-text"
    name = "DoclingRemote"

    inputs = [
        StrInput(
            name="base_url",
            display_name="Docling Serve URL",
            info="Base URL of Docling Serve, e.g. http://docling-serve-gpu-2:5001",
            value="http://docling-serve-gpu-2:5001",
            required=True,
            tool=False,
        ),
        StrInput(
            name="api_key",
            display_name="API Key (optional)",
            info="Bearer token for authentication, leave empty if not required.",
            value="",
            password=False,
            tool=False,
        ),
        DropdownInput(
            name="output_format",
            display_name="Output Format",
            info="Format of extracted content.",
            options=["markdown", "json", "text"],
            value="markdown",
            tool=False,
        ),
        FloatInput(
            name="poll_interval",
            display_name="Poll Interval (sec)",
            info="Seconds between status polls.",
            value=2.0,
            tool=False,
        ),
        FloatInput(
            name="max_wait",
            display_name="Max Wait (sec)",
            info="Maximum time to wait for processing.",
            value=120.0,
            tool=False,
        ),
    ]

    outputs = [
        Output(display_name="Data", name="data", method="build_output"),
    ]

    def _get_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        api_key = getattr(self, "api_key", "") or ""
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _upload_and_wait(self, file_path: str, headers: dict, base_url: str, output_format: str, poll_interval: float, max_wait: float):
        c = httpx.Client(timeout=max_wait + 10)
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            b64 = base64.b64encode(content).decode()
            fname = file_path.split("/")[-1] if "/" in file_path else file_path

            r = c.post(
                f"{base_url}/v1/convert/source/async",
                headers=headers,
                json={
                    "options": {"to_formats": ["json"]},
                    "sources": [{"kind": "file", "base64_string": b64, "filename": fname}],
                },
            )
            if r.status_code != 200:
                self.status = f"Upload failed: {r.status_code} {r.text[:200]}"
                return None
            task_id = r.json().get("task_id")
            if not task_id:
                self.status = "No task_id in response"
                return None

            deadline = time.time() + max_wait
            while time.time() < deadline:
                sr = c.get(f"{base_url}/v1/status/poll/{task_id}")
                d = sr.json()
                st = d.get("task_status", "")
                if st in ("success", "COMPLETED", "done"):
                    rr = c.get(f"{base_url}/v1/result/{task_id}")
                    if rr.status_code == 200:
                        return rr.json()
                    return None
                elif st in ("failed", "FAILURE", "error"):
                    self.status = f"Job failed: {sr.text[:200]}"
                    return None
                time.sleep(poll_interval)
            self.status = "Timeout waiting for result"
            return None
        finally:
            c.close()

    def _format_result(self, result: dict, output_format: str) -> str:
        doc = result.get("document", {})
        jc = doc.get("json_content", {})
        if output_format == "json":
            return str(jc)
        if output_format == "text":
            texts = jc.get("texts", [])
            return "\n\n".join(t.get("text", "") for t in texts if t.get("text"))
        # markdown (default)
        md_content = doc.get("md_content")
        if md_content:
            return md_content
        # Build markdown from json_content
        texts = jc.get("texts", [])
        parts = []
        for t in texts:
            label = t.get("label", "text")
            text = t.get("text", "")
            if label == "title":
                parts.append(f"# {text}")
            elif label == "section_header":
                parts.append(f"## {text}")
            elif label == "subtitle_text":
                parts.append(f"### {text}")
            else:
                parts.append(text)
        return "\n\n".join(parts)

    def build_output(self) -> list[Data]:
        base_url = getattr(self, "base_url", "http://docling-serve-gpu-2:5001")
        if not base_url.endswith("/"):
            base_url = base_url + "/"
        output_format = getattr(self, "output_format", "markdown")
        poll_interval = float(getattr(self, "poll_interval", 2.0))
        max_wait = float(getattr(self, "max_wait", 120.0))
        # Read file path from context - need to figure out how Langflow passes files
        # Check if self has input_files or similar
        file_path = None
        for attr in ["input_files", "file", "input_file", "path"]:
            val = getattr(self, attr, None)
            if val:
                if isinstance(val, list) and len(val) > 0:
                    val = val[0]
                if isinstance(val, Data):
                    file_path = val.data.get("file_path") or val.data.get("path")
                else:
                    file_path = str(val)
                break
        if not file_path:
            msg = "No input file provided"
            raise ValueError(msg)
        headers = self._get_headers()
        result = self._upload_and_wait(file_path, headers, base_url, output_format, poll_interval, max_wait)
        if result is None:
            raise ValueError(self.status or "Unknown error")
        text = self._format_result(result, output_format)
        self.status = "Document processed successfully"
        return [Data(data={"raw": result, "text": text, "filename": file_path})]
