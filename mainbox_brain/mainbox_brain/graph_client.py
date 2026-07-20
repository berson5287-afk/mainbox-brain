"""
Mail + calendar client.

On a phone there is no Outlook COM (that's the desktop MaINbox world). The
supported cross-platform path is the Microsoft Graph REST API with OAuth.

This module gives you:
  - MailClient        : the interface the rest of the brain talks to
  - StubMailClient    : a runnable fake (prints actions, canned history) so
                        the whole flow works with zero credentials
  - GraphMailClient   : the LIVE implementation (v0.3) -- device-code login,
                        cached token, real Sent Items -> history miner,
                        real drafts/sends/calendar events

Keeping the brain behind this interface means the Android client, a server
daemon, or the demo can all swap implementations without touching logic.
"""
from __future__ import annotations
import json
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from .models import EmailDraft, SentRecord, Category
from .history_miner import mine, SentMessage, MiningResult
from .reply_miner import ReplyMessage, mine_replies


class MailClient:
    """Interface. Implement these four for a live backend."""

    def recent_sent(self, query: str | None = None) -> list[SentRecord]:
        raise NotImplementedError

    def recent_replies(self, query: str | None = None) -> list[ReplyMessage]:
        raise NotImplementedError

    def create_draft(self, draft: EmailDraft) -> str:
        raise NotImplementedError

    def send_email(self, draft: EmailDraft) -> str:
        raise NotImplementedError

    def add_calendar_event(self, title: str, start: datetime,
                           minutes: int = 30) -> str:
        raise NotImplementedError


class StubMailClient(MailClient):
    """No network. Prints what it would do; returns plausible sent history."""

    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.drafts: list[EmailDraft] = []
        self.sent: list[EmailDraft] = []

    def recent_sent(self, query: str | None = None) -> list[SentRecord]:
        now = datetime.now()
        return [
            SentRecord("mark@brazil-example.com", "brazil",
                       {Category.MC_CABLE, Category.BUILDING_WIRE}, now - timedelta(days=4)),
            SentRecord("thea@pipeandwire-example.com", "pipeandwire",
                       {Category.MC_CABLE, Category.CONDUIT}, now - timedelta(days=11)),
            SentRecord("dana@gearco-example.com", "gear_co",
                       {Category.GEAR}, now - timedelta(days=6)),
        ]

    def recent_replies(self, query: str | None = None) -> list[ReplyMessage]:
        return []

    def create_draft(self, draft: EmailDraft) -> str:
        self.drafts.append(draft)
        if self.verbose:
            print(f"\n[DRAFT CREATED] to {draft.to_name} <{draft.to}>")
            print(f"  Subject: {draft.subject}")
            print("  " + draft.body.replace("\n", "\n  "))
        return f"draft-{len(self.drafts)}"

    def send_email(self, draft: EmailDraft) -> str:
        self.sent.append(draft)
        if self.verbose:
            print(f"\n[EMAIL SENT] to {draft.to_name} <{draft.to}>  | {draft.subject}")
        return f"sent-{len(self.sent)}"

    def add_calendar_event(self, title: str, start: datetime, minutes: int = 30) -> str:
        if self.verbose:
            print(f"\n[CALENDAR] '{title}' at {start:%a %b %d %I:%M %p} "
                  f"({minutes} min)")
        return "event-1"


# ===========================================================================
# LIVE Microsoft Graph client (v0.3)
# ===========================================================================
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.ReadWrite", "Mail.Send", "Calendars.ReadWrite", "User.Read"]
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
TOKEN_CACHE_FILE = os.path.expanduser("~/.mainbox_graph_token.json")


class GraphAuthError(RuntimeError):
    pass


class GraphMailClient(MailClient):
    """Live Graph backend.

    Auth: MSAL device-code flow (no client secret -- safe for a desktop/phone
    'public client'). First run prints a URL + code; after that the token
    cache at ~/.mainbox_graph_token.json silently refreshes.

    Requires:  pip install msal
    Env:       MAINBOX_GRAPH_CLIENT_ID  (your Entra app's Application ID)
               MAINBOX_GRAPH_AUTHORITY  (optional; default /common)
    """

    def __init__(self, client_id: str | None = None,
                 authority: str | None = None,
                 token_cache_path: str = TOKEN_CACHE_FILE) -> None:
        try:
            import msal  # deferred so the rest of the package has no hard dep
        except ImportError as e:
            raise GraphAuthError(
                "msal is not installed. Run:  pip install msal") from e

        self.client_id = client_id or os.environ.get("MAINBOX_GRAPH_CLIENT_ID", "")
        if not self.client_id:
            raise GraphAuthError(
                "No client id. Register an app in Entra ID (see README Tier 3) "
                "and set MAINBOX_GRAPH_CLIENT_ID to its Application (client) ID.")
        self._authority = authority or os.environ.get(
            "MAINBOX_GRAPH_AUTHORITY", DEFAULT_AUTHORITY)

        self._msal = msal
        self._cache = msal.SerializableTokenCache()
        self._cache_path = token_cache_path
        if os.path.exists(token_cache_path):
            try:
                with open(token_cache_path, "r", encoding="utf-8") as f:
                    self._cache.deserialize(f.read())
            except (OSError, ValueError):
                pass  # corrupt cache -> just re-login

        self._app = None  # created lazily in login(): MSAL does network
                          # discovery on construction, which belongs there

    def _ensure_app(self):
        if self._app is None:
            self._app = self._msal.PublicClientApplication(
                self.client_id, authority=self._authority,
                token_cache=self._cache)
        return self._app

    # -- auth ----------------------------------------------------------------
    def login(self) -> str:
        """Acquire a token: silent if cached, else device-code prompt.
        Returns the signed-in account's username/email."""
        app = self._ensure_app()
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result:
            flow = app.initiate_device_flow(scopes=SCOPES)
            if "user_code" not in flow:
                raise GraphAuthError(f"Device flow failed: {flow.get('error_description', flow)}")
            print("\n=== Microsoft sign-in required ===")
            print(flow["message"])          # 'go to https://microsoft.com/devicelogin, enter CODE'
            result = app.acquire_token_by_device_flow(flow)  # blocks until done
        if "access_token" not in result:
            raise GraphAuthError(result.get("error_description", str(result)))
        self._save_cache()
        self._token = result["access_token"]
        me = self._get("/me")
        return me.get("userPrincipalName") or me.get("mail", "signed in")

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            try:
                with open(self._cache_path, "w", encoding="utf-8") as f:
                    f.write(self._cache.serialize())
                os.chmod(self._cache_path, 0o600)
            except OSError:
                pass

    def _token_or_login(self) -> str:
        tok = getattr(self, "_token", None)
        if tok:
            return tok
        self.login()
        return self._token

    # -- raw HTTP --------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = path if path.startswith("http") else GRAPH + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token_or_login()}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise GraphAuthError(f"Graph {method} {path} -> {e.code}: {detail}") from e

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, payload)

    # -- Sent Items -> miner ---------------------------------------------------
    def fetch_sent_messages(self, months: int = 12,
                            max_messages: int = 500) -> list[SentMessage]:
        """Page real Sent Items into the miner's SentMessage shape."""
        since = (datetime.utcnow() - timedelta(days=30 * months)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        params = urllib.parse.urlencode({
            "$filter": f"sentDateTime ge {since}",
            "$select": "subject,toRecipients,sentDateTime,body",
            "$orderby": "sentDateTime desc",
            "$top": "50",
        }, safe="$,:= ")
        url = f"/me/mailFolders/sentitems/messages?{params}"

        out: list[SentMessage] = []
        while url and len(out) < max_messages:
            page = self._get(url)
            for m in page.get("value", []):
                when = None
                if m.get("sentDateTime"):
                    when = datetime.fromisoformat(
                        m["sentDateTime"].replace("Z", "+00:00")).replace(tzinfo=None)
                body = (m.get("body") or {}).get("content", "") or ""
                if (m.get("body") or {}).get("contentType") == "html":
                    body = _strip_html(body)
                for rcpt in m.get("toRecipients", []):
                    addr = (rcpt.get("emailAddress") or {})
                    if not addr.get("address"):
                        continue
                    out.append(SentMessage(
                        to_email=addr["address"],
                        to_display_name=addr.get("name", ""),
                        subject=m.get("subject", "") or "",
                        body=body,
                        when=when,
                    ))
                    if len(out) >= max_messages:
                        break
                if len(out) >= max_messages:
                    break
            url = page.get("@odata.nextLink")
        return out

    def mine_history(self, months: int = 12,
                     max_messages: int = 500, llm=None) -> MiningResult:
        """One call: fetch real Sent Items and learn the vendor registry."""
        return mine(self.fetch_sent_messages(months, max_messages), llm)

    def recent_sent(self, query: str | None = None) -> list[SentRecord]:
        return self.mine_history().records

    # -- Inbox/vendor replies -> reply miner ----------------------------------
    def fetch_received_messages(self, months: int = 12,
                                max_messages: int = 500,
                                folder: str = "inbox") -> list[ReplyMessage]:
        """Page received mail into the reply miner's ReplyMessage shape."""
        since = (datetime.utcnow() - timedelta(days=30 * months)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        params = urllib.parse.urlencode({
            "$filter": f"receivedDateTime ge {since}",
            "$select": "id,subject,from,receivedDateTime,body",
            "$orderby": "receivedDateTime desc",
            "$top": "50",
        }, safe="$,:= ")
        url = f"/me/mailFolders/{folder}/messages?{params}"

        out: list[ReplyMessage] = []
        while url and len(out) < max_messages:
            page = self._get(url)
            for m in page.get("value", []):
                when = None
                if m.get("receivedDateTime"):
                    when = datetime.fromisoformat(
                        m["receivedDateTime"].replace("Z", "+00:00")).replace(tzinfo=None)
                body = (m.get("body") or {}).get("content", "") or ""
                if (m.get("body") or {}).get("contentType") == "html":
                    body = _strip_html(body)
                sender = ((m.get("from") or {}).get("emailAddress") or {})
                addr = sender.get("address") or ""
                if not addr:
                    continue
                out.append(ReplyMessage(
                    from_email=addr,
                    from_display_name=sender.get("name", "") or "",
                    subject=m.get("subject", "") or "",
                    body=body,
                    when=when,
                    message_id=m.get("id", "") or "",
                ))
                if len(out) >= max_messages:
                    break
            url = page.get("@odata.nextLink")
        return out

    def recent_replies(self, query: str | None = None) -> list[ReplyMessage]:
        return self.fetch_received_messages()

    def mine_reply_history(self, store, months: int = 12, max_messages: int = 500):
        """Fetch Inbox replies, mine quote facts, and persist them in Store."""
        replies = mine_replies(self.fetch_received_messages(months, max_messages), store=store)
        store.save_reply_records(replies)
        return replies

    # -- outbound ---------------------------------------------------------------
    @staticmethod
    def _message_payload(draft: EmailDraft) -> dict:
        return {
            "subject": draft.subject,
            "body": {"contentType": "Text", "content": draft.body},
            "toRecipients": [
                {"emailAddress": {"address": draft.to,
                                  **({"name": draft.to_name} if draft.to_name else {})}}
            ],
        }

    def create_draft(self, draft: EmailDraft) -> str:
        created = self._post("/me/messages", self._message_payload(draft))
        return created.get("id", "draft-created")

    def send_email(self, draft: EmailDraft) -> str:
        self._post("/me/sendMail", {"message": self._message_payload(draft),
                                    "saveToSentItems": True})
        return "sent"

    def add_calendar_event(self, title: str, start: datetime,
                           minutes: int = 30) -> str:
        end = start + timedelta(minutes=minutes)
        fmt = "%Y-%m-%dT%H:%M:%S"
        created = self._post("/me/events", {
            "subject": title,
            "start": {"dateTime": start.strftime(fmt), "timeZone": "Eastern Standard Time"},
            "end": {"dateTime": end.strftime(fmt), "timeZone": "Eastern Standard Time"},
        })
        return created.get("id", "event-created")


_TAG = None
def _strip_html(html: str) -> str:
    """Cheap HTML -> text for mined bodies (no external deps)."""
    global _TAG
    import re as _re
    if _TAG is None:
        _TAG = _re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", _re.S | _re.I)
    text = _TAG.sub(" ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return _re.sub(r"[ \t]+", " ", text)
