#!/usr/bin/env python3
"""
Packs `data/stat_genealogy.db` into GitHub-friendly <=45 MB gzip chunks in `data/db_parts/`.
`app.py` (`ensure_database_ready()`) automatically unpacks these chunks on first startup after `git clone`.
"""

import gzip
import os
import sqlite3
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "stat_genealogy.db")
PARTS_DIR = os.path.join(BASE_DIR, "data", "db_parts")
CHUNK_BYTES = 45 * 1024 * 1024  # 45 MB per chunk (< GitHub 50 MB warning & 100 MB limit)


def main():
    os.makedirs(PARTS_DIR, exist_ok=True)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_path = os.path.join(tmpdir, "snap.db")
        gz_path = os.path.join(tmpdir, "snap.db.gz")

        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(snap_path)
        src.backup(dst)
        dst.close()
        src.close()

        with open(snap_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            while True:
                buf = f_in.read(4 * 1024 * 1024)
                if not buf:
                    break
                f_out.write(buf)

        for fn in os.listdir(PARTS_DIR):
            if fn.startswith("stat_genealogy.db.gz.part"):
                os.remove(os.path.join(PARTS_DIR, fn))

        idx = 0
        with open(gz_path, "rb") as f_gz:
            while True:
                chunk = f_gz.read(CHUNK_BYTES)
                if not chunk:
                    break
                out_p = os.path.join(PARTS_DIR, f"stat_genealogy.db.gz.part{idx:02d}")
                with open(out_p, "wb") as fp:
                    fp.write(chunk)
                print(f"Wrote {os.path.basename(out_p)} ({len(chunk) / (1024 * 1024):.2f} MB)")
                idx += 1

    print(f"Packed {idx} parts into {PARTS_DIR} in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
