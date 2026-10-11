"""Command-line entry point: `python -m ig_agent <command>`."""

import argparse
import csv
import json
import logging
import sys
import threading
from datetime import datetime, timedelta, timezone

from .autoreply import Responder, ReplyRules, SeenStore, handle_webhook_event, reply_to_comments
from .config import Settings
from .graph import GraphClient
from .scheduler import PostQueue, parse_time

DEFAULT_ACCOUNT_METRICS = "reach,views,accounts_engaged,total_interactions,likes,comments,shares,saves"
DEFAULT_MEDIA_METRICS = "reach,views,likes,comments,shares,saved,total_interactions"


def _client(s: Settings) -> GraphClient:
    return GraphClient(s.ig_user_id, s.access_token, s.graph_version)


def _rules(args, s: Settings) -> ReplyRules:
    rules = ReplyRules.load(args.rules or s.data_dir / "reply_rules.json")
    if getattr(args, "ai", False):
        rules.use_ai = True
    return rules


def _caption(args) -> str:
    if not getattr(args, "brief", None):
        return args.caption or ""
    from .captions import Writer
    draft = Writer(brand_voice=args.voice or "").caption(
        args.brief, hashtag_count=args.hashtags, image_url=args.media[0])
    text = draft.render()
    if args.caption:  # caption given too: use it, append generated hashtags
        text = args.caption + "\n\n" + " ".join("#" + t.lstrip("#") for t in draft.hashtags)
    print("Generated caption:\n" + text + "\n")
    return text


def cmd_whoami(args, s):
    print(json.dumps(_client(s).me(), indent=2))


def cmd_caption(args, s):
    from .captions import Writer
    draft = Writer(brand_voice=args.voice or "").caption(
        args.brief, hashtag_count=args.hashtags, image_url=args.image_url)
    print(draft.render())


def cmd_post(args, s):
    caption = _caption(args)
    if not args.yes and input("Publish now? [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return
    media_id = _client(s).publish(args.media, caption, args.type)
    print(f"Published media {media_id}")


def cmd_schedule_add(args, s):
    caption = _caption(args)
    post = PostQueue(s.data_dir / "queue.json").add(args.media, caption, parse_time(args.at), args.type)
    print(f"Scheduled post {post['id']} for {post['publish_at']} (UTC)")


def cmd_schedule_list(args, s):
    posts = PostQueue(s.data_dir / "queue.json").load()
    if not posts:
        print("Queue is empty.")
    for p in posts:
        first_line = (p["caption"].splitlines() or [""])[0][:60]
        extra = p.get("media_id") or p.get("error") or ""
        print(f"{p['id']}  {p['publish_at']}  {p['status']:<9}  {p['media_type']:<8}  {first_line}  {extra}")


def cmd_schedule_remove(args, s):
    removed = PostQueue(s.data_dir / "queue.json").remove(args.id)
    print("Removed." if removed else "No post with that id.")


def cmd_schedule_run(args, s):
    for p in PostQueue(s.data_dir / "queue.json").run_due(_client(s)):
        print(f"{p['id']}: {p['status']} {p.get('media_id') or p.get('error', '')}")


def cmd_insights(args, s):
    client = _client(s)
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)
    rows = []
    for m in client.account_insights(args.metrics.split(","), period="day",
                                     since=int(since.timestamp()), until=int(until.timestamp())):
        value = m.get("total_value", {}).get("value")
        if value is None and m.get("values"):
            value = sum(v.get("value", 0) for v in m["values"] if isinstance(v.get("value"), (int, float)))
        rows.append({"metric": m["name"], "value": value})
    print(f"Account insights, last {args.days} days:")
    for r in rows:
        print(f"  {r['metric']:<20} {r['value']}")


def cmd_media_insights(args, s):
    client = _client(s)
    metrics = args.metrics.split(",")
    rows = []
    for media in client.list_media(limit=args.limit):
        row = {"id": media["id"], "timestamp": media.get("timestamp"),
               "type": media.get("media_type"), "permalink": media.get("permalink")}
        try:
            for m in client.media_insights(media["id"], metrics):
                row[m["name"]] = (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value"))
        except Exception as exc:  # some metrics aren't available for every media type
            row["error"] = str(exc)
        rows.append(row)
    if args.csv:
        keys = sorted({k for r in rows for k in r}, key=lambda k: (k not in ("id", "timestamp"), k))
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {args.csv}")
    else:
        print(json.dumps(rows, indent=2))


def cmd_reply_comments(args, s):
    client = _client(s)
    me = client.me()
    sent = reply_to_comments(client, Responder(_rules(args, s)), SeenStore(s.data_dir / "seen_comments.json"),
                             own_username=me["username"], media_limit=args.posts, dry_run=args.dry_run)
    label = "Would reply" if args.dry_run else "Replied"
    for r in sent:
        print(f"{label} to @{r['username']}: {r['text']!r} -> {r['reply']!r}")
    print(f"{len(sent)} replies.")


def cmd_webhook(args, s):
    from .webhook import serve
    client = _client(s)
    responder = Responder(_rules(args, s))
    seen = SeenStore(s.data_dir / "seen_messages.json")
    lock = threading.Lock()

    def on_event(payload):
        with lock:
            for r in handle_webhook_event(payload, client, responder, seen, own_id=s.ig_user_id):
                logging.info("DM reply to %s: %r", r["sender"], r["reply"])

    if not s.webhook_verify_token:
        sys.exit("Set IG_WEBHOOK_VERIFY_TOKEN before running the webhook server.")
    serve(args.host, args.port, s.webhook_verify_token, s.app_secret, on_event)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ig-agent", description="Manage your own Instagram account via the Graph API.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="Show the connected account").set_defaults(func=cmd_whoami)

    c = sub.add_parser("caption", help="Write a caption + hashtags with Claude")
    c.add_argument("brief", help="What the post is about")
    c.add_argument("--image-url")
    c.add_argument("--voice", help="Brand voice, e.g. 'playful, emoji-light'")
    c.add_argument("--hashtags", type=int, default=10)
    c.set_defaults(func=cmd_caption)

    def media_args(sp):
        sp.add_argument("media", nargs="+", help="Public image/video URL(s); 2-10 for a carousel")
        sp.add_argument("--type", default="IMAGE", choices=["IMAGE", "CAROUSEL", "REELS"])
        sp.add_argument("--caption", help="Caption text")
        sp.add_argument("--brief", help="Generate the caption with Claude from this brief")
        sp.add_argument("--voice")
        sp.add_argument("--hashtags", type=int, default=10)

    po = sub.add_parser("post", help="Publish a post now")
    media_args(po)
    po.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    po.set_defaults(func=cmd_post)

    sch = sub.add_parser("schedule", help="Manage scheduled posts")
    ss = sch.add_subparsers(dest="schedule_command", required=True)
    sa = ss.add_parser("add", help="Queue a post")
    media_args(sa)
    sa.add_argument("--at", required=True, help="ISO time, e.g. 2026-10-20T09:00 (local) or ...+05:30")
    sa.set_defaults(func=cmd_schedule_add)
    ss.add_parser("list").set_defaults(func=cmd_schedule_list)
    sr = ss.add_parser("remove")
    sr.add_argument("id")
    sr.set_defaults(func=cmd_schedule_remove)
    ss.add_parser("run", help="Publish due posts (run this from cron)").set_defaults(func=cmd_schedule_run)

    i = sub.add_parser("insights", help="Account-level insights")
    i.add_argument("--days", type=int, default=28, help="Look-back window (max 30)")
    i.add_argument("--metrics", default=DEFAULT_ACCOUNT_METRICS)
    i.set_defaults(func=cmd_insights)

    mi = sub.add_parser("media-insights", help="Per-post insights for recent posts")
    mi.add_argument("--limit", type=int, default=10)
    mi.add_argument("--metrics", default=DEFAULT_MEDIA_METRICS)
    mi.add_argument("--csv", help="Write results to this CSV file")
    mi.set_defaults(func=cmd_media_insights)

    rc = sub.add_parser("reply-comments", help="Auto-reply to new comments on recent posts")
    rc.add_argument("--rules", help="Path to reply rules JSON (default data/reply_rules.json)")
    rc.add_argument("--ai", action="store_true", help="Use Claude when no rule matches")
    rc.add_argument("--posts", type=int, default=10, help="How many recent posts to check")
    rc.add_argument("--dry-run", action="store_true", help="Show replies without sending")
    rc.set_defaults(func=cmd_reply_comments)

    w = sub.add_parser("webhook", help="Run the DM auto-reply webhook server")
    w.add_argument("--host", default="0.0.0.0")
    w.add_argument("--port", type=int, default=8080)
    w.add_argument("--rules")
    w.add_argument("--ai", action="store_true")
    w.set_defaults(func=cmd_webhook)
    return p


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    args.func(args, Settings.from_env())


if __name__ == "__main__":
    main()
