import os
import json
import mimetypes
import shutil
import signal
import sys

mimetypes.add_type('video/mp2t', '.ts')
mimetypes.add_type('video/iso.segment', '.m4s')
mimetypes.add_type('application/vnd.apple.mpegurl', '.m3u8')
from flask import Flask, render_template, request, jsonify, Response
from io import BytesIO
from starlette.middleware.wsgi import WSGIMiddleware

from main import *
from addons import *
from external import External
import library_db
import log_capture

log_capture.init(os.path.join(data_path, 'app.log'))
library_db.init_db()

from api import bp as api_bp

try:
    with open(os.path.join(os.path.dirname(__file__), 'ytdlp_searches.json'), 'r') as _f:
        YTDLP_SEARCH_PREFIXES = json.load(_f).get('prefixes') or []
    print(f'Loaded {len(YTDLP_SEARCH_PREFIXES)} yt-dlp search prefixes')
except Exception as _e:
    print(f'ytdlp_searches.json not loaded: {_e}')
    YTDLP_SEARCH_PREFIXES = []

# Default result count injected when the user omits it (e.g. `ytsearch:cats`
# -> `ytsearch24:cats`). Applies to every SearchInfoExtractor subclass, so it
# covers ytsearch, pornhubsearch, phsearch, bilisearch, etc. Override with
# the env var to change the default (or set to 'all' for all results).
try:
    _default_n = os.getenv('YTDLP_SEARCH_DEFAULT_N', '24').strip()
    YTDLP_SEARCH_DEFAULT_N = 'all' if _default_n.lower() == 'all' else str(int(_default_n))
except Exception:
    YTDLP_SEARCH_DEFAULT_N = '24'

# Prefixes that accept an integer count (SearchInfoExtractor subclasses).
# Introspected at startup so it stays in sync with the installed yt-dlp fork.
try:
    import yt_dlp
    from yt_dlp.extractor.common import SearchInfoExtractor
    COUNTED_SEARCH_PREFIXES = {
        ie._SEARCH_KEY.lower()
        for ie in yt_dlp.list_extractors()
        if isinstance(ie, SearchInfoExtractor) and getattr(ie, '_SEARCH_KEY', None)
    }
    print(f'Detected {len(COUNTED_SEARCH_PREFIXES)} counted search prefixes '
          f'(default N={YTDLP_SEARCH_DEFAULT_N})')
except Exception as _e:
    print(f'Could not enumerate SearchInfoExtractor subclasses: {_e}')
    COUNTED_SEARCH_PREFIXES = set()


app = Flask(__name__)
app.register_blueprint(api_bp)
wsgi = WSGIMiddleware(app)

def signal_handler(signum, frame):
    print(f"Signal {signum} received. Shutting down...")
    Processes.rm_all()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def _render_home():
    print('Started serving home/library')
    show_hidden = request.args.get('show_hidden') in ('1', 'true', 'yes')
    ydl_version = External.get_ytdlp_version()
    js_runtime_version = External.get_js_runtime_version(js_runtime)
    ffmpeg_version = External.get_ffmpeg_version(ffmpeg)
    entries = library_db.list_videos(include_hidden=show_hidden)
    grouped = {}
    for e in entries:
        grouped.setdefault(e.get('source_site') or 'unknown', []).append(e)
    grouped = dict(sorted(grouped.items(), key=lambda kv: kv[0]))
    all_tags = library_db.list_tags(include_hidden=show_hidden)
    all_categories = library_db.list_categories(include_hidden=show_hidden)
    all_sites = library_db.list_sites(include_hidden=show_hidden)
    hidden_sites = library_db.list_hidden_sites()
    saved_searches = library_db.list_saved_searches()
    print('Stopped serving home/library')
    return render_template('index.html',
        entries=entries, grouped=grouped, total=len(entries),
        all_tags=all_tags, all_categories=all_categories, all_sites=all_sites,
        hidden_sites=hidden_sites, show_hidden=show_hidden,
        search_prefixes=YTDLP_SEARCH_PREFIXES,
        saved_searches=saved_searches,
        ydl_version=ydl_version, app_version=app_version,
        js_runtime_version=js_runtime_version, ffmpeg_version=ffmpeg_version,
        app_title=app_title, theme_color=theme_color, amoled_bg=amoled_bg)


@app.route('/')
def index():
    return _render_home()


@app.route('/library')
def library():
    return _render_home()


@app.route('/library/rebuild', methods=['POST'])
def library_rebuild():
    try:
        count = library_db.rebuild()
    except Exception as e:
        return pprint_exc(e)
    return jsonify({"ok": True, "count": count}), 200


@app.route('/library/delete', methods=['POST'])
def library_delete():
    url = get_url(request)
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    Processes.rm_all(url)
    vid_dir = get_data_dir(url)
    library_db.delete_video(url)
    if not os.path.isdir(vid_dir): return jsonify({"ok": True, "note": "not on disk"}), 200
    try:
        shutil.rmtree(vid_dir)
    except Exception as e:
        return pprint_exc(e)
    return jsonify({"ok": True}), 200


@app.route('/library/hide', methods=['POST'])
def library_hide():
    url = get_url(request)
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    library_db.hide_video(url)
    return jsonify({"ok": True}), 200


@app.route('/library/unhide', methods=['POST'])
def library_unhide():
    url = get_url(request)
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    library_db.unhide_video(url)
    return jsonify({"ok": True}), 200


@app.route('/library/site/hide', methods=['POST'])
def library_site_hide():
    name = request.args.get('name', '').strip().lower()
    if not name: return jsonify({"error": "name parameter is required"}), 400
    library_db.hide_site(name)
    return jsonify({"ok": True}), 200


@app.route('/library/site/unhide', methods=['POST'])
def library_site_unhide():
    name = request.args.get('name', '').strip().lower()
    if not name: return jsonify({"error": "name parameter is required"}), 400
    library_db.unhide_site(name)
    return jsonify({"ok": True}), 200


@app.route('/ytsearch')
def ytsearch_route():
    from api import resolve_search_paging  # shared with /api/v1/search
    q = request.args.get('q', '').strip()
    if not q: return jsonify({"error": "q parameter required"}), 400
    if YTDLP_SEARCH_PREFIXES:
        q_lower = q.lower()
        if not any(q_lower.startswith(p) for p in YTDLP_SEARCH_PREFIXES):
            return jsonify({
                "error": "Unknown search prefix. Query must start with one of: " + ", ".join(f"{p}:" for p in YTDLP_SEARCH_PREFIXES),
                "known_prefixes": YTDLP_SEARCH_PREFIXES,
            }), 400
    query, start, end, page, per_page = resolve_search_paging(
        q, request.args, YTDLP_SEARCH_PREFIXES, COUNTED_SEARCH_PREFIXES, YTDLP_SEARCH_DEFAULT_N)
    try:
        # One past the window, so `has_more` costs nothing extra.
        entries = flat_search(query, start, end + 1 if end else None)
    except Exception as e:
        return pprint_exc(e)
    has_more = bool(per_page) and len(entries) > per_page
    if per_page:
        entries = entries[:per_page]
    results = []
    for e in entries:
        url = e.get('webpage_url') or e.get('url') or e.get('original_url') or ''
        if not url: continue
        try:
            url = normalize_url(url)
        except Exception:
            pass
        thumb = pick_search_thumbnail(e)
        if not thumb and (e.get('ie_key') == 'Youtube' or 'youtube' in (e.get('extractor') or '').lower()) and e.get('id'):
            thumb = f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"
        results.append({
            'title': e.get('title') or url,
            'url': url,
            'uploader': e.get('uploader') or e.get('channel') or '',
            'duration': int(e.get('duration') or 0),
            'thumbnail': _proxy_thumb_url(thumb, url) or '',
            'view_count': e.get('view_count'),
            'id': e.get('id') or '',
        })
    return jsonify({'results': results, 'count': len(results),
                    'page': page, 'per_page': per_page, 'has_more': has_more}), 200


@app.route('/search')
def search_page():
    """Bookmarkable URL like /search?q=<prefix>:terms. Renders the home page;
    JS in index.html reads ?q= and auto-runs the search."""
    return _render_home()


def _find_api_docs_path():
    """Locate API_DOCS.md — in Docker it's copied to /app; when running from
    source it lives one directory above src/."""
    here = os.path.dirname(__file__)
    for candidate in (
        os.path.join(here, 'API_DOCS.md'),
        os.path.join(here, '..', 'API_DOCS.md'),
    ):
        if os.path.exists(candidate):
            return candidate
    return None


@app.route('/api/docs')
def api_docs_page():
    path = _find_api_docs_path()
    if not path:
        return Response('API_DOCS.md not found', status=404, mimetype='text/plain')
    try:
        import markdown as _markdown
    except ImportError:
        return Response('markdown package not installed', status=500, mimetype='text/plain')
    with open(path, 'r') as f:
        md = f.read()
    body = _markdown.markdown(md, extensions=['tables', 'fenced_code', 'toc'])
    return render_template('api_docs.html', body=body,
                           app_title=app_title, theme_color=theme_color, amoled_bg=amoled_bg)


@app.route('/api/docs.md')
def api_docs_raw():
    path = _find_api_docs_path()
    if not path:
        return Response('API_DOCS.md not found', status=404, mimetype='text/plain')
    with open(path, 'r') as f:
        return Response(f.read(), mimetype='text/markdown')


@app.route('/logs')
def logs_page():
    return render_template('logs.html', app_title=app_title, theme_color=theme_color, amoled_bg=amoled_bg, log_path=log_capture.LOG_PATH)


@app.route('/logs/tail')
def logs_tail():
    since = request.args.get('since', type=int)
    if since is None:
        data, size = log_capture.read_tail()
    else:
        data, size = log_capture.read_since(since)
    resp = Response(data, mimetype='text/plain; charset=utf-8')
    resp.headers['X-Log-Size'] = str(size)
    return resp


@app.route('/logs/clear', methods=['POST'])
def logs_clear():
    path = log_capture.LOG_PATH
    if not path: return jsonify({"error": "log capture not initialized"}), 500
    try:
        open(path, 'w').close()
    except Exception as e:
        return pprint_exc(e)
    return jsonify({"ok": True}), 200


@app.route('/watch')
def watch():
    print('Started serving watch')
    ydl_version = External.get_ytdlp_version()
    js_runtime_version = External.get_js_runtime_version(js_runtime)
    ffmpeg_version = External.get_ffmpeg_version(ffmpeg)
    url = get_url(request)
    
    video_width = 1280
    video_height = 720
    video_title = app_title
    meta = None

    if check_media(url, 'meta'):
        meta = get_meta(url)
        video_width = meta.get('width') or video_width
        video_height = meta.get('height') or video_height
        video_title = meta.get('title') or app_title
    preload(url)
    if url:
        Thread(target=post_media_hooks, args=(url,), daemon=True).start()

    debug = _watch_debug(url, meta)

    print('Stopped serving watch')
    return render_template('watch.html', original_url=url, ydl_version=ydl_version, app_version=app_version, js_runtime_version=js_runtime_version, ffmpeg_version=ffmpeg_version, app_title=app_title, theme_color=theme_color, amoled_bg=amoled_bg, video_width=video_width, video_height=video_height, video_title=video_title, debug=debug)


def _format_size(n):
    if n is None: return ''
    step = 1024.0
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    x = float(n)
    while x >= step and i < len(units) - 1:
        x /= step
        i += 1
    return f'{x:.1f} {units[i]}' if i else f'{int(x)} {units[i]}'


def _watch_debug(url, meta):
    dir_hash = gen_pathname(url) if url else None
    data_dir = get_data_dir(url) if url else None
    files = []
    if data_dir and os.path.isdir(data_dir):
        for name in sorted(os.listdir(data_dir)):
            p = os.path.join(data_dir, name)
            try:
                if os.path.isdir(p):
                    files.append({'name': name + '/', 'size': '', 'is_dir': True})
                else:
                    files.append({'name': name, 'size': _format_size(os.path.getsize(p)), 'is_dir': False})
            except OSError:
                pass
    highlights = {}
    if meta:
        for k in ('extractor', 'extractor_key', 'title', 'uploader', 'uploader_id', 'channel', 'upload_date',
                  'duration', 'width', 'height', 'fps', 'is_live', 'filesize_approx', 'ext', 'protocol', 'language'):
            v = meta.get(k)
            if v is not None and v != '':
                highlights[k] = v
    return {
        'url': url,
        'dir_hash': dir_hash,
        'data_dir': data_dir,
        'files': files,
        'meta_highlights': highlights,
        'has_meta': meta is not None,
    }


@app.route('/debug/meta')
def debug_meta():
    url = get_url(request)
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    if not check_media(url, 'meta'): return jsonify({"error": "No meta cached for that URL"}), 404
    meta = get_meta(url)
    pretty = request.args.get('pretty', '1') != '0'
    return Response(
        json.dumps(meta, indent=2 if pretty else None, default=str),
        mimetype='application/json'
    )


@app.route('/iframe')
def iframe():
    print('Started serving iframe')
    url = get_url(request)
    
    video_width = 1280
    video_height = 720

    if check_media(url, 'meta'):
        meta = get_meta(url)
        video_width = meta.get('width', video_width)
        video_height = meta.get('height', video_height)
    preload(url)

    print('Stopped serving iframe')
    return render_template('iframe.html', app_title=app_title, theme_color=theme_color, video_width=video_width, video_height=video_height)


def _serve_sprite_tile(sprite_path):
    from PIL import Image
    with Image.open(sprite_path) as im:
        w, h = im.size
        cols = 10 if w >= 200 else 1
        tile_w = w // cols
        tile_h = min(int(tile_w * 9 / 16), h)
        tile = im.crop((0, 0, tile_w, tile_h))
        buf = BytesIO()
        tile.save(buf, format='JPEG', quality=90)
        buf.seek(0)
        return Response(buf.read(), mimetype='image/jpeg')


import re as _re
_DIR_HASH_RE = _re.compile(r'^[a-f0-9]{40}$')


def _blank_thumb():
    """Opt-in placeholder for clients that would rather render a blank tile
    than handle a failure.

    Upstream (59b3366) returns this unconditionally, but this fork's cards
    rely on <img onerror> to swap in the film-icon fallback (the re-fetch
    itself happens server-side when /t/<hash> misses its cache). Answering
    200 with a black PNG would suppress the fallback and
    leave black boxes on the page, so it is only served when the caller asks
    for it with `?fallback=blank` -- useful for native clients with no error
    hook of their own.
    """
    buf = BytesIO()
    Image.new('RGB', (10, 10), color='black').save(buf, format='PNG')
    buf.seek(0)
    return Response(buf, mimetype='image/png', headers={'Cache-Control': 'no-store'})


def _wants_blank_thumb():
    return (request.args.get('fallback') or '').lower() == 'blank'


@app.route('/t/<dir_hash>.mp4')
def serve_thumbnail_video_by_hash(dir_hash):
    """The animated preview clip some sites hand back instead of a still.
    Kept on a separate route so /t/<hash> stays an image for <img> tags."""
    if not _DIR_HASH_RE.match(dir_hash):
        return jsonify({"error": "invalid hash"}), 400
    data_dir = library_db.data_dir_for_hash(dir_hash)
    if data_dir:
        video_thumb = os.path.join(data_dir, 'thumb.mp4')
        if os.path.exists(video_thumb):
            return send_file_partial(video_thumb)
    return jsonify({"error": "no video thumb"}), 404


@app.route('/t/<dir_hash>')
@app.route('/t/<dir_hash>.jpg')
def serve_thumbnail_by_hash(dir_hash):
    if not _DIR_HASH_RE.match(dir_hash):
        return jsonify({"error": "invalid hash"}), 400
    data_dir = library_db.data_dir_for_hash(dir_hash)
    if data_dir:
        thumb_path = os.path.join(data_dir, 'thumb.jpg')
        if os.path.exists(thumb_path):
            return send_file_partial(thumb_path)
        sprite_path = os.path.join(data_dir, 'sprite.jpg')
        if os.path.exists(sprite_path):
            return _serve_sprite_tile(sprite_path)
    # Nothing cached — try to (re)fetch via the normal thumb pipeline. If a
    # download is already running, don't queue behind its lock (up to 10 min
    # per request thread — a grid of missing thumbs would park every worker);
    # answer 404 now and let the next page load pick up the finished file.
    url = library_db.get_url_by_hash(dir_hash)
    if url:
        in_progress = data_dir and os.path.exists(os.path.join(data_dir, 'thumb.temp'))
        if not in_progress:
            try:
                return host_file(url, 'thumb')
            except Exception as e:
                pprint_exc(e)
    if _wants_blank_thumb():
        return _blank_thumb()
    return jsonify({"error": "no thumb or sprite"}), 404


def _thumb_proxy_error(msg, status, url=None, /, **extra):
    """Return a JSON error for /thumb-proxy and print the same detail.

    Leading params are positional-only so callers can pass detail keys that
    shadow them (``status=502`` is a useful field name for the upstream code).

    Proxied thumbs are consumed by <img src>, so nothing ever renders this
    body -- the printed line is usually the only place a human sees why a
    thumbnail came up blank.
    """
    payload = {"error": msg}
    if url:
        payload["url"] = url
    payload.update(extra)
    detail = ' '.join(f'{k}={v}' for k, v in extra.items())
    print(f'thumb-proxy: {msg}'
          + (f' | {detail}' if detail else '')
          + (f' | url={url}' if url else ''))
    return jsonify(payload), status


def _thumb_body_snippet(r):
    """First 2 KB of an upstream error body, de-tagged and whitespace-collapsed.
    CDNs put the real reason (expired token, hotlink block, geo deny) in there,
    so it is worth surfacing next to a bare status code."""
    try:
        chunk = next(r.iter_content(2048), b'') or b''
    except Exception:
        return ''
    text = chunk.decode('utf-8', 'replace') if isinstance(chunk, bytes) else str(chunk)
    text = _re.sub(r'(?is)<(script|style).*?</\1>', ' ', text)
    text = _re.sub(r'<[^>]+>', ' ', text)
    return _re.sub(r'\s+', ' ', text).strip()[:200]


def _thumb_referer(target_url, ref_hint=None):
    """Referer to send upstream. Thumb CDNs commonly hotlink-protect their
    images and answer a refererless GET with 403, so we always send one:
    the source page when the caller told us (``ref``), otherwise the origin
    of the thumbnail itself, which is enough for the CDNs seen so far."""
    from urllib.parse import urlparse
    for candidate in (ref_hint, target_url):
        if not candidate:
            continue
        try:
            p = urlparse(candidate)
        except Exception:
            continue
        if p.scheme in ('http', 'https') and p.hostname:
            return f'{p.scheme}://{p.netloc}/'
    return None


@app.route('/thumb-proxy')
def serve_thumb_proxy():
    """Fetch an external CDN thumbnail through the server so the client never
    connects to the CDN directly (hides client IP). Whitelists common thumb
    CDNs by default; set THUMB_PROXY_ALLOW_ANY=true to allow any host.

    Serves video thumbnails (animated mp4/webm previews) as well as images —
    some sites hand back a short clip where others send a JPEG."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    import requests as _requests

    raw = (request.args.get('url') or '').strip()
    if not raw:
        return _thumb_proxy_error('url parameter required', 400)
    try:
        parsed = urlparse(raw)
    except Exception as e:
        return _thumb_proxy_error('invalid url', 400, raw,
                                  cause=f'{type(e).__name__}: {e}')
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return _thumb_proxy_error(
            f'unsupported url (scheme={parsed.scheme or "none"}, '
            f'host={parsed.hostname or "none"})', 400, raw)

    # SSRF: refuse private/loopback/link-local IPs.
    host = parsed.hostname
    try:
        addr = socket.gethostbyname(host)
        ip = ipaddress.ip_address(addr)
    except Exception as e:
        return _thumb_proxy_error(f'host lookup failed for {host}', 400, raw,
                                  host=host, cause=f'{type(e).__name__}: {e}')
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return _thumb_proxy_error(f'blocked host {host} (resolves to private/reserved {addr})',
                                  400, raw, host=host, ip=addr)

    allow_any = os.getenv('THUMB_PROXY_ALLOW_ANY', 'false').lower() in ('1', 'true', 'yes')
    if not allow_any:
        host_l = host.lower()
        allowed_suffixes = (
            '.phncdn.com', '.ytimg.com', '.googleusercontent.com',
            '.googlevideo.com', '.twimg.com', '.cdninstagram.com',
            '.tiktokcdn.com', '.tiktokcdn-us.com', '.rdcdn.com',
            '.redditmedia.com', '.redd.it', '.imgur.com',
            '.vimeocdn.com', '.dmcdn.net',
            # CDNs of search prefixes shipped in ytdlp_searches.json
            '.hdslb.com', '.sndcdn.com', '.nimg.jp', '.nicovideo.jp',
        )
        if not any(host_l == s[1:] or host_l.endswith(s) for s in allowed_suffixes):
            return _thumb_proxy_error(
                f'host not allowed: {host_l} (not in the CDN whitelist; '
                f'set THUMB_PROXY_ALLOW_ANY=true to permit any host)',
                403, raw, host=host_l, allowed=' '.join(allowed_suffixes))

    headers = {'User-Agent': 'Mozilla/5.0'}
    referer = _thumb_referer(raw, request.args.get('ref'))
    if referer:
        headers['Referer'] = referer
    try:
        r = _requests.get(raw, timeout=15, stream=True, allow_redirects=True,
                          headers=headers, proxies=proxies)
    except Exception as e:
        return _thumb_proxy_error(f'upstream fetch failed for {host}: '
                                  f'{type(e).__name__}: {e}', 502, raw, host=host,
                                  referer=referer)
    if r.status_code >= 400:
        why = _thumb_body_snippet(r)
        extra = {"host": host, "status": r.status_code, "referer": referer}
        if r.reason:
            extra["reason"] = r.reason
        if r.url != raw:
            extra["final_url"] = r.url
        if why:
            extra["upstream_body"] = why
        r.close()
        return _thumb_proxy_error(
            f'upstream returned {r.status_code} {r.reason or ""}'.strip()
            + f' from {host}' + (f' -- {why}' if why else ''),
            502, raw, **extra)

    ctype = (r.headers.get('Content-Type') or 'image/jpeg').split(';')[0].strip()
    if not (ctype.startswith('image/') or ctype.startswith('video/')):
        why = _thumb_body_snippet(r)
        r.close()
        return _thumb_proxy_error(
            f'not an image or video: {host} sent {ctype or "no content-type"}'
            + (f' -- {why}' if why else ''),
            415, raw, host=host, content_type=ctype, status=r.status_code)

    # 8 MB cap to avoid memory abuse; thumbnails are usually <200 KB, and the
    # animated previews that come back as video are a couple of MB at most.
    max_bytes = 8 * 1024 * 1024
    body = bytearray()
    for chunk in r.iter_content(64 * 1024):
        body.extend(chunk)
        if len(body) > max_bytes:
            r.close()
            return _thumb_proxy_error(
                f'response too large: {host} sent over {max_bytes // (1024 * 1024)} MB',
                502, raw, host=host, content_type=ctype, limit_bytes=max_bytes)
    resp = Response(bytes(body), mimetype=ctype)
    resp.headers['Cache-Control'] = 'public, max-age=3600, immutable'
    return resp


def _proxy_thumb_url(u, page_url=None):
    """Wrap an external thumbnail URL in /thumb-proxy so the client fetches
    from us, not the CDN. Passes through empty/local URLs unchanged.

    ``page_url`` is the video page the thumb belongs to; it rides along as
    ``ref`` so the proxy can send a Referer the CDN's hotlink check accepts."""
    if not u or not isinstance(u, str) or u.startswith('/') or u.startswith('data:'):
        return u
    from urllib.parse import quote as _quote
    proxied = f'/thumb-proxy?url={_quote(u, safe="")}'
    if page_url:
        proxied += f'&ref={_quote(page_url, safe="")}'
    return proxied


@app.route('/thumb')
def serve_thumbnail():
    try:
        url = get_url(request)
        if url:
            data_dir = get_data_dir(url)
            thumb_path = os.path.join(data_dir, 'thumb.jpg')
            sprite_path = os.path.join(data_dir, 'sprite.jpg')
            # Name the still explicitly: this route feeds <img> tags, and a
            # prefix match would happily hand back thumb.mp4 instead.
            if os.path.exists(thumb_path):
                return send_file_partial(thumb_path)
            if os.path.exists(sprite_path):
                return _serve_sprite_tile(sprite_path)
        return host_file(url, 'thumb')
    except Exception as e:
        if _wants_blank_thumb():
            pprint_exc(e)
            return _blank_thumb()
        return pprint_exc(e)


@app.route('/sprite')
def serve_sprite():
    url = get_url(request)
    return host_file(url, 'sprite')


@app.route('/sb')
def get_sponsor_segments():
    return get_sb(get_url(request)) or []


@app.route('/raw')
def raw():
    html_template = f'<video controls autoplay><source src="/download?url={get_url(request)}" type="video/mp4"></video>'
    return html_template


@app.route('/download')
def download_media():
    try:
        res = (request.args.get('quality') or '')
        start_time = request.args.get('start', 0, type=float)
        end_time = request.args.get('end', 0, type=float)
        
        media_type = 'audio' if res == 'audio' else f'video-{res}'.removesuffix('-')
        
        if start_time > 0 or end_time > 0:
            media_type += f'_{start_time:.1f}-{end_time:.1f}'

        url = get_url(request)
        video_title = get_meta(url).get('title')
        return host_file(url, media_type, download_name=video_title)

    except Exception as e:
        return pprint_exc(e)


@app.route('/low')
def download_low_quality():
    try:
        return host_file(get_url(request), 'low')
    except Exception as e:
        return pprint_exc(e)


@app.route('/direct')
def resp_direct():
    try:
        res = request.args.get('quality') or ''
        media_type = f'direct-{res}'.removesuffix('-')
        url = get_url(request)
        def stream_from_url_file(path):
            with open(path, 'r') as f:
                return stream_media_file(f.readline().rstrip('\n'), f.readline().rstrip('\n'), f.readline().rstrip('\n'))

        def resp_status(resp):
            if isinstance(resp, tuple): return resp[1]
            return getattr(resp, 'status_code', 200)

        media = check_media(url, media_type)
        if not media:
            # Build the redirect file, then fall through to the streaming
            # branch below. host_file() would send the just-written .url text
            # file itself, which players fail on instantly — only a client
            # that happened to warm the cache with an extra request ever hit
            # the correct path.
            MediaDownloader(url, media_type).run()
            media = check_media(url, media_type)
        if media and media.endswith('.url'):
            resp = stream_from_url_file(media)
            if resp_status(resp) < 400:
                return resp
            # The CDN URL inside the redirect file carries a signed expiry;
            # once it lapses the CDN refuses (e.g. 472) and this file is dead
            # weight. Prefer any locally cached copy, else force a fresh meta
            # (the stale source came from it) and rebuild once.
            print(f'Stale redirect file {media}, self-healing')
            local = (check_media(url, f'video-{res}') if res else None) \
                or check_media(url, 'audio' if res == 'audio' else 'video')
            if local and not local.endswith('.url'):
                return send_file_partial(local)
            os.remove(media)
            if meta_path := check_media(url, 'meta'): os.remove(meta_path)
            MediaDownloader(url, media_type).run()
            media = check_media(url, media_type)
            if media and media.endswith('.url'):
                return stream_from_url_file(media)
        return host_file(url, media_type)
    except Exception as e:
        return pprint_exc(e)


@app.route('/external')
def serve_external():
    url = request.args.get('url')
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    return stream_media_file(url, request.args.get('headers'), request.args.get('cookies'))


@app.route('/subtitle')
def serve_subtitle():
    return host_file(get_url(request), f'sub-{request.args.get("lang")}')


@app.route('/info')
def serve_info():
    try:
        url = get_url(request)
        if not url: return jsonify({"error": "URL parameter is required"}), 400
        return get_video_info(get_meta(url))
    except Exception as e:
        return pprint_exc(e)


@app.route('/manifest.json')
def serve_manifest():
    manifest = render_template('manifest.json', app_title=app_title, theme_color=theme_color, amoled_bg=amoled_bg)
    return Response(manifest.encode('utf-8'), mimetype='application/manifest+json')


@app.route('/playlist')
def serve_playlist():
    try:
        return host_file(get_url(request), 'playlist')
    except Exception as e:
        return pprint_exc(e)


@app.route('/favicon.svg')
def serve_favicon():
    with open(os.path.join(app.static_folder, 'favicon.svg'), 'r') as f:
        favicon = f.read()
    favicon = favicon.replace('#ff7300', theme_color)
    return Response(favicon, mimetype='image/svg+xml')


@app.route('/favicon<int:size>.png')
def serve_favicon_png(size=512):

    from PIL import Image

    img = Image.open(os.path.join(app.static_folder, 'favicon-template.png')).convert('RGBA')
    color = tuple(int(theme_color[i:i+2], 16) / 255 for i in (1, 3, 5))

    data = img.getdata()
    new_data = []
    for item in data:
        new_data.append((int(item[0] * color[0]), int(item[1] * color[1]), int(item[2] * color[2]), item[3]))

    img.putdata(new_data)
    img = img.resize((size, size), Image.Resampling.BICUBIC)

    favicon_png = BytesIO()
    img.save(favicon_png, format='PNG')
    favicon_png.seek(0)
    return Response(favicon_png, mimetype='image/png')


@app.route('/sw.js')
def serve_sw():
    with open(os.path.join(app.static_folder, 'sw.js'), 'r') as f:
        sw = f.read()
    return Response(sw, mimetype='text/javascript')


@app.route('/extension.js')
def serve_extension():
    if os.path.exists(os.path.join(app.static_folder, 'extension.js')):
        with open(os.path.join(app.static_folder, 'extension.js'), 'r') as f:
            extension = f.read()
    elif os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'extension', 'extension.js')):
        with open(os.path.join(os.path.dirname(__file__), '..', 'extension', 'extension.js'), 'r') as f:
            extension = f.read()
    else:
        return Response('extension.js not bundled with this build', status=404, mimetype='text/plain')
    request_url = request.url_root.rstrip('/')
    if p := request.headers.get('X-Forwarded-Proto'): request_url = request_url.replace('http', p, 1)

    extension = extension.replace('https://github.com/Matszwe02/ytdlp_web_player/raw/main/extension', request_url)
    extension = extension.replace('https://github.com/Matszwe02/ytdlp_web_player/raw/main/src/static', request_url)
    extension = extension.replace('1.0.0', External.get_app_version(), 1)
    extension = extension.replace('YT-DLP Web Player', app_title, 1)
    extension = extension.replace("var playerUrl = '';", f"var playerUrl = '{request_url}';", 1)

    return Response(extension, mimetype='text/javascript')


@app.route('/cache_status')
def cache_status():
    """How much of a stream the server has cached, for progress UI.

    HLS: the manifest is written in full up front while ffmpeg fills the
    segment directory behind it, so segments-on-disk vs #EXTINF entries is an
    exact measure of transcode progress. Files: presence/size of the
    downloaded media (a *.part shows in-flight yt-dlp downloads).
    Read-only — never triggers a download."""
    try:
        url = get_url(request)
        if not url: return jsonify({"error": "URL parameter is required"}), 400
        res = (request.args.get('quality') or '').strip()
        data_dir = get_data_dir(url)
        out = {}
        if os.path.isdir(data_dir):
            hls_name = f'hls-{res}'.removesuffix('-')
            m3u8 = os.path.join(data_dir, f'{hls_name}.m3u8')
            if os.path.exists(m3u8):
                with open(m3u8, 'r') as f:
                    total = f.read().count('#EXTINF')
                seg_dir = os.path.join(data_dir, f'hls_segment-{res or "audio"}')
                done = 0
                if os.path.isdir(seg_dir):
                    done = sum(1 for s in os.listdir(seg_dir) if s.endswith('.ts'))
                out['hls'] = {'done': done, 'total': total}
            file_name = 'audio' if res == 'audio' else (f'video-{res}' if res else 'video')
            for i in os.listdir(data_dir):
                if not i.startswith(file_name): continue
                path = os.path.join(data_dir, i)
                if i.endswith('.part'):
                    out.setdefault('file', {'bytes': os.path.getsize(path), 'complete': False})
                elif not i.endswith(('.ytdl', '.temp')):
                    out['file'] = {'bytes': os.path.getsize(path), 'complete': True}
                    break
            try:
                with open(os.path.join(data_dir, 'meta.json'), 'r') as f:
                    out['duration'] = json.load(f).get('duration')
            except Exception:
                pass
        return jsonify(out), 200
    except Exception as e:
        return pprint_exc(e)


@app.route('/hls')
def download_hls():
    try:
        res = (request.args.get('quality') or '')  
        media_type = f'hls-{res}'.removesuffix('-')
        return host_file(get_url(request), media_type)
    except Exception as e:
        return pprint_exc(e)


@app.route('/hls_segment')
def hls_segment():
    import time as _time
    url = get_url(request)
    data_dir = get_data_dir(url)
    quality = request.args.get('quality')
    seg = request.args.get('seg')
    file = os.path.join(data_dir, f'hls_segment-{quality}/segment{seg:>0{4}}.ts')

    if not os.path.exists(file):
        media_type = f'hls-{quality}'.removesuffix('-')
        # Kick off (or ensure) the HLS build in the background.
        host_file(get_url(request), media_type)
        # Poll briefly: strict clients (libmpv/media_kit) give up on immediate 404,
        # so wait up to ~15s for ffmpeg to produce this segment before returning.
        deadline = _time.time() + 15
        while _time.time() < deadline:
            if os.path.exists(file):
                break
            _time.sleep(0.25)
        if not os.path.exists(file):
            resp = jsonify({"error": "Segment not ready"})
            resp.status_code = 503
            resp.headers['Retry-After'] = '2'
            return resp

    return send_file_partial(file)


@app.route('/cookies', methods=['POST'])
def cookies_endpoint():
    try:
        url = get_url(request)
        cookies = request.form.get('cookies')
        if not cookies: return jsonify({"error": "cookies are required"}), 400
        if file := get_global_cookies_file():
            with open(file, 'r') as f:
                cookies += '\n' + f.read()
        os.makedirs(get_data_dir(url), exist_ok=True)
        with open(os.path.join(get_data_dir(url), 'cookies.txt'), 'w') as f:
            f.write(cookies)
        return "OK", 200

    except Exception as e:
        return pprint_exc(e)


@app.route('/cancel')
def cancel_download():
    url = get_url(request)
    if not url: return jsonify({"error": "URL parameter is required"}), 400
    cancelled_count = Processes.rm_all(url)
    return jsonify({"message": f"Cancelled {cancelled_count} ongoing processes"}), 200


@app.after_request
def after_request(response):
    response.headers.add('Accept-Ranges', 'bytes')
    response.headers.add('Content-Security-Policy', "frame-src *")
    return response
