#!/usr/bin/env python3
"""Aluria：鏡像 AIHOT 公開的 Tibo 重置監控資料（https://aihot.news/codex-reset）。

來源是該頁自己用的公開 JSON API（/api/site/codex-reset 與 /days/<date>），不解析 HTML。
轉成台灣用語（OpenCC s2twp）後寫到 data/tibo/aihot-snapshot.json。
任何一步失敗（網路、HTTP 非 200、JSON 結構不對）就不覆寫，保留上一份好的快照，退出碼 1。
用法：python3 scripts/fetch-aihot-tibo.py   （建議每 10 分鐘排程一次）
"""
import json, os, sys, time, tempfile, urllib.request
from datetime import date, timedelta, datetime, timezone

import opencc

BASE = os.environ.get("TIBO_BASE", "https://aihot.news/api/site/codex-reset")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("TIBO_SNAPSHOT", os.path.join(ROOT, "data/tibo/aihot-snapshot.json"))
UA = "AluriaMirror/1.0 (+zh-TW fork of AIHOT, MIT; credits aihot.news)"
cc = opencc.OpenCC("s2twp")

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200: raise RuntimeError(f"{url} -> HTTP {r.status}")
        return json.loads(r.read().decode("utf-8"))

def tw(v):
    """遞迴轉繁中；英文原文、網址、ID 不受影響（OpenCC 只動中文字）。北京時間與台灣時間同為 UTC+8。"""
    if isinstance(v, str):
        out = cc.convert(v)
        for a, b in (("北京時間", "台灣時間"), ("北京时间", "台灣時間"), ("北京", "台北"), ("報道", "報導"), ("飛書", "通知群"), ("髮卡", "發卡")):
            out = out.replace(a, b)
        return out
    if isinstance(v, list): return [tw(x) for x in v]
    if isinstance(v, dict): return {k: (x if k in ("url", "id", "eventId", "authorAvatar") else tw(x)) for k, x in v.items()}
    return v

def main():
    old = None
    if os.path.exists(OUT):
        try: old = json.load(open(OUT, encoding="utf-8"))
        except Exception: old = None
    page = get(BASE)
    for k in ("stats", "calendar", "lastLanded", "version"):
        if k not in page: raise RuntimeError(f"缺少欄位 {k}，AIHOT API 結構可能改了")
    # 每天的明細：3 天前以前的日子沿用快取（AIHOT 很少回頭改舊日子）；最近 3 天每次都重抓。
    cached = (old or {}).get("days", {})
    recent = {(date.fromisoformat(page["today"]) - timedelta(days=i)).isoformat() for i in range(3)}
    days = {}
    for d in sorted({c["date"] for c in page["calendar"]}):
        if d in cached and d not in recent:
            days[d] = tw(cached[d]); continue  # 舊快取也重跑一次轉換，規則改了也會套用
        days[d] = tw(get(f"{BASE}/days/{d}")); time.sleep(0.4)
    snap = {
        "source": "AIHOT（aihot.news/codex-reset）公開 API 鏡像",
        "sourceUrl": "https://aihot.news/codex-reset",
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "upstreamVersion": page["version"],
        "page": {**tw({k: v for k, v in page.items() if k != "authorAvatar"}), "timezone": "Asia/Taipei"},
        "days": days,
    }
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(OUT), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(snap, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    print(f"ok version={page['version']} calendar={len(page['calendar'])} days={len(days)} last={page['stats'].get('lastResetDate')}")

if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    try: main()
    except Exception as e:
        print(f"抓取失敗，保留上一份快照：{e}", file=sys.stderr); sys.exit(1)
