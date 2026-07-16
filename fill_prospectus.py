#!/usr/bin/env python3
"""
为现有 data.json 补全巨潮招股说明书 PDF 直链。

逻辑：
1. 用已有的 org_id 和板块，调用 CNInfo hisAnnouncement/query 接口查询标题含“招股说明书”的公告。
2. 过滤掉“提示性公告”“摘要”等干扰项，取最匹配的招股说明书公告。
3. 将 adjunctUrl 拼接成 https://static.cninfo.com.cn/{adjunctUrl} 存为 prospectus_url。

这样前端点击“查找招股说明书”会直接打开巨潮 PDF，而不是可能空白的搜索页或个股页。
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "data.json"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "https://static.cninfo.com.cn"


def get_board_params(board: str) -> tuple[str, str, str]:
    """根据板块返回 CNInfo 查询参数 (column, plate, category)。"""
    if "科创板" in board or "沪市" in board:
        return "sse", "sh", "category_szsh_all"
    if "创业板" in board or "深市" in board:
        return "szse", "sz", "category_szsh_all"
    if "北交所" in board:
        return "bjse", "bj", ""
    # 默认按深市处理
    return "szse", "sz", "category_szsh_all"


def pick_prospectus(announcements: list[dict]) -> dict | None:
    if not announcements:
        return None

    # 优先选标题明确为招股说明书的，排除提示性公告、摘要、更正
    excludes = ("提示性公告", "摘要", "更正", "修订")
    for a in announcements:
        title = str(a.get("announcementTitle", ""))
        if "招股说明书" in title and not any(e in title for e in excludes):
            return a

    #  fallback：只要有招股说明书字样
    for a in announcements:
        if "招股说明书" in str(a.get("announcementTitle", "")):
            return a

    return announcements[0]


def fetch_prospectus(record: dict) -> tuple[str, str | None]:
    code = str(record.get("code", "")).zfill(6)
    org_id = record.get("org_id", "")
    board = record.get("board", "")

    if not org_id:
        return code, None

    column, plate, category = get_board_params(board)

    try:
        res = requests.post(
            QUERY_URL,
            data={
                "pageNum": "1",
                "pageSize": "30",
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": f"{code},{org_id}",
                "searchkey": "招股说明书",
                "seDate": "",
                "category": category,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        res.raise_for_status()
        data = res.json()
        announcements = data.get("announcements") or []
        if not announcements:
            return code, None

        picked = pick_prospectus(announcements)
        adjunct = picked.get("adjunctUrl") if picked else None
        if adjunct:
            return code, f"{PDF_BASE}/{adjunct}"
    except Exception as e:
        print(f"[WARN] {code} 招股说明书查询失败: {e}", file=sys.stderr)

    return code, None


def main():
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"找不到 {DATA_FILE}")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    if not records:
        print("[WARN] data.json 中没有记录")
        return

    matched = 0
    print(f"[*] 正在查询 {len(records)} 家企业的招股说明书 PDF...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_record = {executor.submit(fetch_prospectus, r): r for r in records}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_record), 1):
            code, pdf_url = future.result()
            if pdf_url:
                for r in records:
                    if r["code"].zfill(6) == code:
                        r["prospectus_url"] = pdf_url
                        matched += 1
                        break
            if i % 100 == 0 or i == len(records):
                print(f"  进度 {i}/{len(records)}，已匹配 {matched} 条")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已更新 {len(records)} 条记录，其中 {matched} 条匹配到招股说明书 PDF ({matched / len(records) * 100:.1f}%)")


if __name__ == "__main__":
    main()
