from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
SSIM_PATTERN = re.compile(r"All:(\d+(?:\.\d+)?)")
SIMILAR_FRAME_THRESHOLD = 0.995
SSIM_PATTERN = re.compile(r"All:(\d+(?:\.\d+)?)")
SIMILAR_FRAME_THRESHOLD = 0.995


class VideoProcessingError(RuntimeError):
    """视频无法由 FFmpeg 读取或处理。"""


@dataclass(frozen=True)
class ExtractedFrame:
    timestamp_ms: int
    image_name: str
    duplicate_of_timestamp_ms: int | None = None
    similarity_score: float | None = None
    duplicate_of_timestamp_ms: int | None = None
    similarity_score: float | None = None


def resolve_ffmpeg() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise VideoProcessingError("未找到 FFmpeg，请先安装并配置 FFmpeg。") from exc


def _run_ffmpeg(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [resolve_ffmpeg(), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise VideoProcessingError(f"FFmpeg 启动失败：{exc}") from exc


def probe_duration_ms(video_path: Path) -> int:
    result = _run_ffmpeg(["-hide_banner", "-i", str(video_path)])
    output = f"{result.stdout}\n{result.stderr}"
    match = DURATION_PATTERN.search(output)
    if not match:
        raise VideoProcessingError("无法读取视频时长，请确认文件是有效的 MP4 视频。")

    hours, minutes, seconds = match.groups()
    duration_ms = round(
        (int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000
    )
    if duration_ms <= 0:
        raise VideoProcessingError("视频时长必须大于 0 秒。")
    return duration_ms


def extraction_timestamps(duration_ms: int, interval_ms: int = 5_000) -> list[int]:
    if duration_ms <= 0:
        return []
    return list(range(0, duration_ms, interval_ms))


def frame_similarity(reference_path: Path, candidate_path: Path) -> float | None:
    result = _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(reference_path),
            "-i",
            str(candidate_path),
            "-lavfi",
            "ssim",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
    )
    if result.returncode != 0:
        return None
    match = SSIM_PATTERN.search(f"{result.stdout}\n{result.stderr}")
    if not match:
        return None
    return max(0.0, min(1.0, float(match.group(1))))


def extract_frames(video_path: Path, output_dir: Path) -> tuple[int, list[ExtractedFrame]]:
    duration_ms = probe_duration_ms(video_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    extracted: list[ExtractedFrame] = []
    last_representative: ExtractedFrame | None = None

    for index, timestamp_ms in enumerate(extraction_timestamps(duration_ms), start=1):
        image_name = f"frame_{index:06d}_{timestamp_ms:012d}ms.jpg"
        image_path = output_dir / image_name
        result = _run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp_ms / 1000:.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                "scale='min(960,iw)':-2",
                "-q:v",
                "2",
                "-y",
                str(image_path),
            ]
        )
        if result.returncode != 0 or not image_path.is_file():
            raise VideoProcessingError(
                f"无法提取 {timestamp_ms / 1000:.3f} 秒画面：{result.stderr.strip()}"
            )
        duplicate_of_timestamp_ms = None
        similarity_score = None
        if last_representative is not None:
            similarity_score = frame_similarity(
                output_dir / last_representative.image_name,
                image_path,
            )
            if (
                similarity_score is not None
                and similarity_score >= SIMILAR_FRAME_THRESHOLD
            ):
                duplicate_of_timestamp_ms = last_representative.timestamp_ms

        frame = ExtractedFrame(
            timestamp_ms=timestamp_ms,
            image_name=image_name,
            duplicate_of_timestamp_ms=duplicate_of_timestamp_ms,
            similarity_score=similarity_score,
        )
        extracted.append(frame)
        if duplicate_of_timestamp_ms is None:
            last_representative = frame

    return duration_ms, extracted


def ffmpeg_version() -> str:
    result = _run_ffmpeg(["-version"])
    if result.returncode != 0:
        raise VideoProcessingError("FFmpeg 版本检查失败。")
    return result.stdout.splitlines()[0]
