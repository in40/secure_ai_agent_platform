#!/usr/bin/env python3
"""lfx_init — стартовая инициализация LFX-runtime (без UI).

Вызывается из entrypoint после старта langflow. Идемпотентно:
1. ждёт /health;
2. логинится локально суперпользователем;
3. заводит/обновляет креды провайдера модели из env:
     LFX_LLM_BASE_URL   -> переменная OPENAI_COMPATIBLE_BASE_URL (Credential)
     LFX_LLM_API_KEY    -> переменная OPENAI_COMPATIBLE_API_KEY  (Credential)
4. импортирует запечённый flow /app/lfx/flow.json (если его ещё нет);
5. прописывает в нодах-LLM (LFX_LLM_NODE, по подстроке типа ноды)
   provider=LFX_LLM_PROVIDER и model_name=LFX_LLM_MODEL.

Всё настраивается только env-переменными — flow остаётся переносимым.
"""
import os
import sys
import time

import httpx

BASE = "http://127.0.0.1:7860"
FLOW_PATH = os.environ.get("LFX_FLOW_PATH", "/app/lfx/flow.json")


def log(msg: str) -> None:
    print(f"[lfx_init] {msg}", flush=True)


def wait_health(timeout_s: int = 240) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/health", timeout=5).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    sys.exit("[lfx_init] langflow не поднялся за время ожидания")


def login() -> str:
    user = os.environ.get("LANGFLOW_SUPERUSER", "admin")
    pw = os.environ.get("LANGFLOW_SUPERUSER_PASSWORD", "")
    for _ in range(20):
        try:
            r = httpx.post(
                f"{BASE}/api/v1/login",
                data={"username": user, "password": pw},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            if r.status_code == 200:
                return r.json()["access_token"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    sys.exit("[lfx_init] не удалось залогиниться")


def upsert_variable(h: dict, name: str, value: str) -> None:
    existing = httpx.get(f"{BASE}/api/v1/variables/", headers=h, timeout=20).json()
    for v in existing:
        if v.get("name") == name and v.get("type") == "Credential":
            if str(v.get("id")):
                httpx.patch(
                    f"{BASE}/api/v1/variables/{v['id']}",
                    headers=h,
                    json={"value": value},
                    timeout=20,
                )
                log(f"variable {name}: обновлена")
                return
    r = httpx.post(
        f"{BASE}/api/v1/variables/",
        headers=h,
        json={"name": name, "type": "Credential", "value": value},
        timeout=20,
    )
    log(f"variable {name}: создана ({r.status_code})")


def import_flow(h: dict) -> str | None:
    if not os.path.isfile(FLOW_PATH):
        log(f"flow-файла нет ({FLOW_PATH}) — импорт пропущен")
        return None
    with open(FLOW_PATH, "rb") as f:
        r = httpx.post(
            f"{BASE}/api/v1/flows/upload/",
            headers=h,
            files={"file": ("flow.json", f, "application/json")},
            timeout=60,
        )
    if r.status_code >= 400:
        log(f"импорт flow: HTTP {r.status_code} {r.text[:200]}")
        return None
    data = r.json()
    fid = data[0].get("id") if isinstance(data, list) and data else None
    log(f"flow импортирован/обновлён: {fid}")
    return fid


def patch_llm_nodes(h: dict) -> None:
    provider = os.environ.get("LFX_LLM_PROVIDER", "OpenAI Compatible")
    model = os.environ.get("LFX_LLM_MODEL", "")
    needle = os.environ.get("LFX_LLM_NODE", "languagemodelcomponent").lower()
    if not model:
        log("LFX_LLM_MODEL не задан — ноды не патчатся")
        return
    flows = httpx.get(f"{BASE}/api/v1/flows/", headers=h, timeout=30).json()
    for f in flows if isinstance(flows, list) else flows.get("flows", []):
        fid = f.get("id")
        det = httpx.get(f"{BASE}/api/v1/flows/{fid}", headers=h, timeout=30).json()
        nodes = (det.get("data") or {}).get("nodes", [])
        changed = 0
        for n in nodes:
            dd = n.get("data", {})
            if needle not in (dd.get("type") or "").lower():
                continue
            tpl = dd.setdefault("template", {})
            tpl.setdefault("provider", {"type": "str", "value": None})["value"] = provider
            tpl.setdefault("model_name", {"type": "str", "value": None})["value"] = model
            changed += 1
        if changed:
            r = httpx.put(
                f"{BASE}/api/v1/flows/{fid}",
                headers=h,
                json={"name": det.get("name"), "description": det.get("description"), "data": det.get("data")},
                timeout=60,
            )
            log(f"flow {fid}: патч LLM-нод ({changed}) -> HTTP {r.status_code}")


def main() -> None:
    wait_health()
    token = login()
    h = {"Authorization": f"Bearer {token}"}
    if os.environ.get("LFX_LLM_BASE_URL"):
        upsert_variable(h, "OPENAI_COMPATIBLE_BASE_URL", os.environ["LFX_LLM_BASE_URL"])
    if os.environ.get("LFX_LLM_API_KEY"):
        upsert_variable(h, "OPENAI_COMPATIBLE_API_KEY", os.environ["LFX_LLM_API_KEY"])
    import_flow(h)
    patch_llm_nodes(h)
    log("готово")


if __name__ == "__main__":
    main()
