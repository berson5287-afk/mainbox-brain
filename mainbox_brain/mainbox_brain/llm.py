"""
LLM client -- three-tier, graceful-degradation, stdlib only.

    tillium-bridge (gemma3:27b)  ->  local Ollama (gemma3:4b)  ->  None

If nothing answers within the timeout, .complete() returns None and every
caller is written to fall back to a deterministic path. This mirrors the
MaINbox principle: the AI makes things better when present, but its absence
never breaks the workflow.

No third-party packages: we POST to Ollama's /api/generate with urllib.
"""
from __future__ import annotations
import json
import urllib.request
import urllib.error
from typing import Optional

from . import config


def server_models(host: str, timeout: float | None = None) -> tuple[bool, list[str]]:
    """Fast reachability probe via GET /api/tags.

    Returns (reachable, [installed model names]). Does NOT load any model, so
    it's quick even when the big model would be slow to generate.
    """
    if timeout is None:
        timeout = config.LLM_CONNECT_TIMEOUT
    req = urllib.request.Request(f"{host}/api/tags")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return True, [m.get("name", "") for m in body.get("models", [])]
    except (urllib.error.URLError, TimeoutError, ConnectionError,
            json.JSONDecodeError, OSError):
        return False, []


class LLMClient:
    def __init__(self) -> None:
        self._tiers = [
            (config.TRIAGE_HOST, config.TRIAGE_MODEL),
            (config.LOCAL_OLLAMA, config.LOCAL_FALLBACK_MODEL),
        ]
        self.last_tier_used: Optional[str] = None

    def complete(self, prompt: str, system: str | None = None) -> Optional[str]:
        """Return model text, or None if no tier is reachable.

        Each tier is reachability-probed first (fast). Only if the host answers
        do we wait the longer generate timeout -- so a sleeping host fails over
        quickly, while a live-but-loading model gets the time it needs.
        """
        for host, model in self._tiers:
            reachable, _ = server_models(host)
            if not reachable:
                continue
            text = self._call_ollama(host, model, prompt, system)
            if text is not None:
                self.last_tier_used = f"{model}@{host}"
                return text
        self.last_tier_used = None
        return None

    @property
    def available(self) -> bool:
        return self.complete("ping", system="Reply with: ok") is not None

    # -- internal -----------------------------------------------------------
    def _call_ollama(self, host: str, model: str, prompt: str,
                     system: str | None) -> Optional[str]:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        if system:
            payload["system"] = system
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{host}/api/generate", data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("response", "").strip() or None
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError, OSError):
            return None


def extract_json(text: str):
    """Best-effort JSON extraction from an LLM reply (handles code fences)."""
    if not text:
        return None
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("[")
    obj_start = cleaned.find("{")
    if obj_start != -1 and (start == -1 or obj_start < start):
        start = obj_start
    if start == -1:
        return None
    depth, end = 0, None
    open_ch = cleaned[start]
    close_ch = "]" if open_ch == "[" else "}"
    for i in range(start, len(cleaned)):
        if cleaned[i] == open_ch:
            depth += 1
        elif cleaned[i] == close_ch:
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        return json.loads(cleaned[start:end])
    except json.JSONDecodeError:
        return None


def _probe(host: str, model: str) -> dict:
    """Two-phase probe: (1) is the host reachable + does it have the model?
    (2) can the model actually generate (timed)?"""
    import time
    reachable, models = server_models(host)
    if not reachable:
        return {"phase": "connect", "ok": False,
                "detail": "host not reachable (server down, wrong host, or "
                          "Tailscale not connected)"}
    has_model = any(m == model or m.split(":")[0] == model.split(":")[0]
                    for m in models)
    model_note = (f"model '{model}' is installed"
                  if has_model else
                  f"model '{model}' NOT installed here "
                  f"(have: {', '.join(models) or 'none'}; try: ollama pull {model})")
    # phase 2: timed generate (allowed the full, generous timeout)
    payload = {"model": model, "prompt": "Reply with exactly: ok",
               "stream": False, "options": {"temperature": 0}}
    req = urllib.request.Request(
        f"{host}/api/generate", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ms = int((time.time() - t0) * 1000)
        if body.get("error"):
            return {"phase": "generate", "ok": False, "reachable": True,
                    "detail": f"{model_note}; generate error: {body['error']}"}
        return {"phase": "generate", "ok": True, "ms": ms,
                "detail": f"{model_note}; generated in {ms} ms"}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        ms = int((time.time() - t0) * 1000)
        slow = " -- model still loading? raise MAINBOX_LLM_TIMEOUT and retry" \
               if has_model else ""
        return {"phase": "generate", "ok": False, "reachable": True,
                "detail": f"{model_note}; generate timed out after {ms} ms{slow}"}
    except json.JSONDecodeError:
        return {"phase": "generate", "ok": False, "reachable": True,
                "detail": f"{model_note}; unexpected (non-Ollama?) response"}


def main() -> None:
    """`py -m mainbox_brain.llm` -- check which LLM tier is reachable."""
    print("MaINbox Brain - LLM tier check")
    print(f"(connect timeout {config.LLM_CONNECT_TIMEOUT}s, "
          f"generate timeout {config.LLM_TIMEOUT_S}s)\n")
    tiers = [(config.TRIAGE_HOST, config.TRIAGE_MODEL, "primary "),
             (config.LOCAL_OLLAMA, config.LOCAL_FALLBACK_MODEL, "fallback")]
    first_ok = None
    for host, model, role in tiers:
        r = _probe(host, model)
        reach = "reachable" if r.get("ok") or r.get("reachable") else "NOT reachable"
        print(f"  [{'OK' if r['ok'] else '--'}] {role}  {model} @ {host}")
        print(f"         server: {reach}")
        print(f"         {r['detail']}")
        if r["ok"] and first_ok is None:
            first_ok = f"{model}@{host}"
    print()
    if first_ok:
        print(f"--> ask --llm will use: {first_ok}")
    else:
        print("--> No tier usable. ask --llm falls back to regex intent routing.")
        print("    If a server is reachable but generate timed out, the model is "
              "likely cold-loading -- raise MAINBOX_LLM_TIMEOUT and retry.")


if __name__ == "__main__":
    main()
