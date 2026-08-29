from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import sqlite3
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, AsyncIterator, Callable
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.config import VisionConfigurationError, VisionSettings
from app.database import Database
from app.folder_scan_service import (
    FolderScanError,
    FolderScanService,
    infer_media_created_at,
)
from app.search_service import SearchService, SearchValidationError
from app.video_service import VideoProcessingError, extract_frames
from app.vision_provider import (
    DeepSeekVisionProvider,
    VisionProvider,
    VisionResult,
    VisionSearchAliases,
)
from app.vision_service import VisionAnalysisService


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
FRAME_DIR = DATA_DIR / "frames"
DATABASE_PATH = DATA_DIR / "media.db"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
MANAGED_UPLOADS_FOLDER_KEY = "managed-uploads"
WATCH_ROOT_KEY_PREFIX = "watch-"
UNASSIGNED_ROOT_KEY = "unassigned-folders"

database = Database(DATABASE_PATH)
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def create_vision_provider() -> VisionProvider:
    settings = VisionSettings.from_environment(BASE_DIR / ".env")
    return DeepSeekVisionProvider(settings)


vision_provider_factory: Callable[[], VisionProvider] = create_vision_provider
folder_scan_service: FolderScanService | None = None


class VisionStartRequest(BaseModel):
    force: bool = False


VisionEditText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
]
VisionEditSummary = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
]


class VisionResultEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: VisionEditSummary
    subjects: list[VisionEditText] = Field(default_factory=list, max_length=50)
    actions: list[VisionEditText] = Field(default_factory=list, max_length=50)
    scene: list[VisionEditText] = Field(default_factory=list, max_length=50)
    shot_type: list[VisionEditText] = Field(default_factory=list, max_length=50)
    ocr_text: list[VisionEditText] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0, le=1)

    @field_validator("subjects", "actions", "scene", "shot_type", "ocr_text")
    @classmethod
    def remove_duplicate_values(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))


class WatchFolderRequest(BaseModel):
    path: str
    auto_analyze: bool = False
    scan_interval_seconds: int = 60


class ScanStartRequest(BaseModel):
    auto_analyze: bool | None = None


async def folder_monitor_loop(service: FolderScanService) -> None:
    while True:
        now = datetime.now(timezone.utc)
        for folder in database.list_watch_folders(enabled_only=True):
            last_scan_at = folder.get("last_scan_at")
            due = last_scan_at is None
            if last_scan_at:
                try:
                    last_scan = datetime.fromisoformat(last_scan_at)
                    due = (now - last_scan).total_seconds() >= int(
                        folder["scan_interval_seconds"]
                    )
                except (TypeError, ValueError):
                    due = True
            if due:
                try:
                    service.start_scan(int(folder["id"]))
                except (FolderScanError, sqlite3.Error):
                    continue
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global folder_scan_service
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    database.initialize()
    folder_scan_service = FolderScanService(
        database=database,
        upload_dir=UPLOAD_DIR,
        frame_dir=FRAME_DIR,
        provider_factory=vision_provider_factory,
    )
    monitor_task = asyncio.create_task(
        folder_monitor_loop(folder_scan_service), name="folder-monitor"
    )
    try:
        yield
    finally:
        monitor_task.cancel()
        await asyncio.gather(monitor_task, return_exceptions=True)
        await folder_scan_service.shutdown()
        folder_scan_service = None


app = FastAPI(title="AI媒资库", version="0.4.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.mount("/media/videos", StaticFiles(directory=UPLOAD_DIR), name="videos")
app.mount("/media/frames", StaticFiles(directory=FRAME_DIR), name="frames")


def serialize_video(record: dict | None) -> dict | None:
    if record is None:
        return None
    video = record["video"]
    video_id = video["id"]
    serialized_frames = []
    for frame in record["frames"]:
        vision_result = None
        if frame.get("vision_result_json"):
            try:
                vision_result = json.loads(frame["vision_result_json"])
            except json.JSONDecodeError:
                vision_result = None
        serialized_frames.append(
            {
                "id": frame["id"],
                "timestamp_ms": frame["timestamp_ms"],
                "timestamp_seconds": frame["timestamp_ms"] / 1000,
                "image_url": f"/media/frames/{video_id}/{frame['image_name']}",
                "vision_status": frame.get("vision_status") or "pending",
                "duplicate_of_timestamp_ms": frame.get("duplicate_of_timestamp_ms"),
                "duplicate_of_timestamp_seconds": (
                    frame["duplicate_of_timestamp_ms"] / 1000
                    if frame.get("duplicate_of_timestamp_ms") is not None
                    else None
                ),
                "similarity_score": frame.get("similarity_score"),
                "vision_model": frame.get("vision_model"),
                "vision_analyzed_at": frame.get("vision_analyzed_at"),
                "vision_edited_at": frame.get("vision_edited_at"),
                "vision_result": vision_result,
                "vision_error": frame.get("vision_error"),
                "vision_duration_ms": frame.get("vision_duration_ms"),
                "vision_input_tokens": frame.get("vision_input_tokens"),
                "vision_output_tokens": frame.get("vision_output_tokens"),
                "vision_total_tokens": frame.get("vision_total_tokens"),
            }
        )

    statuses = [
        frame["vision_status"]
        for frame in serialized_frames
        if frame["vision_status"] != "duplicate"
    ]
    duplicate_count = sum(
        frame["vision_status"] == "duplicate" for frame in serialized_frames
    )
    completed = sum(status in {"success", "failed"} for status in statuses)
    return {
        "id": video_id,
        "original_name": video["original_name"],
        "duration_ms": video["duration_ms"],
        "duration_seconds": video["duration_ms"] / 1000,
        "uploaded_at": video["uploaded_at"],
        "media_created_at": video.get("media_created_at") or video["uploaded_at"],
        "source_kind": video.get("source_kind") or "upload",
        "index_status": video.get("index_status") or "pending_analysis",
        "index_error": video.get("index_error"),
        "video_url": f"/media/videos/{video['stored_name']}",
        "frames": serialized_frames,
        "vision_progress": {
            "total": len(statuses),
            "completed": completed,
            "success": statuses.count("success"),
            "failed": statuses.count("failed"),
            "processing": statuses.count("processing"),
            "pending": statuses.count("pending"),
            "duplicate": duplicate_count,
            "done": bool(statuses) and completed == len(statuses),
        },
    }


def serialize_scan_job(job: dict | None) -> dict | None:
    if job is None:
        return None
    total = int(job.get("discovered") or 0)
    processed = int(job.get("processed") or 0)
    return {
        **job,
        "auto_analyze": bool(job.get("auto_analyze")),
        "progress_percent": round(processed / total * 100) if total else 0,
    }


def video_thumbnail_url(video: dict) -> str | None:
    image_name = video.get("first_frame_image_name")
    if not image_name or Path(image_name).name != image_name:
        return None
    if not (FRAME_DIR / video["id"] / image_name).is_file():
        return None
    return f"/media/frames/{video['id']}/{image_name}"


def encode_source_folder(source_folder: str) -> str:
    encoded = base64.urlsafe_b64encode(source_folder.encode("utf-8")).decode("ascii")
    return f"path-{encoded.rstrip('=')}"


def decode_source_folder(folder_key: str) -> str:
    if not folder_key.startswith("path-"):
        raise ValueError("invalid folder key")
    encoded = folder_key[5:]
    padding = "=" * (-len(encoded) % 4)
    try:
        source_folder = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("invalid folder key") from exc
    if not source_folder or len(source_folder) > 4000:
        raise ValueError("invalid folder key")
    return source_folder


def source_folder_name(source_folder: str) -> str:
    normalized = source_folder.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or source_folder


def folder_is_within(source_folder: str, root_folder: str) -> bool:
    try:
        normalized_source = os.path.normcase(os.path.abspath(source_folder))
        normalized_root = os.path.normcase(os.path.abspath(root_folder))
        return os.path.commonpath((normalized_source, normalized_root)) == normalized_root
    except (OSError, ValueError):
        return False


def watch_root_name(root_folder: str) -> str:
    normalized = root_folder.replace("\\", "/").rstrip("/")
    if len(normalized) == 2 and normalized[1] == ":":
        return f"{normalized[0].upper()}盘"
    return normalized.rsplit("/", 1)[-1] or root_folder


def matching_watch_root_id(
    source_folder: str,
    watch_folders: list[dict],
) -> int | None:
    matches = [
        folder
        for folder in watch_folders
        if folder_is_within(source_folder, str(folder["path"]))
    ]
    if not matches:
        return None
    most_specific = max(
        matches,
        key=lambda folder: len(os.path.normcase(os.path.abspath(str(folder["path"])))),
    )
    return int(most_specific["id"])


def serialize_source_folder(row: dict) -> dict:
    source_folder = row.get("source_folder")
    managed_uploads = source_folder is None
    return {
        "folder_key": (
            MANAGED_UPLOADS_FOLDER_KEY
            if managed_uploads
            else encode_source_folder(source_folder)
        ),
        "name": (
            "历史视频上传"
            if managed_uploads
            else source_folder_name(source_folder)
        ),
        "path": str(UPLOAD_DIR.resolve()) if managed_uploads else source_folder,
        "managed_uploads": managed_uploads,
        "video_count": int(row.get("video_count") or 0),
        "indexed_count": int(row.get("indexed_count") or 0),
        "pending_count": int(row.get("pending_count") or 0),
        "latest_media_created_at": row.get("latest_media_created_at"),
    }


def request_may_open_server_file(request: Request) -> bool:
    host = request.url.hostname or ""
    client_host = request.client.host if request.client else ""

    def is_loopback(value: str) -> bool:
        if value.lower() == "localhost":
            return True
        try:
            return ip_address(value).is_loopback
        except ValueError:
            return False

    return is_loopback(host) and is_loopback(client_host)


def resolve_video_file(record: dict) -> tuple[Path, str]:
    video = record["video"]
    source_path = video.get("source_path")
    if source_path:
        source = Path(source_path)
        if source.is_file():
            return source.resolve(strict=True), "source"

    upload_root = UPLOAD_DIR.resolve()
    managed_copy = (UPLOAD_DIR / video["stored_name"]).resolve(strict=True)
    if not managed_copy.is_relative_to(upload_root):
        raise OSError("视频路径超出受控素材目录。")
    return managed_copy, "managed_copy"


def open_file_in_manager(path: Path) -> None:
    if sys.platform == "win32":
        explorer = shutil.which("explorer.exe") or "explorer.exe"
        command = [explorer, f"/select,{path}"]
    elif sys.platform == "darwin":
        command = ["open", "-R", str(path)]
    else:
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            raise OSError("服务器没有图形桌面，无法打开文件管理器。")
        opener = shutil.which("xdg-open")
        if not opener:
            raise OSError("服务器未安装 xdg-open，无法打开文件管理器。")
        command = [opener, str(path.parent)]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def require_folder_scan_service() -> FolderScanService:
    if folder_scan_service is None:
        raise HTTPException(status_code=503, detail="文件夹监测服务尚未启动。")
    return folder_scan_service


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"initial_video": None},
    )


@app.get("/api/videos/latest")
async def latest_video() -> dict:
    video = serialize_video(database.get_latest_video())
    if video is None:
        raise HTTPException(status_code=404, detail="尚未上传视频。")
    return video


@app.get("/api/video-roots")
async def list_video_roots() -> dict:
    watch_folders = database.list_watch_folders()
    folder_rows = database.list_video_folders()
    roots: list[dict] = []

    managed_row = next(
        (row for row in folder_rows if row.get("source_folder") is None),
        None,
    )
    if managed_row:
        roots.append(
            {
                "root_key": MANAGED_UPLOADS_FOLDER_KEY,
                "name": "历史视频上传",
                "path": str(UPLOAD_DIR.resolve()),
                "managed_uploads": True,
                "direct_videos": True,
                "folder_count": 1,
                "video_count": int(managed_row.get("video_count") or 0),
                "indexed_count": int(managed_row.get("indexed_count") or 0),
                "pending_count": int(managed_row.get("pending_count") or 0),
            }
        )

    for watch_folder in watch_folders:
        watch_id = int(watch_folder["id"])
        rows = [
            row
            for row in folder_rows
            if row.get("source_folder")
            and matching_watch_root_id(str(row["source_folder"]), watch_folders)
            == watch_id
        ]
        roots.append(
            {
                "root_key": f"{WATCH_ROOT_KEY_PREFIX}{watch_id}",
                "name": watch_root_name(str(watch_folder["path"])),
                "path": watch_folder["path"],
                "managed_uploads": False,
                "direct_videos": False,
                "folder_count": len(rows),
                "video_count": sum(int(row.get("video_count") or 0) for row in rows),
                "indexed_count": sum(
                    int(row.get("indexed_count") or 0) for row in rows
                ),
                "pending_count": sum(
                    int(row.get("pending_count") or 0) for row in rows
                ),
            }
        )

    unassigned_rows = [
        row
        for row in folder_rows
        if row.get("source_folder")
        and matching_watch_root_id(str(row["source_folder"]), watch_folders) is None
    ]
    if unassigned_rows:
        roots.append(
            {
                "root_key": UNASSIGNED_ROOT_KEY,
                "name": "其他已入库目录",
                "path": "不在当前监测配置中的历史目录",
                "managed_uploads": False,
                "direct_videos": False,
                "folder_count": len(unassigned_rows),
                "video_count": sum(
                    int(row.get("video_count") or 0) for row in unassigned_rows
                ),
                "indexed_count": sum(
                    int(row.get("indexed_count") or 0) for row in unassigned_rows
                ),
                "pending_count": sum(
                    int(row.get("pending_count") or 0) for row in unassigned_rows
                ),
            }
        )
    return {"count": len(roots), "roots": roots}


@app.get("/api/video-folders")
async def list_video_folders(root_key: str | None = None) -> dict:
    folder_rows = database.list_video_folders()
    watch_folders = database.list_watch_folders()

    if root_key is None:
        selected_rows = folder_rows
    elif root_key == MANAGED_UPLOADS_FOLDER_KEY:
        selected_rows = [
            row for row in folder_rows if row.get("source_folder") is None
        ]
    elif root_key == UNASSIGNED_ROOT_KEY:
        selected_rows = [
            row
            for row in folder_rows
            if row.get("source_folder")
            and matching_watch_root_id(str(row["source_folder"]), watch_folders)
            is None
        ]
    elif root_key.startswith(WATCH_ROOT_KEY_PREFIX):
        try:
            watch_id = int(root_key.removeprefix(WATCH_ROOT_KEY_PREFIX))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="监测目录标识无效。"
            ) from exc
        if database.get_watch_folder(watch_id) is None:
            raise HTTPException(status_code=400, detail="监测目录不存在。")
        selected_rows = [
            row
            for row in folder_rows
            if row.get("source_folder")
            and matching_watch_root_id(str(row["source_folder"]), watch_folders)
            == watch_id
        ]
    else:
        raise HTTPException(status_code=400, detail="监测目录标识无效。")

    folders = [serialize_source_folder(row) for row in selected_rows]
    return {"count": len(folders), "root_key": root_key, "folders": folders}


@app.get("/api/videos")
async def list_videos(
    limit: int = 200,
    offset: int = 0,
    folder_key: str | None = None,
) -> dict:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 必须在 1 至 500 之间。")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset 不能小于 0。")
    source_folder = None
    managed_uploads_only = False
    if folder_key == MANAGED_UPLOADS_FOLDER_KEY:
        managed_uploads_only = True
    elif folder_key:
        try:
            source_folder = decode_source_folder(folder_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="素材文件夹标识无效。",
            ) from exc
    videos = database.list_videos(
        limit=limit,
        offset=offset,
        source_folder=source_folder,
        managed_uploads_only=managed_uploads_only,
    )
    total = database.count_videos(
        source_folder=source_folder,
        managed_uploads_only=managed_uploads_only,
    )
    return {
        "count": len(videos),
        "total": total,
        "offset": offset,
        "has_more": offset + len(videos) < total,
        "videos": [
            {
                "id": video["id"],
                "original_name": video["original_name"],
                "media_created_at": video.get("media_created_at")
                or video["uploaded_at"],
                "source_kind": video.get("source_kind") or "upload",
                "source_folder": video.get("source_folder"),
                "index_status": video.get("index_status") or "pending_analysis",
                "index_error": video.get("index_error"),
                "thumbnail_url": video_thumbnail_url(video),
                "frame_count": int(video.get("frame_count") or 0),
                "success_count": int(video.get("success_count") or 0),
                "failed_count": int(video.get("failed_count") or 0),
                "processing_count": int(video.get("processing_count") or 0),
                "pending_count": int(video.get("pending_count") or 0),
                "duplicate_count": int(video.get("duplicate_count") or 0),
                "analysis_frame_count": (
                    int(video.get("frame_count") or 0)
                    - int(video.get("duplicate_count") or 0)
                ),
            }
            for video in videos
        ],
    }


@app.get("/api/watch-folders")
async def list_watch_folders() -> dict:
    folders = database.list_watch_folders()
    return {
        "folders": [
            {
                **folder,
                "enabled": bool(folder["enabled"]),
                "auto_analyze": bool(folder["auto_analyze"]),
                "latest_job": serialize_scan_job(
                    {
                        "id": folder["latest_job_id"],
                        "status": folder["latest_job_status"],
                        "auto_analyze": folder["auto_analyze"],
                        "discovered": folder["discovered"],
                        "processed": folder["processed"],
                        "imported": folder["imported"],
                        "skipped": folder["skipped"],
                        "failed": folder["failed"],
                        "current_file": folder["current_file"],
                        "current_stage": folder["current_stage"],
                        "error": folder["job_error"],
                        "started_at": folder["started_at"],
                        "finished_at": folder["finished_at"],
                    }
                    if folder["latest_job_id"]
                    else None
                ),
            }
            for folder in folders
        ]
    }


def resolve_watch_folder(raw_path: str) -> Path:
    cleaned = raw_path.strip()
    if not cleaned or len(cleaned) > 2000:
        raise HTTPException(status_code=400, detail="请输入有效的本地文件夹路径。")
    try:
        folder_path = Path(cleaned).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="文件夹不存在或无法访问。") from exc
    if not folder_path.is_dir():
        raise HTTPException(status_code=400, detail="指定路径不是文件夹。")
    data_path = DATA_DIR.resolve()
    if folder_path == data_path or data_path.is_relative_to(folder_path):
        raise HTTPException(
            status_code=400,
            detail="不能监测项目 data 目录或它的上级目录，以免重复导入运行文件。",
        )
    return folder_path


@app.post("/api/watch-folders", status_code=status.HTTP_201_CREATED)
async def add_watch_folder(request: WatchFolderRequest) -> dict:
    if request.scan_interval_seconds < 15 or request.scan_interval_seconds > 3600:
        raise HTTPException(status_code=400, detail="扫描间隔必须在 15 到 3600 秒之间。")
    folder_path = resolve_watch_folder(request.path)
    folder = database.upsert_watch_folder(
        path=str(folder_path),
        auto_analyze=request.auto_analyze,
        scan_interval_seconds=request.scan_interval_seconds,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        job = require_folder_scan_service().start_scan(int(folder["id"]))
    except FolderScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"folder": folder, "job": serialize_scan_job(job)}


@app.post("/api/watch-folders/{folder_id}/scan")
async def scan_watch_folder(folder_id: int, request: ScanStartRequest) -> dict:
    try:
        job = require_folder_scan_service().start_scan(
            folder_id, auto_analyze=request.auto_analyze
        )
    except FolderScanError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_scan_job(job)


@app.delete("/api/watch-folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watch_folder(folder_id: int) -> None:
    if database.get_active_scan_job(folder_id):
        raise HTTPException(status_code=409, detail="目录正在扫描，请等待完成后再停止监测。")
    if not database.delete_watch_folder(folder_id):
        raise HTTPException(status_code=404, detail="未找到该监测目录。")


@app.get("/api/scan-jobs/{job_id}")
async def get_scan_job(job_id: str) -> dict:
    job = database.get_scan_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="未找到该扫描任务。")
    return serialize_scan_job(job)


@app.get("/api/search")
async def search_frames(q: str = "", limit: int = 20) -> dict:
    try:
        response = SearchService(database).search(q, limit=limit)
    except SearchValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="搜索索引暂时不可用。") from exc
    return {
        "query": response.query,
        "count": response.count,
        "elapsed_ms": response.elapsed_ms,
        "backend": response.backend,
        "results": response.results,
    }


@app.get("/api/videos/{video_id}")
async def get_video(video_id: str) -> dict:
    video = serialize_video(database.get_video(video_id))
    if video is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    return video


@app.put("/api/videos/{video_id}/frames/{frame_id}/vision")
async def update_frame_vision_result(
    video_id: str,
    frame_id: int,
    request: VisionResultEditRequest,
) -> dict:
    record = database.get_video(video_id)
    if record is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    frame = next(
        (item for item in record["frames"] if int(item["id"]) == frame_id),
        None,
    )
    if frame is None:
        raise HTTPException(status_code=404, detail="未找到该关键帧。")
    if frame.get("vision_status") != "success":
        raise HTTPException(
            status_code=409,
            detail="只有已成功识别的关键帧才能人工修改。",
        )

    corrected = VisionResult(
        summary=request.summary,
        subjects=request.subjects,
        actions=request.actions,
        scene=request.scene,
        shot_type=request.shot_type,
        ocr_text=request.ocr_text,
        search_aliases=VisionSearchAliases(),
        confidence=request.confidence,
    )
    try:
        updated = database.update_vision_result(
            video_id=video_id,
            frame_id=frame_id,
            result_json=corrected.model_dump_json(),
            edited_at=datetime.now(timezone.utc).isoformat(),
        )
    except (sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=500,
            detail="识别信息保存失败，原有内容未被修改。",
        ) from exc
    if not updated:
        raise HTTPException(
            status_code=409,
            detail="该关键帧当前不能修改，请刷新后重试。",
        )
    return serialize_video(database.get_video(video_id))


@app.post("/api/videos/{video_id}/open-location")
async def open_video_location(video_id: str, request: Request) -> dict:
    if not request_may_open_server_file(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "只有在服务器本机通过 localhost 或 127.0.0.1 访问时，"
                "才能打开资源管理器。内网其他电脑需要使用共享素材路径。"
            ),
        )
    record = database.get_video(video_id)
    if record is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    try:
        path, location_kind = resolve_video_file(record)
        open_file_in_manager(path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"无法打开视频所在位置：{exc}",
        ) from exc
    return {
        "opened": True,
        "video_name": record["video"]["original_name"],
        "location_kind": location_kind,
    }


@app.post("/api/videos/{video_id}/vision/start")
async def start_vision_analysis(video_id: str, request: VisionStartRequest) -> dict:
    if database.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    try:
        vision_provider_factory()
    except VisionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    database.reset_vision(video_id, force=request.force)
    return serialize_video(database.get_video(video_id))


@app.post("/api/videos/{video_id}/vision/next")
async def analyze_next_frame(video_id: str) -> dict:
    if database.get_video(video_id) is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    try:
        provider = vision_provider_factory()
    except VisionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    service = VisionAnalysisService(
        database=database,
        frame_root=FRAME_DIR,
        provider=provider,
    )
    processed = await service.process_next(video_id)
    video = serialize_video(database.get_video(video_id))
    return {
        "processed": processed,
        "done": video["vision_progress"]["done"],
        "video": video,
    }


@app.post("/api/videos/{video_id}/frames/{frame_id}/vision/analyze")
async def analyze_duplicate_frame(video_id: str, frame_id: int) -> dict:
    record = database.get_video(video_id)
    if record is None:
        raise HTTPException(status_code=404, detail="未找到该视频。")
    frame = next(
        (item for item in record["frames"] if int(item["id"]) == frame_id),
        None,
    )
    if frame is None:
        raise HTTPException(status_code=404, detail="未找到该关键帧。")
    if frame.get("vision_status") != "duplicate":
        raise HTTPException(
            status_code=409,
            detail="只有已跳过的相似关键帧可以使用此操作。",
        )
    try:
        provider = vision_provider_factory()
    except VisionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    service = VisionAnalysisService(
        database=database,
        frame_root=FRAME_DIR,
        provider=provider,
    )
    processed = await service.process_duplicate(video_id, frame_id)
    if not processed:
        raise HTTPException(
            status_code=409,
            detail="该关键帧状态已经变化，请刷新后重试。",
        )
    return serialize_video(database.get_video(video_id))


@app.post("/api/videos", status_code=status.HTTP_201_CREATED)
async def upload_video(file: UploadFile = File(...)) -> dict:
    original_name = Path(file.filename or "").name
    if not original_name or Path(original_name).suffix.lower() != ".mp4":
        raise HTTPException(status_code=415, detail="目前仅支持 MP4 视频。")

    video_id = uuid4().hex
    stored_name = f"{video_id}.mp4"
    video_path = UPLOAD_DIR / stored_name
    output_dir = FRAME_DIR / video_id
    bytes_written = 0

    try:
        with video_path.open("xb") as destination:
            while chunk := await file.read(COPY_CHUNK_BYTES):
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="视频不能超过 2 GB。")
                destination.write(chunk)

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="上传文件不能为空。")

        duration_ms, frames = extract_frames(video_path, output_dir)
        uploaded_at = datetime.now(timezone.utc).isoformat()
        database.insert_video(
            video_id=video_id,
            original_name=original_name,
            stored_name=stored_name,
            duration_ms=duration_ms,
            uploaded_at=uploaded_at,
            media_created_at=infer_media_created_at(
                Path(original_name), video_path.stat().st_mtime
            ),
            frames=[
                (
                    frame.timestamp_ms,
                    frame.image_name,
                    frame.duplicate_of_timestamp_ms,
                    frame.similarity_score,
                )
                for frame in frames
            ],
        )
    except HTTPException:
        video_path.unlink(missing_ok=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except VideoProcessingError as exc:
        video_path.unlink(missing_ok=True)
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await file.close()

    return serialize_video(database.get_video(video_id))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
