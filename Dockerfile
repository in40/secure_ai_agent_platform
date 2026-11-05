FROM langflowai/langflow:1.13.0

USER root

RUN microdnf install -y --setopt=install_weak_deps=0 \
    mesa-libGL \
    glib2 \
    poppler-utils \
    && microdnf clean all && rm -rf /var/cache/dnf/*

USER user

RUN uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
RUN uv pip install "lfx-docling[local,image-description]" "python-calamine" "lfx-bundles" neo4j pdfplumber easyocr

COPY patched_file.py /app/.venv/lib/python3.14/site-packages/lfx/components/files_and_knowledge/file.py
COPY patched_tracing.py /app/.venv/lib/python3.14/site-packages/lfx/services/tracing/service.py

# Pre-cache all Docling/OCR models so runtime SSL/network failures are avoided
COPY --chown=1000:0 precache_models.py /tmp/precache_models.py
RUN uv run python3 /tmp/precache_models.py && rm /tmp/precache_models.py

ENV HF_HOME=/app/data/.cache/huggingface

EXPOSE 7860

# Custom components
COPY pdfplumber_component.py /app/custom_components/pdfplumber_component.py
ENV LANGFLOW_COMPONENTS_PATH=/app/custom_components

# EasyOCR models (craft detection + recognition incl. cyrillic) for offline OCR
COPY --chown=1000:0 easyocr_models/ /app/data/.cache/docling/models/EasyOcr/

# tesserocr language data (eng/rus/ukr, tessdata_fast)
COPY tessdata/ /usr/share/tessdata/
ENV TESSDATA_PREFIX=/usr/share/tessdata

# startup pre-imports: avoid openai import-lock deadlock in docling child procs
COPY sitecustomize.py /app/.venv/lib/python3.14/site-packages/sitecustomize.py
COPY patched_docling_inline.py /app/.venv/lib/python3.14/site-packages/lfx_docling/components/docling/docling_inline.py
