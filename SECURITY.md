# Security policy

Deskboard is a **proof of concept** Linux client for Tumblr's private web API.
Please treat it accordingly.

## What this software touches

- **Tumblr session cookies** — imported from a local browser (Firefox or a
  Chromium-family profile), a cookies.txt export, or a pasted header. They are
  stored with mode `0600` at `~/.local/share/deskboard/cookies.json` and sent
  only to `https://www.tumblr.com` (and Tumblr's media CDNs for the content you
  view). They are never logged, never uploaded, and never committed.
- **Tumblr's public web token** — served to every visitor by tumblr.com. It is
  fetched at first launch and cached at `~/.config/deskboard/api_token` (mode
  `0600`). No token or credential is present in this repository.
- **No telemetry or analytics.** The application makes no outbound requests to
  anything except Tumblr.
- **Local files only** for caches (`~/.cache/deskboard`), preferences, and the
  installed copy (`~/.local/share/deskboard`).

## Credential handling rules the project follows

- Credentials are read from the user's own machine, at their explicit action.
- Nothing secret is written to logs, error messages, screenshots, fixtures, or
  tests.
- Fixture data under `data/fixtures/` is captured from **public blogs** only.
- If a write endpoint's payload could not be verified without touching a real
  account, it is documented as unverified in `docs/COVERAGE.md` rather than
  guessed at silently.

## Reporting a vulnerability

Open a private security advisory on the repository (Security → Advisories), or
contact the maintainer directly. Please do not open a public issue for anything
involving credential handling or account actions.

## Scope caveats

Because this is a proof of concept:

- The internal API may change without notice and break the client.
- Write actions (like, reblog, follow, reply, ask, submit, settings) are only
  as safe as the user's own account actions; the app never performs them
  automatically.
- The code is expected to be rewritten for Windows/macOS; security fixes will
  target the rewrite going forward.
