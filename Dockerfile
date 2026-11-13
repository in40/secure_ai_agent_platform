FROM langflowai/langflow:1.13.0

USER root

RUN microdnf install -y --setopt=install_weak_deps=0 \
    mesa-libGL \
    glib2 \
    poppler-utils \
    && microdnf clean all && rm -rf /var/cache/dnf/*

# LibreOffice (headless) — legacy doc/ppt/xls conversion for docling
RUN microdnf install -y --setopt=install_weak_deps=0 \
      curl expat glib2 fontconfig freetype libX11 libXext libXrender libXft libXt \
      libXinerama libXrandr libXcursor libXcomposite libXdamage libXfixes \
      libxkbcommon gtk3 pango cairo nss cups-libs dbus-libs glibc-langpack-en findutils \
    && curl -sL -o /tmp/lo.tar.gz https://download.documentfoundation.org/libreoffice/stable/26.8.2/rpm/x86_64/LibreOffice_26.8.2_Linux_x86-64_rpm.tar.gz \
    && cd /tmp && tar xzf lo.tar.gz \
    && rpm -ivh --nodeps \
         /tmp/LibreOffice_*/RPMS/libreoffice26.8-26*.rpm \
         /tmp/LibreOffice_*/RPMS/libreoffice26.8-ure*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-core*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-writer*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-impress*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-calc*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-en-US*.rpm \
         /tmp/LibreOffice_*/RPMS/libobasis26.8-graphicfilter*.rpm \
    && ln -sf /opt/libreoffice26.8/program/soffice /usr/bin/soffice \
    && rm -rf /tmp/lo.tar.gz /tmp/LibreOffice_* \
    && microdnf clean all && rm -rf /var/cache/dnf/*

USER user

RUN uv pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
RUN uv pip install "lfx-docling[local,image-description]" "python-calamine" "lfx-bundles" neo4j pdfplumber easyocr && rm -rf /app/.venv/lib/python3.14/site-packages/faker/providers/*/uk_UA /app/data/.cache/uv/archive-v0/*/faker/providers/*/uk_UA

# Infosec: pillow>=13 (CVE-2026-112403/112398/112900/96639), certifi>=2026.10.19 (emSign CA), drop node runtime (CVE-2026-112393)
RUN uv pip install -U "pillow>=13.0.0" "certifi>=2026.10.19"
USER root
RUN rm -f /usr/local/bin/node
USER user

COPY patched_file.py /app/.venv/lib/python3.14/site-packages/lfx/components/files_and_knowledge/file.py
COPY patched_tracing.py /app/.venv/lib/python3.14/site-packages/lfx/services/tracing/service.py

# Pre-cache all Docling/OCR models so runtime SSL/network failures are avoided
COPY --chown=1000:0 precache_models.py /tmp/precache_models.py
RUN uv run python3 /tmp/precache_models.py && rm /tmp/precache_models.py

ENV HF_HOME=/app/data/.cache/huggingface

EXPOSE 7860

# Custom components
COPY custom_components/ /app/custom_components/
ENV LANGFLOW_COMPONENTS_PATH=/app/custom_components

# EasyOCR models (craft detection + recognition incl. cyrillic) for offline OCR
COPY --chown=1000:0 easyocr_models/ /app/data/.cache/docling/models/EasyOcr/

# tesserocr language data (eng/rus/ukr, tessdata_fast)
COPY tessdata/ /usr/share/tessdata/
ENV TESSDATA_PREFIX=/usr/share/tessdata

# startup pre-imports: avoid openai import-lock deadlock in docling child procs
COPY sitecustomize.py /app/.venv/lib/python3.14/site-packages/sitecustomize.py
COPY patched_docling_inline.py /app/.venv/lib/python3.14/site-packages/lfx_docling/components/docling/docling_inline.py
COPY patched_qdrant_bundle.py /app/.venv/lib/python3.14/site-packages/lfx_bundles/qdrant/qdrant.py
COPY patched_frontend_index.js /app/.venv/lib/python3.14/site-packages/langflow/frontend/assets/index-CpAth-6K.js
COPY patched_chat.py /app/.venv/lib/python3.14/site-packages/lfx/components/input_output/chat.py
COPY patched_lfx_schemas.py /app/.venv/lib/python3.14/site-packages/lfx/utils/schemas.py
COPY patched_langflow_schemas.py /app/.venv/lib/python3.14/site-packages/langflow/utils/schemas.py
COPY patched_main.py /app/.venv/lib/python3.14/site-packages/langflow/main.py
COPY simpleui/ /app/simpleui/
ENV PYTHONPATH=/app/custom_components
