import glob
import os
import urllib.parse

from langflow.custom import Component
from langflow.io import Output, StrInput
from langflow.schema import Data


class UploadedFileComponent(Component):
    display_name = "Uploaded File"
    description = (
        "Resolves file path(s) uploaded via the API (POST /api/v2/files) or attached in Chat Input. "
        "Set the file_path input at run time via tweaks from the Flow Runner UI."
    )
    icon = "paperclip"
    name = "UploadedFile"

    inputs = [
        StrInput(
            name="file_path",
            display_name="File Path(s)",
            info=(
                "Path(s) returned by /api/v2/files upload, relative to LANGFLOW_CONFIG_DIR. "
                "Separate multiple files with newlines or commas."
            ),
            value="",
            is_list=False,
            dynamic=True,
            show=True,
        ),
    ]

    outputs = [
        Output(display_name="File Path", name="file_path_out", method="build_path", group_outputs=True),
        Output(display_name="File Data", name="file_data", method="build_data", group_outputs=True),
        Output(display_name="Filename", name="filename", method="build_filename", group_outputs=True),
    ]

    def _config_dir(self) -> str:
        return os.environ.get("LANGFLOW_CONFIG_DIR", "/app/langflow")

    def _locate(self, p: str) -> str | None:
        """Resolve an upload path leniently.

        Exact match first; then search user storage dirs by basename, tolerating
        URL-encoding (%20), case differences and date/timestamp prefixes that
        storage layers sometimes add to saved file names.
        """
        if not os.path.isabs(p):
            p = os.path.join(self._config_dir(), p)
        if os.path.exists(p):
            return p
        base = os.path.basename(urllib.parse.unquote(p))
        for cand in (
            base,
            urllib.parse.unquote(base),
        ):
            if not cand:
                continue
            for pat in (
                os.path.join(self._config_dir(), "*", cand),
                os.path.join(self._config_dir(), "*", "*_" + cand),
                os.path.join(self._config_dir(), "*", "*" + cand),
                os.path.join(self._config_dir(), "*", "*", cand),
            ):
                hits = sorted(glob.glob(pat))
                if hits:
                    return hits[-1]
            low = cand.lower()
            for d in glob.glob(os.path.join(self._config_dir(), "*/")):
                try:
                    for f in os.listdir(d):
                        if f.lower() == low or f.lower().endswith("_" + low):
                            return os.path.join(d, f)
                except OSError:
                    continue
        return None

    def _paths(self) -> list[str]:
        raw = self.file_path or ""
        parts = [p.strip() for p in raw.replace(",", "\n").splitlines() if p.strip()]
        resolved = []
        for p in parts:
            found = self._locate(p)
            if found is None:
                msg = f"UploadedFile: file not found: {p} (searched {self._config_dir()} by name too)"
                raise ValueError(msg)
            resolved.append(found)
        return resolved

    def build_path(self) -> str:
        paths = self._paths()
        self.status = f"{len(paths)} file(s)"
        return "\n".join(paths)

    def build_data(self) -> list[Data]:
        paths = self._paths()
        self.status = f"{len(paths)} file(s)"
        return [
            Data(data={"file_path": p, "path": p, "file_name": os.path.basename(p), "filename": os.path.basename(p)})
            for p in paths
        ]

    def build_filename(self) -> str:
        paths = self._paths()
        self.status = f"{len(paths)} file(s)"
        return ", ".join(os.path.basename(p) for p in paths)
