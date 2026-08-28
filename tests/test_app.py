from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import Database
from app.video_service import extraction_timestamps, resolve_ffmpeg


def make_test_video(path: Path, duration_seconds: int = 12) -> None:
    result = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=640x360:rate=25:duration={duration_seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture()
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    upload_dir = tmp_path / "uploads"
    frame_dir = tmp_path / "frames"
    upload_dir.mkdir()
    frame_dir.mkdir()
    test_database = Database(tmp_path / "test.db")

    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "FRAME_DIR", frame_dir)
    monkeypatch.setattr(main, "database", test_database)

    with TestClient(main.app) as client:
        yield client, test_database, upload_dir, frame_dir


def test_extraction_timestamps_are_exact_five_second_points() -> None:
    assert extraction_timestamps(12_040) == [0, 5_000, 10_000]
    assert extraction_timestamps(5_000) == [0]


def test_real_mp4_upload_extracts_frames_and_persists_timestamps(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, upload_dir, frame_dir = isolated_app
    source = tmp_path / "县融媒测试素材.mp4"
    make_test_video(source)

    with source.open("rb") as video:
        response = client.post(
            "/api/videos",
            files={"file": (source.name, video, "video/mp4")},
        )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["original_name"] == source.name
    assert [frame["timestamp_ms"] for frame in payload["frames"]] == [0, 5_000, 10_000]
    assert all(Path(frame_dir / payload["id"] / Path(frame["image_url"]).name).is_file() for frame in payload["frames"])
    assert len(list(upload_dir.glob("*.mp4"))) == 1

    with sqlite3.connect(test_database.path) as connection:
        stored = connection.execute(
            "SELECT timestamp_ms FROM frames ORDER BY timestamp_ms"
        ).fetchall()
    assert stored == [(0,), (5_000,), (10_000,)]

    page = client.get("/")
    assert page.status_code == 200
    assert "县媒智搜" in page.text
    assert payload["video_url"] in page.text


def test_rejects_non_mp4(isolated_app, tmp_path: Path) -> None:
    client, *_ = isolated_app
    response = client.post(
        "/api/videos",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 415
