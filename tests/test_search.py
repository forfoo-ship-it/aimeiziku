from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import Database
from app.search_service import SearchService
from app.vision_provider import VisionResult


def result_json(
    *,
    summary: str,
    subjects: list[str],
    actions: list[str],
    scene: list[str],
    shot_type: list[str],
    ocr_text: list[str],
) -> str:
    return VisionResult(
        summary=summary,
        subjects=subjects,
        actions=actions,
        scene=scene,
        shot_type=shot_type,
        ocr_text=ocr_text,
        confidence=0.94,
    ).model_dump_json()


def add_video_with_results(
    database: Database,
    *,
    video_id: str,
    video_name: str,
    results: list[tuple[int, str]],
) -> None:
    database.insert_video(
        video_id=video_id,
        original_name=video_name,
        stored_name=f"{video_id}.mp4",
        duration_ms=max(timestamp for timestamp, _ in results) + 5_000,
        uploaded_at=datetime.now(timezone.utc).isoformat(),
        frames=[
            (timestamp, f"frame_{index:03d}.jpg")
            for index, (timestamp, _) in enumerate(results, start=1)
        ],
    )
    with database.connect() as connection:
        frame_rows = connection.execute(
            "SELECT id, timestamp_ms FROM frames WHERE video_id = ? ORDER BY timestamp_ms",
            (video_id,),
        ).fetchall()
    payload_by_time = dict(results)
    for frame in frame_rows:
        raw = payload_by_time[frame["timestamp_ms"]]
        database.save_vision_success(
            frame_id=frame["id"],
            model="fake-vision-model",
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            result_json=raw,
            raw_text=raw,
            duration_ms=10,
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
        )


@pytest.fixture()
def search_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "search.db")
    database.initialize()
    add_video_with_results(
        database,
        video_id="dragon-video",
        video_name="dragon-boat-demo.mp4",
        results=[
            (
                10_000,
                result_json(
                    summary="龙舟队在河面加速冲刺，船头鼓手连续击鼓",
                    subjects=["龙舟", "鼓手", "划手"],
                    actions=["击鼓", "划桨", "冲刺"],
                    scene=["河面", "龙舟比赛"],
                    shot_type=["横屏", "中近景"],
                    ocr_text=["2026中国传统龙舟大赛"],
                ),
            ),
            (
                20_000,
                result_json(
                    summary="龙舟接近终点，队员正在发力",
                    subjects=["龙舟", "划手"],
                    actions=["划桨", "发力"],
                    scene=["河面", "比赛"],
                    shot_type=["横屏", "远景"],
                    ocr_text=[],
                ),
            ),
        ],
    )
    add_video_with_results(
        database,
        video_id="old-town-video",
        video_name="zhongnan-gate-night.mp4",
        results=[
            (
                5_000,
                result_json(
                    summary="夜晚从空中俯瞰灯光照亮的古城建筑",
                    subjects=["古建筑", "城门"],
                    actions=[],
                    scene=["古城", "夜景", "传统建筑"],
                    shot_type=["横屏", "航拍", "全景"],
                    ocr_text=["中南门"],
                ),
            )
        ],
    )
    return database


def test_identified_frames_enter_fts_index(search_database: Database) -> None:
    assert search_database.search_uses_fts5() is True
    assert search_database.search_index_count() == 3


def test_subject_search_returns_correct_video(search_database: Database) -> None:
    response = SearchService(search_database).search("龙舟")
    assert response.results[0]["video_id"] == "dragon-video"
    assert response.results[0]["timestamp"] == 10.0
    assert response.results[0]["video_url"] == "/media/videos/dragon-video.mp4"
    assert response.results[0]["media_month"] != "unknown"
    assert response.results[0]["media_month_label"].endswith("月")


def test_action_synonym_finds_indexed_action(search_database: Database) -> None:
    drumming = SearchService(search_database).search("打鼓")
    assert drumming.results[0]["actions"] == ["击鼓", "划桨", "冲刺"]
    assert "actions" in drumming.results[0]["matched_fields"]


def test_shot_type_synonym_finds_indexed_shot(search_database: Database) -> None:
    horizontal = SearchService(search_database).search("横版")
    assert horizontal.results[0]["shot_type"][0] == "横屏"
    assert "shot_type" in horizontal.results[0]["matched_fields"]


def test_ocr_is_searchable(search_database: Database) -> None:
    response = SearchService(search_database).search("龙舟大赛")
    assert response.results[0]["ocr_text"] == ["2026中国传统龙舟大赛"]
    assert "ocr_text" in response.results[0]["matched_fields"]


def test_structured_weights_rank_best_result_first(search_database: Database) -> None:
    response = SearchService(search_database).search("龙舟")
    assert response.results[0]["timestamp"] == 10.0
    assert response.results[0]["score"] > response.results[1]["score"]


def test_multiple_concepts_must_all_match_same_frame(
    search_database: Database,
) -> None:
    response = SearchService(search_database).search("龙舟鼓手击鼓的横屏镜头")
    assert len(response.results) == 1
    assert response.results[0]["timestamp"] == 10.0
    assert {"subjects", "actions", "shot_type"}.issubset(
        response.results[0]["matched_fields"]
    )


def test_vertical_drone_search_excludes_partial_keyword_matches(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "strong-search.db")
    database.initialize()
    add_video_with_results(
        database,
        video_id="strong-video",
        video_name="strong-search.mp4",
        results=[
            (
                0,
                result_json(
                    summary="竖屏无人机航拍运动场",
                    subjects=["无人机", "运动场"],
                    actions=["航拍"],
                    scene=["户外"],
                    shot_type=["竖屏", "全景"],
                    ocr_text=[],
                ),
            ),
            (
                5_000,
                result_json(
                    summary="无人机横屏航拍运动场",
                    subjects=["无人机"],
                    actions=["航拍"],
                    scene=["运动场"],
                    shot_type=["横屏", "全景"],
                    ocr_text=[],
                ),
            ),
            (
                10_000,
                result_json(
                    summary="竖屏采访画面",
                    subjects=["受访者"],
                    actions=["采访"],
                    scene=["室内"],
                    shot_type=["竖屏", "近景"],
                    ocr_text=[],
                ),
            ),
        ],
    )

    direct = SearchService(database).search("竖屏无人机")
    synonym = SearchService(database).search("竖版航拍")
    assert [result["timestamp"] for result in direct.results] == [0.0]
    assert [result["timestamp"] for result in synonym.results] == [0.0]


def test_empty_query_is_rejected(search_database: Database) -> None:
    with pytest.raises(ValueError):
        SearchService(search_database).search(" ")


def test_missing_content_returns_empty_list(search_database: Database) -> None:
    assert SearchService(search_database).search("肯定不存在的企鹅画面").results == []


def test_reanalysis_updates_without_duplicate_index(search_database: Database) -> None:
    with search_database.connect() as connection:
        frame_id = connection.execute(
            "SELECT id FROM frames WHERE video_id = 'dragon-video' ORDER BY timestamp_ms LIMIT 1"
        ).fetchone()["id"]
    updated = result_json(
        summary="鼓手在龙舟船头擂鼓",
        subjects=["鼓手", "龙舟"],
        actions=["擂鼓"],
        scene=["河面"],
        shot_type=["横屏"],
        ocr_text=["龙舟大赛"],
    )
    search_database.save_vision_success(
        frame_id=frame_id,
        model="fake-vision-model",
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        result_json=updated,
        raw_text=updated,
        duration_ms=10,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
    )
    assert search_database.search_index_count() == 3
    assert SearchService(search_database).search("擂鼓").results[0]["frame_id"] == frame_id


def test_delete_video_cleans_search_index(search_database: Database) -> None:
    assert search_database.delete_video("old-town-video") is True
    assert search_database.search_index_count() == 2
    assert SearchService(search_database).search("中南门").results == []


def test_search_api_does_not_call_vision_provider(
    search_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "database", search_database)

    def forbidden_provider():
        raise AssertionError("搜索不得调用视觉模型")

    monkeypatch.setattr(main, "vision_provider_factory", forbidden_provider)
    with TestClient(main.app) as client:
        response = client.get("/api/search", params={"q": "龙舟", "limit": 20})
        empty = client.get("/api/search", params={"q": " "})
        missing = client.get("/api/search", params={"q": "企鹅滑雪"})

    assert response.status_code == 200
    assert empty.status_code == 400
    assert missing.status_code == 200
    assert missing.json()["results"] == []


def test_search_api_returns_media_addresses_and_timestamps(
    search_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "database", search_database)
    with TestClient(main.app) as client:
        response = client.get("/api/search", params={"q": "龙舟", "limit": 20})

    assert response.status_code == 200
    payload = response.json()
    result = payload["results"][0]
    assert result["thumbnail_url"].endswith("frame_001.jpg")
    assert result["video_url"] == "/media/videos/dragon-video.mp4"
    assert result["timestamp"] == 10.0
    assert result["timestamp_ms"] == 10_000
