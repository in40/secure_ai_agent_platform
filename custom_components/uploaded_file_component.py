import os

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

    def _paths(self) -> list[str]:
        raw = self.file_path or ""
        parts = [p.strip() for p in raw.replace(",", "\n").splitlines() if p.strip()]
        resolved = []
        for p in parts:
            if not os.path.isabs(p):
                cand = os.path.join(self._config_dir(), p)
                if os.path.exists(cand):
                    p = cand
            resolved.append(p)
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
