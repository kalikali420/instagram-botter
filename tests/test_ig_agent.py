import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ig_agent.autoreply import Responder, ReplyRules, SeenStore, handle_webhook_event, reply_to_comments
from ig_agent.captions import CaptionDraft, ReplyDraft, Writer
from ig_agent.graph import GraphAPIError, GraphClient
from ig_agent.scheduler import PostQueue
from ig_agent.webhook import valid_signature


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self.payload


class FakeSession:
    """Routes (method, path) to canned payloads and records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _handle(self, method, url, params):
        path = url.split("/v23.0/", 1)[1]
        self.calls.append((method, path, params))
        payload = self.routes.get((method, path), {"error": {"message": f"no route {method} {path}"}})
        if callable(payload):
            payload = payload(params)
        return FakeResponse(payload, 400 if "error" in payload else 200)

    def get(self, url, params=None, timeout=None):
        return self._handle("GET", url, params)

    def post(self, url, data=None, timeout=None):
        return self._handle("POST", url, data)


def client(routes):
    session = FakeSession(routes)
    return GraphClient("1789", "TOKEN", session=session), session


def test_publish_single_image():
    c, s = client({
        ("POST", "1789/media"): {"id": "c1"},
        ("GET", "c1"): {"status_code": "FINISHED"},
        ("POST", "1789/media_publish"): {"id": "m1"},
    })
    assert c.publish(["https://x/img.jpg"], "hello") == "m1"
    create = s.calls[0][2]
    assert create["image_url"] == "https://x/img.jpg" and create["caption"] == "hello"
    assert create["access_token"] == "TOKEN"
    assert s.calls[-1][2]["creation_id"] == "c1"


def test_publish_carousel_creates_children():
    counter = iter(["k1", "k2", "parent"])
    c, s = client({
        ("POST", "1789/media"): lambda p: {"id": next(counter)},
        ("GET", "k1"): {"status_code": "FINISHED"},
        ("GET", "k2"): {"status_code": "FINISHED"},
        ("GET", "parent"): {"status_code": "FINISHED"},
        ("POST", "1789/media_publish"): {"id": "m2"},
    })
    assert c.publish(["https://x/a.jpg", "https://x/b.jpg"], "two") == "m2"
    parent = [call for call in s.calls if call[2].get("media_type") == "CAROUSEL"][0][2]
    assert parent["children"] == "k1,k2"


def test_carousel_size_validated():
    c, _ = client({})
    with pytest.raises(ValueError):
        c.publish(["u"] * 11, "", "CAROUSEL")


def test_graph_error_raised():
    c, _ = client({})
    with pytest.raises(GraphAPIError):
        c.me()


def test_queue_publishes_only_due_posts(tmp_path):
    q = PostQueue(tmp_path / "queue.json")
    now = datetime.now(timezone.utc)
    due = q.add(["https://x/a.jpg"], "due", now - timedelta(minutes=1))
    later = q.add(["https://x/b.jpg"], "later", now + timedelta(hours=1))
    c, _ = client({
        ("POST", "1789/media"): {"id": "c1"},
        ("GET", "c1"): {"status_code": "FINISHED"},
        ("POST", "1789/media_publish"): {"id": "m1"},
    })
    touched = q.run_due(c, now=now)
    assert [p["id"] for p in touched] == [due["id"]]
    status = {p["id"]: p["status"] for p in q.load()}
    assert status == {due["id"]: "published", later["id"]: "pending"}
    assert q.run_due(c, now=now) == []  # never double-posts


def test_queue_records_failures(tmp_path):
    q = PostQueue(tmp_path / "queue.json")
    q.add(["https://x/a.jpg"], "x", datetime.now(timezone.utc) - timedelta(minutes=1))
    c, _ = client({})
    [post] = q.run_due(c)
    assert post["status"] == "failed" and "Graph API error" in post["error"]


def test_comment_replies_skip_own_and_seen(tmp_path):
    rules = ReplyRules(rules=[{"keywords": ["price"], "reply": "See link in bio!"}])
    c, s = client({
        ("GET", "1789/media"): {"data": [{"id": "p1", "caption": "cake"}]},
        ("GET", "p1/comments"): {"data": [
            {"id": "a", "text": "What's the PRICE?", "username": "fan"},
            {"id": "b", "text": "price?", "username": "me"},
            {"id": "c", "text": "price pls", "username": "fan2",
             "replies": {"data": [{"id": "r", "username": "me"}]}},
            {"id": "d", "text": "nice", "username": "fan3"},
        ]},
        ("POST", "a/replies"): {"id": "r1"},
    })
    seen = SeenStore(tmp_path / "seen.json")
    sent = reply_to_comments(c, Responder(rules), seen, own_username="me")
    assert [r["comment_id"] for r in sent] == ["a"]
    assert ("POST", "a/replies") in [(m, p) for m, p, _ in s.calls]
    # second run sends nothing new
    assert reply_to_comments(c, Responder(rules), SeenStore(tmp_path / "seen.json"), "me") == []


def test_dry_run_sends_nothing(tmp_path):
    rules = ReplyRules(rules=[{"keywords": ["hi"], "reply": "hello!"}])
    c, s = client({
        ("GET", "1789/media"): {"data": [{"id": "p1"}]},
        ("GET", "p1/comments"): {"data": [{"id": "a", "text": "hi", "username": "fan"}]},
    })
    sent = reply_to_comments(c, Responder(rules), SeenStore(tmp_path / "s.json"), "me", dry_run=True)
    assert len(sent) == 1 and all(m == "GET" for m, _, _ in s.calls)


def test_dm_webhook_replies_once(tmp_path):
    rules = ReplyRules(rules=[{"keywords": ["open"], "reply": "8am-7pm"}])
    c, s = client({("POST", "me/messages"): {"message_id": "x"}})
    payload = {"entry": [{"messaging": [
        {"sender": {"id": "u1"}, "message": {"mid": "m1", "text": "when are you open?"}},
        {"sender": {"id": "1789"}, "message": {"mid": "m2", "text": "open", "is_echo": True}},
    ]}]}
    seen = SeenStore(tmp_path / "seen.json")
    assert len(handle_webhook_event(payload, c, Responder(rules), seen, own_id="1789")) == 1
    assert json.loads(s.calls[0][2]["recipient"]) == {"id": "u1"}
    assert handle_webhook_event(payload, c, Responder(rules), seen, own_id="1789") == []


def test_signature_check():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert valid_signature("secret", body, sig)
    assert not valid_signature("secret", body, "sha256=bad")
    assert not valid_signature("secret", body, None)


class FakeAnthropic:
    def __init__(self, parsed):
        self.kwargs = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self.parsed = parsed

    def _parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.parsed)


def test_caption_render_and_request():
    fake = FakeAnthropic(CaptionDraft(caption="Fresh croissants!", hashtags=["#bakery", "pune food"]))
    draft = Writer(client=fake, brand_voice="warm").caption("new croissants", hashtag_count=2)
    assert draft.render() == "Fresh croissants!\n\n#bakery #punefood"
    assert fake.kwargs["model"] == "claude-opus-5-5"
    assert fake.kwargs["output_format"] is CaptionDraft
    assert fake.kwargs["fallbacks"] == "default"


def test_ai_reply_used_when_no_rule_matches():
    fake = FakeAnthropic(ReplyDraft(should_reply=True, reply="Thanks!"))
    responder = Responder(ReplyRules(use_ai=True), writer=Writer(client=fake))
    assert responder.draft("love this") == "Thanks!"
    fake.parsed = ReplyDraft(should_reply=False, reply="")
    assert responder.draft("spam spam") is None
