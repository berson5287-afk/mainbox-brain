"""
MaINbox Brain HTTP API -- the brain as a server.

This is the piece the thin Android client (or anything) talks to. Stdlib only
(http.server), runs anywhere Python runs -- tillium-bridge over Tailscale is
the natural home.

Start:
    py -m mainbox_brain.server                      # 127.0.0.1:8585, stub mail
    py -m mainbox_brain.server --host 0.0.0.0       # reachable on the tailnet
    py -m mainbox_brain.server --db mainbox.db --port 8585

Endpoints (JSON in/out):
    GET  /health                      -> {ok, vendors, records, version}
    GET  /vendors                     -> learned registry (ranked contacts)
    GET  /search?q=8400+connector     -> recall: past RFQ lines, no confidence floor
    GET  /reply/search?q=12/2+MC    -> mined vendor reply facts: price/ETA/stock/no-quote
    POST /refresh {months?,scope?,replies_only?} -> run Outlook COM update in the
                     background; returns immediately (202) with a job id
    GET  /refresh/status              -> {status, added, before, after, log_tail, ...}
    POST /ask          {"text": "..."}            -> answer questions OR start RFQ
    POST /quote/start  {"text": "..."}            -> {session, message, done}
    POST /quote/reply  {"session": "...", "text"} -> {message, done}

Refresh uses Outlook COM (no Graph) via the `update` pipeline, so the server
must run on the Windows desktop where Outlook lives. Start it with
--auto-refresh <minutes> to keep the db fresh on a timer; the phone then mostly
reads already-current data and POST /refresh is just a nudge.

The quote endpoints drive the same QuoteConversation state machine as the CLI:
start a request, then reply "just Mark" / "yes both" / "draft" / "send now".

SAFETY: mail backend is the printing stub by default. Pass --graph to use the
real GraphMailClient -- and even then "send now" is downgraded to creating a
DRAFT unless you also pass --allow-send. Same training wheels as demo_graph.

Sessions live in memory and expire after 30 minutes idle. Single-user by
design for now -- this is your brain, not a multi-tenant service.
"""
from __future__ import annotations
import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import vendors, config, intents
from .parser import parse_request
from .conversation import QuoteConversation
from .graph_client import StubMailClient, MailClient
from .store import Store, DEFAULT_DB
from .llm import LLMClient
from .reply_miner import mine_replies

SESSION_TTL_S = 30 * 60


class _State:
    """Server-wide state: store-backed registry, records, mail backend, sessions."""

    def __init__(self, db_path: str, mail: MailClient) -> None:
        self.db_path = db_path
        self.store = Store(db_path)
        self.mail = mail
        self.records = self.store.load_records()
        learned = self.store.load_vendors(confident_only=True)
        if learned:
            vendors.VENDORS = learned   # store wins over the hand-built table
        self.sessions: dict[str, tuple[QuoteConversation, float]] = {}
        self.info_sessions: dict[str, tuple] = {}
        self.lock = threading.Lock()
        self.llm = None
        try:
            probe = LLMClient()
            if probe.complete("ping", system="Reply with: ok"):
                self.llm = probe
                print(f"LLM answering enabled: {probe.last_tier_used}")
        except Exception:
            self.llm = None

    def new_session(self) -> tuple[str, QuoteConversation]:
        sid = uuid.uuid4().hex[:12]
        convo = QuoteConversation(self.mail, sent_history=self.records, store=Store(self.db_path))
        with self.lock:
            self._sweep()
            self.sessions[sid] = (convo, time.time())
        return sid, convo

    def get_session(self, sid: str) -> QuoteConversation | None:
        with self.lock:
            entry = self.sessions.get(sid)
            if not entry:
                return None
            convo, _ = entry
            self.sessions[sid] = (convo, time.time())
            return convo

    def info_session(self, sid: str | None) -> tuple[str, "intents.InfoSession"]:
        """Get the conversational InfoSession for this client, or start one.
        Lets phone clients keep context (clarifications, follow-ups) across
        /ask calls by passing the returned session id back each turn."""
        with self.lock:
            now = time.time()
            self.info_sessions = {k: v for k, v in self.info_sessions.items()
                                  if now - v[1] <= SESSION_TTL_S}
            if sid and sid in self.info_sessions:
                sess, _ = self.info_sessions[sid]
                self.info_sessions[sid] = (sess, now)
                return sid, sess
            new_sid = sid or uuid.uuid4().hex[:12]
            sess = intents.InfoSession(Store(self.db_path), llm=self.llm)
            self.info_sessions[new_sid] = (sess, now)
            return new_sid, sess

    def _sweep(self) -> None:
        now = time.time()
        dead = [k for k, (_, t) in self.sessions.items() if now - t > SESSION_TTL_S]
        for k in dead:
            del self.sessions[k]

    def reload(self) -> None:
        """Re-read records + learned vendors from the db (after a refresh)."""
        with self.lock:
            self.store = Store(self.db_path)
            self.records = self.store.load_records()
            learned = self.store.load_vendors(confident_only=True)
            if learned:
                vendors.VENDORS = learned


# ---------------------------------------------------------------------------
# Background refresh -- runs the Outlook COM update pipeline (no Graph).
# A POST /refresh kicks it off in a subprocess (COM is happier in its own
# process) and returns immediately; GET /refresh/status polls progress.
# ---------------------------------------------------------------------------
_REFRESH_LOCK = threading.Lock()
_REFRESH = {
    "status": "idle",        # idle | running | done | failed
    "job": None,
    "started_at": None,
    "finished_at": None,
    "before": None,
    "after": None,
    "added": None,
    "log_tail": "",
    "error": None,
}


def _refresh_snapshot() -> dict:
    with _REFRESH_LOCK:
        snap = dict(_REFRESH)
    for k in ("started_at", "finished_at"):
        if snap.get(k):
            snap[k + "_iso"] = datetime.fromtimestamp(snap[k]).isoformat(timespec="seconds")
    return snap


def _run_refresh(db_path: str, extra_args: list[str]) -> None:
    before = None
    try:
        before = Store(db_path).reply_count()
    except Exception:
        pass
    with _REFRESH_LOCK:
        _REFRESH["before"] = before
    cmd = [sys.executable, "-m", "mainbox_brain.update", "--db", db_path] + extra_args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        after = None
        try:
            after = Store(db_path).reply_count()
        except Exception:
            pass
        ok = proc.returncode == 0
        with _REFRESH_LOCK:
            _REFRESH.update(
                status="done" if ok else "failed",
                finished_at=time.time(), after=after,
                added=(after - before) if (after is not None and before is not None) else None,
                log_tail=(proc.stdout or "")[-1500:],
                error=None if ok else ((proc.stderr or "")[-500:] or f"exit {proc.returncode}"),
            )
        if ok and STATE is not None:
            try:
                STATE.reload()
            except Exception:
                pass
    except subprocess.TimeoutExpired:
        with _REFRESH_LOCK:
            _REFRESH.update(status="failed", finished_at=time.time(),
                            error="refresh timed out (over 60 min)")
    except Exception as e:
        with _REFRESH_LOCK:
            _REFRESH.update(status="failed", finished_at=time.time(), error=str(e)[:500])


def _start_refresh(db_path: str, extra_args: list[str]) -> dict:
    """Start a refresh unless one is already running; return current status."""
    with _REFRESH_LOCK:
        if _REFRESH["status"] == "running":
            already = dict(_REFRESH)
            already["already_running"] = True
            return already
        _REFRESH.update(status="running", job=uuid.uuid4().hex[:12],
                        started_at=time.time(), finished_at=None, error=None,
                        before=None, after=None, added=None, log_tail="")
    threading.Thread(target=_run_refresh, args=(db_path, extra_args), daemon=True).start()
    return _refresh_snapshot()


STATE: _State | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "MaINboxBrain/0.8.3"

    # -- helpers ---------------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 1_000_000:
                return None
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("[api] " + fmt % args + "\n")

    # -- routes ------------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/health":
            self._json(200, {
                "ok": True,
                "vendors": len(vendors.VENDORS),
                "records": len(STATE.records),
                "version": "0.8.3",
                "reply_records": Store(STATE.db_path).reply_count(),
                "llm_answering": STATE.llm is not None,
            })
        elif url.path == "/vendors":
            out = []
            for vid, v in vendors.VENDORS.items():
                out.append({
                    "vendor_id": vid, "name": v.name,
                    "contacts": [{"name": c.name, "email": c.email}
                                 for c in v.contacts],
                })
            self._json(200, {"vendors": out})
        elif url.path in {"/reply/search", "/replies/search"}:
            q = (parse_qs(url.query).get("q") or [""])[0].strip()
            if not q:
                self._json(200, {"query": q, "hits": Store(STATE.db_path).recent_replies()})
                return
            self._json(200, {"query": q, "hits": Store(STATE.db_path).find_replies(q)})
        elif url.path == "/search":
            q = (parse_qs(url.query).get("q") or [""])[0].strip()
            if not q:
                self._json(400, {"error": "missing ?q="})
                return
            # fresh connection per request: handler threads can't share the
            # main thread's SQLite connection
            self._json(200, {"query": q, "hits": Store(STATE.db_path).find(q)})
        elif url.path in {"/refresh/status", "/refresh"}:
            self._json(200, _refresh_snapshot())
        else:
            self._json(404, {"error": f"no route {url.path}"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            self._json(400, {"error": "invalid or missing JSON body"})
            return

        if url.path == "/refresh":
            # trigger the Outlook COM update pipeline in the background.
            # optional body: {months, scope: personal|sales|both, replies_only}
            extra: list[str] = []
            try:
                if payload.get("months") is not None:
                    extra += ["--months", str(int(payload["months"]))]
            except (TypeError, ValueError):
                pass
            if payload.get("scope") in {"personal", "sales", "both"}:
                extra += ["--scope", payload["scope"]]
            if payload.get("replies_only"):
                extra.append("--no-sent")
            snap = _start_refresh(STATE.db_path, extra)
            code = 202 if not snap.get("already_running") else 200
            self._json(code, {"ok": True, **snap})
        elif url.path == "/reply/mine":
            if not hasattr(STATE.mail, "fetch_received_messages"):
                self._json(400, {"error": "reply mining over Graph is disabled; "
                                          "use POST /refresh (Outlook COM) instead"})
                return
            try:
                months = int(payload.get("months") or 12)
                max_messages = int(payload.get("max_messages") or 500)
            except (TypeError, ValueError):
                self._json(400, {"error": "months and max_messages must be numbers"})
                return
            store = Store(STATE.db_path)
            messages = STATE.mail.fetch_received_messages(months=months, max_messages=max_messages)
            replies = mine_replies(messages, store=store)
            saved = store.save_reply_records(replies)
            self._json(200, {"ok": True, "scanned": len(messages), "mined": len(replies),
                             "saved": saved, "reply_records": store.reply_count()})
        elif url.path == "/teach":
            # v0.8.2: structured product-alias teaching (no word caps).  The
            # voice app calls this so a vendor's long confirmation description
            # links to the short name you actually say, and pricing searches
            # match both (store.find/find_replies alias-expand).
            term = (payload.get("term") or "").strip()
            canonical = (payload.get("canonical") or "").strip()
            if not term or not canonical:
                self._json(400, {"error": "need 'term' and 'canonical'"})
                return
            from .knowledge import Knowledge
            k = Knowledge(Store(STATE.db_path).db)
            k.learn_alias(term, canonical)
            self._json(200, {"ok": True, "term": term,
                             "canonical": canonical,
                             "aliases": len(k.aliases())})
        elif url.path == "/ask":
            text = (payload.get("text") or "").strip()
            if not text:
                self._json(400, {"error": "missing 'text'"})
                return
            # conversational session: pass back the session id to keep context
            # (clarifications, follow-ups) across turns
            sid, sess = STATE.info_session(payload.get("session"))
            answer = sess.answer(text)
            if answer is not None:
                self._json(200, {"kind": "answer", "session": sid,
                                 "message": answer,
                                 "pending": sess.pending is not None,
                                 "done": sess.pending is None,
                                 # v0.9: the emails this answer was built from
                                 "sources": list(getattr(sess, "last_sources", []) or [])})
                return
            qsid, convo = STATE.new_session()
            turn = convo.start(parse_request(text, STATE.llm))
            self._json(200, {"kind": "quote", "session": qsid, "message": turn.message,
                             "done": turn.done})
        elif url.path == "/quote/start":
            text = (payload.get("text") or "").strip()
            if not text:
                self._json(400, {"error": "missing 'text'"})
                return
            sid, convo = STATE.new_session()
            turn = convo.start(parse_request(text, STATE.llm))
            self._json(200, {"session": sid, "message": turn.message,
                             "done": turn.done})
        elif url.path == "/quote/reply":
            sid = (payload.get("session") or "").strip()
            text = (payload.get("text") or "").strip()
            convo = STATE.get_session(sid) if sid else None
            if convo is None:
                self._json(404, {"error": "unknown or expired session"})
                return
            if not text:
                self._json(400, {"error": "missing 'text'"})
                return
            turn = convo.handle(text)
            self._json(200, {"session": sid, "message": turn.message,
                             "done": turn.done})
        else:
            self._json(404, {"error": f"no route {url.path}"})


def _build_mail(use_graph: bool, allow_send: bool) -> MailClient:
    if not use_graph:
        return StubMailClient(verbose=True)
    from .graph_client import GraphMailClient
    client = GraphMailClient()
    print("Signing in to Microsoft Graph…")
    print("Signed in as:", client.login())
    if not allow_send:
        client.send_email = lambda d, _c=client: (
            print("  [safety] send downgraded to DRAFT"), _c.create_draft(d))[-1]
    return client


def main() -> None:
    args = sys.argv[1:]

    def opt(flag: str, default: str) -> str:
        return args[args.index(flag) + 1] if flag in args else default

    host = opt("--host", "127.0.0.1")
    port = int(opt("--port", "8585"))
    db_path = opt("--db", DEFAULT_DB)
    use_graph = "--graph" in args
    allow_send = "--allow-send" in args
    auto_refresh = opt("--auto-refresh", "")   # minutes; "" = off

    global STATE
    STATE = _State(db_path, _build_mail(use_graph, allow_send))

    if auto_refresh:
        try:
            mins = float(auto_refresh)
        except ValueError:
            mins = 0.0
        if mins > 0:
            def _auto_loop():
                while True:
                    time.sleep(mins * 60)
                    _start_refresh(db_path, [])
            threading.Thread(target=_auto_loop, daemon=True).start()
            print(f"  auto-refresh: every {mins:g} min via Outlook COM")

    n_v, n_r = len(vendors.VENDORS), len(STATE.records)
    print(f"MaINbox Brain API on http://{host}:{port}  "
          f"(db={db_path}: {n_v} vendors, {n_r} records; "
          f"mail={'GRAPH' if use_graph else 'stub'}"
          f"{', SEND ENABLED' if use_graph and allow_send else ', drafts only' if use_graph else ''})")
    if n_v == 0:
        print("  note: registry is empty — run the corpus miner with this db first.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
