from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    duration_ms INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    image_name TEXT NOT NULL,
    vision_status TEXT NOT NULL DEFAULT 'pending',
    vision_model TEXT,
    vision_analyzed_at TEXT,
    vision_result_json TEXT,
    vision_raw_text TEXT,
    vision_error TEXT,
    vision_duration_ms INTEGER,
    vision_input_tokens INTEGER,
    vision_output_tokens INTEGER,
    vision_total_tokens INTEGER,
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
    UNIQUE (video_id, timestamp_ms)
);

CREATE INDEX IF NOT EXISTS idx_frames_video_time
ON frames(video_id, timestamp_ms);
"""

FRAME_VISION_COLUMNS = {
    "vision_status": "TEXT NOT NULL DEFAULT 'pending'",
    "vision_model": "TEXT",
    "vision_analyzed_at": "TEXT",
    "vision_result_json": "TEXT",
    "vision_raw_text": "TEXT",
    "vision_error": "TEXT",
    "vision_duration_ms": "INTEGER",
    "vision_input_tokens": "INTEGER",
    "vision_output_tokens": "INTEGER",
    "vision_total_tokens": "INTEGER",
}

FTS5_SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE frame_search USING fts5(
    frame_id UNINDEXED,
    video_id UNINDEXED,
    video_name,
    summary,
    subjects,
    actions,
    scene,
    shot_type,
    ocr_text,
    time_text,
    search_text,
    tokenize = 'unicode61'
)
"""

FALLBACK_SEARCH_SCHEMA = """
CREATE TABLE frame_search (
    frame_id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    video_name TEXT NOT NULL,
    summary TEXT NOT NULL,
    subjects TEXT NOT NULL,
    actions TEXT NOT NULL,
    scene TEXT NOT NULL,
    shot_type TEXT NOT NULL,
    ocr_text TEXT NOT NULL,
    time_text TEXT NOT NULL,
    search_text TEXT NOT NULL
)
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(frames)").fetchall()
            }
            for column_name, definition in FRAME_VISION_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE frames ADD COLUMN {column_name} {definition}"
                    )
            self._initialize_search_table(connection)
            self._backfill_search_index(connection)

    def _initialize_search_table(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'frame_search'"
        ).fetchone()
        if existing is not None:
            return
        try:
            connection.execute(FTS5_SEARCH_SCHEMA)
        except sqlite3.OperationalError:
            connection.execute(FALLBACK_SEARCH_SCHEMA)

    def search_uses_fts5(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'frame_search'"
            ).fetchone()
        return bool(row and "VIRTUAL TABLE" in row["sql"].upper())

    @staticmethod
    def _search_uses_fts5_connection(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'frame_search'"
        ).fetchone()
        return bool(row and "VIRTUAL TABLE" in row["sql"].upper())

    def insert_video(
        self,
        *,
        video_id: str,
        original_name: str,
        stored_name: str,
        duration_ms: int,
        uploaded_at: str,
        frames: list[tuple[int, str]],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO videos (id, original_name, stored_name, duration_ms, uploaded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (video_id, original_name, stored_name, duration_ms, uploaded_at),
            )
            connection.executemany(
                """
                INSERT INTO frames (video_id, timestamp_ms, image_name)
                VALUES (?, ?, ?)
                """,
                [(video_id, timestamp_ms, image_name) for timestamp_ms, image_name in frames],
            )

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            video = connection.execute(
                "SELECT * FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if video is None:
                return None
            frames = connection.execute(
                """
                SELECT *
                FROM frames
                WHERE video_id = ?
                ORDER BY timestamp_ms
                """,
                (video_id,),
            ).fetchall()
        return {"video": dict(video), "frames": [dict(frame) for frame in frames]}

    def get_latest_video(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM videos ORDER BY uploaded_at DESC LIMIT 1"
            ).fetchone()
        return self.get_video(row["id"]) if row else None

    def reset_vision(self, video_id: str, *, force: bool) -> bool:
        with self.connect() as connection:
            video_exists = connection.execute(
                "SELECT 1 FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if not video_exists:
                return False

            if force:
                try:
                    connection.execute(
                        "DELETE FROM frame_search WHERE video_id = ?", (video_id,)
                    )
                except sqlite3.Error:
                    pass
                connection.execute(
                    """
                    UPDATE frames
                    SET vision_status = 'pending',
                        vision_model = NULL,
                        vision_analyzed_at = NULL,
                        vision_result_json = NULL,
                        vision_raw_text = NULL,
                        vision_error = NULL,
                        vision_duration_ms = NULL,
                        vision_input_tokens = NULL,
                        vision_output_tokens = NULL,
                        vision_total_tokens = NULL
                    WHERE video_id = ?
                    """,
                    (video_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE frames
                    SET vision_status = 'pending', vision_error = NULL
                    WHERE video_id = ? AND vision_status = 'failed'
                    """,
                    (video_id,),
                )
        return True

    def claim_next_vision_frame(self, video_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            frame = connection.execute(
                """
                SELECT * FROM frames
                WHERE video_id = ? AND vision_status = 'pending'
                ORDER BY timestamp_ms
                LIMIT 1
                """,
                (video_id,),
            ).fetchone()
            if frame is None:
                return None
            connection.execute(
                "UPDATE frames SET vision_status = 'processing' WHERE id = ?",
                (frame["id"],),
            )
        claimed = dict(frame)
        claimed["vision_status"] = "processing"
        return claimed

    def save_vision_success(
        self,
        *,
        frame_id: int,
        model: str,
        analyzed_at: str,
        result_json: str,
        raw_text: str,
        duration_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE frames
                SET vision_status = 'success',
                    vision_model = ?,
                    vision_analyzed_at = ?,
                    vision_result_json = ?,
                    vision_raw_text = ?,
                    vision_error = NULL,
                    vision_duration_ms = ?,
                    vision_input_tokens = ?,
                    vision_output_tokens = ?,
                    vision_total_tokens = ?
                WHERE id = ?
                """,
                (
                    model,
                    analyzed_at,
                    result_json,
                    raw_text,
                    duration_ms,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    frame_id,
                ),
            )
            try:
                self._upsert_search_frame(connection, frame_id)
            except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError):
                pass

    def save_vision_failure(
        self,
        *,
        frame_id: int,
        model: str,
        analyzed_at: str,
        error: str,
        duration_ms: int,
        raw_text: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE frames
                SET vision_status = 'failed',
                    vision_model = ?,
                    vision_analyzed_at = ?,
                    vision_raw_text = ?,
                    vision_error = ?,
                    vision_duration_ms = ?
                WHERE id = ?
                """,
                (model, analyzed_at, raw_text, error[:1000], duration_ms, frame_id),
            )

    def _backfill_search_index(self, connection: sqlite3.Connection) -> None:
        frame_ids = connection.execute(
            "SELECT id FROM frames WHERE vision_status = 'success'"
        ).fetchall()
        for row in frame_ids:
            try:
                self._upsert_search_frame(connection, row["id"])
            except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError):
                continue

    def _upsert_search_frame(
        self, connection: sqlite3.Connection, frame_id: int
    ) -> None:
        row = connection.execute(
            """
            SELECT f.id AS frame_id, f.video_id, f.timestamp_ms,
                   f.vision_status, f.vision_result_json,
                   v.original_name AS video_name
            FROM frames f
            JOIN videos v ON v.id = f.video_id
            WHERE f.id = ?
            """,
            (frame_id,),
        ).fetchone()
        if row is None or row["vision_status"] != "success":
            connection.execute(
                "DELETE FROM frame_search WHERE frame_id = ?", (frame_id,)
            )
            return

        result = json.loads(row["vision_result_json"])

        def joined(field: str) -> str:
            value = result.get(field, [])
            if not isinstance(value, list):
                raise TypeError(f"{field} must be a list")
            return " ".join(str(item).strip() for item in value if str(item).strip())

        summary = str(result.get("summary", "")).strip()
        subjects = joined("subjects")
        actions = joined("actions")
        scene = joined("scene")
        shot_type = joined("shot_type")
        ocr_text = joined("ocr_text")
        timestamp_ms = int(row["timestamp_ms"])
        time_text = f"{timestamp_ms / 1000:g}秒 {timestamp_ms}毫秒"
        values = (
            frame_id,
            row["video_id"],
            row["video_name"],
            summary,
            subjects,
            actions,
            scene,
            shot_type,
            ocr_text,
            time_text,
            " ".join(
                part
                for part in (
                    row["video_name"],
                    summary,
                    subjects,
                    actions,
                    scene,
                    shot_type,
                    ocr_text,
                    time_text,
                )
                if part
            ),
        )
        connection.execute("DELETE FROM frame_search WHERE frame_id = ?", (frame_id,))
        connection.execute(
            """
            INSERT INTO frame_search (
                frame_id, video_id, video_name, summary, subjects, actions,
                scene, shot_type, ocr_text, time_text, search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    def rebuild_search_index(self) -> int:
        with self.connect() as connection:
            self._initialize_search_table(connection)
            connection.execute("DELETE FROM frame_search")
            self._backfill_search_index(connection)
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM frame_search"
            ).fetchone()["count"]
        return int(count)

    def search_index_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM frame_search"
            ).fetchone()
        return int(row["count"])

    def search_candidates(
        self,
        *,
        fts_query: str,
        like_terms: list[str],
        candidate_limit: int = 500,
    ) -> list[dict[str, Any]]:
        select_sql = """
            SELECT CAST(fs.frame_id AS INTEGER) AS frame_id,
                   fs.video_id, fs.video_name, fs.summary, fs.subjects,
                   fs.actions, fs.scene, fs.shot_type, fs.ocr_text,
                   fs.time_text, fs.search_text, f.timestamp_ms, f.image_name,
                   f.vision_result_json, v.stored_name
            FROM frame_search fs
            JOIN frames f ON f.id = CAST(fs.frame_id AS INTEGER)
            JOIN videos v ON v.id = fs.video_id
        """
        found: dict[int, dict[str, Any]] = {}
        with self.connect() as connection:
            if fts_query and self._search_uses_fts5_connection(connection):
                try:
                    rows = connection.execute(
                        select_sql
                        + " WHERE frame_search MATCH ? LIMIT ?",
                        (fts_query, candidate_limit),
                    ).fetchall()
                    found.update({int(row["frame_id"]): dict(row) for row in rows})
                except sqlite3.OperationalError:
                    pass

            if like_terms and len(found) < candidate_limit:
                conditions = " OR ".join(
                    "fs.search_text LIKE ? ESCAPE '\\'" for _ in like_terms
                )
                parameters = [
                    "%"
                    + term.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                    + "%"
                    for term in like_terms
                ]
                parameters.append(candidate_limit)
                rows = connection.execute(
                    select_sql + f" WHERE ({conditions}) LIMIT ?", parameters
                ).fetchall()
                found.update({int(row["frame_id"]): dict(row) for row in rows})
        return list(found.values())

    def delete_video(self, video_id: str) -> bool:
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                "DELETE FROM frame_search WHERE video_id = ?", (video_id,)
            )
            connection.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        return True
