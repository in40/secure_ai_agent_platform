FROM astra-linux/alse-python:1.8.1-3.13.5
USER root
# ИБ: база на frozen/1.8.4 — переключаемся на 1.8.7 (openssl 3.4.0-2-astra14, perl astra9,
# util-linux astra16, glibc astra14, python3.11 astra20, expat astra5, brotli b4) и обновляем ОС
RUN sed -i 's|frozen/1.8_x86-64/1.8.4|frozen/1.8_x86-64/1.8.7|g' /etc/apt/sources.list \
    && apt-get update && apt-get dist-upgrade -y && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 poppler-utils findutils \
      fonts-dejavu-core fonts-liberation libxrender1 libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 -g 0 user && mkdir -p /app /app/data && chown -R 1000:0 /app
USER user
ENV HOME=/app/data \
    PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 HF_HOME=/app/data/.cache/huggingface
RUN python3 -m venv /app/.venv && /app/.venv/bin/pip install -U pip uv
RUN /app/.venv/bin/uv pip install --python /app/.venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
RUN /app/.venv/bin/uv pip install --python /app/.venv/bin/python \
      lfx==1.13.3 langchain-openai docling pdfplumber openpyxl "pillow>=13.0.0" "certifi>=2026.10.19"
COPY components/ /app/custom_components/
COPY vllm_tyk_lib/ /app/_vllm_tyk_lib/
# ИБ/габариты: тулчейн после установки wheels не нужен (flatten squash'ит — удаление реально уменьшает образ)
# + удаление неиспользуемых gnupg/wget (CVE-пакеты; apt к тому моменту не нужен, dpkg --force закрывает зависимость apt от gpgv1)
# + санитайзер политических prompt-примеров langchain_classic (заключение ИБ от 24.11.2026)
COPY sanitize_prompts.py /tmp/sanitize_prompts.py
USER root
RUN apt-get update && apt-get purge -y \
      gcc g++ cpp binutils binutils-common binutils-x86-64-linux-gnu \
      libc6-dev libssl-dev linux-libc-dev libgcc-12-dev libstdc++-12-dev man-db \
    && apt-get autoremove --purge -y \
    && (apt-get purge -y wget || true) \
    && rm -rf /var/lib/apt/lists/* \
    && /app/.venv/bin/python /tmp/sanitize_prompts.py && rm /tmp/sanitize_prompts.py
USER user
ENV LANGFLOW_COMPONENTS_PATH=/app/custom_components PYTHONPATH=/app:/app/custom_components
EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD /app/.venv/bin/python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7860/health',timeout=4)" || exit 1
CMD ["/app/.venv/bin/lfx", "serve", "/flows/flow.json", "--host", "0.0.0.0", "--port", "7860"]
