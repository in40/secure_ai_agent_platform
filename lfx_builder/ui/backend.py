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
BASE_SLIM = os.environ.get("LFXB_BASE_SLIM", "astra-exec-slim:0.1")
# типы/импорты, требующие тяжёлой базы (docling+torch)
DOC_TYPES = {"DoclingInline", "ChunkDoclingDocument"}
SMOKE_ENV = os.environ.get("LFXB_SMOKE_ENV", "/home/sorokin/lfx-builder/smoke.env")
SMOKE_PORTS = range(17860, 17900)
# ноды, для которых L3 (реальный прогон) требует внешних данных — проверяем только загрузку
HEAVY_TYPES = {"QdrantVectorStoreComponent", "EmbeddingModel", "RerankComponent",
               "DoclingInline", "ChunkDoclingDocument", "SessionEnricher"}

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


def _smoke_env_dict() -> dict:
    out = {}
    try:
        for line in Path(SMOKE_ENV).read_text().splitlines():
            k, _, v = line.partition("=")
            if k and v:
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def _free_port() -> int:
    import socket
    for p in SMOKE_PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("нет свободного смоук-порта")


def _smoke(job: Path, meta: dict, comps: list[dict], image: str) -> None:
    """L1 старт+health, L2 flow виден в API — обязательны; L3 реальный /run — advisory."""
    env = _smoke_env_dict()
    if not env.get("LANGFLOW_API_KEY"):
        meta["smoke"] = "skipped: нет smoke.env"
        return
    port = _free_port()
    cname = f"smoke-{meta['id'][:8]}"
    fid = meta["flow_id"]
    heavy = any(c["type"] in HEAVY_TYPES for c in comps)
    lvl = "L1+L2" if heavy else "L3"
    key = env["LANGFLOW_API_KEY"]
    try:
        _run(job, ["docker", "run", "-d", "--name", cname, "--network", "host", "--memory", "2g",
                   "--env-file", SMOKE_ENV, image, "sh", "-c",
                   f"/app/.venv/bin/lfx serve /flows/flow.json --host 127.0.0.1 --port {port}"])
        base = f"http://127.0.0.1:{port}"
        ok = False
        for _ in range(60):
            time.sleep(2)
            r = subprocess.run(["curl", "-sm2", "-o", "/dev/null", "-w", "%{http_code}", base + "/health"],
                               capture_output=True, text=True)
            if r.stdout.strip() == "200":
                ok = True
                break
        if not ok:
            st = subprocess.run(["docker", "inspect", cname, "--format",
                                 "{{.State.Status}}/{{.State.ExitCode}}"], capture_output=True, text=True).stdout.strip()
            logs = subprocess.run(["docker", "logs", cname], capture_output=True, text=True).stdout[-500:]
            raise RuntimeError(f"L1 не прошёл (health timeout, {st}): {logs}")
        flows = httpx.get(f"{base}/flows", headers={"x-api-key": key}, timeout=10).json()
        ids = [f.get("id") for f in (flows if isinstance(flows, list) else flows.get("flows", []))]
        if fid not in ids:
            raise RuntimeError(f"L2: flow {fid} не среди {ids}")
        _log(job, f"смоук {port}: L1 ok, L2 ok (flow виден)")
        meta["smoke"] = f"passed({lvl})"
        if not heavy:
            try:
                r = httpx.post(f"{base}/flows/{fid}/run", headers={"x-api-key": key},
                               json={"input_value": "Ответь одним словом: ок", "session_id": "smoke"},
                               timeout=300)
                body = r.json()
                meta["smoke_run"] = "ok" if body.get("success") else "failed: " + str(body.get("result", ""))[:200]
            except Exception as e:  # noqa: BLE001
                meta["smoke_run"] = f"failed: {str(e)[:200]}"
            _log(job, "смоук L3 (advisory): " + meta["smoke_run"])
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)


def _worker() -> None:
    while True:
        jid = _jobs_q.get()
        job = JOBS_DIR / jid
        meta = _meta(job)
        try:
            meta["status"] = "running"
            _save(job, meta)
            free = shutil.disk_usage("/").free
            if free < 8 * 1024**3:
                raise RuntimeError(f"на хосте мало места: {free // 2**20} МБ (< 8 ГБ)")
            _run(job, ["python3", str(LF2LFX), "--flow", "flow.json", "-o", "flow.lfx.json"])
            comps = _analyze(json.loads((job / "flow.json").read_text()))
            (job / "components.json").write_text(json.dumps(comps, ensure_ascii=False, indent=1))
            _log(job, "компоненты: " + ", ".join(c["type"] + ("*" if c["custom"] else "") for c in comps))
            need_doc = any(c["type"] in DOC_TYPES or "docling" in c["imports"] for c in comps)
            base = BASE_IMAGE if need_doc else BASE_SLIM
            meta["base"] = base
            _log(job, f"база: {base}" + ("" if need_doc else " (slim: docling не нужен)"))
            tag = f"lfx-{meta['name']}:{jid[:8]}"
            (job / "Dockerfile").write_text(
                f"FROM {base}\n"
                "COPY flow.lfx.json /flows/flow.json\n"
                'CMD ["/app/.venv/bin/lfx", "serve", "/flows/flow.json", "--host", "0.0.0.0", "--port", "7860"]\n')
            _run(job, ["docker", "build", "-q", "-t", tag, "."])
            shutil.copy(GEN_CHANGES, "/tmp/gen_changes.py")
            _run(job, ["bash", str(FLATTEN), tag, f"lfx-{meta['name']}:latest"])
            pr = subprocess.run(["docker", "image", "prune", "-f"], capture_output=True, text=True)
            _log(job, "prune: " + (pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else "ok"))
            size = subprocess.run(["docker", "images", f"lfx-{meta['name']}:latest",
                                   "--format", "{{.Size}}"], capture_output=True, text=True).stdout.strip()
            meta.update(image=f"lfx-{meta['name']}:latest", size=size)
            _smoke(job, meta, comps, f"lfx-{meta['name']}:latest")
            meta["status"] = "success"
        except Exception as e:  # noqa: BLE001
            meta.update(status="failed", error=str(e)[:300])
            _log(job, f"ОШИБКА: {e}")
        meta["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save(job, meta)
        _jobs_q.task_done()


threading.Thread(target=_worker, daemon=True).start()

PUSH_LOCK = threading.Lock()
HUB_NS = os.environ.get("LFXB_HUB", "aso9864")


def _push_append(job: Path, line: str) -> None:
    with (job / "build.log").open("a") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] [push] {line}\n")


def _push_job(jid: str) -> None:
    job = JOBS_DIR / jid
    meta = _meta(job)
    name = meta["name"]
    src = f"lfx-{name}:latest"
    hub = f"{HUB_NS}/lfx-{name}"
    ts = f"{hub}:{time.strftime('%Y%m%d-%H%M')}"
    with PUSH_LOCK:
        meta["push"] = {"status": "running", "target": ts}
        _save(job, meta)
        try:
            for cmd in (["docker", "tag", src, ts], ["docker", "tag", src, f"{hub}:latest"],
                        ["docker", "push", ts], ["docker", "push", f"{hub}:latest"]):
                _push_append(job, "$ " + " ".join(cmd))
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
                for t in (p.stdout + p.stderr).strip().splitlines()[-5:]:
                    _push_append(job, t)
                if p.returncode != 0:
                    raise RuntimeError(f"шаг push вернул {p.returncode}")
            meta["push"] = {"status": "success", "target": ts, "latest": f"{hub}:latest",
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        except Exception as e:  # noqa: BLE001
            meta["push"] = {"status": "failed", "target": ts, "error": str(e)[:300],
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save(job, meta)


@app.post("/api/builds/{jid}/push")
def push_build(jid: str, user: str = Depends(auth)) -> dict:
    job = JOBS_DIR / jid
    if not job.exists():
        raise HTTPException(404)
    meta = _meta(job)
    if meta.get("user") != user:
        raise HTTPException(403)
    if meta.get("status") != "success":
        raise HTTPException(409, "только успешно собранное")
    if (meta.get("push") or {}).get("status") in ("queued", "running"):
        raise HTTPException(409, "push уже выполняется")
    meta["push"] = {"status": "queued"}
    _save(job, meta)
    threading.Thread(target=_push_job, args=(jid,), daemon=True).start()
    return {"push": "queued", "target": f"{HUB_NS}/lfx-{meta['name']}"}


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
