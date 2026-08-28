from __future__ import annotations

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
