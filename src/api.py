"""JSON API under /api/v1/* for native/mobile clients.

Design:
- All responses are JSON.
- Errors: {"error": "message"} with an appropriate HTTP status.
- Auth: optional. If env var API_KEY is set, every request must send it in the
  X-API-Key header. If unset, the API is open (same posture as the rest of the
  app).
- CORS: Access-Control-Allow-Origin: * on every /api/v1/* response, plus
  OPTIONS preflight handling.
- URLs are always passed as ?url= query params (not path segments) — video URLs
  contain '/' and other reserved chars.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from urllib.parse import quote_plus

from flask import Blueprint, Response, jsonify, request
from threading import Thread

import library_db
import log_capture
from external import External
from main import data_path
from addons import (
    Processes,
    backfill_duration,
    check_media,
    flat_search,
    get_data_dir,
    get_meta,
    get_video_formats,
    get_video_sources,
    normalize_url,
    post_media_hooks,
    preload,
)


bp = Blueprint('api', __name__, url_prefix='/api/v1')

MEDIA_EXTS = ('.mp4', '.webm', '.mkv', '.mp3', '.m4a', '.opus', '.ogg', '.wav')


def _api_key():
    return os.environ.get('API_KEY', '').strip()


@bp.before_request
def _auth_and_preflight():
    # Preflight always OK; CORS headers added by _cors_headers below.
    if request.method == 'OPTIONS':
        return ('', 204)
    required = _api_key()
    if not required:
        return None
    supplied = (request.headers.get('X-API-Key') or '').strip()
    if supplied != required:
        return jsonify({"error": "invalid or missing X-API-Key"}), 401


@bp.after_request
def _cors_headers(resp):
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
    resp.headers['Access-Control-Max-Age'] = '86400'
    return resp


def _err(msg, status=400, **extra):
    payload = {"error": msg}
    payload.update(extra)
    return jsonify(payload), status


_ID_RE = re.compile(r'^[a-f0-9]{40}$')


def _url_from_id(dir_hash):
    """Given a dir_hash, resolve back to the source URL. Prefer library_db
    (fast, indexed), fall back to reading meta.json from the dir on disk."""
    if not dir_hash or not _ID_RE.match(dir_hash):
        return None
    url = library_db.get_url_by_hash(dir_hash)
    if url: return url
    # Fallback: scan the cache dirs for a matching hash + read its meta.json
    data_dir = library_db.data_dir_for_hash(dir_hash)
    if data_dir:
        meta_path = os.path.join(data_dir, 'meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    m = json.load(f)
                u = m.get('original_url')
                if u: return u
            except Exception:
                pass
    return None


def _get_body_val(name):
    if request.method == 'GET': return None
    body = request.get_json(silent=True) or {}
    return body.get(name)


def _get_url():
    """Resolve the target URL from either ?id= (dir_hash) or ?url= param.
    Prefers id since it doesn't leak the source domain into the request line."""
    id_arg = (request.args.get('id') or _get_body_val('id') or '').strip().lower()
    if id_arg:
        u = _url_from_id(id_arg)
        if u:
            try: return normalize_url(u)
            except Exception: return u
    raw = request.args.get('url') or _get_body_val('url')
    if not raw: return None
    try: return normalize_url(raw)
    except Exception: return raw


# Back-compat alias — used by earlier code in this file
_normalize_url_arg = _get_url


def _int(name, default=None):
    v = request.args.get(name)
    if v is None: return default
    try: return int(v)
    except ValueError: return default


def _multi(name):
    return [v.lower() for v in request.args.getlist(name) if v]


# ---------- Health / Config ----------

@bp.route('/health', methods=['GET', 'OPTIONS'])
def health():
    return jsonify({"ok": True, "version": _app_version()}), 200


def _app_version():
    try:
        return External.get_app_version()
    except Exception:
        return None


@bp.route('/config', methods=['GET', 'OPTIONS'])
def config():
    from main import (
        app_title, theme_color, amoled_bg, default_quality, max_quality,
        autoplay, playlist_support, save_all, max_video_age, disable_transcoding,
        audio_visualizer, data_path,
    )
    return jsonify({
        "app_title": app_title,
        "theme_color": theme_color,
        "amoled_bg": amoled_bg,
        "default_quality": default_quality,
        "max_quality": max_quality,
        "autoplay": autoplay,
        "playlist_support": playlist_support,
        "save_all": save_all,
        "max_video_age": max_video_age,
        "disable_transcoding": disable_transcoding,
        "audio_visualizer": audio_visualizer,
        "data_path": data_path,
        "app_version": _app_version(),
        "ytdlp_version": _safe(External.get_ytdlp_version),
    }), 200


def _safe(fn, *a, **kw):
    try: return fn(*a, **kw)
    except Exception: return None


# ---------- Library: list + facets ----------

@bp.route('/library/videos', methods=['GET', 'OPTIONS'])
def library_videos():
    include_hidden = request.args.get('hidden') in ('1', 'true', 'yes')
    sites = _multi('site')
    tags = _multi('tag')
    cats = _multi('category')
    q = (request.args.get('q') or '').strip().lower()
    limit = _int('limit')
    offset = _int('offset', 0)

    videos = library_db.list_videos(include_hidden=include_hidden)

    def keep(v):
        if sites and (v.get('source_site') or '').lower() not in sites: return False
        vt = {t.lower() for t in (v.get('tags') or [])}
        if tags and not all(t in vt for t in tags): return False
        vc = {c.lower() for c in (v.get('categories') or [])}
        if cats and not all(c in vc for c in cats): return False
        if q:
            hay = ' '.join([v.get('title') or '', v.get('uploader') or '', v.get('url') or '']).lower()
            if q not in hay: return False
        return True

    filtered = [v for v in videos if keep(v)]
    total = len(filtered)
    if offset: filtered = filtered[offset:]
    if limit is not None: filtered = filtered[:limit]

    for v in filtered:
        u = v.get('url') or ''
        h = v.get('dir_hash') or ''
        v['id'] = h
        if h:
            v['thumb_url'] = f'/t/{h}'
            v['api_url'] = f'/api/v1/videos?id={h}'
            v['streams_url'] = f'/api/v1/videos/streams?id={h}'
        if u:
            # /watch still takes ?url= since it may be called for uncached URLs
            v['watch_url'] = f'/watch?url={quote_plus(u)}'

    return jsonify({
        "total": total,
        "count": len(filtered),
        "offset": offset,
        "limit": limit,
        "videos": filtered,
    }), 200


@bp.route('/library/tags', methods=['GET', 'OPTIONS'])
def library_tags():
    include_hidden = request.args.get('hidden') in ('1', 'true', 'yes')
    return jsonify({"tags": library_db.list_tags(include_hidden=include_hidden)}), 200


@bp.route('/library/categories', methods=['GET', 'OPTIONS'])
def library_categories():
    include_hidden = request.args.get('hidden') in ('1', 'true', 'yes')
    return jsonify({"categories": library_db.list_categories(include_hidden=include_hidden)}), 200


@bp.route('/library/sites', methods=['GET', 'OPTIONS'])
def library_sites():
    include_hidden = request.args.get('hidden') in ('1', 'true', 'yes')
    return jsonify({"sites": library_db.list_sites(include_hidden=include_hidden)}), 200


@bp.route('/library/rebuild', methods=['POST', 'OPTIONS'])
def library_rebuild():
    try:
        count = library_db.rebuild()
    except Exception as e:
        return _err(str(e), 500)
    return jsonify({"ok": True, "count": count}), 200


# ---------- Library: hide / unhide / delete ----------

@bp.route('/library/videos/hide', methods=['POST', 'OPTIONS'])
def video_hide():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    library_db.hide_video(url)
    return jsonify({"ok": True}), 200


@bp.route('/library/videos/unhide', methods=['POST', 'OPTIONS'])
def video_unhide():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    library_db.unhide_video(url)
    return jsonify({"ok": True}), 200


@bp.route('/library/videos', methods=['DELETE', 'OPTIONS'])
def video_delete():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    Processes.rm_all(url)
    vid_dir = get_data_dir(url)
    library_db.delete_video(url)
    if os.path.isdir(vid_dir):
        try:
            shutil.rmtree(vid_dir)
        except Exception as e:
            return _err(str(e), 500)
    return jsonify({"ok": True}), 200


@bp.route('/library/sites/hide', methods=['POST', 'OPTIONS'])
def site_hide():
    name = (request.args.get('name') or '').strip().lower()
    if not name: return _err("name parameter required")
    library_db.hide_site(name)
    return jsonify({"ok": True}), 200


@bp.route('/library/sites/unhide', methods=['POST', 'OPTIONS'])
def site_unhide():
    name = (request.args.get('name') or '').strip().lower()
    if not name: return _err("name parameter required")
    library_db.unhide_site(name)
    return jsonify({"ok": True}), 200


# ---------- Videos: add / details / meta / streams ----------

@bp.route('/videos', methods=['POST', 'OPTIONS'])
def video_add():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    # Kick off preload in the background; return immediately with dir_hash.
    Thread(target=preload, args=[url], daemon=True).start()
    Thread(target=post_media_hooks, args=[url], daemon=True).start()
    return jsonify({"ok": True, "url": url, "dir_hash": library_db.dir_hash(url)}), 202


@bp.route('/videos', methods=['GET', 'OPTIONS'])
def video_details():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    dir_hash = library_db.dir_hash(url)
    data_dir = get_data_dir(url)
    meta = None
    if check_media(url, 'meta'):
        try: meta = get_meta(url)
        except Exception: pass
    files = []
    if os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            p = os.path.join(data_dir, name)
            try:
                if os.path.isdir(p):
                    files.append({"name": name, "size": None, "is_dir": True})
                else:
                    files.append({"name": name, "size": os.path.getsize(p), "is_dir": False})
            except OSError:
                pass
    highlights = {}
    if meta:
        for k in ('extractor', 'extractor_key', 'title', 'uploader', 'uploader_id',
                  'channel', 'upload_date', 'duration', 'width', 'height', 'fps',
                  'is_live', 'filesize_approx', 'ext', 'protocol', 'language',
                  'tags', 'categories', 'thumbnail', 'view_count', 'like_count'):
            v = meta.get(k)
            if v is not None and v != '':
                highlights[k] = v
    return jsonify({
        "id": dir_hash,
        "url": url,
        "dir_hash": dir_hash,
        "has_meta": meta is not None,
        "meta_highlights": highlights,
        "files": files,
        "watch_url": f"/watch?url={quote_plus(url)}",
        "thumb_url": f"/t/{dir_hash}",
        "meta_url": f"/api/v1/videos/meta?id={dir_hash}",
        "streams_url": f"/api/v1/videos/streams?id={dir_hash}",
    }), 200


@bp.route('/videos/meta', methods=['GET', 'OPTIONS'])
def video_meta():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    if not check_media(url, 'meta'): return _err("no meta cached for that URL", 404)
    try:
        return Response(json.dumps(get_meta(url), default=str), mimetype='application/json'), 200
    except Exception as e:
        return _err(str(e), 500)


@bp.route('/videos/streams', methods=['GET', 'OPTIONS'])
def video_streams():
    url = _normalize_url_arg()
    if not url: return _err("url parameter required")
    try:
        formats = get_video_formats(url)
    except Exception as e:
        return _err(str(e), 500)
    enc = quote_plus(url)
    dir_hash = library_db.dir_hash(url)
    options = []
    for res in formats:
        options.append({
            "quality": res,
            "hls": f"/hls?url={enc}&quality={res}",
            "direct": f"/direct?url={enc}&quality={res}",
            "download": f"/download?url={enc}&quality={res}",
        })
    options.append({
        "quality": "audio",
        "hls": f"/hls?url={enc}&quality=audio",
        "direct": f"/direct?url={enc}&quality=audio",
        "download": f"/download?url={enc}&quality=audio",
    })
    return jsonify({
        "id": dir_hash,
        "url": url,
        "watch_url": f"/watch?url={enc}",
        "options": options,
    }), 200


@bp.route('/videos/thumb', methods=['GET', 'OPTIONS'])
def video_thumb_redirect():
    url = _get_url()
    if not url: return _err("id or url parameter required")
    from flask import redirect
    return redirect(f'/t/{library_db.dir_hash(url)}', code=302)


# ---------- Search ----------

_BARE_PREFIX_RE = re.compile(r'^([a-z][a-z0-9]*):', re.IGNORECASE)


def _inject_default_count(q, counted_prefixes, default_n):
    """Turn `ytsearch:cats` into `ytsearch24:cats` when the user omitted the
    count. Leaves `ytsearch5:cats` and `ytsearchall:cats` alone, and doesn't
    touch prefixes that aren't SearchInfoExtractor subclasses (e.g.
    `pornhubcategory:`, `ytuser:`)."""
    m = _BARE_PREFIX_RE.match(q)
    if not m:
        return q
    prefix = m.group(1).lower()
    if prefix not in counted_prefixes:
        return q
    return f'{m.group(1)}{default_n}:{q[m.end():]}'


@bp.route('/search', methods=['GET', 'OPTIONS'])
def api_search():
    # Imported lazily to avoid circular import at module load.
    from app import YTDLP_SEARCH_PREFIXES, COUNTED_SEARCH_PREFIXES, YTDLP_SEARCH_DEFAULT_N
    q = (request.args.get('q') or '').strip()
    if not q: return _err("q parameter required")
    if YTDLP_SEARCH_PREFIXES:
        ql = q.lower()
        if not any(ql.startswith(p) for p in YTDLP_SEARCH_PREFIXES):
            return _err(
                "unknown search prefix",
                400,
                known_prefixes=YTDLP_SEARCH_PREFIXES,
            )
    q = _inject_default_count(q, COUNTED_SEARCH_PREFIXES, YTDLP_SEARCH_DEFAULT_N)
    try:
        entries = flat_search(q)
    except Exception as e:
        return _err(str(e), 500)
    results = []
    for e in entries:
        u = e.get('webpage_url') or e.get('url') or e.get('original_url') or ''
        if not u: continue
        try: u = normalize_url(u)
        except Exception: pass
        thumb = e.get('thumbnail')
        if not thumb:
            thumbs = e.get('thumbnails') or []
            if thumbs and isinstance(thumbs[-1], dict):
                thumb = thumbs[-1].get('url')
        if not thumb and (e.get('ie_key') == 'Youtube' or 'youtube' in (e.get('extractor') or '').lower()) and e.get('id'):
            thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
        from app import _proxy_thumb_url
        results.append({
            "title": e.get('title') or u,
            "url": u,
            "uploader": e.get('uploader') or e.get('channel') or '',
            "duration": int(e.get('duration') or 0),
            "thumbnail": _proxy_thumb_url(thumb) or '',
            "view_count": e.get('view_count'),
            # Extractor-provided source id (e.g. YouTube video id) — for display
            "source_id": e.get('id') or '',
            # Our cache id (what you'd use to reference this once downloaded).
            # Client can POST /videos?url=<url> to trigger the download, then
            # subsequent requests can use this id.
            "id": library_db.dir_hash(u),
        })
    return jsonify({"count": len(results), "results": results}), 200


@bp.route('/search/prefixes', methods=['GET', 'OPTIONS'])
def api_search_prefixes():
    from app import YTDLP_SEARCH_PREFIXES
    return jsonify({"prefixes": YTDLP_SEARCH_PREFIXES}), 200


# ---------- Logs ----------

@bp.route('/logs', methods=['GET', 'OPTIONS'])
def api_logs():
    since = _int('since')
    if since is None:
        data, size = log_capture.read_tail()
    else:
        data, size = log_capture.read_since(since)
    return jsonify({
        "size": size,
        "since": since or 0,
        "content": data.decode('utf-8', errors='replace'),
    }), 200


@bp.route('/logs/clear', methods=['POST', 'OPTIONS'])
def api_logs_clear():
    path = log_capture.LOG_PATH
    if not path: return _err("log capture not initialized", 500)
    try:
        open(path, 'w').close()
    except Exception as e:
        return _err(str(e), 500)
    return jsonify({"ok": True}), 200
