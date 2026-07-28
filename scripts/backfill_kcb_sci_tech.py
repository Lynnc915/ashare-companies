#!/usr/bin/env python3
"""
科创板 IPO 受理企业科创属性指标回填脚本。

读取现有 data/ipo_accepted.json，仅对 exchange == "科创板" 的记录
重新提取招股说明书中的科创属性指标，写回 JSON。

用法：
    python3 scripts/backfill_kcb_sci_tech.py
    python3 scripts/backfill_kcb_sci_tech.py --limit 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# 把项目根目录加入模块路径
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from update_data import extract_kcb_sci_tech  # noqa: E402

IPO_ACCEPTED_FILE = ROOT / "data" / "ipo_accepted.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="回填科创板 IPO 科创属性指标")
    parser.add_argument("--limit", type=int, default=None, help="仅处理前 N 条（用于测试）")
    parser.add_argument("--output", type=Path, default=IPO_ACCEPTED_FILE, help="输出文件路径")
    args = parser.parse_args()

    print(f"[*] 读取 {IPO_ACCEPTED_FILE}")
    with open(IPO_ACCEPTED_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    records = payload.get("data", [])
    kcb_records = [r for r in records if r.get("exchange") == "科创板" and r.get("prospectus_url")]
    if args.limit:
        kcb_records = kcb_records[: args.limit]

    print(f"[*] 共 {len(records)} 条 IPO 记录，其中科创板 {len(kcb_records)} 条需要回填")

    success = 0
    failed = 0
    for i, record in enumerate(kcb_records, 1):
        url = record.get("prospectus_url")
        name = record.get("name", "未知")
        sci_tech = extract_kcb_sci_tech(url)
        record["sci_tech"] = sci_tech
        if sci_tech and sci_tech.get("metrics"):
            success += 1
        else:
            failed += 1
        if i % 10 == 0 or i == len(kcb_records):
            print(f"  进度 {i}/{len(kcb_records)}，成功 {success}，失败 {failed}")

    payload["update_time"] = datetime.now(timezone.utc).astimezone().isoformat()
    payload["count"] = len(records)

    print(f"[*] 保存到 {args.output}")
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"[OK] 回填完成，成功 {success}，失败 {failed}")


if __name__ == "__main__":
    main()
