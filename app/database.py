from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from app.search_vocabulary import expand_index_values


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    duration_ms INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL,
    media_created_at TEXT,
    source_kind TEXT NOT NULL DEFAULT 'upload',
    source_path TEXT,
    source_folder TEXT,
    source_size INTEGER,
    source_mtime_ns INTEGER,
    content_sha256 TEXT,
    index_status TEXT NOT NULL DEFAULT 'pending_analysis',
    index_error TEXT
);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    image_name TEXT NOT NULL,
    vision_status TEXT NOT NULL DEFAULT 'pending',
    duplicate_of_timestamp_ms INTEGER,
    similarity_score REAL,
    vision_model TEXT,
    vision_analyzed_at TEXT,
    vision_edited_at TEXT,
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

CREATE TABLE IF NOT EXISTS watch_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_analyze INTEGER NOT NULL DEFAULT 0,
    scan_interval_seconds INTEGER NOT NULL DEFAULT 60,
    created_at TEXT NOT NULL,
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS scan_jobs (
    id TEXT PRIMARY KEY,
    folder_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    auto_analyze INTEGER NOT NULL DEFAULT 0,
    discovered INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    current_file TEXT,
    current_stage TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (folder_id) REFERENCES watch_folders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scan_jobs_folder_started
ON scan_jobs(folder_id, started_at DESC);
"""

VIDEO_COLUMNS = {
    "media_created_at": "TEXT",
    "source_kind": "TEXT NOT NULL DEFAULT 'upload'",
    "source_path": "TEXT",
    "source_folder": "TEXT",
    "source_size": "INTEGER",
    "source_mtime_ns": "INTEGER",
    "content_sha256": "TEXT",
    "index_status": "TEXT NOT NULL DEFAULT 'pending_analysis'",
    "index_error": "TEXT",
}

FRAME_VISION_COLUMNS = {
    "vision_status": "TEXT NOT NULL DEFAULT 'pending'",
    "duplicate_of_timestamp_ms": "INTEGER",
    "similarity_score": "REAL",
    "vision_model": "TEXT",
    "vision_analyzed_at": "TEXT",
    "vision_edited_at": "TEXT",
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
            video_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(videos)").fetchall()
            }
            for column_name, definition in VIDEO_COLUMNS.items():
                if column_name not in video_columns:
                    connection.execute(
                        f"ALTER TABLE videos ADD COLUMN {column_name} {definition}"
                    )
            existing_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(frames)").fetchall()
            }
            for column_name, definition in FRAME_VISION_COLUMNS.items():
                if column_name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE frames ADD COLUMN {column_name} {definition}"
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_content_sha256
                ON videos(content_sha256) WHERE content_sha256 IS NOT NULL
                """
            )
            self._backfill_video_dates(connection)
            self._backfill_source_folders(connection)
            connection.execute(
                """
                UPDATE scan_jobs
                SET status = 'failed', error = '服务重启，扫描任务已中止',
                    finished_at = COALESCE(finished_at, started_at)
                WHERE status IN ('queued', 'running')
                """
            )
            self._initialize_search_table(connection)
            self._backfill_search_index(connection)
            self._refresh_all_video_index_statuses(connection)

    @staticmethod
    def _backfill_video_dates(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, original_name, uploaded_at, media_created_at
            FROM videos
            """
        ).fetchall()
        pattern = re.compile(
            r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)"
        )
        for row in rows:
            media_created_at = row["media_created_at"] or row["uploaded_at"]
            match = pattern.search(row["original_name"])
            if match:
                try:
                    media_created_at = datetime(
                        int(match.group(1)),
                        int(match.group(2)),
                        int(match.group(3)),
                        tzinfo=timezone.utc,
                    ).isoformat()
                except ValueError:
                    pass
            if media_created_at != row["media_created_at"]:
                connection.execute(
                    "UPDATE videos SET media_created_at = ? WHERE id = ?",
                    (media_created_at, row["id"]),
                )

    @staticmethod
    def _backfill_source_folders(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT id, source_path
            FROM videos
            WHERE source_folder IS NULL AND source_path IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            source_path = str(row["source_path"])
            path_type = PureWindowsPath if "\\" in source_path else Path
            connection.execute(
                "UPDATE videos SET source_folder = ? WHERE id = ?",
                (str(path_type(source_path).parent), row["id"]),
            )

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
        frames: list[
            tuple[int, str] | tuple[int, str, int | None, float | None]
        ],
        media_created_at: str | None = None,
        source_kind: str = "upload",
        source_path: str | None = None,
        source_folder: str | None = None,
        source_size: int | None = None,
        source_mtime_ns: int | None = None,
        content_sha256: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO videos (
                    id, original_name, stored_name, duration_ms, uploaded_at,
                    media_created_at, source_kind, source_path, source_folder,
                    source_size, source_mtime_ns, content_sha256, index_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_analysis')
                """,
                (
                    video_id,
                    original_name,
                    stored_name,
                    duration_ms,
                    uploaded_at,
                    media_created_at or uploaded_at,
                    source_kind,
                    source_path,
                    source_folder,
                    source_size,
                    source_mtime_ns,
                    content_sha256,
                ),
            )
            normalized_frames = []
            for frame in frames:
                timestamp_ms, image_name = frame[:2]
                duplicate_of_timestamp_ms = frame[2] if len(frame) > 2 else None
                similarity_score = frame[3] if len(frame) > 3 else None
                vision_status = (
                    "duplicate"
                    if duplicate_of_timestamp_ms is not None
                    else "pending"
                )
                normalized_frames.append(
                    (
                        video_id,
                        timestamp_ms,
                        image_name,
                        vision_status,
                        duplicate_of_timestamp_ms,
                        similarity_score,
                    )
                )
            connection.executemany(
                """
                INSERT INTO frames (
                    video_id, timestamp_ms, image_name, vision_status,
                    duplicate_of_timestamp_ms, similarity_score
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                normalized_frames,
            )

    def find_video_by_source(
        self, source_path: str, source_size: int, source_mtime_ns: int
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM videos
                WHERE source_path = ? AND source_size = ? AND source_mtime_ns = ?
                LIMIT 1
                """,
                (source_path, source_size, source_mtime_ns),
            ).fetchone()
        return dict(row) if row else None

    def find_video_by_hash(self, content_sha256: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE content_sha256 = ? LIMIT 1",
                (content_sha256,),
            ).fetchone()
        return dict(row) if row else None

    def count_videos(
        self,
        *,
        source_folder: str | None = None,
        managed_uploads_only: bool = False,
    ) -> int:
        where = ""
        parameters: tuple[Any, ...] = ()
        if managed_uploads_only:
            where = " WHERE source_folder IS NULL"
        elif source_folder is not None:
            where = " WHERE source_folder = ?"
            parameters = (source_folder,)
        with self.connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM videos{where}",
                parameters,
            ).fetchone()
        return int(row["total"])

    def list_video_folders(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    source_folder,
                    COUNT(*) AS video_count,
                    SUM(index_status = 'indexed') AS indexed_count,
                    SUM(index_status != 'indexed') AS pending_count,
                    MAX(media_created_at) AS latest_media_created_at
                FROM videos
                GROUP BY source_folder
                ORDER BY latest_media_created_at DESC, source_folder
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_videos(
        self,
        limit: int = 200,
        offset: int = 0,
        *,
        source_folder: str | None = None,
        managed_uploads_only: bool = False,
    ) -> list[dict[str, Any]]:
        where = ""
        parameters: list[Any] = []
        if managed_uploads_only:
            where = "WHERE videos.source_folder IS NULL"
        elif source_folder is not None:
            where = "WHERE videos.source_folder = ?"
            parameters.append(source_folder)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    videos.*,
                    COUNT(frames.id) AS frame_count,
                    COALESCE(SUM(CASE WHEN frames.vision_status = 'success' THEN 1 ELSE 0 END), 0) AS success_count,
                    COALESCE(SUM(CASE WHEN frames.vision_status = 'failed' THEN 1 ELSE 0 END), 0) AS failed_count,
                    COALESCE(SUM(CASE WHEN frames.vision_status = 'processing' THEN 1 ELSE 0 END), 0) AS processing_count,
                    COALESCE(SUM(CASE WHEN frames.vision_status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                    COALESCE(SUM(CASE WHEN frames.vision_status = 'duplicate' THEN 1 ELSE 0 END), 0) AS duplicate_count,
                    (
                        SELECT first_frame.image_name
                        FROM frames AS first_frame
                        WHERE first_frame.video_id = videos.id
                        ORDER BY first_frame.timestamp_ms
                        LIMIT 1
                    ) AS first_frame_image_name
                FROM videos
                LEFT JOIN frames ON frames.video_id = videos.id
                {where}
                GROUP BY videos.id
                ORDER BY videos.media_created_at DESC, videos.uploaded_at DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def set_video_index_status(
        self, video_id: str, status: str, error: str | None = None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE videos SET index_status = ?, index_error = ? WHERE id = ?",
                (status, error[:1000] if error else None, video_id),
            )

    def _refresh_video_index_status(
        self, connection: sqlite3.Connection, video_id: str
    ) -> None:
        counts = connection.execute(
            """
            SELECT SUM(vision_status != 'duplicate') AS total,
                   SUM(vision_status = 'success') AS success,
                   SUM(vision_status = 'failed') AS failed,
                   SUM(vision_status = 'processing') AS processing,
                   SUM(vision_status = 'pending') AS pending
            FROM frames WHERE video_id = ?
            """,
            (video_id,),
        ).fetchone()
        total = int(counts["total"] or 0)
        success = int(counts["success"] or 0)
        failed = int(counts["failed"] or 0)
        processing = int(counts["processing"] or 0)
        pending = int(counts["pending"] or 0)
        if total and success == total:
            index_status = "indexed"
        elif processing:
            index_status = "analyzing"
        elif pending:
            index_status = "pending_analysis"
        elif failed and success:
            index_status = "partial"
        elif failed:
            index_status = "failed"
        else:
            index_status = "pending_analysis"
        connection.execute(
            "UPDATE videos SET index_status = ?, index_error = NULL WHERE id = ?",
            (index_status, video_id),
        )

    def _refresh_all_video_index_statuses(
        self, connection: sqlite3.Connection
    ) -> None:
        video_ids = connection.execute("SELECT id FROM videos").fetchall()
        for row in video_ids:
            self._refresh_video_index_status(connection, row["id"])

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
                    SET vision_status = CASE
                            WHEN duplicate_of_timestamp_ms IS NULL THEN 'pending'
                            ELSE 'duplicate'
                        END,
                        vision_model = NULL,
                        vision_analyzed_at = NULL,
                        vision_edited_at = NULL,
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
            self._refresh_video_index_status(connection, video_id)
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
            connection.execute(
                "UPDATE videos SET index_status = 'analyzing', index_error = NULL WHERE id = ?",
                (video_id,),
            )
        claimed = dict(frame)
        claimed["vision_status"] = "processing"
        return claimed

    def claim_duplicate_vision_frame(
        self, video_id: str, frame_id: int
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            frame = connection.execute(
                """
                SELECT * FROM frames
                WHERE id = ? AND video_id = ? AND vision_status = 'duplicate'
                """,
                (frame_id, video_id),
            ).fetchone()
            if frame is None:
                return None
            connection.execute(
                """
                UPDATE frames
                SET vision_status = 'processing',
                    duplicate_of_timestamp_ms = NULL,
                    similarity_score = NULL
                WHERE id = ?
                """,
                (frame_id,),
            )
            connection.execute(
                "UPDATE videos SET index_status = 'analyzing', index_error = NULL WHERE id = ?",
                (video_id,),
            )
        claimed = dict(frame)
        claimed["vision_status"] = "processing"
        claimed["duplicate_of_timestamp_ms"] = None
        claimed["similarity_score"] = None
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
            video_row = connection.execute(
                "SELECT video_id FROM frames WHERE id = ?", (frame_id,)
            ).fetchone()
            connection.execute(
                """
                UPDATE frames
                SET vision_status = 'success',
                    vision_model = ?,
                    vision_analyzed_at = ?,
                    vision_edited_at = NULL,
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
            if video_row:
                self._refresh_video_index_status(connection, video_row["video_id"])

    def update_vision_result(
        self,
        *,
        video_id: str,
        frame_id: int,
        result_json: str,
        edited_at: str,
    ) -> bool:
        with self.connect() as connection:
            frame = connection.execute(
                """
                SELECT id
                FROM frames
                WHERE id = ? AND video_id = ? AND vision_status = 'success'
                """,
                (frame_id, video_id),
            ).fetchone()
            if frame is None:
                return False
            connection.execute(
                """
                UPDATE frames
                SET vision_result_json = ?,
                    vision_edited_at = ?,
                    vision_error = NULL
                WHERE id = ?
                """,
                (result_json, edited_at, frame_id),
            )
            self._upsert_search_frame(connection, frame_id)
        return True

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
            video_row = connection.execute(
                "SELECT video_id FROM frames WHERE id = ?", (frame_id,)
            ).fetchone()
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
            if video_row:
                self._refresh_video_index_status(connection, video_row["video_id"])

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

        search_aliases = result.get("search_aliases") or {}
        if not isinstance(search_aliases, dict):
            raise TypeError("search_aliases must be an object")

        def joined(field: str, *, expand_aliases: bool = True) -> str:
            value = result.get(field, [])
            if not isinstance(value, list):
                raise TypeError(f"{field} must be a list")
            if not expand_aliases:
                return " ".join(
                    str(item).strip() for item in value if str(item).strip()
                )
            aliases = search_aliases.get(field, [])
            if not isinstance(aliases, list):
                raise TypeError(f"search_aliases.{field} must be a list")
            return " ".join(expand_index_values(value, aliases))

        summary = str(result.get("summary", "")).strip()
        subjects = joined("subjects")
        actions = joined("actions")
        scene = joined("scene")
        shot_type = joined("shot_type")
        ocr_text = joined("ocr_text", expand_aliases=False)
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
                   f.vision_result_json, v.stored_name, v.media_created_at,
                   v.index_status
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

    def upsert_watch_folder(
        self,
        *,
        path: str,
        auto_analyze: bool,
        scan_interval_seconds: int,
        created_at: str,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO watch_folders (
                    path, enabled, auto_analyze, scan_interval_seconds, created_at
                ) VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    enabled = 1,
                    auto_analyze = excluded.auto_analyze,
                    scan_interval_seconds = excluded.scan_interval_seconds
                """,
                (path, int(auto_analyze), scan_interval_seconds, created_at),
            )
            row = connection.execute(
                "SELECT * FROM watch_folders WHERE path = ?", (path,)
            ).fetchone()
        return dict(row)

    def get_watch_folder(self, folder_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM watch_folders WHERE id = ?", (folder_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_watch_folders(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        condition = "WHERE wf.enabled = 1" if enabled_only else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT wf.*,
                       sj.id AS latest_job_id,
                       sj.status AS latest_job_status,
                       sj.discovered, sj.processed, sj.imported,
                       sj.skipped, sj.failed, sj.current_file,
                       sj.current_stage, sj.error AS job_error,
                       sj.started_at, sj.finished_at
                FROM watch_folders wf
                LEFT JOIN scan_jobs sj ON sj.id = (
                    SELECT id FROM scan_jobs
                    WHERE folder_id = wf.id
                    ORDER BY started_at DESC LIMIT 1
                )
                {condition}
                ORDER BY wf.created_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_watch_folder_scanned(self, folder_id: int, scanned_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE watch_folders SET last_scan_at = ? WHERE id = ?",
                (scanned_at, folder_id),
            )

    def delete_watch_folder(self, folder_id: int) -> bool:
        with self.connect() as connection:
            deleted = connection.execute(
                "DELETE FROM watch_folders WHERE id = ?", (folder_id,)
            ).rowcount
        return bool(deleted)

    def get_active_scan_job(self, folder_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM scan_jobs
                WHERE folder_id = ? AND status IN ('queued', 'running')
                ORDER BY started_at DESC LIMIT 1
                """,
                (folder_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_scan_job(
        self,
        *,
        job_id: str,
        folder_id: int,
        auto_analyze: bool,
        started_at: str,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_jobs (
                    id, folder_id, status, auto_analyze, started_at
                ) VALUES (?, ?, 'queued', ?, ?)
                """,
                (job_id, folder_id, int(auto_analyze), started_at),
            )
        return self.get_scan_job(job_id)

    def get_scan_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def update_scan_job(self, job_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "discovered",
            "processed",
            "imported",
            "skipped",
            "failed",
            "current_file",
            "current_stage",
            "error",
            "finished_at",
        }
        updates = {key: value for key, value in changes.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{column} = ?" for column in updates)
        parameters = [*updates.values(), job_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE scan_jobs SET {assignments} WHERE id = ?", parameters
            )

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
