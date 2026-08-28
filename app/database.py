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
    FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE,
    UNIQUE (video_id, timestamp_ms)
);

CREATE INDEX IF NOT EXISTS idx_frames_video_time
ON frames(video_id, timestamp_ms);
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
                SELECT timestamp_ms, image_name
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

