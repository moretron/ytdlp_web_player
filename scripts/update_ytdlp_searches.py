#!/usr/bin/env python3
"""Fetch yt-dlp's supportedsites.md and extract every documented search prefix.

Writes the result to src/ytdlp_searches.json. The app loads that file at startup
and uses it to validate incoming /ytsearch queries.

Usage:
    python3 scripts/update_ytdlp_searches.py           # fetch + write
    python3 scripts/update_ytdlp_searches.py --check   # diff vs existing, exit 1 if changed
    python3 scripts/update_ytdlp_searches.py --print   # print prefixes to stdout, don't write
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

DOC_URL = 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/supportedsites.md'
OUT_PATH = Path(__file__).resolve().parent.parent / 'src' / 'ytdlp_searches.json'
PREFIX_RE = re.compile(r'"([a-z][a-z0-9]*):"\s+prefix', re.IGNORECASE)

# Prefixes from our local yt-dlp fork that upstream docs don't list. Merged in
# on every run so a refresh doesn't drop them.
LOCAL_EXTRAS = ['pornhubsearch', 'pornhubcategory', 'phsearch', 'phcategory']


def fetch_prefixes():
    req = urllib.request.Request(DOC_URL, headers={'User-Agent': 'ytdlp-web-player-updater'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode('utf-8')
    upstream = {m.group(1).lower() for m in PREFIX_RE.finditer(text)}
    return sorted(upstream | set(LOCAL_EXTRAS))


def load_existing():
    if not OUT_PATH.exists():
        return []
    try:
        return json.loads(OUT_PATH.read_text()).get('prefixes', [])
    except Exception:
        return []


def main():
    args = set(sys.argv[1:])
    check_only = '--check' in args
    print_only = '--print' in args

    new_list = fetch_prefixes()
    existing = load_existing()

    added = sorted(set(new_list) - set(existing))
    removed = sorted(set(existing) - set(new_list))

    print(f'Fetched {len(new_list)} search prefixes from yt-dlp docs.')
    if added:
        print(f'  NEW ({len(added)}):     {", ".join(added)}')
    if removed:
        print(f'  GONE ({len(removed)}):    {", ".join(removed)}')
    if not added and not removed:
        print('  No changes vs on-disk list.')

    if print_only:
        for p in new_list:
            print(p)
        return 0

    if check_only:
        return 1 if (added or removed) else 0

    payload = {
        'source': DOC_URL,
        'count': len(new_list),
        'prefixes': new_list,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + '\n')
    print(f'Wrote {OUT_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
