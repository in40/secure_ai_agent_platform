#!/usr/bin/env python3
"""lfx_subtract — build a slim LFX image from the full langflow image by subtraction.

Takes a profile.json (from lfx_profile.py), creates a container from the full
base image, uninstalls unneeded pip packages and deletes unneeded model/system
paths inside it, then re-flattens via docker export|import preserving the
original image Config (same mechanism as flatten.sh + gen_changes.py).

Runs fully offline: everything it needs is the base image already on the host.

Usage:
  python3 lfx_subtract.py --profile profile.json [--base aso9864/langflow-v2:1.0.28]
                          [--tag lfx-name:latest] [--dry-run] [--smoke]
  python3 lfx_subtract.py --profile p.json --tag lfx-x:latest --smoke-only
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent


def run(cmd: str | list, check: bool = True) -> str:
    printable = cmd if isinstance(cmd, str) else " ".join(map(shlex.quote, cmd))
    print("+", printable, flush=True)
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip()[-1500:], flush=True)
    if check and r.returncode != 0:
        print(r.stderr[-1500:], file=sys.stderr)
        raise SystemExit(f"command failed (rc={r.returncode}): {printable}")
    return r.stdout


def smoke(tag: str) -> bool:
    name = "lfx-smoke"
    run(f"docker rm -f {name} >/dev/null 2>&1 || true")
    run(["docker", "run", "-d", "--name", name,
         "-e", "LANGFLOW_DATABASE_URL=sqlite:////tmp/smoke.db",
         "-e", "LANGFLOW_AUTO_LOGIN=True", tag])
    ok = False
    try:
        for _ in range(30):
            time.sleep(5)
            r = subprocess.run(
                ["docker", "exec", name, "sh", "-c",
                 "curl -sm5 -o /dev/null -w %{http_code} http://127.0.0.1:7860/health"],
                capture_output=True, text=True)
            if "200" in r.stdout:
                ok = True
                break
        print("SMOKE:", "OK (health=200)" if ok else "FAILED — container did not become healthy")
        if not ok:
            run(f"docker logs --tail 40 {name}", check=False)
    finally:
        run(f"docker rm -f {name} >/dev/null 2>&1 || true", check=False)
    return ok


def build(prof: dict, args, tag: str) -> None:
    pip_rm = prof.get("pip_remove", [])
    path_rm = prof.get("paths_remove", [])
    print(f"pip uninstall ({len(pip_rm)}): {', '.join(pip_rm) or '-'}")
    print(f"rm -rf ({len(path_rm)}): {', '.join(path_rm) or '-'}")

    cname = "lfx-sub-tmp"
    run(f"docker rm -f {cname} >/dev/null 2>&1 || true")
    run(["docker", "run", "-d", "--name", cname, "--entrypoint", "sleep", args.base, "infinity"])
    try:
        if pip_rm:
            run(["docker", "exec", "--user", "root", cname, args.venv_pip, "uninstall", "-y", *pip_rm])
        if path_rm:
            run(["docker", "exec", "--user", "root", cname, "rm", "-rf", *path_rm])
        # flow-only custom components -> в запекаемый каталог компонентов
        comp_dir = args.components_dir or prof.get("components_dir")
        if comp_dir:
            cd = Path(comp_dir)
            pys = sorted(cd.glob("*.py")) if cd.is_dir() else []
            for py in pys:
                run(["docker", "cp", str(py), f"{cname}:/app/custom_components/{py.name}"])
            if pys:
                print(f"baked {len(pys)} custom component(s) from {cd}")
        # LFX-runtime: flow + init + entrypoint + (опц.) удаление UI
        if args.flow_json:
            run(["docker", "exec", "--user", "root", cname, "mkdir", "-p", "/app/lfx"])
            run(["docker", "cp", args.flow_json, f"{cname}:/app/lfx/flow.json"])
            print(f"baked flow: {args.flow_json} -> /app/lfx/flow.json")
        if args.llm_env:
            run(["docker", "exec", "--user", "root", cname, "mkdir", "-p", "/app/lfx"])
            run(["docker", "cp", str(HERE / "lfx_init.py"), f"{cname}:/app/lfx/lfx_init.py"])
            run(["docker", "cp", str(HERE / "lfx_entrypoint.sh"), f"{cname}:/app/lfx/entrypoint.sh"])
            run(["docker", "exec", "--user", "root", cname, "chmod", "+x", "/app/lfx/entrypoint.sh"])
            print("baked LFX init/entrypoint (LLM настраивается из env)")
        if args.no_ui:
            run(["docker", "exec", "--user", "root", cname, "sh", "-c",
                 "rm -rf /app/.venv/lib/python*/site-packages/langflow/frontend/assets/* /app/simpleui 2>/dev/null; true"])
            print("UI удалён (frontend assets + simpleui)")
        run(["docker", "exec", "--user", "root", cname, "sh", "-c",
             "rm -rf /root/.cache /home/*/.cache /app/.npm /var/cache/* 2>/dev/null; "
             "/app/.venv/bin/pip cache purge 2>/dev/null; true"])
        run(["docker", "stop", cname])

        gen = Path(args.gen_changes)
        if not gen.exists():
            raise SystemExit(f"gen_changes.py not found at {gen} (needed to preserve image Config)")
        changes = run(f"docker inspect -f '{{{{json .Config}}}}' {args.base} | python3 {gen}")
        change_args = changes.splitlines()
        if args.llm_env or args.flow_json:
            for i, a in enumerate(change_args):
                if a.startswith("CMD "):
                    change_args[i] = 'CMD ["/app/lfx/entrypoint.sh"]'
                    print("CMD overridden -> /app/lfx/entrypoint.sh")
        print(f"config changes: {len(change_args)} args")

        export = subprocess.Popen(["docker", "export", cname], stdout=subprocess.PIPE)
        imp = subprocess.Popen(["docker", "import", *change_args, "-", tag],
                               stdin=export.stdout, stdout=subprocess.PIPE, text=True)
        export.stdout.close()
        out, err = imp.communicate()
        if imp.returncode != 0:
            raise SystemExit(f"docker import failed: {err[-1000:]}")
        print(out.strip())
    finally:
        run(f"docker rm -f {cname} >/dev/null 2>&1 || true", check=False)

    size = run(f"docker images {tag} --format '{{{{.Size}}}}'").strip()
    print(f"BUILT {tag}  size={size}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, help="profile.json from lfx_profile.py")
    ap.add_argument("--base", default="aso9864/langflow-v2:1.0.28")
    ap.add_argument("--tag", default=None, help="default: lfx-<flow-name>:latest")
    ap.add_argument("--gen-changes", default=str(HERE / "gen_changes.py"))
    ap.add_argument("--components-dir", default=None, help="папка .py для запекания в /app/custom_components (default: из профиля)")
    ap.add_argument("--flow-json", default=None, help="flow JSON для запекания в /app/lfx/flow.json (авто-импорт при старте)")
    ap.add_argument("--llm-env", action="store_true", help="запечь lfx_init+entrypoint: провайдер/модель LLM из env при старте")
    ap.add_argument("--no-ui", action="store_true", help="удалить frontend-ассеты и simpleui (чистый API-runtime)")
    ap.add_argument("--venv-pip", default="/app/.venv/bin/pip")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--smoke", action="store_true", help="start resulting image and hit /health")
    ap.add_argument("--smoke-only", action="store_true", help="skip build, only smoke --tag")
    args = ap.parse_args()

    prof = json.loads(Path(args.profile).read_text())
    flow_name = prof.get("flow_name") or "flow"
    slug = re.sub(r"[^a-z0-9]+", "-", flow_name.lower()).strip("-") or "flow"
    tag = args.tag or f"lfx-{slug}:latest"
    print(f"base={args.base}  tag={tag}")

    if args.smoke_only:
        return 0 if smoke(tag) else 1
    if args.dry_run:
        build_probe = prof
        print(f"pip uninstall ({len(build_probe.get('pip_remove', []))}): {', '.join(build_probe.get('pip_remove', []))}")
        print(f"rm -rf ({len(build_probe.get('paths_remove', []))}): {', '.join(build_probe.get('paths_remove', []))}")
        return 0

    build(prof, args, tag)
    if args.smoke:
        return 0 if smoke(tag) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
