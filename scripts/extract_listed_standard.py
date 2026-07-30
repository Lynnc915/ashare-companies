#!/usr/bin/env python3
"""
为已上市企业提取招股说明书中申报的上市标准。

读取 data/data.json，对存在 prospectus_url 的记录，按所属板块调用
update_data._extract_listing_standard 识别适用的第 x 套上市标准，
将结果写入 data.json。

采用两阶段 + 多线程策略：先用 120 页快速识别，再对失败记录用 250 页重试。

用法：
    python3 scripts/extract_listed_standard.py
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from update_data import PDF_SESSION, extract_pdf_text, _extract_listing_standard  # noqa: E402

DATA_FILE = ROOT / "data" / "data.json"
PRINT_LOCK = threading.Lock()


def safe_print(msg: str) -> None:
    with PRINT_LOCK:
        print(msg)


def extract_one(record: dict, max_pages: int) -> dict | None:
    """处理单条记录，返回识别结果或 None。"""
    url = record["prospectus_url"]
    board = record.get("board") or "科创板"
    res = PDF_SESSION.get(url, timeout=(10, 30))
    res.raise_for_status()
    text = extract_pdf_text(res.content, max_pages=max_pages)
    return _extract_listing_standard(text, board=board)


def phase_one_worker(args: tuple[int, dict, int]) -> tuple[int, dict, dict | None, str]:
    """阶段 1 工作线程。"""
    i, record, total = args
    name = record.get("name", "")
    board = record.get("board", "")

    existing = record.get("listing_standard_detected")
    if existing and existing.get("standard"):
        safe_print(f"  进度 {i}/{total} - {name} ({board}) SKIP")
        return i, record, existing, "skip"

    std = None
    status = "fail"
    try:
        std = extract_one(record, max_pages=120)
        if std:
            status = "ok"
    except Exception as e:
        safe_print(f"[WARN] 处理 {name} ({board}) 失败: {e}")

    record["listing_standard_detected"] = std if std else None
    safe_print(f"  进度 {i}/{total} - {name} ({board}) {status.upper()}")
    return i, record, std, status


def phase_two_worker(args: tuple[int, dict, int]) -> tuple[int, dict, dict | None, str]:
    """阶段 2 工作线程：对阶段 1 失败记录重试。"""
    i, record, total = args
    name = record.get("name", "")
    board = record.get("board", "")

    std = None
    status = "fail"
    try:
        std = extract_one(record, max_pages=250)
        if std:
            status = "ok"
    except Exception as e:
        safe_print(f"[WARN] 重试 {name} ({board}) 失败: {e}")

    if std:
        record["listing_standard_detected"] = std

    safe_print(f"  重试 {i}/{total} - {name} ({board}) {status.upper()}")
    return i, record, std, status


def main() -> None:
    parser = argparse.ArgumentParser(description="为已上市企业提取招股说明书中申报的上市标准")
    parser.add_argument(
        "--board",
        default=None,
        help="只处理指定板块（如 科创板），默认处理所有板块",
    )
    args = parser.parse_args()

    print(f"[*] 读取 {DATA_FILE}")
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    target_records = [r for r in records if r.get("prospectus_url")]
    if args.board:
        target_records = [r for r in target_records if r.get("board") == args.board]
    total = len(target_records)
    print(f"[*] 共 {len(records)} 条已上市企业，其中存在 prospectus_url 且板块为 {args.board or '全部'} 的 {total} 条")

    # 阶段 1：快速识别（120 页，5 线程）
    print("[*] 阶段 1：快速识别（120 页，5 线程）")
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(phase_one_worker, (i, r, total)): (i, r)
            for i, r in enumerate(target_records, 1)
        }
        for future in as_completed(futures):
            future.result()

    # 阶段 2：对阶段 1 失败的记录增加页数重试
    retry_targets = [r for r in target_records if not (r.get("listing_standard_detected") or {}).get("standard")]
    if retry_targets:
        print(f"[*] 阶段 2：对 {len(retry_targets)} 条失败记录增加页数重试（250 页，5 线程）")
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {
                executor.submit(phase_two_worker, (i, r, len(retry_targets))): (i, r)
                for i, r in enumerate(retry_targets, 1)
            }
            for future in as_completed(futures):
                future.result()

    # 统计
    success = sum(1 for r in target_records if (r.get("listing_standard_detected") or {}).get("standard"))
    failed = sum(1 for r in target_records if not (r.get("listing_standard_detected") or {}).get("standard"))
    already_has = sum(1 for r in target_records if r.get("listing_standard_detected") and r["listing_standard_detected"].get("standard"))

    payload["update_time"] = datetime.now(timezone.utc).astimezone().isoformat()

    print(f"[*] 保存到 {DATA_FILE}")
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 完成，成功识别 {success}，失败 {failed}，原本已有 {already_has}")


if __name__ == "__main__":
    main()
