"""VeriAgent Grounding Guard — Langflow 1.13 custom component.
RAG-защита: сверяет ответ LLM с источниками. Блоки ОТКЛЮЧАЕМЫ:
  - use_judge: 122B NLI «ответ подтверждён источником?» (opt-in)
  - use_overlap: дешёвый lexical overlap (без судьи)
  - use_receipts: источники = подписанные расписки VeriAgent Tool Guard этого
    прогона (postgres, ключ self.graph.run_id) + проверка Ed25519-подписей.
    Иначе источники берутся из входа `context`.
Судья: env VERIAGENT_JUDGE_URL / VERIAGENT_JUDGE_KEY / VERIAGENT_JUDGE_MODEL.
Стор расписок: env LANGFLOW_DATABASE_URL, таблица verai_receipts.
"""
import os
import re
import hashlib
import base64
import requests

from langflow.custom import Component
from langflow.io import StrInput, BoolInput, MessageTextInput, Output
from langflow.schema import Data
from langflow.schema.message import Message

_JUDGE_PROMPT = (
    "Ты — строгий верификатор. Сравни УТВЕРЖДЕНИЕ с ИСТОЧНИКОМ, ответь ОДНИМ словом:\n"
    "bound (есть в источнике) / inferred (вывод из него) / unsupported (нет в источнике) "
    "/ contradicted (источник назвал другое).\n\nУТВЕРЖДЕНИЕ: {claim}\nИСТОЧНИК: {src}\n\nОтвет:"
)


def _words(s):
    return set(w for w in re.findall(r"[a-zа-яё0-9']+", (s or "").lower()) if len(w) >= 4)


class VeriAgentGroundingGuard(Component):
    display_name = "VeriAgent Grounding Guard"
    description = "RAG grounding check: answer vs retrieved sources (judge + overlap)."
    icon = "shield-check"
    name = "VeriAgentGroundingGuard"

    inputs = [
        MessageTextInput(name="answer", display_name="LLM answer", required=True),
        MessageTextInput(name="context", display_name="Retrieved sources", is_list=True),
        BoolInput(name="use_judge", display_name="122B judge (semantic)", value=True),
StrInput(name="judge_url", display_name="Judge URL", info="OpenAI-compatible judge endpoint; empty = env VERIAGENT_JUDGE_URL", value=""),
        BoolInput(name="use_overlap", display_name="Lexical overlap (cheap)", value=True),
        BoolInput(name="use_receipts", display_name="Sources = signed receipts (this run)", value=False),
        BoolInput(name="inject_warning", display_name="Inject warning into answer", value=True),
    ]

    outputs = [
        Output(display_name="Grounded Answer", name="grounded_answer", method="as_message"),
        Output(display_name="Verdict", name="verdict", method="as_data"),
    ]

    def _overlap(self, claim, src):
        cw = _words(claim)
        if not cw:
            return 0.0
        sw = _words(src)
        return len(cw & sw) / len(cw)

    def _judge(self, claim, src):
        url = (getattr(self, "judge_url", "") or "").strip() or os.environ.get("VERIAGENT_JUDGE_URL", "https://chat.sorokinonline.com/v1/chat/completions")
        key = os.environ.get("VERIAGENT_JUDGE_KEY", "")
        try:
            r = requests.post(url, headers={"Authorization": "Bearer " + key},
                             json={"model": os.environ.get("VERIAGENT_JUDGE_MODEL", "qwen3.5-122b"),
                                   "temperature": 0.0, "max_tokens": 2048,
                                   "messages": [{"role": "user",
                                                 "content": _JUDGE_PROMPT.format(claim=claim[:1500], src=src[:4000])}]},
                             timeout=60)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            out = ((msg.get("content") or "") + "\n" + (msg.get("reasoning_content") or "")).strip().lower()
            best, pos = "unknown", -1
            for k in ("bound", "inferred", "unsupported", "contradicted"):
                i = out.rfind(k)
                if i > pos:
                    pos, best = i, k
            return best
        except Exception:
            return "judge_error"

    def _load_receipts(self) -> dict:
        import psycopg
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        gr = getattr(self, "graph", None)
        run_id = getattr(gr, "run_id", None)
        out = {"src": "", "verified": 0, "tampered": 0, "handles": [], "run_id": run_id,
               "error": None, "inj": False}
        try:
            with psycopg.connect(os.environ.get("LANGFLOW_DATABASE_URL", ""), autocommit=True) as c:
                rows = c.execute(
                    "SELECT handle,sha256,sig,pubkey,raw,flags FROM verai_receipts WHERE run_id=%s ORDER BY id",
                    (run_id,)).fetchall()
        except Exception as e:
            out["error"] = type(e).__name__
            return out
        raws = []
        for handle, sha, sig, pub, raw, flags in rows:
            out["handles"].append(handle)
            if any(k in (flags or "") for k in ("special_token", "role_forgery", "override_phrase")):
                out["inj"] = True
            ok = False
            try:
                Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(
                    base64.b64decode(sig), (handle + sha).encode())
                ok = hashlib.sha256(raw.encode()).hexdigest() == sha
            except Exception:
                ok = False
            out["verified" if ok else "tampered"] += 1
            raws.append(raw)
        out["src"] = "\n\n".join(raws)
        return out

    def _evaluate(self) -> dict:
        if getattr(self, "_res", None) is not None:
            return self._res
        verified = tampered = 0
        handles = []
        run_id = None
        err = None
        tool_inj = False
        if self.use_receipts:
            rc = self._load_receipts()
            src = rc["src"]
            verified, tampered, handles, run_id, err = (
                rc["verified"], rc["tampered"], rc["handles"], rc["run_id"], rc["error"])
            tool_inj = rc["inj"]
        else:
            ctx = self.context or []
            if isinstance(ctx, str):
                ctx = [ctx]
            src = "\n\n".join(str(c) for c in ctx)
        verdict = "no_context"
        ov = 0.0
        jv = None
        if src.strip():
            if self.use_overlap:
                ov = self._overlap(self.answer, src)
                verdict = "grounded" if ov >= 0.4 else ("weak" if ov >= 0.2 else "unsupported")
            if self.use_judge:
                jv = self._judge(self.answer, src)
                if jv in ("bound", "inferred"):
                    verdict = "grounded"
                elif jv in ("unsupported", "contradicted"):
                    verdict = jv
        if self.use_receipts and tampered > 0:
            verdict = "tampered"
        warnings = []
        if verdict in ("unsupported", "contradicted"):
            warnings.append("⚠️ ОПАСНОСТЬ: выявлены галлюцинации — ответ не подтверждён источником.")
        if verdict == "tampered":
            warnings.append("⚠️ ОПАСНОСТЬ: целостность данных инструмента нарушена (подпись не сходится).")
        if tool_inj:
            warnings.append("⚠️ ОПАСНОСТЬ: атака через инструмент — в выводе инструмента признаки prompt-injection.")
        self._res = {"answer": self.answer, "verdict": verdict, "overlap": round(ov, 3),
                     "judge": jv, "grounded": verdict == "grounded", "verified": verified,
                     "tampered": tampered, "handles": handles, "run_id": run_id,
                     "store_error": err, "tool_injection": tool_inj, "warnings": warnings}
        return self._res

    def as_message(self) -> Message:
        r = self._evaluate()
        text = r["answer"]
        if self.inject_warning and r["warnings"]:
            text = text + "\n\n" + "\n".join(r["warnings"])
        return Message(text=text)

    def as_data(self) -> Data:
        r = self._evaluate()
        return Data(answer=r["answer"], verdict=r["verdict"], overlap=r["overlap"],
                    judge=r["judge"], grounded=r["grounded"], verified=r["verified"],
                    tampered=r["tampered"], handles=r["handles"], run_id=r["run_id"],
                    store_error=r["store_error"], tool_injection=r["tool_injection"],
                    warnings=r["warnings"])
