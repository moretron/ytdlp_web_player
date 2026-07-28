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

Example: `GET /library/videos?site=youtube.com&tag=music&limit=20`

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
{"sites": [{"name": "youtube.com", "cnt": 30, "hidden": false}, ...]}
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

---

## Search (via yt-dlp)

### `GET /search?q=<query>`

The `q` must start with a known yt-dlp search prefix (e.g. `ytsearch5:bmw repair`, `scsearch10:something`).

```json
{
  "count": 5,
  "results": [
    {
      "title": "...",
      "url": "...",
      "uploader": "...",
      "duration": 380,
      "thumbnail": "https://...",
      "view_count": 12345,
      "source_id": "youtube_video_id_here",
      "id": "abc123..."
    }
  ]
}
```

- `id` is our cache id (sha1 of the URL) — what you'd use as `?id=` after downloading it (`POST /videos?url=<url>`).
- `source_id` is the extractor's native id (e.g. a YouTube video id) for display / cross-referencing.

Unknown prefix → 400 with `known_prefixes` in the error body.

### `GET /search/prefixes`

```json
{"prefixes": ["bilisearch", "gvsearch", "nicosearch", ...]}
```

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
curl 'http://localhost:5000/api/v1/library/videos?site=youtube.com&tag=music'
```

**Search and add first result:**
```bash
FIRST=$(curl -s 'http://localhost:5000/api/v1/search?q=ytsearch:bmw+repair' | jq -r '.results[0].url')
curl -X POST "http://localhost:5000/api/v1/videos?url=$(python3 -c "from urllib.parse import quote; print(quote('$FIRST'))")"
```

**Get playback URLs for a video:**
```bash
curl 'http://localhost:5000/api/v1/videos/streams?url=https%3A%2F%2Fyoutu.be%2Fabc123'
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
