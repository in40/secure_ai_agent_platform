# secure_ai_agent_platform — Langflow v2 deployment

Langflow 1.13.0 + Docling (CPU) + pdfplumber + VeriAgent guard components.
Published image: `aso9864/langflow-v2:1.0.17` (Docker Hub).

## Layout
- `Dockerfile` — builds on `langflowai/langflow:1.13.0`; installs CPU torch first (CUDA wheels never enter layers), then lfx-docling/calamine/neo4j/pdfplumber; patches two lfx files; pre-caches Docling/OCR models into the image.
- `patched_file.py` → `lfx/components/files_and_knowledge/file.py` (rapidocr option + Excel auto-detect)
- `patched_tracing.py` → `lfx/services/tracing/service.py` (full OTel tracing: flow/component spans, OTLP export, LLM scope allowlist)
- `precache_models.py` — build-time model download step
- `custom_components/` — mounted at `/app/custom_components` (LANGFLOW_COMPONENTS_PATH): PDFPlumber + VeriAgent input/grounding/tool guards (advisory, Ed25519 receipts in postgres `verai_receipts`)
- `docker-compose.yml` — langflow + postgres 16; all secrets via `secrets.env`

## Deploy
```
git clone <this repo> && cd secure_ai_agent_platform
cp secrets.env.example secrets.env   # fill real values, chmod 600
docker-compose up -d
```

## Build
```
docker build -t langflow-v2-docling:1.0.22 .
```

## Notes
- Docling runs CPU-only (torch 2.15+cpu); image ~7GB.
- VeriAgent guards are advisory (no block) — enforcement is caller-side.
- `LANGFLOW_CUSTOM_COMPONENT_ADMIN_ONLY=true` — only superuser may create components.
