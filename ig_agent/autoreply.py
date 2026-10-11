"""Auto-replies to comments and DMs on your own account.

Replies come from keyword rules first; if no rule matches and AI replies are
enabled, Claude drafts one. Every handled comment ID is remembered so nothing
is answered twice.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .captions import Writer
from .graph import GraphClient


@dataclass
class ReplyRules:
    rules: list[dict] = field(default_factory=list)  # [{"keywords": [...], "reply": "..."}]
    use_ai: bool = False
    brand_voice: str = ""
    business_info: str = ""
    max_replies_per_run: int = 20

    @classmethod
    def load(cls, path: Path) -> "ReplyRules":
        if not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def match(self, text: str) -> str | None:
        lowered = text.lower()
        for rule in self.rules:
            if any(k.lower() in lowered for k in rule.get("keywords", [])):
                return rule["reply"]
        return None


class Responder:
    def __init__(self, rules: ReplyRules, writer: Writer | None = None):
        self.rules = rules
        self.writer = writer
        if rules.use_ai and writer is None:
            self.writer = Writer(brand_voice=rules.brand_voice, business_info=rules.business_info)

    def draft(self, text: str, kind: str = "comment", context: str = "") -> str | None:
        reply = self.rules.match(text)
        if reply:
            return reply
        if self.rules.use_ai and self.writer:
            draft = self.writer.reply(text, kind=kind, context=context)
            return draft.reply if draft.should_reply and draft.reply.strip() else None
        return None


class SeenStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.ids: set[str] = set(json.loads(self.path.read_text())) if self.path.exists() else set()

    def add(self, item_id: str) -> None:
        self.ids.add(item_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.ids)))

    def __contains__(self, item_id: str) -> bool:
        return item_id in self.ids


def reply_to_comments(client: GraphClient, responder: Responder, seen: SeenStore,
                      own_username: str, media_limit: int = 10, dry_run: bool = False) -> list[dict]:
    """Answer new comments on your most recent posts. Returns what was (or would be) sent."""
    sent = []
    budget = responder.rules.max_replies_per_run
    for media in client.list_media(limit=media_limit):
        for comment in client.list_comments(media["id"]):
            if budget <= 0:
                return sent
            cid = comment["id"]
            if cid in seen or comment.get("username") == own_username:
                continue
            replies = comment.get("replies", {}).get("data", [])
            if any(r.get("username") == own_username for r in replies):
                seen.add(cid)  # already answered by hand
                continue
            reply = responder.draft(comment.get("text", ""), "comment", media.get("caption") or "")
            if reply:
                if not dry_run:
                    client.reply_to_comment(cid, reply)
                sent.append({"comment_id": cid, "username": comment.get("username"),
                             "text": comment.get("text"), "reply": reply})
                budget -= 1
            if not dry_run:
                seen.add(cid)
    return sent


def handle_webhook_event(payload: dict, client: GraphClient, responder: Responder,
                         seen: SeenStore, own_id: str) -> list[dict]:
    """Process an Instagram messaging webhook payload and reply to new DMs."""
    sent = []
    for entry in payload.get("entry", []):
        for event in entry.get("messaging", []):
            message = event.get("message") or {}
            sender = event.get("sender", {}).get("id")
            mid = message.get("mid")
            if (not mid or mid in seen or message.get("is_echo")
                    or sender in (None, own_id) or not message.get("text")):
                continue
            reply = responder.draft(message["text"], kind="direct message")
            if reply:
                client.send_dm(sender, reply)
                sent.append({"sender": sender, "text": message["text"], "reply": reply})
            seen.add(mid)
    return sent
