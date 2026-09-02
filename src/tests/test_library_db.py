"""Tests for library history semantics (saved_at / last_watched_at).

Run inside the container, where the app's imports are already satisfied:

    docker compose exec -T ytdlp_web_player python3 /app/tests/test_library_db.py

The bug these cover: the library sort key used to be meta.json's mtime, which
meta revalidation and direct-manifest self-healing rewrite — so entries
shuffled on /watch visits while genuinely re-watched local videos never
bubbled up. Now saved_at freezes at first insert and playback endpoints bump
last_watched_at explicitly.
"""

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import library_db  # noqa: E402


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class _TempLibrary:
    """Point library_db at a scratch DB + cache root for one test."""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix='ytdlp-libtest-')
        self._db, self._cache = library_db.DB_PATH, library_db.CACHE_ROOT
        library_db.DB_PATH = os.path.join(self.path, 'library.db')
        library_db.CACHE_ROOT = os.path.join(self.path, 'cache')
        os.makedirs(library_db.CACHE_ROOT)
        with library_db._connect() as conn:
            conn.executescript("""
                CREATE TABLE videos (
                    url TEXT PRIMARY KEY, dir_hash TEXT NOT NULL, title TEXT,
                    uploader TEXT, source_site TEXT, duration INTEGER,
                    width INTEGER, height INTEGER, upload_date TEXT,
                    has_thumb INTEGER, saved_at INTEGER,
                    hidden INTEGER NOT NULL DEFAULT 0, last_watched_at INTEGER);
                CREATE TABLE hidden_sites (name TEXT PRIMARY KEY);
                CREATE TABLE tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
                CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
                CREATE TABLE video_tags (url TEXT NOT NULL, tag_id INTEGER NOT NULL,
                    PRIMARY KEY (url, tag_id));
                CREATE TABLE video_categories (url TEXT NOT NULL, category_id INTEGER NOT NULL,
                    PRIMARY KEY (url, category_id));
            """)
        return self

    def __exit__(self, *exc):
        library_db.DB_PATH, library_db.CACHE_ROOT = self._db, self._cache
        shutil.rmtree(self.path, ignore_errors=True)

    def add_cache_dir(self, url, title, mtime=None):
        """Create a cache dir that _has_media() accepts (direct playback)."""
        d = os.path.join(library_db.CACHE_ROOT, library_db.source_site(url), library_db.dir_hash(url))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'meta.json'), 'w') as f:
            json.dump({'original_url': url, 'title': title, 'duration': 10}, f)
        with open(os.path.join(d, 'direct-720.url'), 'w') as f:
            f.write('http://cdn/x\n\n')
        if mtime is not None:
            os.utime(os.path.join(d, 'meta.json'), (mtime, mtime))
        return d


def _row(url, saved_at):
    return (url, library_db.dir_hash(url), 't-' + url, '', library_db.source_site(url),
            10, None, None, '', 0, saved_at)


@case
def saved_at_is_frozen_on_upsert():
    with _TempLibrary():
        with library_db._connect() as conn:
            library_db._upsert_with_meta(conn, _row('http://a.com/1', 100), [], [])
            library_db._upsert_with_meta(conn, _row('http://a.com/1', 999), [], [])
            saved = conn.execute("SELECT saved_at FROM videos").fetchone()['saved_at']
        assert saved == 100, saved


@case
def mark_watched_bumps_and_debounces():
    with _TempLibrary():
        url = 'http://a.com/1'
        with library_db._connect() as conn:
            library_db._upsert_with_meta(conn, _row(url, 100), [], [])
        assert library_db.mark_watched(url) is True
        # Inside the debounce window: row exists, no second bump.
        assert library_db.mark_watched(url) is False
        # Debounce elapsed (simulated with debounce=0): bumps again.
        assert library_db.mark_watched(url, debounce=0) is True
        with library_db._connect() as conn:
            lw = conn.execute("SELECT last_watched_at FROM videos").fetchone()['last_watched_at']
        assert abs(lw - time.time()) < 5, lw


@case
def mark_watched_syncs_missing_row_from_disk():
    with _TempLibrary() as lib:
        url = 'http://a.com/fresh'
        lib.add_cache_dir(url, 'fresh video')
        assert library_db.mark_watched(url) is True
        vids = library_db.list_videos()
        assert [v['url'] for v in vids] == [url], vids
        assert vids[0]['last_watched_at'] is not None


@case
def listing_orders_by_watch_time_then_saved():
    with _TempLibrary():
        with library_db._connect() as conn:
            library_db._upsert_with_meta(conn, _row('http://a.com/old-watched', 100), [], [])
            library_db._upsert_with_meta(conn, _row('http://a.com/newer-saved', 200), [], [])
            conn.execute("UPDATE videos SET last_watched_at = 300 WHERE url = 'http://a.com/old-watched'")
        urls = [v['url'] for v in library_db.list_videos()]
        assert urls == ['http://a.com/old-watched', 'http://a.com/newer-saved'], urls


@case
def rebuild_preserves_history_and_hidden():
    with _TempLibrary() as lib:
        url = 'http://a.com/keep'
        lib.add_cache_dir(url, 'keeper', mtime=5000)
        with library_db._connect() as conn:
            library_db._upsert_with_meta(conn, _row(url, 111), [], [])
            conn.execute("UPDATE videos SET last_watched_at = 222, hidden = 1 WHERE url = ?", (url,))
        count = library_db.rebuild()
        assert count == 1, count
        with library_db._connect() as conn:
            r = conn.execute("SELECT saved_at, last_watched_at, hidden FROM videos").fetchone()
        # Fresh insert would have derived saved_at=5000 from meta mtime.
        assert (r['saved_at'], r['last_watched_at'], r['hidden']) == (111, 222, 1), dict(r)


@case
def rebuild_picks_up_direct_only_dirs():
    with _TempLibrary() as lib:
        lib.add_cache_dir('http://a.com/direct-only', 'watched via direct')
        assert library_db.rebuild() == 1
        vids = library_db.list_videos()
        assert vids and vids[0]['title'] == 'watched via direct', vids


def main():
    failures = 0
    for fn in CASES:
        name = fn.__name__.replace('_', ' ')
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f'FAIL  {name}\n      {e}')
        except Exception as e:
            failures += 1
            print(f'ERROR {name}\n      {type(e).__name__}: {e}')
        else:
            print(f'ok    {name}')
    print(f'\n{len(CASES) - failures}/{len(CASES)} passed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
