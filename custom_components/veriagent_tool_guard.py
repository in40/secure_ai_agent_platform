"""VeriAgent Tool Guard (v3) — Langflow 1.13 custom component.
Обёртка инструмента для Agent: принимает Tool (HandleInput input_types=["Tool"]),
возвращает ОБЁРНУТЫЙ Tool (тот же name/description/args_schema), который при вызове
прогоняет вывод вложенного инструмента через scan(injection) + Ed25519-sign + store(postgres)
и добавляет маркер [verai:HANDLE]. Если Tool не подключён — фолбэк: fetch по URL-аргументу.
Ключ: env VERIAGENT_SIGNING_KEY (base64 32-байтный seed). DSN: env LANGFLOW_DATABASE_URL.
"""
import os
import re
import hashlib
import base64
import requests

from langflow.custom import Component
from langflow.io import BoolInput, HandleInput, Output
from langflow.field_typing import Tool
from langchain_core.tools import Tool as LCTool, StructuredTool
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

_SPECIAL = re.compile(r"<\|[a-zA-Z_]+\|>")
_ROLE = re.compile(r"(?im)^\s*(system|assistant|tool|user)\s*:")
_OVERRIDE = ["ignore previous", "ignore all previous", "disregard previous",
             "you are now", "system prompt", "забудь инструкц", "игнорируй предыдущ"]

_SCHEMA = """CREATE TABLE IF NOT EXISTS verai_receipts(
    id BIGSERIAL PRIMARY KEY, run_id TEXT, session_id TEXT, flow_id TEXT,
    handle TEXT, sha256 TEXT, algo TEXT, sig TEXT, pubkey TEXT,
    raw TEXT, flags TEXT, ts TIMESTAMPTZ DEFAULT now())"""


def _priv():
    seed = os.environ.get("VERIAGENT_SIGNING_KEY")
    if seed:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed))
    return Ed25519PrivateKey.generate()


def _scan(text):
    f = []
    if _SPECIAL.search(text):
        f.append("special_token")
    if _ROLE.search(text):
        f.append("role_forgery")
    low = text.lower()
    if any(k in low for k in _OVERRIDE):
        f.append("override_phrase")
    return f


def _to_text(res):
    if isinstance(res, str):
        return res
    if isinstance(res, list):
        parts = []
        for b in res:
            if isinstance(b, dict) and "text" in b:
                parts.append(b["text"])
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(res)


class VeriAgentToolGuard(Component):
    display_name = "VeriAgent Tool Guard"
    description = "Wrap an Agent tool: scan + Ed25519 receipt to postgres on each call."
    icon = "wrench"
    name = "VeriAgentToolGuard"

    inputs = [
        HandleInput(name="inner_tool", display_name="Tool to guard", input_types=["Tool"], is_list=False),
        BoolInput(name="use_scan", display_name="Injection scan", value=True),
        BoolInput(name="use_sign", display_name="Ed25519 sign + store", value=True),
    ]

    outputs = [Output(name="tool", display_name="Tool", method="build_tool")]

    def _guard(self, raw: str) -> str:
        gr = getattr(self, "graph", None)
        run_id = getattr(gr, "run_id", None)
        session_id = getattr(gr, "session_id", None)
        flow_id = getattr(gr, "flow_id", None)
        flags = _scan(raw) if self.use_scan else []
        handle = ""
        if self.use_sign:
            sha = hashlib.sha256(raw.encode()).hexdigest()
            handle = sha[:12]
            priv = _priv()
            pub = base64.b64encode(priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
            sig = base64.b64encode(priv.sign((handle + sha).encode())).decode()
            try:
                import psycopg
                with psycopg.connect(os.environ.get("LANGFLOW_DATABASE_URL", ""), autocommit=True) as c:
                    c.execute(_SCHEMA)
                    c.execute(
                        "INSERT INTO verai_receipts(run_id,session_id,flow_id,handle,sha256,algo,sig,pubkey,raw,flags) "
                        "VALUES(%s,%s,%s,%s,%s,'ed25519',%s,%s,%s,%s)",
                        (run_id, session_id, flow_id, handle, sha, sig, pub, raw, ",".join(flags)))
            except Exception as e:
                flags.append("store_error:" + type(e).__name__)
        note = f"\n[verai:{handle}]" if handle else ""
        warn = ("  [verai-flag:" + ",".join(flags) + "]") if flags else ""
        return raw[:4000] + note + warn

    def build_tool(self) -> Tool:
        inner = self.inner_tool
        if isinstance(inner, (list, tuple)):
            inner = inner[0] if inner else None
        if inner is None or not hasattr(inner, "invoke"):
            def _fetch(url: str) -> str:
                try:
                    return self._guard(requests.get(url, timeout=30).text)
                except Exception as e:
                    return f"[verai:error] fetch failed: {e}"
            return LCTool(name="veriagent_fetch",
                          description="Fetch a URL; result is integrity-signed with a [verai:HANDLE] marker.",
                          func=_fetch)

        name = getattr(inner, "name", "veriagent_wrapped")
        desc = getattr(inner, "description", "") or ""
        schema = getattr(inner, "args_schema", None)

        def _f(**kw):
            return self._guard(_to_text(inner.invoke(kw if kw else {})))

        async def _c(**kw):
            return self._guard(_to_text(await inner.ainvoke(kw if kw else {})))

        try:
            return StructuredTool(name=name + "_guarded", description=desc,
                                  args_schema=schema, func=_f, coroutine=_c)
        except Exception:
            return LCTool(name=name + "_guarded", description=desc,
                          func=lambda q: self._guard(_to_text(inner.invoke(q))))
