"""Pre-cache Docling and OCR models during Docker build.

Downloads all required models so they are baked into the image,
eliminating runtime SSL/network failures.
"""

import os
import subprocess
import sys
import tempfile
import warnings

# Ensure the HF cache path matches what the container will use at runtime
os.environ.setdefault("HF_HOME", "/app/data/.cache/huggingface")

# Disable progress bars in build logs
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

# ---------------------------------------------------------------------------
# 1. Pre-download HuggingFace model repos used by Docling
# ---------------------------------------------------------------------------
print("=" * 60)
print("Step 1/2: Pre-downloading HuggingFace Docling models...")
print("=" * 60)

from huggingface_hub import snapshot_download  # noqa: E402

REPOS = [
    "docling-project/docling-layout-heron",
    "docling-project/docling-models",
]

for repo in REPOS:
    print(f"  Downloading {repo} ...")
    path = snapshot_download(repo)
    print(f"    -> {path}")

# ---------------------------------------------------------------------------
# 2. Trigger RapidOCR model download by running a test conversion
# ---------------------------------------------------------------------------
print()
print("=" * 60)
print("Step 2/2: Triggering RapidOCR model download via test conversion...")
print("=" * 60)

from docling.document_converter import DocumentConverter  # noqa: E402
from PIL import Image  # noqa: E402

tmp = tempfile.mktemp(suffix=".png")
img = Image.new("RGB", (50, 20), color="white")
img.save(tmp)

try:
    converter = DocumentConverter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = converter.convert(tmp)
        _ = result.document.export_to_text()
    print("  Test conversion completed successfully")
finally:
    os.unlink(tmp)

# ---------------------------------------------------------------------------
# 3. Verify cache exists
# ---------------------------------------------------------------------------
print()
cache_dir = os.environ["HF_HOME"]
if os.path.isdir(cache_dir):
    total = 0
    for dirpath, dirnames, filenames in os.walk(cache_dir):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    print(f"Model cache size: {total / (1024*1024):.0f} MB at {cache_dir}")
else:
    print(f"WARNING: cache directory {cache_dir} not found")

print()
print("All models pre-cached successfully")
