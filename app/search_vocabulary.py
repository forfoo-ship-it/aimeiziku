from __future__ import annotations

import json
import re
from pathlib import Path


DEFAULT_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
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
    ("展示牌", "展牌", "展示板", "展板"),
)


def normalize_vocabulary_text(value: str) -> str:
    return "".join(re.findall(r"[\w\u3400-\u9fff]+", value.lower()))


def load_synonym_groups() -> tuple[tuple[str, ...], ...]:
    path = Path(__file__).with_name("search_synonyms.json")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        groups = []
        for group in payload:
            cleaned = tuple(
                dict.fromkeys(
                    word.strip()
                    for word in group
                    if isinstance(word, str) and len(word.strip()) >= 2
                )
            )
            if len(cleaned) >= 2:
                groups.append(cleaned)
        return tuple(groups) or DEFAULT_SYNONYM_GROUPS
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return DEFAULT_SYNONYM_GROUPS


SYNONYM_GROUPS = load_synonym_groups()


def expand_index_values(
    original_values: list[object], model_aliases: list[object] | None = None
) -> list[str]:
    values = [
        str(value).strip()
        for value in [*original_values, *(model_aliases or [])]
        if str(value).strip()
    ]
    expanded = list(values)
    for value in values:
        normalized_value = normalize_vocabulary_text(value)
        for group in SYNONYM_GROUPS:
            if any(
                normalize_vocabulary_text(alias) in normalized_value
                for alias in group
            ):
                expanded.extend(group)
    return list(dict.fromkeys(expanded))
