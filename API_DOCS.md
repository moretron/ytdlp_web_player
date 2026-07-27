# API v1

Base URL: `http://<host>:5000/api/v1`

- All responses are JSON. Errors: `{"error": "message", ...}`.
- URLs are always passed as `?url=` query parameters (never as path segments) — video URLs contain reserved characters.
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
      "watch_url": "/watch?url=...",
      "thumb_url": "/thumb?url=...",
      "api_url": "/api/v1/videos?url=...",
      "streams_url": "/api/v1/videos/streams?url=..."
    }
  ]
}
```

`thumb_url` transparently falls back to a sprite tile when the source didn't provide a proper thumbnail.

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

Add a URL to the library. Returns immediately (202) and kicks off background preload (meta + thumb + hls-audio) and library sync.

```json
{"ok": true, "url": "...", "dir_hash": "..."}
```

### `GET /videos?url=<url>`

Full details: meta highlights, files on disk, and convenience URLs.

```json
{
  "url": "...",
  "dir_hash": "...",
  "has_meta": true,
  "meta_highlights": {"extractor": "...", "title": "...", "duration": 380, ...},
  "files": [{"name": "video-720.mp4", "size": 12345678, "is_dir": false}, ...],
  "thumb_url": "/api/v1/videos/thumb?url=...",
  "meta_url": "/api/v1/videos/meta?url=...",
  "streams_url": "/api/v1/videos/streams?url=..."
}
```

### `GET /videos/meta?url=<url>`

The raw cached `meta.json` for the video.

### `GET /videos/streams?url=<url>`

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

### `GET /videos/thumb?url=<url>`

302 redirect to `/thumb?url=<url>` (which falls back to a sprite tile if no `thumb.jpg` is present).

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
      "id": "..."
    }
  ]
}
```

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
