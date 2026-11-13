#!/usr/bin/env python3
"""export_flow_components — найти кастомные компоненты, встроенные в flow, и подготовить их к запеканию в образ.

Логика та же, что в lfx_profile: нода считается flow-only кастомом, если её
тип неизвестен базе/манифесту, а в коде есть маркеры CustomComponent.
Скрипт обходит ВСЕ flow инстанса и пишет код таких нод в .py файлы,
которые затем попадают в контекст сборки (COPY custom_components/).

Использование (перед docker build):
  python3 export_flow_components.py --url http://127.0.0.1:7861 \
      --username u --password p --out-dir ../custom_components
Флаги:
  --list-only        только показать, что нашлось (ничего не писать)
  --skip-existing    не перезаписывать существующие .py с тем же типом (default)
  --no-skip-existing перезаписывать
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from lfx_profile import CUSTOM_MARKERS, norm_type, node_code, node_template  # noqa: E402


def known_types(manifest_path: Path) -> set[str]:
    """Типы, которые уже есть в базе/образе — их запекать не надо."""
    if not manifest_path.is_file():
        return set()
    m = json.loads(manifest_path.read_text())
    ks = {norm_type(x) for x in m.get("known_base_types", [])}
    ks |= {norm_type(x) for x in m.get("base_custom_types", [])}
    ks |= {norm_type(mm) for s in m.get("features", {}).values() for mm in s.get("match_types", [])}
    return ks


def load_all_flows(url: str, username: str, password: str) -> list[dict]:
    url = url.rstrip("/")
    r = httpx.post(
        f"{url}/api/v1/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    lst = httpx.get(f"{url}/api/v1/flows/", headers=h, timeout=60)
    lst.raise_for_status()
    flows = lst.json()
    if isinstance(flows, dict):
        flows = flows.get("flows", [])
    out = []
    for f in flows:
        if not f.get("data"):  # список мог прийти без data — догружаем по одному
            det = httpx.get(f"{url}/api/v1/flows/{f['id']}", headers=h, timeout=60)
            det.raise_for_status()
            f = det.json()
        out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:7861")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--out-dir", default=str(Path(__file__).parent / "custom_components"))
    ap.add_argument("--manifest", default=str(Path(__file__).parent / "manifest.json"))
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    ap.set_defaults(skip_existing=True)
    args = ap.parse_args()

    flows = load_all_flows(args.url, args.username, args.password)
    known = known_types(Path(args.manifest))
    found: dict[str, tuple[str, str]] = {}  # type -> (code, flow_name)
    conflicts: list[str] = []
    for flow in flows:
        fname = flow.get("name", "?")
        for n in (flow.get("data") or {}).get("nodes", []) or []:
            d = n.get("data", {})
            t = norm_type(d.get("type", ""))
            if not t or t in known:
                continue  # точное совпадение: подстрока проглотила бы customvllmmodel через vllmmodel
            code = node_code(node_template(n))
            if not code or not any(m in code for m in CUSTOM_MARKERS):
                continue
            if t in found and found[t][0] != code:
                conflicts.append(f"{t}: код различается в flow '{fname}' и '{found[t][1]}' — оставлен первый")
                continue
            found.setdefault(t, (code, fname))

    out_dir = Path(args.out_dir)
    existing_types = set()
    if out_dir.is_dir():
        for py in out_dir.glob("*.py"):
            body = py.read_text(errors="ignore")
            for t in found:
                if f'"{t}"' in body.lower().replace("_", "") or norm_type(py.stem) == t:
                    existing_types.add(t)

    written, skipped = [], []
    for t, (code, src_flow) in sorted(found.items()):
        if t in existing_types and args.skip_existing:
            skipped.append(t)
            continue
        if args.list_only:
            print(f"FOUND  {t}  (из flow '{src_flow}', {len(code)} bytes)")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{t}.py").write_text(code)
        written.append(t)
        print(f"WROTE  {out_dir / (t + '.py')}  (из flow '{src_flow}')")

    for c in dict.fromkeys(conflicts):
        print("CONFLICT:", c)
    if skipped:
        print("SKIPPED (уже есть в out-dir):", ", ".join(skipped))
    print(f"Итого: найдено {len(found)}, записано {len(written)}, пропущено {len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
