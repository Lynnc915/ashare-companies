#!/usr/bin/env python3
"""
IPO 受理数据来源升级脚本。

读取 data/data.json 和 data/ipo_accepted.json：
1. 为每条 IPO 记录补充 source_name、source_url、prospectus_url_em、prospectus_source_name。
2. 对已上市企业，尝试从巨潮资讯网（CNInfo）获取更可靠的招股书 PDF 直链。
3. 未命中则保留东方财富 PDF 链接。

用法：
    python3 scripts/migrate_ipo_sources.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "data.json"
IPO_ACCEPTED_FILE = ROOT / "data" / "ipo_accepted.json"

CNINFO_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_PDF_BASE = "https://static.cninfo.com.cn"


def _get_source_url(exchange: str) -> str:
    """根據擬上市地點返回交易所公開審核入口 URL。"""
    mapping = {
        "科创板": "http://kcb.sse.com.cn/renewal/",
        "沪主板": "https://www.sse.com.cn/listing/renewal/ipo/",
        "深主板": "http://www.szse.cn/market/listing/ipo/",
        "创业板": "http://www.szse.cn/market/listing/ipo/",
        "北交所": "http://www.bse.cn/",
    }
    return mapping.get(exchange, "https://data.eastmoney.com/xg/ipo/")


def _get_cninfo_params(exchange: str) -> tuple[str, str, str]:
    """根據板塊返回 CNInfo 查詢參數。"""
    if "科创板" in exchange or "沪市" in exchange or exchange == "沪主板":
        return "sse", "sh", "category_szsh_all"
    if "创业板" in exchange or "深市" in exchange or exchange in ("深主板", "创业板"):
        return "szse", "sz", "category_szsh_all"
    if "北交所" in exchange:
        return "bjse", "bj", ""
    return "szse", "sz", "category_szsh_all"


def _pick_prospectus(announcements: list[dict]) -> dict | None:
    """從公告列表中挑選招股說明書。"""
    if not announcements:
        return None
    excludes = ("提示性公告", "摘要", "更正", "修订")
    for a in announcements:
        title = str(a.get("announcementTitle", ""))
        if "招股说明书" in title and not any(e in title for e in excludes):
            return a
    for a in announcements:
        if "招股说明书" in str(a.get("announcementTitle", "")):
            return a
    return announcements[0]


def _query_cninfo_prospectus(code: str, org_id: str, exchange: str) -> str | None:
    """通過 CNInfo 查詢招股說明書 PDF 直鏈。"""
    if not code or not org_id:
        return None
    column, plate, category = _get_cninfo_params(exchange)
    try:
        res = requests.post(
            CNINFO_QUERY_URL,
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
        picked = _pick_prospectus(announcements)
        adjunct = picked.get("adjunctUrl") if picked else None
        if adjunct:
            return f"{CNINFO_PDF_BASE}/{adjunct}"
    except Exception as e:
        print(f"[WARN] CNInfo 查询失败 {code}/{org_id}: {e}", file=sys.stderr)
    return None


def _build_listed_company_index(data_payload: dict) -> dict[str, tuple[str, str, str]]:
    """按企業全稱索引已上市企業的 (code, org_id, board)。"""
    index: dict[str, tuple[str, str, str]] = {}
    for item in data_payload.get("data", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        code = str(item.get("code", "")).strip()
        org_id = str(item.get("org_id", "")).strip()
        board = str(item.get("board", "")).strip()
        if org_id:
            index[name] = (code, org_id, board)
    return index


def main() -> None:
    print(f"[*] 读取 {DATA_FILE}")
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data_payload = json.load(f)

    listed_index = _build_listed_company_index(data_payload)
    print(f"[*] 已上市企业索引 {len(listed_index)} 条")

    print(f"[*] 读取 {IPO_ACCEPTED_FILE}")
    with open(IPO_ACCEPTED_FILE, "r", encoding="utf-8") as f:
        ipo_payload = json.load(f)

    records = ipo_payload.get("data", [])
    upgraded = 0
    fallback = 0
    no_prospectus = 0

    for i, record in enumerate(records, 1):
        exchange = record.get("exchange", "")
        record["source_url"] = _get_source_url(exchange)
        record["source_name"] = "东方财富 IPO 数据中心 / 巨潮资讯网 / 交易所公开审核信息"

        # 保留东方财富原始链接
        em_url = record.get("prospectus_url", "")
        if "prospectus_url_em" not in record:
            record["prospectus_url_em"] = em_url

        name = record.get("name", "")
        listed = listed_index.get(name)
        if listed:
            code, org_id, board = listed
            cninfo_url = _query_cninfo_prospectus(code, org_id, board)
            if cninfo_url:
                record["prospectus_url"] = cninfo_url
                record["prospectus_source_name"] = "巨潮资讯网"
                upgraded += 1
            else:
                record["prospectus_source_name"] = "东方财富" if em_url else "-"
                fallback += 1
        else:
            record["prospectus_source_name"] = "东方财富" if em_url else "-"
            no_prospectus += 1

        if i % 50 == 0 or i == len(records):
            print(f"  进度 {i}/{len(records)}，CNInfo 升级 {upgraded}，保留东方财富 {fallback + no_prospectus}")

    ipo_payload["update_time"] = datetime.now(timezone.utc).astimezone().isoformat()
    ipo_payload["source_name"] = "东方财富 IPO 数据中心 / 巨潮资讯网 / 交易所公开审核信息"
    ipo_payload["source_url"] = "https://data.eastmoney.com/xg/ipo/"
    ipo_payload["count"] = len(records)

    print(f"[*] 保存到 {IPO_ACCEPTED_FILE}")
    with open(IPO_ACCEPTED_FILE, "w", encoding="utf-8") as f:
        json.dump(ipo_payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 数据源升级完成：CNInfo {upgraded} 条，东方财富 {fallback + no_prospectus} 条")


if __name__ == "__main__":
    main()
