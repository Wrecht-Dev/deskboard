# Deskboard — internal interface contract

Everything below is the agreed boundary between modules. **Do not rename or
change signatures without saying so in the team channel** — several files are
being written in parallel against this document.

Python 3.14 · PyGObject · GTK 4.22 · libadwaita 1.9 · no external deps.

## Running / testing

```bash
. scripts/env.sh                      # GTK4 typelibs + PYTHONPATH
python3 -m deskboard --demo           # offline UI, bundled fixtures
python3 -m deskboard                  # real session (Firefox cookies autodetected)
python3 -m deskboard --doctor         # environment + API diagnostics

# headless render + screenshot
xvfb-run -a -s "-screen 0 1400x1000x24" python3 -m deskboard --demo &
sleep 4; import -window root /tmp/shot.png
```

`ctx.images`, `ctx.client` and network calls are **blocking**: never call
`client.*` on the GTK main thread. Use `threading.Thread` + `GLib.idle_add`.
`ctx.images.load()` is already async and calls back on the main loop.

## Data model (`deskboard/models.py`, written)

```python
Post:  id blog_name blog_title blog_url blog_avatar post_url timestamp
       tags[list[str]] note_count like_count reblog_count reply_count reblog_key
       can_like can_reblog can_reply is_liked is_nsfw summary post_type
       content[list[Block]] trail[list[TrailItem]] layout[list[str]] raw[dict]
       .display_name .handle .is_reblog .kind_label
       .trail_for_display(limit=3) -> (items, hidden_count)
       .media_blocks .first_image

TrailItem: blog_name blog_title blog_url avatar content[list[Block]]

Block: type data media[list[Media]] formatting[list[Formatting]]
       .text .subtype .is_heading .indent_level .url .title .description
       .provider .poster_url

Media: url width height mime kind("image"|"video"|"audio") poster alt
       .is_gif .aspect

Blog: name title url avatar description followers posts uuid is_following
```

Block types seen in the wild: `text` (subtypes `heading1 heading2 quote
indented chat ordered-list-item unordered-list-item`), `image`, `video`,
`audio`, `link`, `poll`, `answer`. Text blocks carry `formatting` ranges
(offset into `.text`, `type` ∈ `bold italic strikethrough link mention color`).

**A reblog post often has `content == []` and everything lives in `trail`.**
Render order is: trail (oldest first, collapsed from the root when long), then
the post's own `content` under the reblogger's header.

## API (`deskboard/api/client.py`, written)

```python
client.get_dashboard(limit=20) -> Page
client.get_blog_posts(blog, limit=20, tag=None) -> Page
client.get_likes(limit=20) -> Page
client.get_tagged(tag, limit=20) -> Page
client.search(query, limit=20) -> Page
client.fetch_page(url) -> Page            # follow Page.next_url
client.get_blog_info(blog) -> Blog
client.get_following(limit=50) -> list[Blog]
client.get_user_info() -> dict
client.like(post) / unlike(post) / reblog(post, blog, comment="")
client.follow(blog) / unfollow(blog)      # may raise TumblrError
Page: .posts[list[Post]] .next_url[str|None] .total[int|None]
```

## Sources (`deskboard/api/sources.py`, written)

```python
FeedSource(kind, value="", title="", subtitle="", icon="...")
  .kind: "dashboard" | "likes" | "blog" | "tagged" | "search"
  .label .key .requires_session()
  .load(client, cursor=None) -> Page
```

## Context (`deskboard/context.py`, written)

```python
ctx.client        # TumblrClient (or DemoClient)
ctx.session       # SessionManager: .cookies .import_firefox() .import_text(t) .clear()
ctx.prefs         # Preferences: .get(key) .set(key, value)
ctx.images        # ImageCache
ctx.is_signed_in  # bool
ctx.user_name / ctx.user_blog / ctx.user_blogs
ctx.toast(msg)                       # raises an Adw toast in the window
ctx.open_url(url)                    # launch in the default browser
ctx.copy_text(text)
ctx.publish("session-changed", bool) # also: "post-changed", post ; "follow-changed", blog
ctx.subscribe(event, callback) -> unsubscribe
ctx.load_user(callback(ok))
ctx.adopt_session(cookies, source) / ctx.sign_out()
```

## Image cache (`deskboard/services/imagecache.py`, written)

```python
ctx.images.load(url, width, height, callback)   # callback(Gdk.Texture | None), main loop
ctx.images.prefetch(list_of_urls, width=500)
ctx.images.cached(url, width) -> texture | None
```

Use `models.upscale_image_url(url, 1280)` before requesting a photo so posts are
sharp on HiDPI.

## Files and owners

| File | Owner | Must provide |
|---|---|---|
| `deskboard/app.py` | shell | `run(demo=False, cookies_path=None, feed=None) -> int` |
| `deskboard/ui/window.py` | shell | `class MainWindow(Adw.ApplicationWindow)` — `__init__(app, ctx)`, `.ctx`, `.open_feed(source)`, `.toast(msg)` |
| `deskboard/ui/sidebar.py` | shell | `class Sidebar(Gtk.Widget)` — items, selection signal, account footer |
| `deskboard/ui/feed.py` | shell | `class FeedPage(Gtk.Widget)` — `__init__(ctx, source)`, `.load_first()`, `.refresh()`, infinite scroll, error/empty states |
| `deskboard/ui/onboarding.py` | shell | `class OnboardingPage(Gtk.Widget)` — cookie import UI |
| `deskboard/ui/settings.py` | shell | `class SettingsWindow(Adw.PreferencesWindow)` |
| `deskboard/ui/style.css` | shell | all custom CSS |
| `deskboard/ui/postcard.py` | cards | `class PostCard(Gtk.Widget)` — `__init__(post, ctx)` |
| `deskboard/ui/blocks.py` | cards | `render_blocks(container, blocks, ctx, post)` |
| `deskboard/ui/html.py` | cards | `to_pango(text, formatting) -> str`, `plain_text(blocks) -> str` |
| `deskboard/ui/mediawidgets.py` | media | `ImageGrid(medias, ctx)`, `VideoBlock(block, ctx)`, `AudioBlock(block, ctx)` — all `Gtk.Widget` |
| `deskboard/ui/viewer.py` | media | `open_viewer(ctx, medias, index=0)` |
| `deskboard/services/media.py` | media | `gif_to_mp4(url) -> Path|None`, `has_ffmpeg() -> bool`, `poster_for(media) -> Gdk.Texture|None` |
| `tests/`, `scripts/`, `README.md`, `*.desktop`, `assets/` | pack | pytest suite, launcher, desktop entry, icon, docs |

## Style rules

- libadwaita widgets and `Adw.StyleManager`; support light **and** dark.
- Post text renders through Pango markup from `html.to_pango`; never inject raw
  HTML.
- Every network path shows a non-blocking failure state; never an unhandled
  traceback in the UI.
- Keyboard: `j`/`k` move, `o`/`Enter` open, `l` like, `r` reblog, `Ctrl+R`
  refresh, `Ctrl+Q` quit, `/` search.
- Keep widget trees shallow — the feed can hold thousands of posts; use
  `Gtk.ListView` recycling, not a `Gtk.Box` of cards.
