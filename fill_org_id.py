#!/usr/bin/env python3
"""
为现有 data.json 补充 cninfo 的 orgId，用于直接跳转到个股公告页面。

通过 CNInfo 的 topSearch 接口查询：
    POST http://www.cninfo.com.cn/new/information/topSearch/query
    keyWord=股票代码

返回中的 orgId 用于构造个股披露页面：
    https://www.cninfo.com.cn/new/disclosure/stock?stockCode=CODE&orgId=ORGID
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "data.json"
SEARCH_URL = "http://www.cninfo.com.cn/new/information/topSearch/query"


def fetch_org_id(code: str) -> tuple[str, str | None]:
    code = code.zfill(6)
    try:
        res = requests.post(
            SEARCH_URL,
            data={"keyWord": code},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
        if isinstance(data, list) and data:
            # 可能有多个结果，取代码完全匹配的
            for item in data:
                if str(item.get("code", "")).zfill(6) == code:
                    return code, item.get("orgId")
            # 没有完全匹配则取第一个
            return code, data[0].get("orgId")
    except Exception as e:
        print(f"[WARN] {code} 查询 orgId 失败: {e}", file=sys.stderr)
    return code, None


def main():
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"找不到 {DATA_FILE}")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    codes = [r["code"] for r in records]
    if not codes:
        print("[WARN] 没有记录需要更新")
        return

    org_map = {}
    print(f"[*] 正在查询 {len(codes)} 家企业的 orgId...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_code = {executor.submit(fetch_org_id, c): c for c in codes}
        for i, future in enumerate(concurrent.futures.as_completed(future_to_code), 1):
            code, org_id = future.result()
            if org_id:
                org_map[code] = org_id
            if i % 100 == 0 or i == len(codes):
                print(f"  进度 {i}/{len(codes)}，已获取 {len(org_map)} 条 orgId")

    matched = 0
    for record in records:
        org_id = org_map.get(record["code"].zfill(6))
        if org_id:
            record["org_id"] = org_id
            matched += 1
        else:
            record["org_id"] = ""

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已更新 {len(records)} 条记录，其中 {matched} 条匹配到 orgId ({matched / len(records) * 100:.1f}%)")


if __name__ == "__main__":
    main()
