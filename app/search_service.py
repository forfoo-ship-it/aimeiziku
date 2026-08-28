from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from app.database import Database


SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("发力", "加速", "冲刺"),
    ("击鼓", "打鼓", "擂鼓"),
    ("欢呼", "呐喊", "喝彩"),
    ("航拍", "空中", "俯瞰", "无人机"),
    ("夜景", "夜晚", "夜间"),
    ("近景", "近距离"),
    ("特写", "局部", "细节"),
    ("全景", "大场景", "远景"),
    ("横屏", "横版", "横向"),
    ("竖屏", "竖版", "纵向"),
    ("采访", "同期声", "受访者"),
    ("古建筑", "古城", "传统建筑"),
    ("表演", "演出", "展演"),
)

FIELD_WEIGHTS = {
    "subjects": 22.0,
    "actions": 24.0,
    "ocr_text": 26.0,
    "scene": 18.0,
    "shot_type": 18.0,
    "summary": 10.0,
    "video_name": 6.0,
    "time_text": 8.0,
}

FIELD_LABELS = {
    "subjects": "主体",
    "actions": "动作",
    "ocr_text": "OCR文字",
    "scene": "场景",
    "shot_type": "镜头类型",
    "summary": "画面摘要",
    "video_name": "视频名",
    "time_text": "时间点",
}

QUERY_STOP_TERMS = {
    "找到",
    "画面",
    "镜头",
    "素材",
    "出现",
    "视频",
    "一个",
    "中的",
    "时的",
    "的横",
    "的竖",
}


class SearchValidationError(ValueError):
    """搜索参数无效。"""


@dataclass(frozen=True)
class SearchResponse:
    query: str
    count: int
    elapsed_ms: int
    backend: str
    results: list[dict[str, Any]]


def normalize_text(value: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]+", value.lower()))


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def expand_query_terms(query: str) -> list[str]:
    lowered = query.lower().strip()
    normalized_query = normalize_text(lowered)
    terms: list[str] = []

    for chunk in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_.-]{2,}", lowered):
        normalized_chunk = normalize_text(chunk)
        if len(normalized_chunk) >= 2:
            terms.append(normalized_chunk)
        if re.fullmatch(r"[\u3400-\u9fff]+", normalized_chunk):
            terms.extend(
                normalized_chunk[index : index + 2]
                for index in range(len(normalized_chunk) - 1)
            )

    for group in SYNONYM_GROUPS:
        if any(normalize_text(word) in normalized_query for word in group):
            terms.extend(normalize_text(word) for word in group)

    for seconds in re.findall(r"(\d+(?:\.\d+)?)\s*秒", lowered):
        terms.extend((f"{seconds}秒", seconds))

    return [
        term
        for term in _unique(terms)
        if len(term) >= 2 and term not in QUERY_STOP_TERMS
    ][:60]


def build_fts_query(terms: list[str]) -> str:
    safe_terms = [term.replace('"', '""') for term in terms]
    return " OR ".join(f'"{term}"' for term in safe_terms)


class SearchService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def search(self, query: str, *, limit: int = 20) -> SearchResponse:
        cleaned_query = query.strip()
        if len(cleaned_query) < 2:
            raise SearchValidationError("搜索内容至少需要 2 个字符。")
        if len(cleaned_query) > 200:
            raise SearchValidationError("搜索内容不能超过 200 个字符。")
        if limit < 1 or limit > 50:
            raise SearchValidationError("limit 必须在 1 到 50 之间。")

        started = time.perf_counter()
        terms = expand_query_terms(cleaned_query)
        candidates = self.database.search_candidates(
            fts_query=build_fts_query(terms),
            like_terms=terms,
        )
        scored = [
            result
            for candidate in candidates
            if (result := self._score_candidate(candidate, cleaned_query, terms))
        ]
        scored.sort(
            key=lambda item: (
                -item["score"],
                item["video_name"].lower(),
                item["timestamp_ms"],
            )
        )
        results = scored[:limit]
        elapsed_ms = max(1, round((time.perf_counter() - started) * 1000))
        return SearchResponse(
            query=cleaned_query,
            count=len(results),
            elapsed_ms=elapsed_ms,
            backend="fts5" if self.database.search_uses_fts5() else "like",
            results=results,
        )

    def _score_candidate(
        self, row: dict[str, Any], query: str, terms: list[str]
    ) -> dict[str, Any] | None:
        normalized_query = normalize_text(query)
        matched: dict[str, list[str]] = {}
        matched_query_terms: set[str] = set()
        raw_score = 0.0

        for field, weight in FIELD_WEIGHTS.items():
            field_text = str(row.get(field) or "")
            normalized_field = normalize_text(field_text)
            if not normalized_field:
                continue
            field_matches: list[str] = []
            field_score = 0.0
            raw_items = field_text.split()
            items = [normalize_text(item) for item in raw_items]
            for term in terms:
                if term not in normalized_field:
                    continue
                matched_query_terms.add(term)
                if field in {"subjects", "actions", "ocr_text", "scene", "shot_type"}:
                    item_matches = [
                        raw_item
                        for raw_item, normalized_item in zip(raw_items, items)
                        if term in normalized_item
                    ]
                    field_matches.extend(item_matches or [term])
                else:
                    field_matches.append(term)
                exact = term in items
                field_score += weight * (1.0 if exact else 0.55)
            if normalized_query and normalized_query in normalized_field:
                field_score += weight * 0.9
            if field_matches:
                matched[field] = _unique(field_matches)
                raw_score += min(field_score, weight * 1.8)

        if not matched:
            return None

        raw_score += min(16.0, len(matched_query_terms) * 2.0)
        score = min(100, max(1, round(raw_score)))
        result = json.loads(row["vision_result_json"])
        matched_fields = [
            field for field in FIELD_WEIGHTS if field in matched
        ]
        return {
            "video_id": row["video_id"],
            "video_name": row["video_name"],
            "video_url": f"/media/videos/{row['stored_name']}",
            "frame_id": int(row["frame_id"]),
            "timestamp": row["timestamp_ms"] / 1000,
            "timestamp_ms": int(row["timestamp_ms"]),
            "thumbnail_url": (
                f"/media/frames/{row['video_id']}/{row['image_name']}"
            ),
            "summary": result.get("summary", ""),
            "subjects": result.get("subjects", []),
            "actions": result.get("actions", []),
            "scene": result.get("scene", []),
            "shot_type": result.get("shot_type", []),
            "ocr_text": result.get("ocr_text", []),
            "score": score,
            "matched_fields": matched_fields,
            "match_reason": self._match_reason(matched, matched_fields),
        }

    @staticmethod
    def _match_reason(matched: dict[str, list[str]], fields: list[str]) -> str:
        reasons = []
        for field in fields[:4]:
            if field in {"summary", "video_name", "time_text"}:
                reasons.append(f"{FIELD_LABELS[field]}包含相关内容")
                continue
            terms = "、".join(matched[field][:3])
            reasons.append(f"{FIELD_LABELS[field]}命中“{terms}”")
        return "，".join(reasons)
