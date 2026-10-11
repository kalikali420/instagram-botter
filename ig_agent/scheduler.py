"""A JSON-file post queue. Run `ig-agent schedule run` from cron to publish due posts."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .graph import GraphClient


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # treat naive times as local time
    return dt.astimezone(timezone.utc)


class PostQueue:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def save(self, posts: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(posts, indent=2))
        tmp.replace(self.path)

    def add(self, media_urls: list[str], caption: str, publish_at: datetime,
            media_type: str = "IMAGE") -> dict:
        post = {
            "id": uuid.uuid4().hex[:8],
            "media_urls": media_urls,
            "media_type": media_type.upper(),
            "caption": caption,
            "publish_at": publish_at.astimezone(timezone.utc).isoformat(),
            "status": "pending",
        }
        posts = self.load()
        posts.append(post)
        self.save(posts)
        return post

    def remove(self, post_id: str) -> bool:
        posts = self.load()
        kept = [p for p in posts if p["id"] != post_id]
        self.save(kept)
        return len(kept) != len(posts)

    def run_due(self, client: GraphClient, now: datetime | None = None) -> list[dict]:
        """Publish every pending post whose time has come. Returns the posts it touched."""
        now = now or datetime.now(timezone.utc)
        posts = self.load()
        touched = []
        for post in posts:
            if post["status"] != "pending" or datetime.fromisoformat(post["publish_at"]) > now:
                continue
            try:
                post["media_id"] = client.publish(post["media_urls"], post["caption"],
                                                  post["media_type"])
                post["status"] = "published"
                post["published_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as exc:  # keep going; record the error on the post
                post["status"] = "failed"
                post["error"] = str(exc)
            touched.append(post)
            self.save(posts)  # save after each publish so a crash never double-posts
        return touched
