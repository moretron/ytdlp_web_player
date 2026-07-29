import json
import os
import re
import shutil
import sqlite3
from hashlib import sha1
from urllib.parse import urlparse

from main import data_path


def dir_hash(url):
    return sha1(url.encode()).hexdigest()


DB_PATH = os.path.join(data_path, 'library.db')
CACHE_ROOT = os.path.join(data_path, 'cache')
MEDIA_EXTS = ('.mp4', '.webm', '.mkv', '.mp3', '.m4a', '.opus', '.ogg', '.wav')
_HASH_RE = re.compile(r'^[a-f0-9]{40}$')


def source_site(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.removeprefix('www.') or 'unknown'
    except Exception:
        return 'unknown'


def data_dir_for(url):
    """Filesystem path for a URL's cache dir: data/cache/<site>/<hash>/"""
    return os.path.join(CACHE_ROOT, source_site(url), dir_hash(url))


def data_dir_for_hash(dh):
    """Reverse lookup: given a hash, find the on-disk dir path.
    Prefers DB lookup (fast); falls back to scanning cache subdirs."""
    if not dh or not _HASH_RE.match(dh): return None
    url = get_url_by_hash(dh)
    if url:
        p = data_dir_for(url)
        if os.path.isdir(p): return p
    if not os.path.isdir(CACHE_ROOT): return None
    try:
        for site in os.listdir(CACHE_ROOT):
            candidate = os.path.join(CACHE_ROOT, site, dh)
            if os.path.isdir(candidate): return candidate
    except OSError:
        pass
    return None


def _migrate_data_layout():
    """One-time move of legacy data/<hash>/ dirs into data/cache/<site>/<hash>/.
    Idempotent: safe to run every startup."""
    if not os.path.isdir(data_path): return
    moved, skipped = 0, 0
    for name in os.listdir(data_path):
        # skip our new home + root files (library.db, .env, app.log, cookies.txt, PID files)
        if name == 'cache': continue
        if not _HASH_RE.match(name): continue
        old = os.path.join(data_path, name)
        if not os.path.isdir(old): continue
        # figure out the site
        site = 'unknown'
        meta_path = os.path.join(old, 'meta.json')
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    m = json.load(f)
                u = m.get('original_url') or ''
                if u: site = source_site(u)
            except Exception:
                pass
        target_site_dir = os.path.join(CACHE_ROOT, site)
        os.makedirs(target_site_dir, exist_ok=True)
        target = os.path.join(target_site_dir, name)
        if os.path.exists(target):
            print(f'[migrate] target already exists, leaving in place: {name}')
            skipped += 1
            continue
        try:
            shutil.move(old, target)
            print(f'[migrate] {name} -> cache/{site}/')
            moved += 1
        except Exception as e:
            print(f'[migrate] failed to move {name}: {e}')
            skipped += 1
    if moved or skipped:
        print(f'[migrate] finished: moved={moved}, skipped={skipped}')


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    os.makedirs(data_path, exist_ok=True)
    os.makedirs(CACHE_ROOT, exist_ok=True)
    _migrate_data_layout()
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                url TEXT PRIMARY KEY,
                dir_hash TEXT NOT NULL,
                title TEXT,
                uploader TEXT,
                source_site TEXT,
                duration INTEGER,
                width INTEGER,
                height INTEGER,
                upload_date TEXT,
                has_thumb INTEGER,
                saved_at INTEGER,
                hidden INTEGER NOT NULL DEFAULT 0
            )
        """)
        existing_cols = {r['name'] for r in conn.execute("PRAGMA table_info(videos)").fetchall()}
        if 'hidden' not in existing_cols:
            conn.execute("ALTER TABLE videos ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_source_site ON videos(source_site)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_saved_at ON videos(saved_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_hidden ON videos(hidden)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS hidden_sites (
                name TEXT PRIMARY KEY
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_tags (
                url TEXT NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (url, tag_id),
                FOREIGN KEY (url) REFERENCES videos(url) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_categories (
                url TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                PRIMARY KEY (url, category_id),
                FOREIGN KEY (url) REFERENCES videos(url) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_tags_tag ON video_tags(tag_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_video_categories_cat ON video_categories(category_id)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_searches (
                query TEXT PRIMARY KEY,
                added_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_saved_searches_added_at ON saved_searches(added_at)")


def list_saved_searches():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT query, added_at FROM saved_searches ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_saved_search(query):
    """Insert or refresh a single saved search. Returns True if newly added."""
    import time as _time
    q = (query or '').strip()
    if not q:
        return False
    with _connect() as conn:
        existed = conn.execute("SELECT 1 FROM saved_searches WHERE query = ?", (q,)).fetchone()
        conn.execute(
            "INSERT INTO saved_searches (query, added_at) VALUES (?, ?) "
            "ON CONFLICT(query) DO UPDATE SET added_at = excluded.added_at",
            (q, int(_time.time())),
        )
    return not existed


def remove_saved_search(query):
    q = (query or '').strip()
    if not q:
        return False
    with _connect() as conn:
        cur = conn.execute("DELETE FROM saved_searches WHERE query = ?", (q,))
    return cur.rowcount > 0


def _clean_labels(items):
    if not items: return []
    seen = set()
    out = []
    for x in items:
        if not isinstance(x, str): continue
        s = x.strip()
        if not s: continue
        key = s.lower()
        if key in seen: continue
        seen.add(key)
        out.append(s)
    return out


def _ensure_ids(conn, table, names):
    ids = []
    for name in names:
        row = conn.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        if row:
            ids.append(row['id'])
        else:
            cur = conn.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
            ids.append(cur.lastrowid)
    return ids


def _sync_associations(conn, url, tag_names, cat_names):
    conn.execute("DELETE FROM video_tags WHERE url = ?", (url,))
    conn.execute("DELETE FROM video_categories WHERE url = ?", (url,))
    for tag_id in _ensure_ids(conn, 'tags', tag_names):
        conn.execute("INSERT OR IGNORE INTO video_tags (url, tag_id) VALUES (?, ?)", (url, tag_id))
    for cat_id in _ensure_ids(conn, 'categories', cat_names):
        conn.execute("INSERT OR IGNORE INTO video_categories (url, category_id) VALUES (?, ?)", (url, cat_id))


def _prune_orphans(conn):
    conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM video_tags)")
    conn.execute("DELETE FROM categories WHERE id NOT IN (SELECT category_id FROM video_categories)")


def _list_labels_for(conn, table_junction, key_col, table_dim):
    rows = conn.execute(f"""
        SELECT j.url AS url, d.name AS name
        FROM {table_junction} j
        JOIN {table_dim} d ON d.id = j.{key_col}
    """).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['url'], []).append(r['name'])
    for names in grouped.values():
        names.sort(key=str.lower)
    return grouped


_VISIBILITY_WHERE = "v.hidden = 0 AND v.source_site NOT IN (SELECT name FROM hidden_sites)"


def list_videos(include_hidden=False):
    with _connect() as conn:
        hidden_sites = {r['name'] for r in conn.execute("SELECT name FROM hidden_sites").fetchall()}
        sql = "SELECT v.* FROM videos v"
        if not include_hidden:
            sql += " WHERE " + _VISIBILITY_WHERE
        sql += " ORDER BY v.saved_at DESC"
        rows = conn.execute(sql).fetchall()
        videos = [dict(r) for r in rows]
        tags_map = _list_labels_for(conn, 'video_tags', 'tag_id', 'tags')
        cats_map = _list_labels_for(conn, 'video_categories', 'category_id', 'categories')
    for v in videos:
        v['tags'] = tags_map.get(v['url'], [])
        v['categories'] = cats_map.get(v['url'], [])
        v['site_hidden'] = v.get('source_site') in hidden_sites
    return videos


def list_sites(include_hidden=False):
    with _connect() as conn:
        hidden_sites = {r['name'] for r in conn.execute("SELECT name FROM hidden_sites").fetchall()}
        if include_hidden:
            rows = conn.execute("""
                SELECT source_site AS name, COUNT(*) AS cnt
                FROM videos
                WHERE source_site IS NOT NULL AND source_site != ''
                GROUP BY source_site
                ORDER BY cnt DESC, LOWER(source_site)
            """).fetchall()
        else:
            rows = conn.execute(f"""
                SELECT v.source_site AS name, COUNT(*) AS cnt
                FROM videos v
                WHERE v.source_site IS NOT NULL AND v.source_site != ''
                  AND {_VISIBILITY_WHERE}
                GROUP BY v.source_site
                ORDER BY cnt DESC, LOWER(v.source_site)
            """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d['hidden'] = d['name'] in hidden_sites
        out.append(d)
    return out


def list_tags(include_hidden=False):
    join = "" if include_hidden else "JOIN videos v ON v.url = vt.url"
    where = "" if include_hidden else "WHERE " + _VISIBILITY_WHERE
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT t.name AS name, COUNT(vt.url) AS cnt
            FROM tags t
            JOIN video_tags vt ON vt.tag_id = t.id
            {join}
            {where}
            GROUP BY t.id
            ORDER BY cnt DESC, LOWER(t.name)
        """).fetchall()
    return [dict(r) for r in rows]


def list_categories(include_hidden=False):
    join = "" if include_hidden else "JOIN videos v ON v.url = vc.url"
    where = "" if include_hidden else "WHERE " + _VISIBILITY_WHERE
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT c.name AS name, COUNT(vc.url) AS cnt
            FROM categories c
            JOIN video_categories vc ON vc.category_id = c.id
            {join}
            {where}
            GROUP BY c.id
            ORDER BY cnt DESC, LOWER(c.name)
        """).fetchall()
    return [dict(r) for r in rows]


def get_url_by_hash(dir_hash):
    """Reverse lookup: dir_hash → url from the videos table."""
    if not dir_hash: return None
    with _connect() as conn:
        row = conn.execute('SELECT url FROM videos WHERE dir_hash = ?', (dir_hash,)).fetchone()
    return row['url'] if row else None


def hide_video(url):
    with _connect() as conn:
        conn.execute("UPDATE videos SET hidden = 1 WHERE url = ?", (url,))


def unhide_video(url):
    with _connect() as conn:
        conn.execute("UPDATE videos SET hidden = 0 WHERE url = ?", (url,))


def hide_site(name):
    if not name: return
    with _connect() as conn:
        conn.execute("INSERT OR IGNORE INTO hidden_sites (name) VALUES (?)", (name,))


def unhide_site(name):
    if not name: return
    with _connect() as conn:
        conn.execute("DELETE FROM hidden_sites WHERE name = ?", (name,))


def list_hidden_sites():
    with _connect() as conn:
        rows = conn.execute("SELECT name FROM hidden_sites ORDER BY LOWER(name)").fetchall()
    return [r['name'] for r in rows]


def delete_video(url):
    with _connect() as conn:
        conn.execute("DELETE FROM videos WHERE url = ?", (url,))
        _prune_orphans(conn)


def _has_media(vid_dir):
    """True if the dir contains genuinely cached content that would play offline.

    Counts:
      - Local video/audio files (video-*.mp4, audio.mp3, low.mp4, ...)
      - HLS m3u8 that has at least one locally-generated segment on disk

    Does NOT count:
      - direct-* manifests (those are pointers to the source CDN, not caches)
      - hls-*.m3u8 with no segments yet (transcode not done)
    """
    try:
        files = os.listdir(vid_dir)
    except OSError:
        return False
    # Downloaded video / audio files
    for f in files:
        if f.endswith(('.temp', '.part', '.ytdl')):
            continue
        if (f.startswith('video-') or f == 'video.mp4'
                or f.startswith('audio.') or f.startswith('audio-')
                or f.startswith('low.')):
            if os.path.splitext(f)[1].lower() in MEDIA_EXTS:
                return True
    # HLS with locally-generated segments
    for f in files:
        if not (f.startswith('hls_segment-') and os.path.isdir(os.path.join(vid_dir, f))):
            continue
        seg_dir = os.path.join(vid_dir, f)
        try:
            for seg_f in os.listdir(seg_dir):
                if seg_f.endswith('.ts'):
                    return True
        except OSError:
            pass
    return False


def _thumb_kind(vid_dir):
    """0 = no thumb, 1 = still image, 2 = still plus an animated preview clip.
    Stays truthy for every "has a thumb" check that predates the clip."""
    if os.path.exists(os.path.join(vid_dir, 'thumb.mp4')):
        return 2
    if (os.path.exists(os.path.join(vid_dir, 'thumb.jpg'))
            or os.path.exists(os.path.join(vid_dir, 'sprite.jpg'))):
        return 1
    return 0


def _row_from_meta(url, name, vid_dir, meta):
    return (
        url,
        name,
        meta.get('title') or url,
        meta.get('uploader') or '',
        source_site(url),
        int(meta.get('duration') or 0),
        meta.get('width'),
        meta.get('height'),
        meta.get('upload_date') or '',
        _thumb_kind(vid_dir),
        int(os.path.getmtime(os.path.join(vid_dir, 'meta.json'))),
    )


def _upsert_with_meta(conn, row, tag_names, cat_names):
    url = row[0]
    conn.execute("""
        INSERT INTO videos (url, dir_hash, title, uploader, source_site, duration, width, height, upload_date, has_thumb, saved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            dir_hash=excluded.dir_hash,
            title=excluded.title,
            uploader=excluded.uploader,
            source_site=excluded.source_site,
            duration=excluded.duration,
            width=excluded.width,
            height=excluded.height,
            upload_date=excluded.upload_date,
            has_thumb=excluded.has_thumb,
            saved_at=excluded.saved_at
    """, row)
    _sync_associations(conn, url, tag_names, cat_names)


def sync_url(url):
    """Upsert a URL's library row if it has cached media. NEVER deletes —
    stale rows are cleaned up by rebuild() or explicit delete_video(url)."""
    if not url: return None
    name = dir_hash(url)
    vid_dir = data_dir_for(url)
    meta_path = os.path.join(vid_dir, 'meta.json')
    if not os.path.isdir(vid_dir): return None
    if not os.path.exists(meta_path): return None
    if not _has_media(vid_dir): return None
    try:
        with open(meta_path, 'r') as fh:
            meta = json.load(fh)
    except Exception:
        return None
    if not (meta.get('original_url') or url): return None
    tags = _clean_labels(meta.get('tags'))
    cats = _clean_labels(meta.get('categories'))
    with _connect() as conn:
        _upsert_with_meta(conn, _row_from_meta(url, name, vid_dir, meta), tags, cats)
        _prune_orphans(conn)
    return url


def rebuild():
    entries = []
    if not os.path.isdir(CACHE_ROOT):
        os.makedirs(CACHE_ROOT, exist_ok=True)
    try:
        sites = os.listdir(CACHE_ROOT)
    except OSError:
        sites = []
    for site in sites:
        site_dir = os.path.join(CACHE_ROOT, site)
        if not os.path.isdir(site_dir): continue
        for name in os.listdir(site_dir):
            vid_dir = os.path.join(site_dir, name)
            if not os.path.isdir(vid_dir): continue
            if not _HASH_RE.match(name): continue
            meta_path = os.path.join(vid_dir, 'meta.json')
            if not os.path.exists(meta_path): continue
            if not _has_media(vid_dir): continue
            try:
                with open(meta_path, 'r') as fh:
                    meta = json.load(fh)
            except Exception:
                continue
            url = meta.get('original_url') or ''
            if not url: continue
            entries.append((
                _row_from_meta(url, name, vid_dir, meta),
                _clean_labels(meta.get('tags')),
                _clean_labels(meta.get('categories')),
            ))
    with _connect() as conn:
        hidden_urls = {r['url'] for r in conn.execute("SELECT url FROM videos WHERE hidden = 1").fetchall()}
        conn.execute("DELETE FROM video_tags")
        conn.execute("DELETE FROM video_categories")
        conn.execute("DELETE FROM tags")
        conn.execute("DELETE FROM categories")
        conn.execute("DELETE FROM videos")
        for row, tags, cats in entries:
            _upsert_with_meta(conn, row, tags, cats)
        if hidden_urls:
            conn.executemany("UPDATE videos SET hidden = 1 WHERE url = ?", [(u,) for u in hidden_urls])
    return len(entries)
