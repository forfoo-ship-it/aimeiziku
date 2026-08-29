from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.database import Database
from app.video_service import VideoProcessingError, extract_frames
from app.vision_provider import VisionProvider
from app.vision_service import VisionAnalysisService


MAX_FOLDER_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
HASH_CHUNK_BYTES = 8 * 1024 * 1024
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)")


class FolderScanError(RuntimeError):
    """监测目录或其中的视频无法处理。"""


@dataclass(frozen=True)
class ImportOutcome:
    video_id: str
    imported: bool
    index_status: str


def content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def infer_media_created_at(path: Path, modified_timestamp: float) -> str:
    match = DATE_PATTERN.search(path.stem)
    if match:
        try:
            parsed = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                tzinfo=timezone.utc,
            )
            return parsed.isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(modified_timestamp, tz=timezone.utc).isoformat()


class FolderScanService:
    def __init__(
        self,
        *,
        database: Database,
        upload_dir: Path,
        frame_dir: Path,
        provider_factory: Callable[[], VisionProvider],
    ) -> None:
        self.database = database
        self.upload_dir = upload_dir
        self.frame_dir = frame_dir
        self.provider_factory = provider_factory
        self._scan_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def start_scan(
        self, folder_id: int, *, auto_analyze: bool | None = None
    ) -> dict:
        folder = self.database.get_watch_folder(folder_id)
        if folder is None:
            raise FolderScanError("未找到该监测目录。")
        active = self.database.get_active_scan_job(folder_id)
        if active:
            return active

        use_auto_analyze = (
            bool(folder["auto_analyze"])
            if auto_analyze is None
            else bool(auto_analyze)
        )
        job_id = uuid4().hex
        job = self.database.create_scan_job(
            job_id=job_id,
            folder_id=folder_id,
            auto_analyze=use_auto_analyze,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        task = asyncio.create_task(self._run_scan(job_id), name=f"folder-scan-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(job_id, None))
        return job

    async def wait(self, job_id: str) -> dict | None:
        task = self._tasks.get(job_id)
        if task:
            await task
        return self.database.get_scan_job(job_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_scan(self, job_id: str) -> None:
        job = self.database.get_scan_job(job_id)
        if job is None:
            return
        folder = self.database.get_watch_folder(int(job["folder_id"]))
        if folder is None:
            self.database.update_scan_job(
                job_id,
                status="failed",
                error="监测目录记录不存在。",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            return

        async with self._scan_lock:
            try:
                root = Path(folder["path"])
                if not root.is_dir():
                    raise FolderScanError("监测目录不存在或无法访问。")
                self.database.update_scan_job(
                    job_id, status="running", current_stage="正在发现 MP4 视频"
                )
                video_paths = await asyncio.to_thread(self._discover_videos, root)
                self.database.update_scan_job(job_id, discovered=len(video_paths))

                imported = skipped = failed = processed = 0
                provider: VisionProvider | None = None
                for path in video_paths:
                    try:
                        self.database.update_scan_job(
                            job_id,
                            current_file=path.name,
                            current_stage="正在校验文件指纹",
                        )
                        outcome = await asyncio.to_thread(self._import_or_find, path)
                        if outcome.imported:
                            imported += 1
                        else:
                            skipped += 1

                        if bool(job["auto_analyze"]) and outcome.index_status == "pending_analysis":
                            if provider is None:
                                provider = self.provider_factory()
                            await self._analyze_pending_frames(
                                job_id, outcome.video_id, path.name, provider
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        self.database.update_scan_job(
                            job_id,
                            error=f"{path.name}：{str(exc)[:800]}",
                        )
                    finally:
                        processed += 1
                        self.database.update_scan_job(
                            job_id,
                            processed=processed,
                            imported=imported,
                            skipped=skipped,
                            failed=failed,
                        )

                finished_at = datetime.now(timezone.utc).isoformat()
                self.database.update_scan_job(
                    job_id,
                    status="completed",
                    current_file=None,
                    current_stage="扫描完成",
                    finished_at=finished_at,
                )
                self.database.mark_watch_folder_scanned(int(folder["id"]), finished_at)
            except asyncio.CancelledError:
                self.database.update_scan_job(
                    job_id,
                    status="failed",
                    error="服务停止，扫描任务已中止。",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                )
                raise
            except Exception as exc:
                finished_at = datetime.now(timezone.utc).isoformat()
                self.database.update_scan_job(
                    job_id,
                    status="failed",
                    error=str(exc)[:1000],
                    current_stage="扫描失败",
                    finished_at=finished_at,
                )
                self.database.mark_watch_folder_scanned(int(folder["id"]), finished_at)

    @staticmethod
    def _discover_videos(root: Path) -> list[Path]:
        try:
            return sorted(
                (
                    path
                    for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() == ".mp4"
                ),
                key=lambda path: str(path).casefold(),
            )
        except OSError as exc:
            raise FolderScanError(f"无法读取监测目录：{exc}") from exc

    def _import_or_find(self, source_path: Path) -> ImportOutcome:
        resolved = source_path.resolve(strict=True)
        stat_before = resolved.stat()
        if stat_before.st_size <= 0:
            raise FolderScanError("视频文件为空。")
        if stat_before.st_size > MAX_FOLDER_VIDEO_BYTES:
            raise FolderScanError("视频超过 2 GB，已跳过。")

        source_key = os.path.normcase(str(resolved))
        existing = self.database.find_video_by_source(
            source_key, stat_before.st_size, stat_before.st_mtime_ns
        )
        if existing:
            return ImportOutcome(
                video_id=existing["id"],
                imported=False,
                index_status=existing["index_status"],
            )

        fingerprint = content_sha256(resolved)
        stat_after_hash = resolved.stat()
        if (
            stat_after_hash.st_size != stat_before.st_size
            or stat_after_hash.st_mtime_ns != stat_before.st_mtime_ns
        ):
            raise FolderScanError("文件仍在写入，留待下次扫描。")

        duplicate = self.database.find_video_by_hash(fingerprint)
        if duplicate:
            return ImportOutcome(
                video_id=duplicate["id"],
                imported=False,
                index_status=duplicate["index_status"],
            )

        video_id = uuid4().hex
        stored_name = f"{video_id}.mp4"
        destination = self.upload_dir / stored_name
        output_dir = self.frame_dir / video_id
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(resolved, destination)
            copied_stat = resolved.stat()
            if (
                copied_stat.st_size != stat_before.st_size
                or copied_stat.st_mtime_ns != stat_before.st_mtime_ns
            ):
                raise FolderScanError("复制期间源文件发生变化，留待下次扫描。")
            duration_ms, frames = extract_frames(destination, output_dir)
            now = datetime.now(timezone.utc).isoformat()
            self.database.insert_video(
                video_id=video_id,
                original_name=resolved.name,
                stored_name=stored_name,
                duration_ms=duration_ms,
                uploaded_at=now,
                media_created_at=infer_media_created_at(
                    resolved, stat_before.st_mtime
                ),
                source_kind="folder",
                source_path=source_key,
                source_folder=str(resolved.parent),
                source_size=stat_before.st_size,
                source_mtime_ns=stat_before.st_mtime_ns,
                content_sha256=fingerprint,
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
        except Exception:
            destination.unlink(missing_ok=True)
            shutil.rmtree(output_dir, ignore_errors=True)
            raise
        return ImportOutcome(video_id, True, "pending_analysis")

    async def _analyze_pending_frames(
        self,
        job_id: str,
        video_id: str,
        file_name: str,
        provider: VisionProvider,
    ) -> None:
        service = VisionAnalysisService(
            database=self.database,
            frame_root=self.frame_dir,
            provider=provider,
        )
        while True:
            record = self.database.get_video(video_id)
            if record is None:
                return
            analyzable_frames = [
                frame
                for frame in record["frames"]
                if frame["vision_status"] != "duplicate"
            ]
            completed = sum(
                frame["vision_status"] in {"success", "failed"}
                for frame in analyzable_frames
            )
            total = len(analyzable_frames)
            self.database.update_scan_job(
                job_id,
                current_file=file_name,
                current_stage=f"AI画面识别 {completed}/{total}",
            )
            if not await service.process_next(video_id):
                break
