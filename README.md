# ig-agent

Run your **own** Instagram Business or Creator account from the command line with the official Instagram Graph API:

- **Post and schedule** images, carousels and Reels
- **Insights**: account totals and per-post metrics, with CSV export
- **Auto-reply** to comments (polling) and DMs (webhook), using keyword rules and optional Claude-written replies
- **Captions and hashtags** written by Claude in your brand voice

It doesn't do fake engagement. It doesn't scrape, log in with a password, or automate any account you don't own. Everything goes through the API Meta provides for this.

## 1. One-time Meta setup

1. Switch your Instagram account to **Business** or **Creator** (Instagram app → Settings → Account type) and link it to a **Facebook Page**.
2. At [developers.facebook.com](https://developers.facebook.com/apps) create an app (type **Business**) and add the **Instagram** product (*API setup with Facebook login*).
3. In the [Graph API Explorer](https://developers.facebook.com/tools/explorer/), generate a token for your app with these permissions:
   - `instagram_basic`, `pages_show_list`, `pages_read_engagement`, `business_management`
   - `instagram_content_publish` for posting
   - `instagram_manage_insights` for analytics
   - `instagram_manage_comments` for comment replies
   - `instagram_manage_messages`, `pages_messaging` and `pages_manage_metadata` for DMs
4. Exchange it for a **long-lived Page access token** (see Meta's *Access Token* docs). User tokens expire, so tokens used from cron should be long-lived Page tokens.
5. Find your Instagram account ID: `GET /me/accounts?fields=instagram_business_account` in the Explorer. Use the `instagram_business_account.id` value (a long number, not your @handle).

While the app is in Development mode it only works for accounts that have a role on the app, which covers managing your own account.

## 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in IG_USER_ID, IG_ACCESS_TOKEN, and ANTHROPIC_API_KEY for AI features
python -m ig_agent whoami
```

## 3. Usage

### Captions with Claude

```bash
python -m ig_agent caption "New pistachio croissant, launching Saturday, limited batch" \
    --voice "warm, upbeat, one emoji max" --hashtags 12
```

### Post now

Media must be at a **public URL** (S3, Cloudinary, your website…). Instagram downloads it from there.

```bash
python -m ig_agent post https://example.com/croissant.jpg --caption "Saturday 🥐"
python -m ig_agent post https://example.com/a.jpg https://example.com/b.jpg --type CAROUSEL --brief "behind the scenes at the bakery"
python -m ig_agent post https://example.com/clip.mp4 --type REELS --caption "How we laminate dough"
```

`--brief` has Claude write the caption. You see it before the confirmation prompt; `-y` skips the prompt.

### Schedule

```bash
python -m ig_agent schedule add https://example.com/a.jpg --at 2026-10-20T09:00 --brief "Monday menu"
python -m ig_agent schedule list
python -m ig_agent schedule remove <id>
python -m ig_agent schedule run          # publishes everything that's due
```

Run `schedule run` every few minutes from cron:

```cron
*/5 * * * * cd /path/to/ig-agent && .venv/bin/python -m ig_agent schedule run >> data/cron.log 2>&1
```

Instagram allows 100 API-published posts per 24 hours.

### Insights

```bash
python -m ig_agent insights --days 28
python -m ig_agent media-insights --limit 20 --csv data/posts.csv
```

Use `--metrics` to pick other metrics. Which ones are available depends on the Graph API version and media type (see Meta's *Insights* reference).

### Auto-reply to comments

```bash
cp reply_rules.example.json data/reply_rules.json   # edit keywords, replies and business info
python -m ig_agent reply-comments --dry-run         # preview
python -m ig_agent reply-comments                   # send keyword-rule replies
python -m ig_agent reply-comments --ai              # Claude replies when no rule matches
```

It checks your most recent posts, skips your own comments and threads you've already answered, remembers what it has handled in `data/seen_comments.json`, and sends at most `max_replies_per_run` replies per run. You can run it from cron too.

### Auto-reply to DMs

```bash
python -m ig_agent webhook --port 8080 --ai
```

Expose the server over HTTPS (a reverse proxy, or `ngrok http 8080` for testing). In your Meta app → Instagram → Webhooks, set the callback URL to `https://<host>/webhook` and the verify token to `IG_WEBHOOK_VERIFY_TOKEN`, then subscribe to `messages`. Set `IG_APP_SECRET` so incoming requests are signature-checked. Meta only lets you reply within **24 hours** of the person's last message.

## Notes

- **AI replies** use Claude (`claude-opus-5-5`) with the server-side refusal fallback turned on. Claude is told to keep replies short, never invent prices or promises, and hand complaints to a human. Run with `--dry-run` for a while before letting it post on its own.
- **Data** (queue, seen IDs) lives in `data/` and is git-ignored.
- **Tests**: `pip install pytest && python -m pytest`
