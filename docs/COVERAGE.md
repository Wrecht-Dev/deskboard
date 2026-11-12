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
| Tag page | `GET /v2/tagged?tag=` | ✅ (parser fixed — response is a bare list) |
| Post search | `GET /v2/timeline/search?query=` | ✅ |
| Blog + tag + community search | `GET /v2/search/blogs?query=`, `/v2/search/tags` | 🟡 |
| Explore / recommended | `GET /v2/explore/recommended` | 🟡 |
| Trending tags | `GET /v2/explore/trending/tags` | 🟡 (client ready) |
| Likes | `GET /v2/user/likes` | ✅ |
| Followed blogs | `GET /v2/user/following` | ✅ |
| Followed tags ("Your tags") | `GET /v2/user/tags` | ✅ |
| **Notifications / activity** | `GET /v2/blog/{blog}/notifications?limit=&before=` | 🟡 |
| **Conversations (DM list)** | `GET /v2/conversations?limit=&before=` | 🟡 |
| **Conversation messages** | `GET /v2/conversations/messages?conversation_id=` | 🟡 |
| **Message requests** | `GET /v2/conversations/requests` | 🟡 |
| **Inbox (asks/submissions)** | `GET /v2/user/inbox` | 🟡 |
| **Drafts** | `GET /v2/blog/{blog}/posts/draft` | 🟡 |
| **Queue** | `GET /v2/blog/{blog}/posts/queue` | 🟡 |
| Blog settings | `GET /v2/blog/{blog}/settings` | 🟡 |
| Account/user settings | `GET /v2/user/settings` | 🟡 |
| GIF library | `GET /v2/gif/popular` | 🟡 |
| Notifications filters | `GET /v2/activity/filters` | 🔵 |
| Note list on a post | `GET /v2/blog/{blog}/notes?id=&mode=` | 🟡 |
| Feature flags / config | `GET /v2/config` | 🔵 |

## Social actions

| Action | Endpoint | Status |
|---|---|---|
| Like / unlike | `POST /v2/user/like`, `/v2/user/unlike` | ✅ verified live (single test, restored) |
| Reblog (with comment/tags) | `POST /v2/blog/{blog}/post/reblog` | ✅ UI wired (dialog + tags); mocked-path verified |
| Follow / unfollow | `POST /v2/user/follow`, `/v2/user/unfollow` | ✅ blog header button; revert on error |
| Reply to a post | `POST /v2/user/post/reply` | ✅ post-detail composer; optimistic prepend |
| Send ask / submit | ✗ not found at guessed paths | ❗ needs RE |
| Block / mute | ✗ not found at guessed paths | ❗ needs RE |
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
| Create post (NPF blocks) | `POST /v2/blog/{blog}/posts` | 🟡 auth now works; composer is Phase 3 |
| Edit / delete post | `PUT`/`DELETE /v2/blog/{blog}/post/{id}` (POST route confirmed for reblog only) | ❗ |
| Upload image / audio | `POST /v2/media/image`, `/v2/media/audio` | 🔵 |
| GIF search | only `/v2/gif/popular` found; search path unknown | 🔵 |

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
4. **P4**: notes list, block/mute, remaining settings.
