import json
import sys

cfg = json.load(sys.stdin)
out = []
for e in cfg.get("Env") or []:
    k, _, v = e.partition("=")
    if " " in v or '"' in v:
        v = '"' + v.replace('"', '\\"') + '"'
    out += ["--change", "ENV " + k + "=" + v]
if cfg.get("Cmd"):
    out += ["--change", "CMD " + json.dumps(cfg["Cmd"])]
if cfg.get("Entrypoint"):
    out += ["--change", "ENTRYPOINT " + json.dumps(cfg["Entrypoint"])]
if cfg.get("WorkingDir"):
    out += ["--change", "WORKDIR " + cfg["WorkingDir"]]
if cfg.get("User"):
    out += ["--change", "USER " + cfg["User"]]
for p in cfg.get("ExposedPorts") or {}:
    out += ["--change", "EXPOSE " + p]
print("\n".join(out))
