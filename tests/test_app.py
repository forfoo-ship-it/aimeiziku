from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import Database
from app.video_service import extraction_timestamps, resolve_ffmpeg
from app.vision_provider import (
    VisionAnalysis,
    VisionProviderError,
    VisionResult,
    validate_vision_result,
)


class FakeVisionProvider:
    model = "fake-vision-model"

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.failures_remaining = 0

    async def analyze(self, image_path: Path) -> VisionAnalysis:
        self.calls.append(image_path)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise VisionProviderError("模拟单帧识别失败")
        result = VisionResult(
            summary=f"测试画面 {image_path.stem}",
            subjects=["测试图形"],
            actions=["移动"],
            scene=["测试背景"],
            shot_type=["横屏", "全景"],
            ocr_text=[],
            confidence=0.95,
        )
        return VisionAnalysis(
            result=result,
            raw_text=result.model_dump_json(),
            model=self.model,
            duration_ms=25,
            input_tokens=100,
            output_tokens=30,
            total_tokens=130,
        )


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


def make_static_test_video(path: Path, duration_seconds: int = 12) -> None:
    result = subprocess.run(
        [
            resolve_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1f6b4f:size=640x360:rate=25:duration={duration_seconds}",
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
    fake_provider = FakeVisionProvider()

    monkeypatch.setattr(main, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(main, "FRAME_DIR", frame_dir)
    monkeypatch.setattr(main, "database", test_database)
    monkeypatch.setattr(main, "vision_provider_factory", lambda: fake_provider)

    with TestClient(main.app) as client:
        yield client, test_database, upload_dir, frame_dir, fake_provider


def upload_test_video(client: TestClient, source: Path) -> dict:
    with source.open("rb") as video:
        response = client.post(
            "/api/videos",
            files={"file": (source.name, video, "video/mp4")},
        )
    assert response.status_code == 201, response.text
    return response.json()


def test_extraction_timestamps_are_exact_five_second_points() -> None:
    assert extraction_timestamps(12_040) == [0, 5_000, 10_000]
    assert extraction_timestamps(5_000) == [0]


def test_real_mp4_upload_extracts_frames_and_persists_timestamps(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, upload_dir, frame_dir, _ = isolated_app
    source = tmp_path / "县融媒测试素材.mp4"
    make_test_video(source)
    payload = upload_test_video(client, source)
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
    assert "AI媒资库" in page.text
    assert "视频素材智能检索系统" in page.text
    assert 'id="admin-console"' in page.text
    assert 'id="admin-entry-button"' in page.text
    assert 'id="video-library-title"' in page.text
    assert 'id="video-folder-navigation"' in page.text
    assert 'id="video-folder-back"' in page.text
    assert "/static/app.js?v=20260829-search-cleanup" in page.text
    assert "/static/styles.css?v=20260829-search-cleanup" in page.text
    assert "试试：" not in page.text
    assert "例如：找到龙舟冲刺时鼓手击鼓的横屏镜头" not in page.text
    assert 'id="library-frame-detail"' in page.text
    assert 'id="library-frame-detail-list"' in page.text
    assert 'id="workspace" class="workspace" hidden' in page.text
    assert "请点击关键帧播放视频" in page.text
    assert 'id="video-player" controls preload="metadata" hidden' in page.text
    assert "window.__INITIAL_VIDEO__ = null" in page.text
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "adminConsole.scrollIntoView" not in script.text
    assert 'document.querySelector(".search-panel")?.scrollIntoView' not in script.text


def test_rejects_non_mp4(isolated_app, tmp_path: Path) -> None:
    client, *_ = isolated_app
    response = client.post(
        "/api/videos",
        files={"file": ("notes.txt", b"not a video", "text/plain")},
    )
    assert response.status_code == 415


def test_video_library_lists_thumbnail_and_vision_progress(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, *_ = isolated_app
    source = tmp_path / "素材库列表测试.mp4"
    make_test_video(source)
    uploaded = upload_test_video(client, source)

    response = client.get("/api/videos?limit=1&offset=0")
    assert response.status_code == 200
    library = response.json()
    assert library["count"] == 1
    assert library["total"] == 1
    assert library["has_more"] is False
    item = library["videos"][0]
    assert item["id"] == uploaded["id"]
    assert item["thumbnail_url"] == uploaded["frames"][0]["image_url"]
    assert item["frame_count"] == 3
    assert item["pending_count"] == 3
    assert item["success_count"] == 0
    assert item["duplicate_count"] == 0
    assert item["analysis_frame_count"] == 3
    assert client.get("/api/videos?limit=501").status_code == 400
    assert client.get("/api/videos?offset=-1").status_code == 400

    source_folder = tmp_path / "数博会素材" / "展馆"
    watch_folder = test_database.upsert_watch_folder(
        path=str(tmp_path / "数博会素材"),
        auto_analyze=False,
        scan_interval_seconds=60,
        created_at="2026-08-29T07:00:00+00:00",
    )
    test_database.insert_video(
        video_id="folder-video",
        original_name="展馆全景.mp4",
        stored_name="folder-video.mp4",
        duration_ms=5_000,
        uploaded_at="2026-08-29T08:00:00+00:00",
        source_kind="folder",
        source_path=str(source_folder / "展馆全景.mp4"),
        source_folder=str(source_folder),
        frames=[(0, "frame_001.jpg")],
    )
    roots = client.get("/api/video-roots")
    assert roots.status_code == 200
    root_payload = roots.json()
    assert root_payload["count"] == 2
    managed_root = next(
        root for root in root_payload["roots"] if root["managed_uploads"]
    )
    watch_root = next(
        root for root in root_payload["roots"] if not root["managed_uploads"]
    )
    assert managed_root["direct_videos"] is True
    assert managed_root["video_count"] == 1
    assert watch_root["root_key"] == f"watch-{watch_folder['id']}"
    assert watch_root["name"] == "数博会素材"
    assert watch_root["folder_count"] == 1
    assert watch_root["video_count"] == 1

    folders = client.get(
        "/api/video-folders", params={"root_key": watch_root["root_key"]}
    )
    assert folders.status_code == 200
    folder_payload = folders.json()
    assert folder_payload["count"] == 1
    actual = folder_payload["folders"][0]
    assert actual["name"] == "展馆"
    assert actual["path"] == str(source_folder)
    assert actual["video_count"] == 1

    managed_videos = client.get(
        "/api/videos", params={"folder_key": managed_root["root_key"]}
    ).json()
    actual_videos = client.get(
        "/api/videos", params={"folder_key": actual["folder_key"]}
    ).json()
    assert managed_videos["total"] == 1
    assert managed_videos["videos"][0]["id"] == uploaded["id"]
    assert actual_videos["total"] == 1
    assert actual_videos["videos"][0]["id"] == "folder-video"
    assert client.get(
        "/api/videos", params={"folder_key": "invalid-folder"}
    ).status_code == 400
    assert client.get(
        "/api/video-folders", params={"root_key": "watch-not-a-number"}
    ).status_code == 400
    assert client.get(
        "/api/video-folders", params={"root_key": "watch-99999"}
    ).status_code == 400


def test_open_video_location_is_local_only_and_uses_managed_copy(
    isolated_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, test_database, upload_dir, _, _ = isolated_app
    source = tmp_path / "定位视频测试.mp4"
    make_test_video(source, duration_seconds=2)
    uploaded = upload_test_video(client, source)

    blocked = client.post(f"/api/videos/{uploaded['id']}/open-location")
    assert blocked.status_code == 403

    opened_paths: list[Path] = []
    monkeypatch.setattr(main, "request_may_open_server_file", lambda request: True)
    monkeypatch.setattr(main, "open_file_in_manager", opened_paths.append)
    response = client.post(f"/api/videos/{uploaded['id']}/open-location")
    assert response.status_code == 200
    assert response.json()["location_kind"] == "managed_copy"
    assert opened_paths == [
        (upload_dir / f"{uploaded['id']}.mp4").resolve(strict=True)
    ]

    with test_database.connect() as connection:
        connection.execute(
            "UPDATE videos SET source_kind = 'folder', source_path = ? WHERE id = ?",
            (str(source.resolve(strict=True)), uploaded["id"]),
        )
    opened_paths.clear()
    source_response = client.post(f"/api/videos/{uploaded['id']}/open-location")
    assert source_response.status_code == 200
    assert source_response.json()["location_kind"] == "source"
    assert opened_paths == [source.resolve(strict=True)]


def test_vision_json_validation_accepts_fenced_json() -> None:
    result = validate_vision_result(
        """```json
        {"summary":"道路画面","subjects":["车辆"],"actions":["行驶"],
        "scene":["道路"],"shot_type":["横屏"],"ocr_text":[],"confidence":0.9}
        ```"""
    )
    assert result.summary == "道路画面"
    assert result.confidence == 0.9


def test_vision_analysis_persists_results_and_skips_success(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, _, _, fake_provider = isolated_app
    source = tmp_path / "vision-test.mp4"
    make_test_video(source)
    uploaded = upload_test_video(client, source)

    started = client.post(
        f"/api/videos/{uploaded['id']}/vision/start", json={"force": False}
    )
    assert started.status_code == 200
    assert started.json()["vision_progress"]["completed"] == 0

    for expected_completed in (1, 2, 3):
        response = client.post(f"/api/videos/{uploaded['id']}/vision/next")
        assert response.status_code == 200
        assert response.json()["video"]["vision_progress"]["completed"] == expected_completed

    persisted = client.get(f"/api/videos/{uploaded['id']}").json()
    assert persisted["vision_progress"] == {
        "total": 3,
        "completed": 3,
        "success": 3,
        "failed": 0,
        "processing": 0,
        "pending": 0,
        "duplicate": 0,
        "done": True,
    }
    assert all(frame["vision_result"]["summary"] for frame in persisted["frames"])
    assert all(frame["vision_total_tokens"] == 130 for frame in persisted["frames"])
    assert len(fake_provider.calls) == 3

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    duplicate = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    assert duplicate["processed"] is False
    assert len(fake_provider.calls) == 3

    page = client.get("/")
    assert "AI识别画面" in page.text
    assert "fake-vision-model" not in page.text
    assert "window.__INITIAL_VIDEO__ = null" in page.text

    forced = client.post(
        f"/api/videos/{uploaded['id']}/vision/start", json={"force": True}
    ).json()
    assert forced["vision_progress"]["pending"] == 3

    with sqlite3.connect(test_database.path) as connection:
        statuses = connection.execute(
            "SELECT vision_status FROM frames ORDER BY timestamp_ms"
        ).fetchall()
    assert statuses == [("pending",), ("pending",), ("pending",)]


def test_static_video_marks_similar_frames_and_calls_vision_once(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, _, _, fake_provider = isolated_app
    source = tmp_path / "固定机位测试.mp4"
    make_static_test_video(source)
    uploaded = upload_test_video(client, source)

    assert [frame["timestamp_ms"] for frame in uploaded["frames"]] == [
        0,
        5_000,
        10_000,
    ]
    assert [frame["vision_status"] for frame in uploaded["frames"]] == [
        "pending",
        "duplicate",
        "duplicate",
    ]
    assert uploaded["vision_progress"]["total"] == 1
    assert uploaded["vision_progress"]["duplicate"] == 2
    assert uploaded["frames"][1]["duplicate_of_timestamp_ms"] == 0
    assert uploaded["frames"][1]["similarity_score"] >= 0.995

    library_item = client.get("/api/videos?limit=1&offset=0").json()["videos"][0]
    assert library_item["frame_count"] == 3
    assert library_item["analysis_frame_count"] == 1
    assert library_item["duplicate_count"] == 2

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    analyzed = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    assert analyzed["done"] is True
    assert analyzed["video"]["vision_progress"]["success"] == 1
    assert analyzed["video"]["vision_progress"]["duplicate"] == 2
    assert analyzed["video"]["index_status"] == "indexed"
    assert len(fake_provider.calls) == 1

    duplicate = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    assert duplicate["processed"] is False
    assert len(fake_provider.calls) == 1
    assert test_database.search_index_count() == 1

    forced = client.post(
        f"/api/videos/{uploaded['id']}/frames/{uploaded['frames'][1]['id']}/vision/analyze"
    )
    assert forced.status_code == 200
    assert forced.json()["frames"][1]["vision_status"] == "success"
    assert forced.json()["vision_progress"]["duplicate"] == 1
    assert forced.json()["vision_progress"]["total"] == 2
    assert len(fake_provider.calls) == 2
    assert test_database.search_index_count() == 2


def test_manual_vision_edit_updates_result_and_search_index_without_api_call(
    isolated_app, tmp_path: Path
) -> None:
    client, test_database, _, _, fake_provider = isolated_app
    source = tmp_path / "人工纠错测试.mp4"
    make_test_video(source)
    uploaded = upload_test_video(client, source)

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    analyzed = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()["video"]
    frame = analyzed["frames"][0]
    assert len(fake_provider.calls) == 1

    response = client.put(
        f"/api/videos/{uploaded['id']}/frames/{frame['id']}/vision",
        json={
            "summary": "工作人员在数博会展牌旁介绍展览内容",
            "subjects": ["工作人员", "展示牌", "展示牌"],
            "actions": ["讲解"],
            "scene": ["数博会展馆"],
            "shot_type": ["横屏", "中景"],
            "ocr_text": ["数博会人工校对"],
            "confidence": 0.88,
        },
    )
    assert response.status_code == 200
    edited_frame = response.json()["frames"][0]
    assert edited_frame["vision_result"]["summary"].startswith("工作人员")
    assert edited_frame["vision_result"]["subjects"] == ["工作人员", "展示牌"]
    assert edited_frame["vision_result"]["search_aliases"] == {
        "subjects": [],
        "actions": [],
        "scene": [],
        "shot_type": [],
    }
    assert edited_frame["vision_edited_at"]
    assert len(fake_provider.calls) == 1

    corrected_search = client.get("/api/search", params={"q": "展示牌"})
    assert corrected_search.status_code == 200
    assert corrected_search.json()["results"][0]["frame_id"] == frame["id"]
    assert client.get("/api/search", params={"q": "测试图形"}).json()["count"] == 0

    with test_database.connect() as connection:
        stored = connection.execute(
            "SELECT vision_result_json, vision_edited_at FROM frames WHERE id = ?",
            (frame["id"],),
        ).fetchone()
    assert "数博会人工校对" in stored["vision_result_json"]
    assert stored["vision_edited_at"]

    pending_frame = analyzed["frames"][1]
    conflict = client.put(
        f"/api/videos/{uploaded['id']}/frames/{pending_frame['id']}/vision",
        json={
            "summary": "不应保存",
            "subjects": [],
            "actions": [],
            "scene": [],
            "shot_type": [],
            "ocr_text": [],
            "confidence": 0.5,
        },
    )
    assert conflict.status_code == 409


def test_single_vision_failure_does_not_stop_remaining_frames(
    isolated_app, tmp_path: Path
) -> None:
    client, _, _, _, fake_provider = isolated_app
    fake_provider.failures_remaining = 1
    source = tmp_path / "partial-failure.mp4"
    make_test_video(source, duration_seconds=6)
    uploaded = upload_test_video(client, source)

    client.post(f"/api/videos/{uploaded['id']}/vision/start", json={"force": False})
    first = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()
    second = client.post(f"/api/videos/{uploaded['id']}/vision/next").json()

    assert first["video"]["vision_progress"]["failed"] == 1
    assert second["done"] is True
    assert second["video"]["vision_progress"]["success"] == 1
    assert second["video"]["vision_progress"]["failed"] == 1
