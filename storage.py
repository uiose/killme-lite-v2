import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS state_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    round INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS clone_runs (
                    clone_group_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'failed')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    error TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_turns_session_id_id
                    ON turns(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_state_snapshots_session_id_id
                    ON state_snapshots(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_clone_runs_session_id_started_at
                    ON clone_runs(session_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                    ON sessions(updated_at);
                """
            )

    def create_session(self, session_id: str, title: str) -> None:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(session_id, title, created_at, updated_at, closed_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    updated_at = excluded.updated_at,
                    closed_at = NULL
                """,
                (session_id, title, ts, ts),
            )

    def touch_session(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now_iso(), session_id),
            )

    def latest_session_id(self) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM sessions WHERE closed_at IS NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return row["session_id"] if row else None

    def close_session(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET closed_at = ?, updated_at = ? WHERE session_id = ?",
                (now_iso(), now_iso(), session_id),
            )

    def save_turn(
        self,
        session_id: str,
        round_number: int,
        speaker: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO turns(session_id, round, speaker, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    round_number,
                    speaker,
                    content,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now_iso(), session_id),
            )

    def recent_turns(self, session_id: str, limit: int = 8) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT round, speaker, content, metadata, created_at
                FROM turns
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

        turns = []
        for row in reversed(rows):
            turns.append(
                {
                    "round": row["round"],
                    "speaker": row["speaker"],
                    "content": row["content"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                    "created_at": row["created_at"],
                }
            )
        return turns

    def save_snapshot(self, session_id: str, state: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO state_snapshots(session_id, round, state_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    int(state.get("round", 0)),
                    json.dumps(state, ensure_ascii=False, indent=2),
                    now_iso(),
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now_iso(), session_id),
            )

    def start_clone_run(self, clone_group_id: str, session_id: str, role: str) -> None:
        ts = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO clone_runs(
                    clone_group_id, session_id, role, status, started_at, completed_at, error
                )
                VALUES (?, ?, ?, 'pending', ?, NULL, NULL)
                ON CONFLICT(clone_group_id) DO UPDATE SET
                    status = 'pending',
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    error = NULL
                """,
                (clone_group_id, session_id, role, ts),
            )

    def complete_clone_run(self, clone_group_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE clone_runs
                SET status = 'completed', completed_at = ?, error = NULL
                WHERE clone_group_id = ?
                """,
                (now_iso(), clone_group_id),
            )

    def fail_clone_run(self, clone_group_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE clone_runs
                SET status = 'failed', completed_at = ?, error = ?
                WHERE clone_group_id = ?
                """,
                (now_iso(), error, clone_group_id),
            )

    def clone_runs_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT clone_group_id, session_id, role, status, started_at, completed_at, error
                FROM clone_runs
                WHERE session_id = ?
                ORDER BY started_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def load_latest_state(self, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if session_id is None:
            session_id = self.latest_session_id()
        if not session_id:
            return None

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT state_json FROM state_snapshots
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()

        if not row:
            return None
        return json.loads(row["state_json"])

    def reset_all(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM clone_runs")
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM state_snapshots")
            conn.execute("DELETE FROM sessions")
