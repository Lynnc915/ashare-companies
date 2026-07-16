#!/usr/bin/env python3
from __future__ import annotations

"""
为现有 data.json 补全/优化保荐机构字段。

策略：
1. 先用 akshare 的 IPO 审核全量数据做精确名称匹配。
2. 精确未命中的，对企业名称做规范化（去掉 N/C 前缀、括号内容、股份/有限公司等后缀）
   后与注册名单进行模糊匹配（相似度 ≥ 0.8）。
3. 仍缺失的保持为 "-"。

注意：这里拿到的是“保荐机构”，与上市公司的持续督导保荐机构可能不同；
      若需要 100% 准确的持续督导保荐机构，需要购买 Wind/Choice 等付费数据。
"""

import json
import re
from difflib import SequenceMatcher
from pathlib import Path

import akshare as ak

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "data.json"


def normalize_name(name: str) -> str:
    name = str(name).strip()
    name = re.sub(r"^[NC]\s*", "", name)
    name = re.sub(r"[（(].*?[）)]", "", name)
    for suffix in ("股份有限公司", "有限公司", "集团公司", "集团", "股份"):
        name = name.replace(suffix, "")
    return name.strip()


def is_valid_sponsor(v: str) -> bool:
    if not v:
        return False
    v = str(v).strip().lower()
    return v not in {"-", "none", "nan", "null", ""}


def build_register_index() -> tuple[dict[str, str], list[tuple[str, str]]]:
    """从东方财富 IPO 审核数据构建保荐机构索引。"""
    print("[*] 正在抓取 IPO 审核全量数据用于保荐机构匹配...")
    df = ak.stock_register_all_em()
    exact_map = {}
    normalized_entries = []
    for _, row in df.iterrows():
        name = str(row.get("企业名称", "")).strip()
        sponsor = str(row.get("保荐机构", "")).strip()
        if not name:
            continue
        if is_valid_sponsor(sponsor):
            exact_map[name] = sponsor
        normalized_entries.append((normalize_name(name), name))
    print(f"[OK] 注册数据共 {len(normalized_entries)} 条，其中含保荐机构 {len(exact_map)} 条")
    return exact_map, normalized_entries


def find_sponsor(name: str, exact_map: dict[str, str], entries: list[tuple[str, str]]) -> str | None:
    if not name:
        return None

    # 精确匹配
    if name in exact_map:
        return exact_map[name]

    # 规范化后模糊匹配
    nn = normalize_name(name)
    if not nn or not entries:
        return None

    best_orig = None
    best_ratio = 0.0
    for norm_name, orig_name in entries:
        ratio = SequenceMatcher(None, nn, norm_name).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_orig = orig_name

    if best_orig and best_ratio >= 0.8:
        return exact_map.get(best_orig)

    return None


def main():
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"找不到 {DATA_FILE}，请先运行 update_data.py")

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    if not records:
        print("[WARN] data.json 中无记录")
        return

    exact_map, entries = build_register_index()

    matched = 0
    for record in records:
        sponsor = find_sponsor(record.get("name", ""), exact_map, entries)
        if sponsor:
            record["sponsor"] = sponsor
            matched += 1
        else:
            record["sponsor"] = "-"

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 已更新 {len(records)} 条记录，其中 {matched} 条匹配到保荐机构 ({matched / len(records) * 100:.1f}%)")


if __name__ == "__main__":
    main()
