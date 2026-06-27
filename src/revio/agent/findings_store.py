"""Persistent findings history — cross-session memory of past findings.

Stored alongside the LangGraph checkpoint SQLite (~/.cache/revio/<hash>.sqlite)
in a separate table `findings_history`. Each finding gets:
- A stable fingerprint (file_path + line + title hash) so we can match the
  same finding across runs even if its description text changes.
- A snapshot of the underlying source-line content at detection time, so we
  can heuristically tell when the user has actually fixed something vs
  changed location.

Comparison across runs gives us three buckets:
  - **still_present** — same fingerprint AND same source-line content
  - **maybe_fixed**   — same fingerprint, different source-line content
  - **new**           — fingerprint not seen before

This isn't bulletproof (renames + line shifts will confuse it) but it's
useful enough for "what's new since last review" displays.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..output.models import Finding


logger = logging.getLogger(__name__)


# --- Schema ------------------------------------------------------------------


_DDL = """
CREATE TABLE IF NOT EXISTS findings_history (
    fingerprint        TEXT PRIMARY KEY,
    file_path          TEXT NOT NULL,
    line_start         INTEGER NOT NULL,
    line_end           INTEGER,
    title              TEXT NOT NULL,
    severity           TEXT NOT NULL,
    category           TEXT NOT NULL,
    confidence         REAL NOT NULL,
    line_content_hash  TEXT,
    first_seen         REAL NOT NULL,
    last_seen          REAL NOT NULL,
    run_count          INTEGER NOT NULL DEFAULT 1,
    finding_json       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_history_file
    ON findings_history(file_path);
"""


# --- Public dataclasses ------------------------------------------------------


@dataclass
class FindingRecord:
    fingerprint: str
    file_path: str
    line_start: int
    title: str
    severity: str
    category: str
    first_seen: float
    last_seen: float
    run_count: int
    line_content_hash: str | None


@dataclass
class FindingComparison:
    """Classification of a current-run finding vs history."""

    finding: Finding
    status: str       # "still_present" | "maybe_fixed" | "new"
    prior: FindingRecord | None = None


# --- Store -------------------------------------------------------------------


class FindingsStore:
    """SQLite-backed persistent findings store, one DB per repo."""

    def __init__(self, db_path: Path | str, *, max_rows: int | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Count-based retention cap; None = unbounded (callers that care pass it).
        self.max_rows = max_rows
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_DDL)

    # ---- Write side ----

    def record_run(
        self,
        findings: list[Finding],
        *,
        repo_root: Path | str | None = None,
    ) -> dict[str, int]:
        """Record this run's findings; return stats {new, updated}.

        Computes line_content_hash from the actual repo file if available
        so the next run can detect "code was edited at that location".
        """
        now = time.time()
        repo_root_path = Path(repo_root).expanduser().resolve() if repo_root else None

        stats = {"new": 0, "updated": 0}
        with self._conn() as c:
            for f in findings:
                fp = _fingerprint(f)
                line_hash = (
                    _line_content_hash(repo_root_path, f.file_path, f.line_start)
                    if repo_root_path else None
                )
                existing = c.execute(
                    "SELECT first_seen, run_count FROM findings_history WHERE fingerprint = ?",
                    (fp,),
                ).fetchone()

                payload = f.model_dump_json()

                if existing:
                    first_seen, run_count = existing
                    c.execute(
                        """
                        UPDATE findings_history SET
                            last_seen = ?,
                            run_count = ?,
                            line_content_hash = COALESCE(?, line_content_hash),
                            finding_json = ?
                        WHERE fingerprint = ?
                        """,
                        (now, run_count + 1, line_hash, payload, fp),
                    )
                    stats["updated"] += 1
                else:
                    c.execute(
                        """
                        INSERT INTO findings_history (
                            fingerprint, file_path, line_start, line_end, title,
                            severity, category, confidence, line_content_hash,
                            first_seen, last_seen, run_count, finding_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            fp,
                            f.file_path,
                            f.line_start,
                            f.line_end,
                            f.title,
                            f.severity.value,
                            f.category.value,
                            f.confidence,
                            line_hash,
                            now,
                            now,
                            1,
                            payload,
                        ),
                    )
                    stats["new"] += 1

            # Count-based retention: keep the newest `max_rows` by last_seen,
            # delete the oldest beyond the cap (same connection, cheap).
            if self.max_rows is not None:
                c.execute(
                    """
                    DELETE FROM findings_history
                    WHERE fingerprint NOT IN (
                        SELECT fingerprint FROM findings_history
                        ORDER BY last_seen DESC
                        LIMIT ?
                    )
                    """,
                    (self.max_rows,),
                )
        return stats

    # ---- Read side ----

    def list_all(self) -> list[FindingRecord]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT fingerprint, file_path, line_start, title, severity,
                       category, first_seen, last_seen, run_count, line_content_hash
                FROM findings_history
                ORDER BY last_seen DESC
                """
            ).fetchall()
        return [
            FindingRecord(
                fingerprint=r[0], file_path=r[1], line_start=r[2], title=r[3],
                severity=r[4], category=r[5], first_seen=r[6], last_seen=r[7],
                run_count=r[8], line_content_hash=r[9],
            )
            for r in rows
        ]

    def get_by_fingerprint(self, fp: str) -> FindingRecord | None:
        with self._conn() as c:
            row = c.execute(
                """
                SELECT fingerprint, file_path, line_start, title, severity,
                       category, first_seen, last_seen, run_count, line_content_hash
                FROM findings_history WHERE fingerprint = ?
                """,
                (fp,),
            ).fetchone()
        if row is None:
            return None
        return FindingRecord(
            fingerprint=row[0], file_path=row[1], line_start=row[2], title=row[3],
            severity=row[4], category=row[5], first_seen=row[6], last_seen=row[7],
            run_count=row[8], line_content_hash=row[9],
        )

    def compare(
        self,
        current_findings: list[Finding],
        *,
        repo_root: Path | str | None = None,
    ) -> list[FindingComparison]:
        """Classify current findings against history."""
        repo_root_path = Path(repo_root).expanduser().resolve() if repo_root else None
        out: list[FindingComparison] = []
        for f in current_findings:
            fp = _fingerprint(f)
            prior = self.get_by_fingerprint(fp)
            if prior is None:
                out.append(FindingComparison(finding=f, status="new"))
                continue
            # Same fingerprint exists. Did the underlying line change?
            current_hash = (
                _line_content_hash(repo_root_path, f.file_path, f.line_start)
                if repo_root_path else None
            )
            if (
                prior.line_content_hash is not None
                and current_hash is not None
                and prior.line_content_hash != current_hash
            ):
                out.append(FindingComparison(finding=f, status="maybe_fixed", prior=prior))
            else:
                out.append(FindingComparison(finding=f, status="still_present", prior=prior))
        return out

    def clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM findings_history")

    def count(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT COUNT(*) FROM findings_history").fetchone()
        return row[0] if row else 0


# --- Helpers -----------------------------------------------------------------


def _fingerprint(f: Finding) -> str:
    """Stable identity hash for a Finding across runs.

    Uses file_path + line_start + a NORMALIZED title (lowercased + collapsed).
    Skipping description because LLMs vary wording slightly each run.
    """
    norm_title = " ".join(f.title.lower().split())
    key = f"{f.file_path}|{f.line_start}|{norm_title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _line_content_hash(repo_root: Path | None, file_path: str, line: int) -> str | None:
    """Hash the source line at (file_path, line) so we can detect edits.

    Returns None if the file or line can't be read.
    """
    if repo_root is None:
        return None
    try:
        path = (repo_root / file_path).resolve()
        # Stay inside repo root
        try:
            path.relative_to(repo_root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for i, src_line in enumerate(fh, start=1):
                if i == line:
                    return hashlib.sha1(src_line.strip().encode()).hexdigest()[:12]
        return None
    except Exception:
        return None
