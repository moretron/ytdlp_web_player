# API v1

Base URL: `http://<host>:5000/api/v1`

- All responses are JSON. Errors: `{"error": "message", ...}`.
- **Identifying a video:** endpoints that operate on a single video accept **either** `?id=<hash>` **or** `?url=<source-url>`. Prefer `?id=` for anything already in the library — it's an opaque sha1 hash of the URL, so it doesn't leak the source domain into request lines (which URL-based blockers / DNS filters / proxies sometimes match on). `?url=` is required for new URLs the server hasn't seen yet (e.g. adding a video). The same `id` is exposed as `dir_hash` in list responses and as `id` in `/search` results.
- **Auth:** optional. Set `API_KEY` in `data/.env` to require an `X-API-Key` header on every request. If `API_KEY` is unset, the API is open.
- **CORS:** `Access-Control-Allow-Origin: *` on every response; `OPTIONS` preflight supported. Native apps, mobile apps, and cross-origin web clients can all consume the API.

---

## Health & config

### `GET /health`

```json
{"ok": true, "version": "abc123"}
```

### `GET /config`

Runtime configuration snapshot: `app_title`, `theme_color`, `default_quality`, `max_quality`, `save_all`, `max_video_age`, `disable_transcoding`, `data_path`, `app_version`, `ytdlp_version`, etc.

---

## Library

### `GET /library/videos`

List videos with optional filters. All filters combine as AND.

| param      | type        | notes                                               |
|------------|-------------|-----------------------------------------------------|
| `hidden`   | bool        | include hidden videos + hidden-site videos          |
| `site`     | multi       | filter to these source sites (repeat param)         |
| `tag`      | multi       | must contain ALL specified tags                     |
| `category` | multi       | must contain ALL specified categories               |
| `q`        | string      | substring match on title / uploader / url          |
| `limit`    | int         | page size                                           |
| `offset`   | int         | pagination offset                                   |

Example: `GET /library/videos?site=example.com&tag=music&limit=20`

Response:
```json
{
  "total": 42,
  "count": 20,
  "offset": 0,
  "limit": 20,
  "videos": [
    {
      "url": "...",
      "dir_hash": "...",
      "title": "...",
      "uploader": "...",
      "source_site": "...",
      "duration": 380,
      "width": 1920,
      "height": 1080,
      "upload_date": "20240101",
      "has_thumb": 1,
      "saved_at": 1737000000,
      "hidden": 0,
      "site_hidden": false,
      "tags": ["..."],
      "categories": ["..."],
      "id": "abc123...",
      "watch_url": "/watch?url=...",
      "thumb_url": "/t/abc123...",
      "api_url": "/api/v1/videos?id=abc123...",
      "streams_url": "/api/v1/videos/streams?id=abc123..."
    }
  ]
}
```

- `id` is the same value as `dir_hash` — a 40-char sha1 of the source URL. Use it as `?id=` on any single-video endpoint.
- `thumb_url` uses the opaque id (bypasses URL-based blockers) and transparently falls back to a sprite tile when the source didn't provide a proper thumbnail.
- `has_thumb` is `0` (none), `1` (still image) or `2` (still image **plus** an animated preview clip). When it's `2`, `GET /t/<id>.mp4` serves the clip; `/t/<id>` stays a still so `<img>` keeps working.
- `watch_url` still uses `?url=` since /watch is not currently an id-aware route.

### `GET /library/tags?hidden=false`

```json
{"tags": [{"name": "music", "cnt": 12}, ...]}
```

### `GET /library/categories?hidden=false`

```json
{"categories": [{"name": "Music", "cnt": 8}, ...]}
```

### `GET /library/sites?hidden=false`

```json
{"sites": [{"name": "example.com", "cnt": 30, "hidden": false}, ...]}
```

### `POST /library/rebuild`

Rescans `data/` and repopulates the SQLite index. Preserves per-video hidden state.

```json
{"ok": true, "count": 42}
```

---

## Library actions

### `POST /library/videos/hide?url=<url>`

Marks a video as hidden. Non-destructive — files stay on disk.

### `POST /library/videos/unhide?url=<url>`

### `DELETE /library/videos?url=<url>`

Hard delete: kills any in-flight downloads, removes DB row, removes the data dir.

### `POST /library/sites/hide?name=<site>`

Hide every video from that source site.

### `POST /library/sites/unhide?name=<site>`

---

## Videos

### `POST /videos?url=<url>`

Add a URL to the library. Requires `?url=` (an `id` would be circular — you don't have one yet for a new video). Returns immediately (202) and kicks off background preload (meta + thumb + hls-audio) and library sync.

```json
{"ok": true, "url": "...", "dir_hash": "..."}
```

The returned `dir_hash` is the id you'll use for all subsequent requests on this video.

### `GET /videos?id=<hash>` or `?url=<url>`

Full details: meta highlights, files on disk, and convenience URLs.

```json
{
  "id": "abc123...",
  "url": "...",
  "dir_hash": "abc123...",
  "has_meta": true,
  "meta_highlights": {"extractor": "...", "title": "...", "duration": 380, ...},
  "files": [{"name": "video-720.mp4", "size": 12345678, "is_dir": false}, ...],
  "thumb_url": "/t/abc123...",
  "meta_url": "/api/v1/videos/meta?id=abc123...",
  "streams_url": "/api/v1/videos/streams?id=abc123..."
}
```

### `GET /videos/meta?id=<hash>` or `?url=<url>`

The raw cached `meta.json` for the video.

### `GET /videos/streams?id=<hash>` or `?url=<url>`

Playback URL options across formats. Each entry gives HLS, direct, and download URLs for a quality.

```json
{
  "url": "...",
  "watch_url": "/watch?url=...",
  "options": [
    {"quality": 720, "hls": "/hls?url=...&quality=720", "direct": "/direct?url=...&quality=720", "download": "/download?url=...&quality=720"},
    {"quality": "audio", "hls": "...", "direct": "...", "download": "..."}
  ]
}
```

Fetch any `hls`/`direct` URL through the same server; it'll serve the appropriate manifest or file.

### `GET /videos/thumb?id=<hash>` or `?url=<url>`

302 redirect to `/t/<hash>` (which serves `thumb.jpg` or falls back to a sprite tile).

`GET /t/<hash>.mp4` serves the animated preview clip when the source supplied one (`has_thumb == 2`), `404` otherwise.

---

## Search (via yt-dlp)

### `GET /search?q=<query>`

The `q` must start with a known yt-dlp search prefix, in the form `<prefix>[count]:<terms>` (e.g. `<prefix>5:cats`). [`/search/prefixes`](#get-searchprefixes) returns the accepted list.

**Default result count.** If you omit the count (e.g. `<prefix>:cats` instead of `<prefix>24:cats`), the server injects `YTDLP_SEARCH_DEFAULT_N` (env, default `24`; set to `all` for unlimited). Explicit counts and `<prefix>all:` queries are preserved. The rewrite only applies to `SearchInfoExtractor` prefixes — URL-shortcut prefixes (user/channel/category jumps on non-search extractors) are unaffected.

**Fork-only prefixes.** The bundled yt-dlp fork adds site-specific `…search` and `…category` prefixes on top of upstream's, each with a short alias. They appear in `/search/prefixes` like any other prefix; a `…category` prefix resolves its argument as a slug, probing `/categories/<slug>` then `/<slug>` on the target site.

**Caching.** Results are cached in-process for `SEARCH_CACHE_TTL` seconds (env, default `3600`; `0` disables). Cache key is the post-rewrite query, so `<prefix>:cats` and `<prefix>24:cats` share an entry (both rewrite to the same string).

**Thumbnails are proxied.** The `thumbnail` field in each result is a relative URL like `/thumb-proxy?url=<encoded-cdn-url>`, so the client never connects to the CDN directly. See [`/thumb-proxy`](#get-thumb-proxyurlcdn-url) below.

```json
{
  "count": 24,
  "results": [
    {
      "title": "...",
      "url": "...",
      "uploader": "...",
      "duration": 380,
      "thumbnail": "/thumb-proxy?url=https%3A%2F%2Fcdn.example.com%2F...",
      "view_count": 12345,
      "source_id": "native_extractor_id_here",
      "id": "abc123..."
    }
  ]
}
```

- `id` is our cache id (sha1 of the URL) — what you'd use as `?id=` after downloading it (`POST /videos?url=<url>`).
- `source_id` is the extractor's native id (the source site's own video id) for display / cross-referencing.

Unknown prefix → 400 with `known_prefixes` in the error body.

### `GET /search/prefixes`

Returns every prefix the server will accept (upstream + fork extras).

```json
{"prefixes": ["<prefix>", "<prefix>", "<prefix>", "..."]}
```

### `GET /thumb-proxy?url=<cdn-url>`

Streams an image fetched by the server so the client never contacts the CDN. Used automatically for the `thumbnail` field of search results, but callable directly.

- Only `http`/`https` schemes. Private/loopback/link-local IPs are refused (SSRF guard).
- Host must match a whitelisted CDN suffix by default — the image CDNs of the supported sites, listed in `allowed_suffixes` in `src/app.py`. Set `THUMB_PROXY_ALLOW_ANY=true` to lift the whitelist.
- A `Referer` is always sent upstream — thumb CDNs commonly hotlink-protect their assets and answer a refererless request with `403`. Pass `&ref=<source page url>` to send that page as the referer; without it the thumbnail's own origin is used, which is enough for the CDNs seen so far.
- Response must be `image/*` or `video/*` (some sites serve a short animated preview instead of a still), capped at 8 MB.
- `Cache-Control: public, max-age=3600, immutable` on success.

Errors: `400` for invalid/blocked URL, `403` for disallowed host, `415` for a non-image/non-video body, `502` for upstream failure. Error bodies carry the failing `url`, the `host`, the upstream `status`/`reason`, the `referer` that was sent, and a short `upstream_body` snippet — the same line is printed to the server log, since an `<img>` never shows you the body.

### Browser-only helper: `GET /search?q=<query>` (not `/api/v1/search`)

Bookmarkable HTML page that renders the search UI and auto-runs the query on load. Just a shortcut for the home page; not part of the JSON API.

---

## Saved searches

Persistent list of yt-dlp search queries (`<prefix>:<terms>` — see the prefix table above) stored server-side in SQLite. Rendered as chips in the sidebar; each chip links to `/search?q=<query>`.

### `GET /saved-searches`

```json
{
  "saved_searches": [
    {"query": "<prefix>:cats",    "added_at": 1785270000},
    {"query": "<prefix>:sunsets", "added_at": 1785269800}
  ]
}
```

Ordered by `added_at` DESC (newest first).

### `POST /saved-searches`

Save one or many queries. Each query is validated against `/search/prefixes` — only known-prefix queries are accepted.

**Single:**
```
POST /saved-searches?q=<prefix>:cats
```
or `q=` in `application/x-www-form-urlencoded` body.

**Bulk:** send `queries` in the form body, newline- or comma-separated:
```
POST /saved-searches
Content-Type: application/x-www-form-urlencoded

queries=<prefix>:cats%0A<prefix>:sunsets%0A<prefix>:mountains
```

Response:
```json
{
  "added":   ["<prefix>:cats", "<prefix>:mountains"],
  "skipped": ["<prefix>:sunsets"],
  "errors":  [{"query": "not-a-prefix:foo", "reason": "unknown prefix \"not-a-prefix\""}]
}
```

- `added`: newly inserted.
- `skipped`: already present; `added_at` was bumped.
- `errors`: invalid (missing prefix, or prefix not in `/search/prefixes`).

Status is `200` if anything was added or skipped, `400` if everything failed validation.

### `DELETE /saved-searches?q=<query>`

```json
{"removed": true, "query": "<prefix>:cats"}
```

`200` when a row was deleted, `404` if the query wasn't saved.

---

## Logs

### `GET /logs?since=<bytes>`

Live-tail the app's stdout/stderr. Omit `since` for the last ~64KB.

```json
{"size": 12345, "since": 0, "content": "..."}
```

Poll with the returned `size` as the next `since` for a live stream.

### `POST /logs/clear`

Truncate the log file.

---

## Building a client — quick recipes

**Browse library:**
```bash
curl 'http://localhost:5000/api/v1/library/videos?limit=50'
```

**Filter by site + tag:**
```bash
curl 'http://localhost:5000/api/v1/library/videos?site=example.com&tag=music'
```

**Search and add first result:**
```bash
FIRST=$(curl -s 'http://localhost:5000/api/v1/search?q=<prefix>:cats' | jq -r '.results[0].url')
curl -X POST "http://localhost:5000/api/v1/videos?url=$(python3 -c "from urllib.parse import quote; print(quote('$FIRST'))")"
```

**Get playback URLs for a video:**
```bash
curl 'http://localhost:5000/api/v1/videos/streams?url=https%3A%2F%2Fexample.com%2Fv%2Fabc123'
```

**Hide a site:**
```bash
curl -X POST 'http://localhost:5000/api/v1/library/sites/hide?name=example.com'
```

**Tail the logs:**
```bash
curl 'http://localhost:5000/api/v1/logs' | jq -r .content
```

## With auth

Set `API_KEY=secret123` in `data/.env`, restart, then:
```bash
curl -H 'X-API-Key: secret123' 'http://localhost:5000/api/v1/library/videos'
```
