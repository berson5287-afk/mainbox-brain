"""
Central configuration for the MaINbox brain.

Everything that you'd plausibly want to change without touching logic lives here:
LLM hosts, model names, timeouts, and the sender identity used on RFQ emails.
"""
from __future__ import annotations
import os

# ---------------------------------------------------------------------------
# LLM routing (matches your MaINbox triage-host pattern)
#
#   1. Primary: gemma3 on your home GPU host (tillium-bridge) over Tailscale
#   2. Fallback: a smaller local Ollama model on this machine
#   3. Last resort: no LLM at all -> deterministic regex paths take over
#
# The brain NEVER hard-depends on the LLM. If every tier is unreachable, the
# regex parser and rule-based resolver still produce a usable result.
# ---------------------------------------------------------------------------
TRIAGE_HOST = os.environ.get("MAINBOX_TRIAGE_HOST", "http://tillium-bridge:11434")
LOCAL_OLLAMA = os.environ.get("MAINBOX_LOCAL_OLLAMA", "http://localhost:11434")

TRIAGE_MODEL = os.environ.get("MAINBOX_TRIAGE_MODEL", "gemma3:12b")
LOCAL_FALLBACK_MODEL = os.environ.get("MAINBOX_LOCAL_MODEL", "llama3.2:3b")

# Two separate timeouts:
#  - CONNECT is a fast reachability probe (GET /api/tags). If the host is
#    asleep we fail over quickly instead of blocking the UI.
#  - GENERATE allows the model time to load + answer. A cold 27B can take far
#    longer than a few seconds to load into VRAM on its first request, so this
#    is generous; subsequent (warm) calls return in a second or two.
LLM_CONNECT_TIMEOUT = float(os.environ.get("MAINBOX_LLM_CONNECT_TIMEOUT", "4"))
LLM_TIMEOUT_S = float(os.environ.get("MAINBOX_LLM_TIMEOUT", "45"))

# ---------------------------------------------------------------------------
# Identity used when drafting RFQ emails
# ---------------------------------------------------------------------------
SENDER_NAME = os.environ.get("MAINBOX_SENDER_NAME", "Steve")
COMPANY_NAME = os.environ.get("MAINBOX_COMPANY", "American Power Electrical Supply Co.")
SENDER_EMAIL = os.environ.get("MAINBOX_SENDER_EMAIL", "steve@americanpoweresc.com")
