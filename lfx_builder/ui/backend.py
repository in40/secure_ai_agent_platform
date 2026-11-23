"""lfx-builder backend: приём заданий на сборку LFX-образа из flow JSON.

Auth: Bearer-токен langflow валидируется через /api/v1/users/whoami (кэш 60 с).
Jobs: последовательный воркер; состояние в jobs/<id>/meta.json, лог в build.log.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

LANGFLOW_URL = os.environ.get("LFXB_LANGFLOW_URL", "http://127.0.0.1:7861")
JOBS_DIR = Path(os.environ.get("LFXB_JOBS", "/home/sorokin/lfx-builder/jobs"))
REPO = Path(os.environ.get("LFXB_REPO", "/home/sorokin/repos/secure_ai_agent_platform"))
LF2LFX = REPO / "lfx_builder" / "lf2lfx.py"
FLATTEN = REPO / "flatten.sh"
GEN_CHANGES = REPO / "gen_changes.py"
BASE_IMAGE = os.environ.get("LFXB_BASE", "astra-exec:0.1")

app = FastAPI(docs_url=None, redoc_url=None)
_jobs_q: queue.Queue[str] = queue.Queue()
_auth_cache: dict[str, tuple[str, float]] = {}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


def auth(authorization: str = Header("")) -> str:
    m = re.match(r"Bearer\s+(\S+)", authorization)
    if not m:
        raise HTTPException(401, "нет токена")
    tok = m.group(1)
    key = hashlib.sha256(tok.encode()).hexdigest()
    hit = _auth_cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        r = httpx.get(f"{LANGFLOW_URL}/api/v1/users/whoami",
                      headers={"Authorization": f"Bearer {tok}"}, timeout=10)
    except Exception as e:
        raise HTTPException(502, f"langflow недоступен: {e}")
    if r.status_code != 200:
        raise HTTPException(401, "токен не принят langflow")
    user = (r.json().get("username") or "?")
    _auth_cache[key] = (user, time.time() + 60)
    return user


class BuildReq(BaseModel):
    flow_id: str
    name: str
    flow: dict


def _meta(job: Path) -> dict:
    return json.loads((job / "meta.json").read_text())


def _save(job: Path, meta: dict) -> None:
    (job / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))


def _log(job: Path, line: str) -> None:
    with (job / "build.log").open("a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")


def _run(job: Path, cmd: list[str]) -> None:
    _log(job, "$ " + " ".join(cmd))
    p = subprocess.run(cmd, cwd=job, capture_output=True, text=True, timeout=3600)
    tail = (p.stdout + p.stderr).strip().splitlines()[-40:]
    for t in tail:
        _log(job, t)
    if p.returncode != 0:
        raise RuntimeError(f"шаг завершился с кодом {p.returncode}")


def _analyze(flow: dict) -> list[dict]:
    nodes = (flow.get("data") or flow).get("nodes", [])
    out: dict[str, dict] = {}
    for n in nodes:
        t = (n.get("data") or {}).get("type") or n.get("type") or "?"
        code = ((n.get("data") or {}).get("node") or {}).get("template", {}).get("code", {}).get("value", "")
        imports = sorted({m.group(1).split(".")[0] for m in re.finditer(r"^(?:from|import)\s+([a-zA-Z_][\w.]*)", code, re.M)}
                         - {"lfx", "langflow", "typing", "os", "sys", "json", "time", "re", "__future__"})
        e = out.setdefault(t, {"type": t, "custom": False, "imports": set()})
        e["custom"] = e["custom"] or bool(code)
        e["imports"].update(imports)
    return [{**v, "imports": sorted(v["imports"])[:8]} for v in out.values()]


def _worker() -> None:
    while True:
        jid = _jobs_q.get()
        job = JOBS_DIR / jid
        meta = _meta(job)
        try:
            meta["status"] = "running"
            _save(job, meta)
            _log(job, f"база: {BASE_IMAGE}")
            _run(job, ["python3", str(LF2LFX), "--flow", "flow.json", "-o", "flow.lfx.json"])
            comps = _analyze(json.loads((job / "flow.json").read_text()))
            (job / "components.json").write_text(json.dumps(comps, ensure_ascii=False, indent=1))
            _log(job, "компоненты: " + ", ".join(c["type"] + ("*" if c["custom"] else "") for c in comps))
            tag = f"lfx-{meta['name']}:{jid[:8]}"
            (job / "Dockerfile").write_text(
                f"FROM {BASE_IMAGE}\n"
                "COPY flow.lfx.json /flows/flow.json\n"
                'CMD ["/app/.venv/bin/lfx", "serve", "/flows/flow.json", "--host", "0.0.0.0", "--port", "7860"]\n')
            _run(job, ["docker", "build", "-q", "-t", tag, "."])
            shutil.copy(GEN_CHANGES, "/tmp/gen_changes.py")
            _run(job, ["bash", str(FLATTEN), tag, f"lfx-{meta['name']}:latest"])
            size = subprocess.run(["docker", "images", f"lfx-{meta['name']}:latest",
                                   "--format", "{{.Size}}"], capture_output=True, text=True).stdout.strip()
            meta.update(status="success", image=f"lfx-{meta['name']}:latest", size=size)
        except Exception as e:  # noqa: BLE001
            meta.update(status="failed", error=str(e)[:300])
            _log(job, f"ОШИБКА: {e}")
        meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(job, meta)
        _jobs_q.task_done()


threading.Thread(target=_worker, daemon=True).start()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "base": BASE_IMAGE}


@app.post("/api/builds")
def create_build(req: BuildReq, user: str = Depends(auth)) -> dict:
    if not NAME_RE.match(req.name):
        raise HTTPException(400, "имя образа: только строчные латиница/цифры/дефис, 2–40")
    jid = uuid.uuid4().hex
    job = JOBS_DIR / jid
    job.mkdir(parents=True)
    (job / "flow.json").write_text(json.dumps(req.flow, ensure_ascii=False))
    _save(job, {"id": jid, "user": user, "name": req.name, "flow_id": req.flow_id,
                "flow_name": req.flow.get("name", ""), "status": "queued",
                "created": time.strftime("%Y-%m-%d %H:%M:%S")})
    _jobs_q.put(jid)
    return {"job_id": jid}


@app.get("/api/builds")
def list_builds(user: str = Depends(auth)) -> list[dict]:
    out = []
    for j in sorted(JOBS_DIR.iterdir(), reverse=True) if JOBS_DIR.exists() else []:
        try:
            m = _meta(j)
        except Exception:
            continue
        if m.get("user") == user:
            m.pop("flow", None)
            out.append(m)
    return out[:50]


@app.get("/api/builds/{jid}")
def get_build(jid: str, user: str = Depends(auth)) -> dict:
    job = JOBS_DIR / jid
    if not job.exists():
        raise HTTPException(404)
    m = _meta(job)
    if m.get("user") != user:
        raise HTTPException(403)
    log = (job / "build.log").read_text().splitlines()[-120:] if (job / "build.log").exists() else []
    comps = json.loads((job / "components.json").read_text()) if (job / "components.json").exists() else []
    return {"meta": m, "log": log, "components": comps}
