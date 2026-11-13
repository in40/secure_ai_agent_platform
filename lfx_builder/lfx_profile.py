#!/usr/bin/env python3
"""lfx_profile — profile a Langflow flow and report what can be stripped.

Reads a flow (JSON file or via API), maps node types + field values through
manifest.json, and prints which heavy features (pip packages / model paths)
the flow needs and which can be removed by the subtractive builder.

Usage:
  python3 lfx_profile.py --file flow.json [--json out.json] [--strict] [--no-lo]
  python3 lfx_profile.py --flow <id> --url http://host:7860 --username u --password p
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent


def norm_type(raw: str) -> str:
    """Normalize node type: 'ext:docling:ChunkDoclingDocumentComponent@official' -> 'chunkdoclingdocumentcomponent'."""
    t = (raw or "").lower()
    t = re.sub(r"^ext:[^:]+:", "", t)
    t = re.sub(r"@.*$", "", t)
    return t.replace("_", "").replace("-", "").replace(" ", "")


def node_template(node: dict) -> dict:
    d = node.get("data", {})
    tpl = d.get("template")
    if not isinstance(tpl, dict) or not tpl:
        tpl = d.get("node", {}).get("template", {})
    return tpl if isinstance(tpl, dict) else {}


def field_value(tpl: dict, field: str):
    v = tpl.get(field)
    if isinstance(v, dict):
        return v.get("value")
    return v


def load_flow_api(flow_id: str, url: str, username: str, password: str) -> dict:
    import httpx

    url = url.rstrip("/")
    r = httpx.post(
        f"{url}/api/v1/login",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    r2 = httpx.get(
        f"{url}/api/v1/flows/{flow_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r2.raise_for_status()
    return r2.json()


CUSTOM_MARKERS = ("CustomComponent", "@custom_component", "@langflow_component", "LCModelComponent", "BaseComponent")


def node_code(tpl: dict) -> str:
    code = tpl.get("code")
    if isinstance(code, dict):
        code = code.get("value")
    return code if isinstance(code, str) else ""


def top_level_imports(code: str) -> set[str]:
    """AST-разбор кода ноды -> множество top-level импортируемых модулей."""
    import ast

    mods: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return mods
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for a in item.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(item, ast.ImportFrom):
            if item.module and item.level == 0:
                mods.add(item.module.split(".")[0])
    return mods


def analyze_custom_nodes(flow: dict, manifest: dict, unknown: list[str], export_dir: Path | None) -> dict:
    """Для flow-only кастомных нод: экспорт кода в .py + анализ импортов.

    Возвращает dict для профиля: custom_components, custom_imports,
    forced_keep (фичи, которые нельзя вычитать из-за импортов), warnings.
    """
    import_map = manifest.get("import_map", {})
    features = manifest["features"]
    codes: dict[str, str] = {}
    for n in flow.get("data", {}).get("nodes", []) or []:
        d = n.get("data", {})
        t = norm_type(d.get("type", ""))
        if t in unknown:
            code = node_code(node_template(n))
            if code and any(m in code for m in CUSTOM_MARKERS):
                codes[t] = code

    exported: dict[str, str] = {}
    imports: set[str] = set()
    warnings: list[str] = []
    forced_keep: set[str] = set()
    pip_keep: set[str] = set()

    for t, code in sorted(codes.items()):
        if export_dir is not None:
            export_dir.mkdir(parents=True, exist_ok=True)
            fn = export_dir / f"{t}.py"
            fn.write_text(code)
            exported[t] = str(fn)
        for m in top_level_imports(code):
            imports.add(m)
            if m in ("_vllm_tyk_lib", "custom_components"):
                continue  # наши baked-модули, всегда в образе
            mapped = import_map.get(m, "??")
            if mapped == "??":
                warnings.append(f"{t}: импорт '{m}' не в import_map — пакет не будет удалён только если он не в pip_remove; проверьте вручную")
                continue
            if not mapped:
                continue  # базовый пакет (httpx/pydantic/...)
            pip_keep.add(mapped)
            for fname, spec in features.items():
                if mapped in spec.get("pip", []):
                    forced_keep.add(fname)

    return {
        "custom_components": exported,
        "custom_imports": sorted(imports),
        "forced_keep": sorted(forced_keep),
        "pip_keep": sorted(pip_keep),
        "custom_warnings": warnings,
    }


def profile(flow: dict, manifest: dict, no_lo: bool = False, export_dir: Path | None = None) -> dict:
    nodes = flow.get("data", {}).get("nodes", []) or []
    types, fields = [], []
    for n in nodes:
        d = n.get("data", {})
        t = norm_type(d.get("type", ""))
        types.append(t)
        fields.append((t, node_template(n)))

    keep: set[str] = set()
    reasons: dict[str, list[str]] = {}
    features = manifest["features"]

    for fname, spec in features.items():
        if spec.get("always_remove"):
            continue
        hit = False
        why: list[str] = []
        for t in types:
            for m in spec.get("match_types", []):
                if norm_type(m) in t:
                    hit = True
                    why.append(f"type~{m}")
        rule = spec.get("field_rule")
        if rule:
            want_t = norm_type(rule["type"])
            for t, tpl in fields:
                if t == want_t:
                    val = field_value(tpl, rule["field"])
                    vals = [str(x).lower() for x in rule["values"]]
                    if str(val).lower() in vals:
                        hit = True
                        why.append(f"{rule['field']}={val}")
        if hit:
            if fname == "libreoffice" and no_lo:
                reasons.setdefault(fname, []).append("skipped via --no-lo")
                continue
            keep.add(fname)
            reasons[fname] = why or ["matched"]

    # OCR engine fallback for Docling Inline: explicit per-node value, else docling default (rapidocr auto)
    if "docling_core" in keep:
        for t, tpl in fields:
            if t != "doclinginline":
                continue
            val = field_value(tpl, "ocr_engine")
            if val in (None, ""):
                keep.add("ocr_rapidocr")
                reasons.setdefault("ocr_rapidocr", []).append("ocr_engine unset -> docling default auto")

    known = {norm_type(x) for x in manifest.get("known_base_types", [])}
    known |= {norm_type(x) for x in manifest.get("base_custom_types", [])}
    feat_types = {norm_type(m) for s in features.values() for m in s.get("match_types", [])}
    unknown = sorted({t for t in types if t and not any(k in t or t in k for k in known | feat_types)})

    # flow-only custom nodes: export code + analyze imports
    custom = analyze_custom_nodes(flow, manifest, unknown, export_dir)
    for t in custom["custom_components"]:
        if t in unknown:
            unknown.remove(t)  # экспортирован и проанализирован — не требует ручного ревью
    for f in custom["forced_keep"]:
        keep.add(f)
        reasons.setdefault(f, []).append("custom node imports")

    remove = [f for f in features if f not in keep]
    pip_remove, paths_remove = [], []
    for f in remove:
        pip_remove += features[f].get("pip", [])
        paths_remove += features[f].get("paths", [])
    # paths fully owned by kept features must not be removed (e.g. docling cache parent vs easyocr subdir)
    kept_paths = set()
    for f in keep:
        kept_paths.update(features[f].get("paths", []))
    paths_remove = [p for p in paths_remove if not any(p != k and (p in k or k in p) for k in kept_paths)]
    # packages required by custom node code must survive subtraction
    pip_remove = [p for p in pip_remove if p not in custom["pip_keep"]]

    return {
        "flow_name": flow.get("name", ""),
        "nodes": sorted(set(types)),
        "keep": sorted(keep),
        "remove": sorted(set(remove)),
        "pip_remove": sorted(set(pip_remove)),
        "paths_remove": sorted(set(paths_remove)),
        "unknown_types": unknown,
        "reasons": reasons,
        **custom,
        "components_dir": str(export_dir) if (export_dir and custom["custom_components"]) else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", help="flow JSON file")
    ap.add_argument("--flow", help="flow id (with --url)")
    ap.add_argument("--url", default="http://localhost:7860")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--manifest", default=str(HERE / "manifest.json"))
    ap.add_argument("--json", dest="json_out", help="write machine-readable profile")
    ap.add_argument("--strict", action="store_true", help="fail on unknown component types")
    ap.add_argument("--no-lo", action="store_true", help="drop LibreOffice even if Docling present")
    ap.add_argument("--export-dir", help="куда экспортировать код flow-only кастомных нод (default: lfx_components рядом с --json)")
    args = ap.parse_args()

    if args.file:
        flow = json.loads(Path(args.file).read_text())
    elif args.flow and args.username:
        flow = load_flow_api(args.flow, args.url, args.username, args.password)
    else:
        ap.error("need --file or --flow + --username/--password")

    if args.export_dir:
        export_dir = Path(args.export_dir)
    elif args.json_out:
        export_dir = Path(args.json_out).parent / "lfx_components"
    else:
        export_dir = None

    manifest = json.loads(Path(args.manifest).read_text())
    prof = profile(flow, manifest, no_lo=args.no_lo, export_dir=export_dir)

    print(f"Flow: {prof['flow_name']}  ({len(prof['nodes'])} node types)")
    print("Nodes:  ", ", ".join(prof["nodes"]))
    print("KEEP:   ", ", ".join(prof["keep"]) or "— base only —")
    for f, why in prof["reasons"].items():
        print(f"   + {f}: {'; '.join(why)}")
    print("REMOVE: ", ", ".join(prof["remove"]))
    print("  pip:   ", ", ".join(prof["pip_remove"]) or "—")
    print("  paths: ", ", ".join(prof["paths_remove"]) or "—")
    if prof["custom_components"]:
        print("CUSTOM (flow-only, экспортировано):")
        for t, fn in prof["custom_components"].items():
            print(f"   - {t} -> {fn}")
        print("  imports:", ", ".join(prof["custom_imports"]))
        if prof["forced_keep"]:
            print("  forced KEEP из-за импортов:", ", ".join(prof["forced_keep"]))
    for w in prof["custom_warnings"]:
        print("  WARN:", w)
    if prof["unknown_types"]:
        print("UNKNOWN component types (kept base, review!):", ", ".join(prof["unknown_types"]))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(prof, indent=2, ensure_ascii=False))
    if args.strict and prof["unknown_types"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
