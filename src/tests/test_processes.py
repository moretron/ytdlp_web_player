"""Regression tests for the PID tracker.

Run inside the container, where the app's imports are already satisfied:

    docker compose exec -T ytdlp_web_player python3 /app/tests/test_processes.py

The bug these cover: `Processes.get()` used to read *every* non-directory file
in the data root and `json.load` it. Upstream's data root holds nothing but PID
files, but this fork also keeps library.db, app.log, .env and cookies.txt there
-- and library.db is binary, so the read raised UnicodeDecodeError. That
propagated out of `preload()` and every `/watch` request returned a 500.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import addons  # noqa: E402


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


class _TempDataPath:
    """Point addons.data_path at a scratch directory for one test."""

    def __enter__(self):
        self._original = addons.data_path
        self.path = tempfile.mkdtemp(prefix='ytdlp-test-')
        addons.data_path = self.path
        return self.path

    def __exit__(self, *exc):
        addons.data_path = self._original
        shutil.rmtree(self.path, ignore_errors=True)


def _write(path, name, content, binary=False):
    mode = 'wb' if binary else 'w'
    with open(os.path.join(path, name), mode) as f:
        f.write(content)


@case
def binary_library_db_does_not_break_the_scan():
    with _TempDataPath() as data:
        # A real SQLite header, which is exactly what tripped the UTF-8 decode.
        _write(data, 'library.db', b'SQLite format 3\x00\x95\x01\x02binary', binary=True)
        _write(data, '4242', json.dumps(['http://example.com/v', 'FFMPEG abc123', 1785736153.6]))

        procs = addons.Processes.get()

        assert list(procs) == ['4242'], procs
        assert procs['4242'][1] == 'FFMPEG abc123', procs


@case
def non_pid_files_are_ignored():
    with _TempDataPath() as data:
        _write(data, 'app.log', 'plain text, not json\n')
        _write(data, '.env', 'API_KEY=secret\n')
        _write(data, 'cookies.txt', '# Netscape HTTP Cookie File\n')
        _write(data, 'library.db', b'\x00\x01\x02', binary=True)

        assert addons.Processes.get() == {}


@case
def corrupt_pid_file_is_skipped_not_fatal():
    with _TempDataPath() as data:
        _write(data, '7', '{ this is not valid json')
        _write(data, '8', json.dumps(['http://example.com/v', 'FFMPEG def456', 1.0]))

        procs = addons.Processes.get()

        # The good one still counts; the broken one just doesn't.
        assert list(procs) == ['8'], procs


@case
def directories_are_not_treated_as_pids():
    with _TempDataPath() as data:
        os.makedirs(os.path.join(data, '1234'))
        os.makedirs(os.path.join(data, 'cache'))

        assert addons.Processes.get() == {}


@case
def valid_pid_files_are_all_returned():
    with _TempDataPath() as data:
        for pid in ('1', '22', '333'):
            _write(data, pid, json.dumps([f'http://example.com/{pid}', f'FFMPEG {pid}', float(pid)]))

        procs = addons.Processes.get()

        assert sorted(procs) == ['1', '22', '333'], procs


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
