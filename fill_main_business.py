#!/usr/bin/env python3
"""补充 data.json 中缺少的主营业务字段。"""
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings('ignore')

# 把项目根目录加入 sys.path，以便导入 update_data.py
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from update_data import fetch_main_business

DATA_FILE = ROOT / "data" / "data.json"

print(f"[*] 读取 {DATA_FILE}...")
with open(DATA_FILE, "r", encoding="utf-8") as f:
    payload = json.load(f)

records = payload["data"]
existing_codes = [r["code"] for r in records if r.get("main_business") and r["main_business"] != "-"]
missing_codes = [r["code"] for r in records if not r.get("main_business") or r["main_business"] == "-"]

print(f"[*] 已有主营业务: {len(existing_codes)} 条，待补充: {len(missing_codes)} 条")

if missing_codes:
    business_map = fetch_main_business(missing_codes)
    updated = 0
    for r in records:
        code = r["code"]
        if code in business_map:
            r["main_business"] = business_map[code]
            updated += 1
        elif not r.get("main_business"):
            r["main_business"] = "-"
    print(f"[OK] 更新 {updated} 条记录的主营业务")
else:
    print("[*] 无需补充")

payload["count"] = len(records)
with open(DATA_FILE, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)

print(f"[OK] 已保存到 {DATA_FILE}")
