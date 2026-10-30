# twitter-promoter

A scheduler and poster for running many X/Twitter accounts from one machine.

Each account posts a configurable number of times per day, with its own media, its
own caption, and its own network exit. It is built around the observation that a
fleet gets detected by its *patterns* — identical captions, shared IPs, reused
media, simultaneous posting — so almost every component exists to break one of
those patterns.

Posting goes through [`twikit`](https://github.com/d60/twikit) (cookie/password
session auth), not the official API, so no developer account is needed. An
optional `official_publisher.py` path is included for anyone who does have API
credentials.

> **Use this on accounts you own, within the terms of the platforms you connect to,
> and within the law where you are.** Automating accounts you do not own, or
> operating at a scale designed to evade enforcement, will get them banned and may
> breach the X Terms of Service. No warranty — see `LICENSE`.

---

## How it works

```
scheduler.py              long-running loop; decides who posts and when
  └─ twitter_poster.py    builds the post and ships it
       ├─ store.py            account state, post log, dedupe
       ├─ media_ledger.py     which clip went to which account, and when
       ├─ ramp.py             new-account ramp-up gating
       ├─ follow_seed.py      follows a seed set so the profile isn't empty
       ├─ communities.py      optional posting into X Communities
       └─ mint_ct0.py         re-mints the csrf cookie when it expires
dashboard.py              local web UI: accounts, creatives, reach, gainers
stats_runner.py           polls views/likes for recent posts
health.py                 per-account health + suspension sweep
```

### Scheduling

`scheduler.py` spreads each account's `posts_per_day` across configured windows.
The exact minute is a deterministic hash of `(account, day, window)`, so a restart
never double-posts and accounts never all fire on the same minute.
`stagger_floor_seconds` and `min/max_seconds_between_accounts` keep the fleet from
posting in a burst. State lives in `_state.json`; the process is single-instance
behind a lock file.

### Captions

Two engines, picked by whether `caption_template` is set:

- **Template** — `"{emojis} {tags}"`. `{tags}` draws `caption_tags_min`..`max`
  tags from `caption_tag_pool`; `{emojis}` draws `caption_emoji_count` from
  `caption_emoji_pool`. Spintax (`{a|b|c}`) is expanded after substitution.
- **Bank** — a phrase from `caption_bank`, optionally plus a CTA from
  `caption_cta_bank`, plus tags.

An **anchor arm** (`caption_tag_anchors`, `caption_anchor_share`) can pin a
known-good tag combination for a stable hash-selected slice of the fleet while the
rest keeps rotating — useful for A/B testing a tag set without changing everyone.

Set `unique_captions` to keep every post's text distinct. This matters more than it
looks: a single shared caption across a fleet is the easiest fingerprint there is.

### Media

`media_dir` is sampled with a reuse gap (`media_reuse_gap_hours`) so two accounts
don't post the same clip at the same time, tracked in `media_ledger.py`.
`unique_encode` re-encodes each upload so byte-identical files never repeat.
`media_transcode`, `media_max_seconds` and `media_max_width` bound what gets sent.
`sfw_mode` / `sfw_share` / `sfw_media_dir` mix in a safe-for-work pool, and
`mark_media_sensitive` sets X's sensitive-media flag.

### Network

`proxy_template` substitutes `{sessid}` with a stable per-account session id, so
each account keeps its own residential exit across runs. `unique_ip_per_post`
forces a fresh exit for every post. `proxy_rotate_tries` rotates on transport
failures, which are per-exit rather than per-account. `proxies.txt` overrides the
template for individual accounts.

### Ramp-up and warmup

New accounts are the ones that get banned. `ramp.py` holds a new account to
`ramp_max_new_per_hour` first posts and keeps it below full volume for
`ramp_days`. `warmup_enabled` has it read and follow before it ever posts.

---

## Setup

```bash
git clone <this repo> && cd twitter-promoter
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp config.example.json config.json
cp accounts.example.txt accounts.txt
```

Fill in `accounts.txt` — one account per line, colon-separated:

```
username:password:email:email_password:auth_token:totp_secret
```

The parser is shape-tolerant, so trailing fields can be omitted. `auth_token` is
the 40-character hex cookie from a logged-in browser session; supplying it avoids a
password login, which is the step most likely to trip a checkpoint.
`browser_login.py` can harvest one interactively.

Then edit `config.json` — at minimum `media_dir`, `posts_per_day`, `timezone`, and
`proxy_template` if you are using proxies.

### Run

```bash
python scheduler.py          # DRY RUN: prints the plan and what it would post
python scheduler.py --live   # actually post
python dashboard.py          # http://127.0.0.1:8787
python stats_runner.py       # poll metrics for recent posts
python health.py             # per-account health sweep
python preflight.py          # check config, accounts and media before going live
```

Run the scheduler under whatever keeps it alive — systemd, `nohup`, or Windows
Task Scheduler.

### Tests

```bash
python -m pytest tests/
```

---

## Operational notes

Things that are not obvious from the code:

- **Tag choice matters more than tag count.** Tags that are themselves heavily
  moderated will take accounts down regardless of how few you use. If survival
  collapses after a tag change, change the tags back before concluding anything
  about volume.
- **Keywords are matched as text, not as hashtags.** Dropping the `#` from a
  flagged term does not launder it.
- **Media upload is the fragile step.** `twikit` builds a transaction key by
  fetching the site's page source; when that fetch fails the upload cannot be
  signed, and the error surfaces as a generic "could not prepare media" rather
  than anything about auth. It is per-exit, so the same file can fail on one proxy
  and succeed on another.
- **A suspended account's posts report zero reach retroactively**, so any reach
  figure has to be computed over accounts that are still alive or it will drift
  downward on its own.
- **Check suspensions from logged out.** An authenticated client can keep
  returning a profile for an account the public cannot see.

## Configuration reference

Every key in `config.example.json` is read by the code; see `twitter_poster.py`
and `scheduler.py` for exact defaults. Notable ones:

| key | meaning |
|---|---|
| `posts_per_day` | posts per account per day |
| `timezone` | IANA zone the posting windows are evaluated in |
| `catchup_grace_minutes` | how late a missed window may still fire |
| `unique_captions` | require every caption to be distinct |
| `unique_media` / `media_reuse_gap_hours` | stop two accounts sharing a clip |
| `unique_encode` | re-encode each upload so bytes never repeat |
| `unique_ip_per_post` | force a fresh proxy exit per post |
| `ramp_enabled` / `ramp_days` | hold new accounts below full volume |
| `warmup_enabled` / `warmup_days` | read/follow before first post |
| `stats_enabled` | poll post metrics in the background |
| `mark_media_sensitive` | set X's sensitive-media flag |

## License

MIT — see `LICENSE`.
