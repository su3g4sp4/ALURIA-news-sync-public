#!/usr/bin/env python3
"""模型榜新鮮度檢查：5 個榜與每個模型詳情必須是同一份快照（同一個版本、同一個同步時間）。任何不一致就退出碼 1。

用法：
  python3 scripts/check-lb-snapshot.py --rows rows.tsv        # 檢查資料庫（每次同步後在 GitHub Actions 跑）
  python3 scripts/check-lb-snapshot.py https://aluria-web.vercel.app   # 檢查線上 API 實際回給頁面的內容
"""
import json, sys, urllib.request
BOARDS = ["overall", "coding", "reasoning", "professional", "knowledge"]

def check(meta, boards, models):
    errs = []
    v, t = meta.get("version"), meta.get("syncedAt")
    if not v or not t: errs.append("lb-meta 缺 version／syncedAt")
    for k in BOARDS:
        b = boards.get(k)
        if not b: errs.append(f"缺榜單 {k}"); continue
        if b.get("version") != v or b.get("syncedAt") != t: errs.append(f"榜單 {k}: {b.get('version')} {b.get('syncedAt')} ≠ {v} {t}")
    for slug in meta.get("models") or []:
        d = models.get(slug)
        if not d: errs.append(f"缺模型詳情 {slug}"); continue
        dv = d.get("version", d.get("runId")); dt = d.get("syncedAt", t)
        if dv != v or dt != t: errs.append(f"模型 {slug}: {dv} {dt} ≠ {v} {t}")
    return errs

def from_rows(path):
    rows = {}
    for line in open(path, encoding="utf-8"):
        if "\t" in line: k, d = line.rstrip("\n").split("\t", 1); rows[k] = json.loads(d)
    meta = rows.get("lb-meta") or {}
    return meta, {k: rows.get(f"lb-{k}") for k in BOARDS}, {s: rows.get(f"lb-model-{s}") for s in meta.get("models") or []}

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "AINEWS-check/1.0", "Origin": "https://aluria-web.vercel.app"}), timeout=20) as r: return json.loads(r.read())

def from_api(base):
    boards = {k: get(f"{base}/api/site/models-board/{k}") for k in BOARDS}
    o = boards["overall"]
    meta = {"version": o.get("version"), "syncedAt": o.get("syncedAt"), "models": sorted({e["model"]["slug"] for b in boards.values() for e in b["entries"] + b["more"] + b.get("pending", [])})}
    models = {s: get(f"{base}/api/site/models-detail/{s}") for s in meta["models"]}
    return meta, boards, models

if __name__ == "__main__":
    meta, boards, models = from_rows(sys.argv[2]) if sys.argv[1] == "--rows" else from_api(sys.argv[1].rstrip("/"))
    errs = check(meta, boards, models)
    for e in errs: print("✗", e)
    if errs: sys.exit(1)
    print(f"✓ 5 個榜 + {len(meta.get('models') or [])} 個模型詳情同一份快照：version={meta['version']} syncedAt={meta['syncedAt']}")
