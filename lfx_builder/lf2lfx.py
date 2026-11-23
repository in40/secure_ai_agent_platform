#!/usr/bin/env python3
"""lf2lfx — конвертер кастомных компонентов langflow -> lfx (Langflow Executor, PyPI `lfx`).

Основано на том, что в langflow>=1.6 символы `langflow.*` — ре-экспорты `lfx.*`
(проверено: `langflow.base.models.LCModelComponent is lfx.base.models.LCModelComponent`).
Поэтому конвертер переписывает только префикс импорта (построчно, комментарии/отступы
сохраняются), а семантика не меняется.

Режимы:
  lf2lfx.py component.py [-o out.py]        — конвертировать .py
  lf2lfx.py --check component.py            — проверить импортом с заблокированным langflow
                                              + сборкой шаблона (нужен lfx в окружении)
  lf2lfx.py --flow flow.json [-o out.json]  — переписать код нод внутри экспорта flow
                                              (node.data.node.template.code.value)

Исключения из правила «langflow.X -> lfx.X» — в MODULE_MAP.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

# Полные исключения вида "langflow.a.b": "lfx.c.d". Всё остальное — замена префикса.
MODULE_MAP: dict[str, str] = {}

# langflow-специфичные импорты, которых нет в standalone lfx: замена на try/except-заглушку.
_TRACING_RE = re.compile(
    r"^(\s*)from lfx\.services\.tracing\.service import trace_context_var( as \w+)?\s*$",
    re.MULTILINE,
)


def _special_fixes(src: str) -> str:
    """trace_context_var живёт в langflow-пакете (OTel-трассировка), в lfx его нет.
    Валидатор lfx (custom/validate.py) исполняет только Import/ImportFrom/Assign/Def/Class —
    Try на уровне модуля отбрасывается, поэтому заглушка безусловная (ContextVar, в
    компонентах только .get())."""

    def repl(m: re.Match) -> str:
        ind, alias = m.group(1), m.group(2) or ""
        name = alias.split()[-1] if alias else "trace_context_var"
        return (
            f"{ind}import contextvars  # lf2lfx: tracing есть только в langflow-пакете\n"
            f"{ind}{name} = contextvars.ContextVar(\"trace_context_var\", default=None)"
        )

    return _TRACING_RE.sub(repl, src)


_IMPORT_RE = re.compile(r"^(\s*(?:from|import)\s+)langflow(?=[.\s])")


def convert_source(src: str) -> tuple[str, list[str]]:
    """Переписать langflow-импорты на lfx (замена префикса). Возвращает (код, список замен)."""
    changed: list[str] = []
    out = []
    for line in src.splitlines(keepends=True):
        if _IMPORT_RE.match(line):
            new_line = _IMPORT_RE.sub(lambda mm: mm.group(1) + "lfx", line, count=1)
            changed.append(line.strip())
            out.append(new_line)
        else:
            out.append(line)
    return "".join(out), changed


def _apply_map(src: str) -> str:
    """Второй проход: полные имена из MODULE_MAP (после префикс-замены)."""
    for old, new in MODULE_MAP.items():
        src = re.sub(rf"(^(\s*(?:from|import)\s+)){re.escape(old)}(?=[\s.])",
                     lambda m, n=new: m.group(1) + n, src, flags=re.MULTILINE)
    return src


def convert_file(path: Path, out_path: Path | None) -> int:
    src = path.read_text(encoding="utf-8")
    new, changed = convert_source(src)
    new = _special_fixes(_apply_map(new))
    if out_path:
        out_path.write_text(new, encoding="utf-8")
        print(f"written: {out_path}")
    else:
        sys.stdout.write(new)
    for c in changed:
        print(f"  rewrote: {c}", file=sys.stderr)
    if not changed:
        print("  (langflow-импорты не найдены)", file=sys.stderr)
    return 0


def check_file(path: Path) -> int:
    """Импорт с заблокированным langflow + сборка шаблона каждого Component-подкласса."""
    sys.modules["langflow"] = None  # type: ignore[assignment]
    sys.modules["langflow.base"] = None  # type: ignore[assignment]
    spec = importlib.util.spec_from_file_location("_lf2lfx_mod", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # build_custom_component_template резолвит класс по sys.modules
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        print(f"IMPORT FAIL: {type(e).__name__}: {e}")
        return 1
    from lfx.custom import Component
    from lfx.custom.utils import build_custom_component_template

    subs = [v for v in vars(mod).values()
            if isinstance(v, type) and issubclass(v, Component) and v is not Component]
    if not subs:
        print("CHECK FAIL: ни одного Component-подкласса")
        return 1
    rc = 0
    for cls in subs:
        try:
            tpl, err = build_custom_component_template(cls())
            print(f"OK   {cls.__name__}: template {'built' if tpl else 'FAIL: ' + str(err)[:200]}")
            rc = rc or (0 if tpl else 1)
        except Exception as e:
            print(f"FAIL {cls.__name__}: {type(e).__name__}: {str(e)[:300]}")
            rc = 1
    return rc


def convert_flow(path: Path, out_path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    graph = data.get("data") if isinstance(data.get("data"), dict) else data
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    if not nodes:
        raise SystemExit("в flow нет нод (пустой flow?)")
    n = 0
    for node in nodes:
        code = (node.get("data", {}).get("node", {}).get("template", {})
                .get("code", {}).get("value"))
        if isinstance(code, str) and "langflow" in code:
            new, changed = convert_source(code)
            new = _special_fixes(_apply_map(new))
            if changed:
                node["data"]["node"]["template"]["code"]["value"] = new
                n += 1
                print(f"  node {node.get('data', {}).get('type', '?')}: {len(changed)} импортов")
    out_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"flow: обновлено нод с кодом: {n} -> {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--check", action="store_true", help="валидация импортом без langflow")
    ap.add_argument("--flow", action="store_true", help="режим экспорта flow JSON")
    args = ap.parse_args()
    if args.check:
        return check_file(args.path)
    if args.flow:
        return convert_flow(args.path, args.out or args.path.with_suffix(".lfx.json"))
    return convert_file(args.path, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
