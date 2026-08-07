#!/usr/bin/env python3
"""
抓取港交所正在處理中的新上市申請數據，生成 data/hk_applications.json。

數據源：港交所披露易（HKEXnews）新上市資料進度報告 JSON 接口
- 主板：https://www1.hkexnews.hk/ncms/json/eds/appactive_app_sehk_c.json
- 創業板：https://www1.hkexnews.hk/ncms/json/eds/appactive_app_gem_c.json

返回字段：
- genDate: 數據生成時間戳
- uDate: 數據更新日期（DD/MM/YYYY）
- app: 申請列表，每條含申請日期、申請人名稱、狀態、相關文件鏈接等
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

warnings.filterwarnings("ignore")

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www1.hkexnews.hk/app/appindex.html",
})

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "hk_applications.json"

BASE_URL = "https://www1.hkexnews.hk"
JSON_PATHS = {
    "主板": "/ncms/json/eds/appactive_app_sehk_c.json",
    "创业板": "/ncms/json/eds/appactive_app_gem_c.json",
}


def parse_hkex_date(value: Any) -> str | None:
    """將 DD/MM/YYYY 轉為 YYYY-MM-DD。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def build_document_url(path: str | None) -> str | None:
    """補全港交所文件相對路徑為完整 URL。

    披露易上市申請頁面的基礎路徑為 /app/，因此相對路徑需解析為
    https://www1.hkexnews.hk/app/{path}。
    """
    if not path:
        return None
    p = str(path).strip()
    if not p:
        return None
    if p.startswith("http"):
        return p
    return f"{BASE_URL}/app/{p.lstrip('/')}"


def fetch_board_applications(board: str, path: str) -> list[dict[str, Any]]:
    """抓取某一板塊的上市申請數據。"""
    url = f"{BASE_URL}{path}"
    res = SESSION.get(url, timeout=60)
    res.raise_for_status()
    data = res.json()

    records: list[dict[str, Any]] = []
    for item in data.get("app", []):
        # 提取最新/主要的文檔鏈接
        docs: list[dict[str, Any]] = []
        for doc in item.get("ls", []):
            docs.append({
                "date": parse_hkex_date(doc.get("d")),
                "name": doc.get("nS1") or doc.get("nF"),
                "url": build_document_url(doc.get("u1") or doc.get("u2")),
            })

        records.append({
            "id": item.get("id"),
            "name": item.get("a"),
            "apply_date": parse_hkex_date(item.get("d")),
            "posting_date": item.get("postingDate"),
            "status": item.get("s"),
            "status_desc": "處理中" if item.get("s") == "A" else item.get("s"),
            "board": board,
            "warning_url": build_document_url(item.get("w")),
            "documents": [d for d in docs if d.get("url")],
            "has_phip": bool(item.get("hasPhip")),
        })
    return records


def main():
    parser = argparse.ArgumentParser(description="抓取港交所正在處理中的新上市申請")
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="僅保留申請日期在此日期之后的記錄（YYYY-MM-DD）",
    )
    args = parser.parse_args()

    since = parse_hkex_date(args.since) if args.since else None

    all_records: list[dict[str, Any]] = []
    update_date = None

    for board, path in JSON_PATHS.items():
        print(f"[*] 正在抓取 {board} 上市申請...")
        try:
            records = fetch_board_applications(board, path)
        except Exception as e:
            print(f"[WARN] {board} 抓取失敗: {e}", file=sys.stderr)
            continue
        print(f"[*] {board}: {len(records)} 條")
        all_records.extend(records)

        # 記錄更新日期
        if update_date is None:
            url = f"{BASE_URL}{path}"
            try:
                res = SESSION.get(url, timeout=60)
                data = res.json()
                update_date = data.get("uDate")
            except Exception:
                pass

    if since:
        all_records = [r for r in all_records if r.get("apply_date") and r["apply_date"] >= since]
        print(f"[*] 申請日期在 {since} 之后：{len(all_records)} 條")

    all_records.sort(key=lambda r: r["apply_date"] or "", reverse=True)

    main_count = sum(1 for r in all_records if r["board"] == "主板")
    gem_count = sum(1 for r in all_records if r["board"] == "创业板")
    print(f"[*] 主板: {main_count}，创业板: {gem_count}，合計: {len(all_records)}")

    payload = {
        "update_time": datetime.now(timezone.utc).astimezone().isoformat(),
        "source_date": update_date,
        "count": len(all_records),
        "source_name": "香港交易所披露易（HKEXnews）",
        "source_url": "https://www1.hkexnews.hk/app/appindex.html",
        "data": all_records,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已保存 {len(all_records)} 條記錄到 {DATA_FILE}")


if __name__ == "__main__":
    main()
