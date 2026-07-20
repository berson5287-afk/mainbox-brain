"""
MaINbox brain -- the conversational quote-routing engine.

The headless core of the assistant: parse a request, resolve vendors from the
line card + your sourcing history, run the confirm flow, draft/send the RFQ.
Designed to sit behind an HTTP layer that an Android (or any) client calls.

Quick start:
    from mainbox_brain import QuoteConversation, parse_request, StubMailClient

    mail = StubMailClient()
    convo = QuoteConversation(mail)
    print(convo.start(parse_request("price and availability on 10,000ft of 12/2 MC")).message)
    print(convo.handle("just Mark").message)
    print(convo.handle("draft").message)
"""
from .parser import parse_request
from .resolver import resolve, summarize_proposal
from .conversation import QuoteConversation, Turn, State
from .graph_client import MailClient, StubMailClient, GraphMailClient
from .llm import LLMClient
from .intent import detect_meeting, classify_followup, MeetingHint
from .rfq import draft_rfq
from .history_miner import mine, SentMessage, MiningResult, classify_direction
from .reply_miner import mine_replies, ReplyMessage, VendorReplyRecord

__all__ = [
    "parse_request", "resolve", "summarize_proposal",
    "QuoteConversation", "Turn", "State",
    "MailClient", "StubMailClient", "GraphMailClient",
    "LLMClient", "detect_meeting", "classify_followup", "MeetingHint",
    "draft_rfq",
    "mine", "SentMessage", "MiningResult", "classify_direction",
    "mine_replies", "ReplyMessage", "VendorReplyRecord",
]
__version__ = "0.44.0"
