from __future__ import annotations

from app.main import database


def main() -> None:
    database.initialize()
    count = database.rebuild_search_index()
    backend = "FTS5" if database.search_uses_fts5() else "普通全文匹配"
    print(f"搜索索引重建完成：{count} 张关键帧，后端：{backend}")


if __name__ == "__main__":
    main()

