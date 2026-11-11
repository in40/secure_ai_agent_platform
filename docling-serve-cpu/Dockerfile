FROM quay.io/docling-project/docling-serve:latest

# EasyOCR Cyrillic model (not shipped in upstream CPU image)
COPY --chown=1001:1001 cyrillic_g2.pth /opt/app-root/src/.cache/docling/models/EasyOcr/cyrillic_g2.pth

# Memory watchdog: recycles container when idle memory stays above WATCHDOG_MEM_LIMIT_GB
COPY --chown=1001:1001 mem_watchdog.py /opt/mem_watchdog.py
COPY --chown=1001:1001 dls_entrypoint.sh /opt/entrypoint-wrapper.sh
RUN chmod +x /opt/entrypoint-wrapper.sh
ENV WATCHDOG_MEM_LIMIT_GB=6.5
ENTRYPOINT ["/opt/entrypoint-wrapper.sh"]
