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
hc = cfg.get("Healthcheck") or {}
test = hc.get("Test") or []
if test:
    opts = ""
    for key, flag in (("Interval", "--interval"), ("Timeout", "--timeout"), ("StartPeriod", "--start-period")):
        ns = hc.get(key) or 0
        if ns:
            opts += f" {flag}={ns // 10**9}s"
    if test[0] == "NONE":
        out += ["--change", "HEALTHCHECK NONE"]
    elif test[0] == "CMD-SHELL":
        out += ["--change", "HEALTHCHECK" + opts + " CMD " + test[1]]
    elif test[0] == "CMD":
        out += ["--change", "HEALTHCHECK" + opts + " CMD " + json.dumps(test[1:])]
if cfg.get("Entrypoint"):
    out += ["--change", "ENTRYPOINT " + json.dumps(cfg["Entrypoint"])]
if cfg.get("WorkingDir"):
    out += ["--change", "WORKDIR " + cfg["WorkingDir"]]
if cfg.get("User"):
    out += ["--change", "USER " + cfg["User"]]
for p in cfg.get("ExposedPorts") or {}:
    out += ["--change", "EXPOSE " + p]
print("\n".join(out))
