# secure_ai_agent_platform — Langflow v2 deployment

Langflow 1.13.0 + Docling (CPU) + pdfplumber + VeriAgent guard components.
Published images (Docker Hub, deploy as a pair):
- `aso9864/langflow-v2:1.0.29` — main app
- `aso9864/docling-serve-cpu:1.0.29` — document conversion service (EasyOCR Cyrillic baked in)

## Layout
- `Dockerfile` — builds on `langflowai/langflow:1.13.0`; installs CPU torch first (CUDA wheels never enter layers), then lfx-docling/calamine/neo4j/pdfplumber; patches two lfx files; pre-caches Docling/OCR models into the image.
- `patched_file.py` → `lfx/components/files_and_knowledge/file.py` (rapidocr option + Excel auto-detect)
- `patched_tracing.py` → `lfx/services/tracing/service.py` (full OTel tracing: flow/component spans, OTLP export, LLM scope allowlist)
- `precache_models.py` — build-time model download step
- `custom_components/` — mounted at `/app/custom_components` (LANGFLOW_COMPONENTS_PATH): PDFPlumber + VeriAgent input/grounding/tool guards (advisory, Ed25519 receipts in postgres `verai_receipts`) + **DoclingRemote** (sends files to the docling-serve container)
- `docker-compose.yml` — langflow + docling-serve + postgres 16; all secrets via `secrets.env`

## Deploy
```
git clone <this repo> && cd secure_ai_agent_platform
cp secrets.env.example secrets.env   # fill real values, chmod 600
docker-compose up -d
```
Offline host: `docker save` both images on a machine with internet, transfer, `docker load`, then the same compose up.

## Simple Flow Runner UI
`GET /simpleui` — минимальный веб-интерфейс для запуска flow (то же, что curl к `/api/v1/run/{flow_id}`):
API-ключ + flow (список через логин) + session_id + чат. Тот же origin, что и API — CORS не нужен.
Реализация: `patched_main.py` → `langflow/main.py` (роут) + `simpleui/index.html` → `/app/simpleui/`.

## DoclingRemote component
Flow node that converts documents via the `docling-serve` container instead of in-process Docling.
- URL: `http://docling-serve:5001` (compose service name)
- Inputs: files (list), OCR engine (easyocr/tesseract/rapidocr/auto), languages (`en,ru`), images scale (default **2.0**), picture classification, force OCR.
- Outputs: Text (markdown/text) and Docling JSON (`doc` key — feeds the Chunk DoclingDocument node directly).

## Memory (measured, 28-page scanned Cyrillic PDF, easyocr + picture classification, images_scale=2.0)
| Container | Limit | Peak |
|---|---|---|
| langflow (1 worker) | 4g | ~1.6 GB |
| docling-serve (1 worker) | 8g | ~4–7 GB |

- `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1` is required: the default 2 workers double model memory and OOM at 8 GB.
- docling-serve RSS grows across consecutive conversions; with 1 worker + 8 GB limit it sustains normal sequential load. If it ever OOMs, `restart: unless-stopped` brings it back clean.
- easyocr at default scale (3.0) needs >10 GB — keep `images_scale=2.0`.
- tesserocr is ~5x faster and lighter (~5.5 GB) if Cyrillic quality via EasyOCR is not required.

## Build
```
docker build -t langflow-v2-docling:1.0.29-rc .
bash flatten.sh            # -> langflow-v2-docling:1.0.29
docker tag langflow-v2-docling:1.0.29 aso9864/langflow-v2:1.0.29
# docling-serve-cpu: see docling-serve-cpu/Dockerfile (base image + cyrillic_g2.pth)
docker build -t aso9864/docling-serve-cpu:1.0.29 docling-serve-cpu/
```

## Notes
- Docling runs CPU-only (torch 2.15+cpu); image ~7GB.
- VeriAgent guards are advisory (no block) — enforcement is caller-side.
- `LANGFLOW_CUSTOM_COMPONENT_ADMIN_ONLY=true` — only superuser may create components.

## Flattened builds
Builds 1.0.28+ are single-layer (docker export|docker import via flatten.sh + gen_changes.py) so removed files (node, old pillow/certifi, faker uk_UA) are absent from ALL image blobs for composition scanners. Rebuild: docker build as usual, then bash flatten.sh.
