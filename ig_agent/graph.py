"""Thin client for the official Instagram Graph API (Instagram API with Facebook Login).

Requires an Instagram Business or Creator account connected to a Facebook Page,
and a Page/User access token with the permissions listed in the README.
"""

import json
import time
from typing import Any, Iterable

import requests


class GraphAPIError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        msg = err.get("message") or str(payload)
        super().__init__(f"Graph API error {status}: {msg}")


class GraphClient:
    BASE = "https://graph.facebook.com"

    def __init__(self, ig_user_id: str, access_token: str, version: str = "v23.0",
                 session: requests.Session | None = None, timeout: float = 30.0):
        self.ig_user_id = ig_user_id
        self.access_token = access_token
        self.version = version
        self.session = session or requests.Session()
        self.timeout = timeout

    # -- low level -----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.BASE}/{self.version}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **params: Any) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params["access_token"] = self.access_token
        for key, value in list(params.items()):
            if isinstance(value, (dict, list)):
                params[key] = json.dumps(value)
        if method == "GET":
            resp = self.session.get(self._url(path), params=params, timeout=self.timeout)
        else:
            resp = self.session.post(self._url(path), data=params, timeout=self.timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": {"message": resp.text}}
        if resp.status_code >= 400 or (isinstance(payload, dict) and "error" in payload):
            raise GraphAPIError(resp.status_code, payload)
        return payload

    def get(self, path: str, **params: Any) -> dict:
        return self.request("GET", path, **params)

    def post(self, path: str, **params: Any) -> dict:
        return self.request("POST", path, **params)

    # -- account -------------------------------------------------------------

    def me(self) -> dict:
        return self.get(self.ig_user_id,
                        fields="id,username,name,followers_count,follows_count,media_count")

    # -- publishing ----------------------------------------------------------

    def create_image_container(self, image_url: str, caption: str | None = None,
                               is_carousel_item: bool = False) -> str:
        params: dict[str, Any] = {"image_url": image_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption
        return self.post(f"{self.ig_user_id}/media", **params)["id"]

    def create_reel_container(self, video_url: str, caption: str | None = None,
                              share_to_feed: bool = True) -> str:
        return self.post(f"{self.ig_user_id}/media", media_type="REELS", video_url=video_url,
                         caption=caption, share_to_feed=str(share_to_feed).lower())["id"]

    def create_carousel_container(self, children: Iterable[str], caption: str | None = None) -> str:
        return self.post(f"{self.ig_user_id}/media", media_type="CAROUSEL",
                         children=",".join(children), caption=caption)["id"]

    def wait_until_ready(self, container_id: str, timeout: float = 300, interval: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while True:
            status = self.get(container_id, fields="status_code").get("status_code")
            if status in (None, "FINISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise GraphAPIError(400, {"error": {"message": f"container {container_id} {status}"}})
            if time.monotonic() > deadline:
                raise TimeoutError(f"container {container_id} still {status} after {timeout}s")
            time.sleep(interval)

    def publish_container(self, container_id: str) -> str:
        self.wait_until_ready(container_id)
        return self.post(f"{self.ig_user_id}/media_publish", creation_id=container_id)["id"]

    def publish(self, media_urls: list[str], caption: str = "", media_type: str = "IMAGE") -> str:
        """Publish a post and return the new media ID.

        media_type: IMAGE (one url), REELS (one video url) or CAROUSEL (2-10 image urls).
        Media URLs must be publicly reachable; Instagram fetches them itself.
        """
        media_type = media_type.upper()
        if media_type == "REELS":
            container = self.create_reel_container(media_urls[0], caption)
        elif media_type == "CAROUSEL" or len(media_urls) > 1:
            if not 2 <= len(media_urls) <= 10:
                raise ValueError("carousel posts need between 2 and 10 media urls")
            children = [self.create_image_container(u, is_carousel_item=True) for u in media_urls]
            for child in children:
                self.wait_until_ready(child)
            container = self.create_carousel_container(children, caption)
        else:
            container = self.create_image_container(media_urls[0], caption)
        return self.publish_container(container)

    def publishing_limit(self) -> dict:
        return self.get(f"{self.ig_user_id}/content_publishing_limit", fields="config,quota_usage")

    # -- media & insights ----------------------------------------------------

    def list_media(self, limit: int = 25) -> list[dict]:
        fields = "id,caption,media_type,permalink,timestamp,like_count,comments_count"
        return self.get(f"{self.ig_user_id}/media", fields=fields, limit=limit).get("data", [])

    def account_insights(self, metrics: Iterable[str], period: str = "day",
                         since: int | None = None, until: int | None = None,
                         metric_type: str | None = "total_value") -> list[dict]:
        return self.get(f"{self.ig_user_id}/insights", metric=",".join(metrics), period=period,
                        since=since, until=until, metric_type=metric_type).get("data", [])

    def media_insights(self, media_id: str, metrics: Iterable[str]) -> list[dict]:
        return self.get(f"{media_id}/insights", metric=",".join(metrics)).get("data", [])

    # -- comments ------------------------------------------------------------

    def list_comments(self, media_id: str, limit: int = 50) -> list[dict]:
        fields = "id,text,username,timestamp,replies{id,username}"
        return self.get(f"{media_id}/comments", fields=fields, limit=limit).get("data", [])

    def reply_to_comment(self, comment_id: str, message: str) -> str:
        return self.post(f"{comment_id}/replies", message=message)["id"]

    # -- direct messages -----------------------------------------------------

    def send_dm(self, recipient_igsid: str, text: str) -> dict:
        """Send a DM via the Messenger Platform. Needs a Page access token, and
        Instagram only allows replies within 24h of the user's last message."""
        return self.post("me/messages", recipient={"id": recipient_igsid},
                         message={"text": text})
