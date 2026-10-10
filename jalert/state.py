"""SQLite bookkeeping: dedupe across runs and keep a cumulative reading log."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime
from pathlib import Path
from .identity import doi_key, fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    uid           TEXT PRIMARY KEY,
    doi           TEXT,
    title         TEXT NOT NULL,
    journal       TEXT,
    issn          TEXT,
    url           TEXT,
    pub_date      TEXT,
    score         INTEGER,
    tier          TEXT,
    source        TEXT,
    keywords      TEXT,
    first_seen    TEXT
);
CREATE INDEX IF NOT EXISTS idx_articles_first_seen ON articles(first_seen);
CREATE INDEX IF NOT EXISTS idx_articles_pub_date   ON articles(pub_date);
CREATE INDEX IF NOT EXISTS idx_articles_doi ON articles(lower(doi));

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started       TEXT,
    finished      TEXT,
    fetched       INTEGER,
    matched       INTEGER,
    new_items     INTEGER,
    pushed        INTEGER,
    report_path   TEXT,
    note          TEXT
);
CREATE TABLE IF NOT EXISTS observations (
    uid TEXT PRIMARY KEY, fingerprint TEXT, doi TEXT, first_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_fingerprint ON observations(fingerprint);
CREATE TABLE IF NOT EXISTS conference_scans (
    venue TEXT, source TEXT, completed TEXT, PRIMARY KEY(venue, source)
);
CREATE TABLE IF NOT EXISTS metadata_cache (
    url TEXT PRIMARY KEY, fetched TEXT, payload TEXT
);
"""


class Store:
    def __init__(self, path: str | Path, readonly: bool = False):
        self.path = str(path)
        if readonly:
            self.conn = sqlite3.connect(":memory:")
            if Path(self.path).is_file():
                original = sqlite3.connect(Path(self.path).resolve().as_uri() + "?mode=ro", uri=True)
                original.backup(self.conn)
                original.close()
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def known_uids(self, uids: list[str]) -> set[str]:
        if not uids:
            return set()
        found: set[str] = set()
        chunk = 400
        for start in range(0, len(uids), chunk):
            batch = uids[start : start + chunk]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT uid FROM articles WHERE uid IN ({placeholders})", batch
            ).fetchall()
            found.update(row["uid"] for row in rows)
        return found

    def scanned(self, venue, source):
        return self.conn.execute("SELECT 1 FROM conference_scans WHERE venue=? AND source=?", (venue, source)).fetchone() is not None

    def complete_scans(self, statuses):
        for status in statuses:
            if status["ok"] and status["items"]:
                self.conn.execute("INSERT OR REPLACE INTO conference_scans VALUES (?, ?, ?)",
                    (status["journal"], status["source"], datetime.now().isoformat()))
        self.conn.commit()

    def cached(self, url, hours):
        row = self.conn.execute("SELECT payload FROM metadata_cache WHERE url=? AND fetched>=?",
            (url, datetime.fromtimestamp(datetime.now().timestamp() - hours * 3600).isoformat())).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, url, payload):
        self.conn.execute("INSERT OR REPLACE INTO metadata_cache VALUES (?, ?, ?)",
            (url, datetime.now().isoformat(), json.dumps(payload)))
        self.conn.execute("DELETE FROM metadata_cache WHERE datetime(fetched) < datetime('now', '-7 days')")
        self.conn.commit()

    def observe(self, items):
        known = set()
        for item in items:
            doi, fp = doi_key(item.doi), fingerprint(item)
            previous = self.conn.execute("SELECT uid, doi, first_seen FROM observations WHERE uid=? OR (fingerprint<>'' AND fingerprint=?)",
                (item.uid, fp)).fetchall()
            compatible = [r for r in previous if not (doi and r['doi'] and doi != r['doi'])]
            aliases = [item.uid] + [r['uid'] for r in compatible]
            article = self.conn.execute("SELECT first_seen FROM articles WHERE uid=? OR (doi<>'' AND lower(doi)=?) LIMIT 1", (item.uid, doi)).fetchone()
            if self.known_uids(aliases) or article:
                known.add(item.uid)
            times = [r['first_seen'] for r in compatible]
            if article and article['first_seen']:
                times.append(article['first_seen'])
            item.first_seen = min(times, default=datetime.now().isoformat(timespec="seconds"))
            self.conn.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
                (item.uid, fp, doi, item.first_seen))
        self.conn.commit()
        return known

    def save(self, entries: list[tuple[object, object]]) -> int:
        """Persist ``(item, scored)`` pairs; returns the number of newly inserted rows."""
        now = datetime.now().isoformat(timespec="seconds")
        inserted = 0
        for item, scored in entries:
            cursor = self.conn.execute(
                """
                INSERT OR IGNORE INTO articles
                    (uid, doi, title, journal, issn, url, pub_date, score, tier, source, keywords, first_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.uid,
                    item.doi,
                    item.title,
                    item.journal,
                    item.issn,
                    item.url,
                    item.date,
                    scored.score,
                    getattr(scored, "tier", ""),
                    item.source,
                    " / ".join(scored.labels),
                    getattr(item, "first_seen", "") or now,
                ),
            )
            inserted += cursor.rowcount
        self.conn.commit()
        return inserted

    def record_run(
        self,
        *,
        started: str,
        fetched: int,
        matched: int,
        new_items: int,
        pushed: int,
        report_path: str,
        note: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO runs (started, finished, fetched, matched, new_items, pushed, report_path, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started,
                datetime.now().isoformat(timespec="seconds"),
                fetched,
                matched,
                new_items,
                pushed,
                report_path,
                note[:500],
            ),
        )
        self.conn.commit()

    def recent_counts(self, days: int = 30) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) AS total FROM articles WHERE first_seen >= datetime('now', ?)",
            (f"-{int(days)} days",),
        ).fetchone()
        return {"articles_total": self.conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
                "articles_last_%dd" % days: row["total"]}


def last_run_started(db_path: str | Path) -> str | None:
    """Timestamp of the most recent run, or ``None`` if there is no history yet.

    Used to widen the lookback window after the machine has been switched off
    for a while, so papers published during the gap are not missed forever.
    """
    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT started FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return row[0] if row and row[0] else None
    finally:
        conn.close()
