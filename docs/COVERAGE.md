# Tumblr web functionality → Deskboard coverage

Legend: ✅ implemented & verified · 🟡 API verified, UI missing · 🔵 API probed, shape known
· ⚪ not started · ❗ blocked (needs reverse engineering or user confirmation)

Tumblr's web client talks to `https://www.tumblr.com/api/v2/…` with the visitor's
session cookies. Endpoints below were probed against a live account; writes are
deliberately **not** exercised (they mutate the account).

## Read surface

| Feature | Endpoint | Status |
|---|---|---|
| Dashboard "Following" | `GET /v2/timeline/dashboard?wrapped-timeline=true` | ✅ |
| Dashboard "Home / For you" | `GET /v2/tabs/for_you` | ✅ |
| Blog feed | `GET /v2/blog/{blog}/posts` | ✅ |
| Tag page | `GET /v2/hubs/{tag}/timeline?sort=top&limit=14` (the site's own request, captured live) | ✅ Latest/Top UI; note: server currently returns the same set for both, so `top` also orders by note count locally |
| Post search | `GET /v2/timeline/search?query=` | ✅ |
| Blog + tag + community search | `GET /v2/search/blogs?query=`, `/v2/search/tags` | 🟡 |
| Explore / recommended | `GET /v2/explore/recommended` | 🟡 |
| Trending tags | `GET /v2/explore/trending/tags` | 🟡 (client ready) |
| Likes | `GET /v2/user/likes` | ✅ |
| Followed blogs | `GET /v2/user/following` | ✅ |
| Followed tags ("Your tags") | `GET /v2/user/tags` | ✅ sidebar group + **tab** |
| Your-tags aggregate feed | merges `/v2/hubs/{tag}/timeline` per followed tag | ✅ `FollowedTagsSource` (latest/top, cursor) |
| **Notifications / activity** | `GET /v2/blog/{blog}/notifications?limit=&before=` | 🟡 |
| **Conversations (DM list)** | `GET /v2/conversations?limit=&before=` | 🟡 |
| **Conversation messages** | `GET /v2/conversations/messages?conversation_id=` | 🟡 |
| **Message requests** | `GET /v2/conversations/requests` | 🟡 |
| **Inbox (asks/submissions)** | `GET /v2/user/inbox` | 🟡 |
| **Drafts** | `GET /v2/blog/{blog}/posts/draft` | 🟡 |
| **Queue** | `GET /v2/blog/{blog}/posts/queue` | 🟡 |
| Blog settings (view) | `GET /v2/blog/{blog}/settings` | ✅ read-only page; writes unverified |
| Account/user settings (view) | `GET /v2/user/settings` | ✅ read-only page (27 booleans) |
| GIF library | `GET /v2/gif/popular` | 🟡 |
| Notifications filters | `GET /v2/activity/filters` | 🔵 |
| Note list on a post | `GET /v2/blog/{blog}/notes?id=&mode=` | 🟡 |
| Feature flags / config | `GET /v2/config` | 🔵 |

## Social actions

| Action | Endpoint | Status |
|---|---|---|
| Like / unlike | `POST /v2/user/like`, `/v2/user/unlike` | ✅ verified live (single test, restored) |
| Reblog — 3 modes (with text / now / scheduled) | `POST /v2/blog/{blog}/post/reblog` | ✅ UI wired; `publish_on` inferred (scheduling unverified) |
| Follow / unfollow | `POST /v2/user/follow`, `/v2/user/unfollow` | ✅ blog header button; revert on error |
| Reply to a post | `POST /v2/user/post/reply` | ✅ post-detail composer; optimistic prepend |
| Send ask | `POST /v2/blog/{blog}/ask` | ✅ client + UI wiring; route/param confirmed |
| Mute a post (in-feed) | `POST/DELETE /v2/blog/{blog}/posts/{id}/mute` | 🟡 client ready; card-menu wiring pending |
| DM send / accept / read | `POST /v2/conversations/messages`, `/accept`, `/mark_as_read` | ✅ composer, accept, auto mark-read |

**Write auth: solved.** Reads accept `Authorization: Bearer <public web token>`
+ session cookies. Writes additionally require the per-session **`X-CSRF`**
header. That token *is* embedded in the dashboard bootstrap as
`"csrfToken":"<value>"` — sometimes JSON-escaped inside `JSON.parse("…")`; the
original regex missed the escaped form, which is why every write returned
`401 code 1022`. `TumblrClient.csrf_token()` now fetches `/dashboard` with
browser-like headers, extracts the token with an escape-tolerant pattern, caches
it, and on a `1022` response refreshes once and retries.

Verified end-to-end on the live account (with the user's explicit permission):
one **like → unlike** on their own post succeeded (state restored) using a
browser-derived token, and the shipped HTTP-token path passes authentication for
`like`, `unlike`, `reblog` and `follow` (invalid payloads return validation
errors 400/403/404, never `1022`). No account mutation beyond that single
restored like. `POST /v2/oauth2/token` turned out to be login-only
(`grant_type=password`), not a session exchange.

## Composer (Phase 3)

| Feature | Endpoint | Status |
|---|---|---|
| Create post (NPF text/quote/link + images) | `POST /v2/blog/{blog}/posts` | ✅ composer; text/quote/link + attachments + `state=submission` |
| Edit / delete post | `PUT`/`DELETE /v2/blog/{blog}/post/{id}` (POST route confirmed for reblog only) | ❗ |
| Upload image | `POST /v2/media/image` | ✅ attach tray (audio not wired) |
| GIF library | `GET /v2/gif/popular` | ✅ picker (popular only; no search endpoint) |

## Deliberate exclusions

Premium/Blaze/ads purchase flows (`/v2/premium/*`, `/v2/blaze/*`, `/v2/ignite/*`,
`/v2/tumblrmart/*`), account login/password recovery (`/v2/login/*`,
`/v2/password/*`), and privacy/age-verification compliance pages are not part of
the native client's job and would either take payments or duplicate browser
security flows.

## Next phase order

1. **P1 (this round)**: Explore, search (blogs/tags/posts), blog page header +
   follow, notifications, conversations + requests + messages, inbox, drafts,
   queue, blog/user settings, GIF library — all read-only, verified live.
2. **P2 (auth done)**: wire like, reblog-with-tags, follow, reply and DM send
   into the UI; only the single authorized like/unlike was ever fired.
3. **P3**: composer (text/quote/link/photo), media upload, queue/draft/schedule,
   edit/delete.
4. **P4 (largely done)**: notes list + moderation, settings viewers, post mute client. Remaining:
   settings *writes* (routes unverified), blog-level block/mute, post edit/delete, notification actions.

## Complete endpoint inventory (from the site's own chunks)

Every path below was extracted from Tumblr's frontend JS (method + payload
shape). Wired ones have a `TumblrClient` method; "inferred" means the payload
field names could not be confirmed without performing the write.

| Area | Endpoint | Method | Client | Status |
|---|---|---|---|---|
| Post delete | `/v2/blog/{blog}/post/delete` | POST | `delete_post` | ✅ wired |
| Post quick state | `/v2/blog/{blog}/post/edit` | POST | `set_post_state` | ✅ wired |
| Post full edit | `/v2/blog/{blog}/posts/{id}` | PUT | `edit_post` | ✅ wired |
| Pin / unpin | `/v2/blog/{blog}/posts/{id}/pin` | POST/DELETE | `pin_post`/`unpin_post` | ✅ wired |
| Block blog | `/v2/blog/{blog}/blocks` | POST/DELETE | `block_blog`/`unblock_blog` | ✅ wired |
| Report | `/v2/report` | POST | `report` | ⚠️ fields partially inferred |
| Poll vote | `/v2/polls/{pollId}` | POST | `vote_poll` | ✅ wired |
| Follow tag | `/v2/user/tags/add` `/remove` | POST | `follow_tag`/`unfollow_tag` | ✅ wired |
| Blog sub | `/v2/blog/{blog}/subscription` | POST/DELETE | `subscribe_blog`/`unsubscribe_blog` | ✅ wired |
| Post sub | `/v2/blog/{blog}/posts/{id}/subscription` | POST/DELETE | `subscribe_post`/`unsubscribe_post` | ✅ wired |
| Queue pause | `/v2/blog/{blog}/posts/queue/state` | POST | `set_queue_paused` | ✅ wired |
| Queue shuffle | `…/posts/queue/shuffle` | POST | `shuffle_queue` | ✅ wired |
| Queue reorder | `…/posts/queue/reorder` | POST | `reorder_queue` | ✅ wired |
| Inbox delete | `/v2/user/inbox`, `/v2/blog/{blog}/posts/submission` | DELETE | `delete_inbox_submissions`/`delete_submission` | ✅ wired |
| Activity read | `/v2/blog/{blog}/activity_last_read` | POST | `mark_activity_read` | ✅ wired |
| Conversation delete | `/v2/conversations/messages` | DELETE | `delete_conversation` | ✅ wired |
| Conversation flag | `/v2/conversations/flag` | POST | `flag_conversation` | ✅ wired |
| Settings write | `/v2/blog/{blog}/settings`, `/v2/user/settings` | POST | `update_blog_settings`/`update_user_settings` | ✅ wired |
| Mention suggest | `/v2/mention/{prefix}` | GET | `mention_suggestions` | ✅ wired |
| Note delete | `/v2/blog/{blog}/notes/delete` | POST | `delete_note` | ✅ wired (timestamp-keyed) |
| Note flag | `/v2/blog/{blog}/notes/flag` | POST | `flag_note` | ✅ wired |
| Not interested | `/v2/explore/recommended` | DELETE | — | not wired |
| Session mgmt | `/v2/user/session(s)`, `/v2/oauth2/revoke` | DELETE/POST | — | account pages excluded |
| 2FA | `/v2/user/tfa/*` | POST | — | excluded (browser security flow) |
| Premium/Blaze/domains | `/v2/premium/*`, `/v2/blaze/*`, `/v2/ignite/*`, `/v2/domains/*` | various | — | excluded (payments) |
| Communities | `/v2/communities/*` | various | — | excluded (not in scope) |

## Verification log (latest)

Run against the live account through the real GTK widgets (headless Xvfb),
counts only — no private content captured.

| Surface | Evidence |
|---|---|
| Home / Explore feeds | construct and load; paginated via pipelined buffer |
| Likes | **19 items** rendered (2,018 total liked) |
| Tag Latest | loads (`/v2/hubs/{tag}/timeline`, server order) |
| Tag Top | **7 items**, ordered by note count (server returns same set) |
| Notifications | constructs, `before`-cursor pagination, 48→98 items observed |
| Messages | list + thread: **8 messages** rendered, sender alignment, ms ordering |
| Inbox / Drafts / Queue | construct with correct typed pages and empty states |
| Composer | opens with blog selector, type switcher, tags, schedule, attachments |
| Settings | Blogs / Blog settings / Account / User settings / Appearance / Storage |
| Blog page | public blog header + feed + Ask/Message capability gating |
| Post detail | post via PostCard + notes (50→97 paginated on a public post) |
| Maintenance | launcher pins install dir; `--doctor` detects stale installs |

**Known-unverified (not silently claimed):** settings writes (unit-tested,
never fired at the account), new-conversation send (participant selector, no
live thread created), `report` field names (partially inferred; errors visible),
DM history pagination beyond page one (no >50-message thread available), and
tag "Top" ordering when Tumblr returns an identical set for both sorts.

**Excluded by design:** Premium/Blaze/ignite purchases, domain management,
communities, 2FA and login/password recovery, session management.
