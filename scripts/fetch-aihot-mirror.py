#!/usr/bin/env python3
"""AI熱點：鏡像 AIHOT 公開 API（https://aihot.news/openapi-v1.json）的精選、熱榜與日／週／月報。

作者已同意使用（使用者 2026-10-17 確認）。每個鍵各自驗證：成功才寫 data/mirror/<key>.json，
失敗的鍵不寫 → 存庫步驟不會動到它，網站沿用 Neon 裡上一份好的。任何鍵失敗時退出碼 2（存庫後再讓工作標紅）。
文字轉台灣用語（OpenCC s2tw）；時間保留 UTC ISO，網站顯示時換成台灣時間（UTC+8，與北京同時區，日期鍵不變）。
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone
import opencc

BASE = os.environ.get("AIHOT_API", "https://aihot.news/api/v1")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data/mirror")
# 模型榜整份快照先寫到暫存資料夾，全部抓齊、版本一致才一起搬進 OUT（存庫步驟在同一個交易裡寫入）。
LBTMP = os.path.join(ROOT, "data/mirror-lb.tmp")
UA = "AINEWSMirror/1.0 (+zh-TW fork of AIHOT, MIT; credits aihot.news)"
cc = opencc.OpenCC("s2tw")
KEEP = {"id", "links", "url", "date", "category", "nextCursor", "key", "week", "month"}

def get(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200: raise RuntimeError(f"{path} -> HTTP {r.status}")
        return json.loads(r.read().decode("utf-8"))

def tw(v, k=None):
    if k in KEEP or (isinstance(k, str) and k.endswith("At")): return v
    if isinstance(v, str): return cc.convert(v).replace("北京時間", "台灣時間").replace("報道", "報導")
    if isinstance(v, list): return [tw(x) for x in v]
    if isinstance(v, dict): return {kk: tw(x, kk) for kk, x in v.items()}
    return v

def items():
    out, cursor = [], None
    for _ in range(2):
        d = get("/items?mode=selected&window=7d&limit=100" + (f"&cursor={cursor}" if cursor else ""))
        if not isinstance(d.get("items"), list): raise RuntimeError("items: 結構不對")
        out += d["items"]; cursor = (d.get("page") or {}).get("nextCursor")
        if not cursor: break
        time.sleep(0.2)
    if not out: raise RuntimeError("items: 空的")
    return {"items": out}

def need(path, field):
    d = get(path)
    if field not in d or not d[field]: raise RuntimeError(f"{path}: 缺 {field}")
    return d

LB = os.environ.get("AIHOT_LB", "https://aihot.news/api/site/leaderboard/boards")
LB_BOARDS = ("overall", "coding", "reasoning", "professional", "knowledge")

LB_ROOT = LB.rsplit("/boards", 1)[0]
def scrub(t):
    """上游說明文字轉繁中，並拿掉上游站名。"""
    if not isinstance(t, str): return t
    t = cc.convert(t)
    for a, b in (("AIHOT 評分", "綜合分數"), ("AIHOT評分", "綜合分數"), ("AIHOT 按", "本站按"), ("AIHOT", "本站")): t = t.replace(a, b)
    return t

def lb_get(path):
    req = urllib.request.Request(LB_ROOT + path, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200: raise RuntimeError(f"lb {path} -> HTTP {r.status}")
        return json.loads(r.read().decode("utf-8"))

def lb_details(boards, run):
    """模型詳情與各項評測（寫進 LBTMP）。每個模型都必須和榜單同一輪，否則整份放棄。"""
    src = lb_get("/sources")
    sources = {}
    for g in src.get("groups") or []:
        for x in g.get("sources") or []:
            sources[x["key"]] = {"name": x.get("name"), "operator": x.get("operator"), "status": x.get("status"), "group": g.get("key"), "description": scrub(x.get("description")), "count": None}
    def count(k):
        try:
            d = lb_get(f"/sources/{k}"); sources[k]["count"] = len(d.get("rows") or [])
        except Exception as e: print(f"warn source {k}: {e}", file=sys.stderr)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=3) as ex: list(ex.map(count, list(sources)))
    slugs = []
    for b in boards.values():
        for e in b["entries"] + b["more"] + b["pending"]:
            if e["model"]["slug"] not in slugs: slugs.append(e["model"]["slug"])
    def model(slug):
        d = lb_get(f"/models/{slug}"); m = d.get("model") or {}
        if (d.get("run") or {}).get("id") != run: raise RuntimeError(f"{slug}: 上游輪次不一致（抓取途中換輪了）")
        rate = ((d.get("run") or {}).get("fx") or {}).get("rate")
        p = d.get("price"); price = None
        if p and p.get("currency") == "USD": price = {"input": p.get("input"), "output": p.get("output"), "cached": p.get("cached"), "converted": False}
        elif p and p.get("currency") == "CNY" and rate:
            c = lambda v: None if v is None else round(v / rate, 4)
            price = {"input": c(p.get("input")), "output": c(p.get("output")), "cached": c(p.get("cached")), "converted": True}
        o = d.get("overall") or {}
        out = {"runId": run, "generatedAt": (d.get("run") or {}).get("generatedAt"),
               "model": {"slug": slug, "name": m.get("name"), "provider": m.get("provider"), "releasedAt": m.get("releasedAt"), "contextWindow": m.get("contextWindowTokens"),
                         "openWeights": m.get("weightsUrl"), "mark": (m.get("brand") or {}).get("src", "").rsplit("/", 1)[-1] or None, "monogram": (m.get("brand") or {}).get("monogram")},
               "price": price, "metricCount": d.get("metricCount"),
               "overall": {"rank": o.get("rank"), "score": o.get("score"), "coverage": o.get("coverage"), "pendingReason": scrub(o.get("pendingReason"))},
               "categories": [{"key": c["key"], "rank": c.get("rank"), "score": c.get("score"), "sourceCount": c.get("sourceCount"), "onBoard": c.get("onBoard")} for c in d.get("categories") or []],
               "evidence": [{"key": g["key"], "score": g.get("score"), "items": [{"source": i.get("sourceKey"), "name": i.get("name"), "operator": i.get("operator"), "display": i.get("display"),
                              "rank": i.get("sourceRank"), "count": (sources.get(i.get("sourceKey")) or {}).get("count"), "config": scrub(i.get("configurationLabel"))} for i in g.get("items") or []]} for g in d.get("evidence") or []],
               "excluded": [{"source": x.get("key"), "name": x.get("name"), "reason": scrub(x.get("reason"))} for x in d.get("excluded") or []]}
        with open(os.path.join(LBTMP, f"lb-model-{slug}.json"), "w", encoding="utf-8") as fh: json.dump(out, fh, ensure_ascii=False)
        time.sleep(0.2)
    with ThreadPoolExecutor(max_workers=3) as ex: list(ex.map(model, slugs))
    with open(os.path.join(LBTMP, "lb-sources.json"), "w", encoding="utf-8") as fh: json.dump({"runId": run, "sources": sources}, fh, ensure_ascii=False)
    print(f"ok lb details: {len(slugs)} models, {len(sources)} sources")

def lb_board(key):
    """模型榜：只留數字與模型資料（名次、分數、評測數、覆蓋率、官方價格、開源權重），不帶上游的說明文字。
    價格一律換成美元／每百萬 token：原本就是美元的照用；人民幣定價用同一輪的歐洲央行匯率換算並標 converted。"""
    req = urllib.request.Request(f"{LB}/{key}", headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status != 200: raise RuntimeError(f"lb {key} -> HTTP {r.status}")
        d = json.loads(r.read().decode("utf-8"))
    rate = ((d.get("run") or {}).get("fx") or {}).get("rate")
    def price(p):
        if not p: return None
        cur = p.get("currency")
        if cur == "USD": return {"input": p.get("input"), "output": p.get("output"), "cached": p.get("cached"), "converted": False}
        if cur == "CNY" and rate:
            c = lambda v: None if v is None else round(v / rate, 4)
            return {"input": c(p.get("input")), "output": c(p.get("output")), "cached": c(p.get("cached")), "converted": True}
        return None
    def entry(e):
        m = e["model"]
        return {"rank": e["rank"], "score": e["score"], "sourceCount": e.get("sourceCount"), "coverage": e.get("coverage"),
                "model": {"slug": m["slug"], "name": m["name"], "provider": m.get("provider"), "releasedAt": m.get("releasedAt"),
                          "mark": (m.get("brand") or {}).get("src", "").rsplit("/", 1)[-1] or None, "monogram": (m.get("brand") or {}).get("monogram")},
                "price": price(e.get("price")), "openWeights": (e.get("access") or {}).get("weightsUrl"), "china": bool((e.get("access") or {}).get("domestic"))}
    entries = [entry(e) for e in d.get("entries") or []]
    if len(entries) < 5: raise RuntimeError(f"lb {key}: 名單太短")
    b = d.get("board") or {}
    return {"key": key, "generatedAt": (d.get("run") or {}).get("generatedAt"), "sourceCount": b.get("sourceCount"), "operatorCount": b.get("operatorCount"),
            "fxRate": rate, "entries": entries, "more": [entry(e) for e in d.get("filterEntries") or []],
            "pending": [{"model": {"slug": p["model"]["slug"], "name": p["model"]["name"], "provider": p["model"].get("provider"), "releasedAt": p["model"].get("releasedAt"),
                                   "mark": (p["model"].get("brand") or {}).get("src", "").rsplit("/", 1)[-1] or None, "monogram": (p["model"].get("brand") or {}).get("monogram")},
                         "score": p.get("score"), "sourceCount": p.get("sources"), "reason": scrub(p.get("reason"))} for p in d.get("pending") or []],
            "runId": (d.get("run") or {}).get("id")}

TPE = 8 * 3600
def tpe_day(iso):
    return datetime.fromtimestamp(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() + TPE, timezone.utc).strftime("%Y-%m-%d")

HAVE = set(filter(None, os.environ.get("MIRROR_HAVE", "").split(",")))

def slim(i):
    """全部動態一條只留網站用得到的欄位（省 Neon 空間與傳輸）。"""
    return {k: i.get(k) for k in ("id", "title", "originalTitle", "summary", "source", "publishedAt", "discoveredAt", "category", "score", "selected", "reason")} | {"links": {"original": (i.get("links") or {}).get("original")}}

def all_days():
    """全部 AI 動態（mode=all，每天約 200 條），按台灣日期分鍵 all-YYYY-MM-DD。
    平常只往回翻到「昨天 0 點」為止（約 4–5 個請求）；近 7 天有哪天還沒存過才整段翻完 7 天（約 15 個請求）。
    只寫完整翻到的日子：今天（進行中）和翻過其 0 點的日子。"""
    now = time.time()
    days = [datetime.fromtimestamp(now + TPE - 86400 * k, timezone.utc).strftime("%Y-%m-%d") for k in range(7)]
    backfill = any(f"all-{d}" not in HAVE for d in days[1:6])
    stop = days[5] if backfill else days[1]
    out, cursor, reached = [], None, False
    for _ in range(25):
        d = get("/items?mode=all&window=7d&limit=100" + (f"&cursor={cursor}" if cursor else ""))
        page = d.get("items")
        if not isinstance(page, list): raise RuntimeError("all: 結構不對")
        out += page; cursor = (d.get("page") or {}).get("nextCursor")
        if page and min(tpe_day(i.get("discoveredAt") or i["publishedAt"]) for i in page) < stop: reached = True; break
        if not cursor: break
        time.sleep(0.15)
    if not out: raise RuntimeError("all: 空的")
    groups = {}
    for i in out: groups.setdefault(tpe_day(i.get("discoveredAt") or i["publishedAt"]), []).append(slim(i))
    res = {}
    for day, xs in groups.items():
        if day < stop or (day == stop and not reached): continue  # 沒翻完整的日子不覆蓋
        xs.sort(key=lambda i: i.get("discoveredAt") or i["publishedAt"], reverse=True)
        res[f"all-{day}"] = {"date": day, "items": xs}
    return res

def story_uuid(h):
    return ((h.get("links") or {}).get("story") or "").rstrip("/").rsplit("/", 1)[-1]

# 熱榜事件的「同一故事線／相關事件」最多再存幾個（事件頁的相關事件才點得開）；每個只留最近 50 篇報導。
NEIGHBORS = 10

def stories():
    """熱榜每個事件的完整報導與 AI 綜述（/stories/{uuid}），按熱榜條目 id 存；
    它們 storyline／related 指到、不在熱榜上的事件另按事件 uuid 存進 byStory（故事線優先，最多 NEIGHBORS 個）。"""
    hot = get("/hot-topics").get("items") or []
    out = {}
    def fetch(uuid):
        st = get(f"/stories/{uuid}").get("story")
        if st: st["reports"] = [r | {"links": {"original": (r.get("links") or {}).get("original")}} for r in st.get("reports") or []]
        return st
    for h in hot:
        uuid = story_uuid(h)
        if not uuid: continue
        try:
            st = fetch(uuid)
            if st: out[h["id"]] = st
        except Exception as e: print(f"warn story {uuid}: {e}", file=sys.stderr)
        time.sleep(0.15)
    if not out: raise RuntimeError("stories: 空的")
    on_board = {story_uuid(h) for h in hot}
    want = [n.get("publicId") for rel in ("storyline", "related") for st in out.values() for n in st.get(rel) or []]
    more = {}
    for uuid in dict.fromkeys(u for u in want if u and u not in on_board):
        if len(more) >= NEIGHBORS: break
        try:
            st = fetch(uuid)
            if st: more[uuid] = st | {"reports": st["reports"][:50]}
        except Exception as e: print(f"warn neighbor story {uuid}: {e}", file=sys.stderr)
        time.sleep(0.15)
    return {"byItem": out, "byStory": more}

HEAT_DAYS = 7

def prev_heat():
    """上一份 heat-history：CI 從 Neon 讀（同一步驟有 DATABASE_URL 與 psql），本機讀 data/mirror 裡上次寫的檔。
    Neon 讀失敗就丟錯（這輪不寫），免得用空的歷史蓋掉累積的 7 天。"""
    url = os.environ.get("DATABASE_URL")
    if url:
        import subprocess
        r = subprocess.run(["psql", url, "-Atqc", "SELECT data::text FROM aihot_mirror WHERE key = 'heat-history'"], capture_output=True, text=True, timeout=30)
        if r.returncode != 0: raise RuntimeError(f"讀不到上一份 heat-history：{r.stderr.strip()[:200]}")
        return json.loads(r.stdout) if r.stdout.strip() else {}
    try:
        with open(os.path.join(OUT, "heat-history.json"), encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return {}

def heat_history(now):
    """熱榜快照：每個事件（按事件 uuid）的 [參與者數, 來源數]，每小時只留最後一筆、保留 7 天（最多 168 筆）。
    網站拿它算 6 小時升降、24 小時走勢線與事件熱度曲線；只有真的存到的時點，沒有補值。"""
    with open(os.path.join(OUT, "hot-topics.json"), encoding="utf-8") as f: hot = json.load(f).get("items") or []
    snap = {story_uuid(h): [h.get("participantCount") or 0, h.get("sourceCount") or 0] for h in hot if story_uuid(h)}
    if not snap: raise RuntimeError("heat-history: 熱榜是空的")
    runs = [r for r in prev_heat().get("runs") or [] if isinstance(r, dict) and isinstance(r.get("t"), str) and r["t"][:13] != now[:13]]
    runs.append({"t": now, "s": snap})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HEAT_DAYS)).isoformat(timespec="seconds")
    return {"runs": sorted((r for r in runs if r["t"] >= cutoff), key=lambda r: r["t"])[-HEAT_DAYS * 24:]}

def archives():
    """日／週／月報歸檔：索引裡還沒存過的期數逐期補抓（每次最多 40 期，幾輪內補齊）；最新兩期每次重抓。"""
    res = {}
    for idx, one, field, prefix in (("/dailies?limit=180", "/dailies/", "date", "daily"), ("/weeklies?limit=52", "/weeklies/", "week", "weekly"), ("/monthlies?limit=24", "/monthlies/", "month", "monthly")):
        keys = [i[field] for i in get(idx).get("items") or [] if i.get(field)]
        todo = [k for n, k in enumerate(keys) if n < 2 or f"{prefix}-{k}" not in HAVE][:40]
        for k in todo:
            try:
                d = get(one + k)
                if d.get("report"): res[f"{prefix}-{k}"] = d
            except Exception as e: print(f"warn {prefix} {k}: {e}", file=sys.stderr)
            time.sleep(0.1)
    return res

# 多鍵工作：一次產生很多個鍵（全部動態按日、歷史報告逐期）。
MULTI = {"all-days": all_days, "archives": archives}

JOBS = {
    "stories": stories,
    "items": items,
    "hot-topics": lambda: need("/hot-topics", "items"),
    "dailies": lambda: need("/dailies?limit=180", "items"),
    "daily-latest": lambda: need("/dailies/latest", "report"),
    "weeklies": lambda: need("/weeklies?limit=52", "items"),
    "weekly-latest": lambda: need("/weeklies/latest", "report"),
    "monthlies": lambda: need("/monthlies?limit=24", "items"),
    "monthly-latest": lambda: need("/monthlies/latest", "report"),
    **{f"lb-{k}": (lambda k=k: lb_board(k)) for k in LB_BOARDS},
}

def lb_snapshot(now):
    """模型榜一份快照：5 個榜 + 每個模型的詳情，同一個版本（上游輪次）與同一個同步時間。
    上游沒換輪：詳情沿用上一份（同一輪的資料本來就一樣），只更新榜單與同步時間；換輪：全部重抓。
    任何一步不一致或失敗就整份不寫，網站繼續用上一份完整快照。"""
    boards = {f"lb-{k}": json.load(open(os.path.join(LBTMP, f"lb-{k}.json"), encoding="utf-8")) for k in LB_BOARDS}
    runs = {b["runId"] for b in boards.values()}
    if len(runs) != 1 or None in runs: raise RuntimeError(f"5 個榜的上游輪次不一致：{runs}")
    run = runs.pop()
    slugs = sorted({e["model"]["slug"] for b in boards.values() for e in b["entries"] + b["more"] + b["pending"]})
    prev = os.environ.get("LB_PREV_RUN", "")
    if run != prev or os.environ.get("LB_FORCE"): lb_details(boards, run)
    else: print(f"lb details unchanged (run {run})")
    stats = {}
    for k in ("coding", "reasoning", "professional", "knowledge"):
        xs = [e["score"] for e in boards[f"lb-{k}"]["entries"] if isinstance(e.get("score"), (int, float))]
        if xs: stats[k] = {"avg": round(sum(xs) / len(xs), 1), "top": max(xs), "n": len(xs)}
    meta = {"version": run, "syncedAt": now, "generatedAt": boards["lb-overall"].get("generatedAt"), "boards": list(LB_BOARDS), "models": slugs, "stats": stats}
    for b in boards.values(): b["version"], b["syncedAt"] = run, now
    for k, b in boards.items():
        with open(os.path.join(LBTMP, f"{k}.json"), "w", encoding="utf-8") as fh: json.dump(b, fh, ensure_ascii=False)
    with open(os.path.join(LBTMP, "lb-meta.json"), "w", encoding="utf-8") as fh: json.dump(meta, fh, ensure_ascii=False)
    for name in os.listdir(LBTMP): os.replace(os.path.join(LBTMP, name), os.path.join(OUT, name))
    print(f"ok lb snapshot version={run} synced={now} models={len(slugs)}")

def main():
    os.makedirs(OUT, exist_ok=True)
    import shutil
    shutil.rmtree(LBTMP, ignore_errors=True); os.makedirs(LBTMP)
    failed = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    from concurrent.futures import ThreadPoolExecutor
    def run(item):
        key, job = item
        try:
            data = job() if key.startswith("lb-") else tw(job())
            data["_mirror"] = {"source": "AIHOT", "sourceUrl": "https://aihot.news", "fetchedAt": now}
            with open(os.path.join(LBTMP if key.startswith("lb-") else OUT, f"{key}.json"), "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
            print(f"ok {key}")
        except Exception as e:
            failed.append(key); print(f"FAIL {key}: {e}", file=sys.stderr)
    # 平行抓（最多 4 條連線）：省 Actions 分鐘數，對 AIHOT 也只是 9 個請求。
    with ThreadPoolExecutor(max_workers=4) as ex: list(ex.map(run, JOBS.items()))
    # 熱度歷史：接在這輪抓到的熱榜後面（熱榜失敗就不動，Neon 裡上一份照舊）。不經 tw()：只有 id、數字與時間。
    if "hot-topics" not in failed:
        try:
            data = heat_history(now)
            data["_mirror"] = {"source": "AIHOT", "sourceUrl": "https://aihot.news", "fetchedAt": now}
            with open(os.path.join(OUT, "heat-history.json"), "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            print(f"ok heat-history: {len(data['runs'])} snapshots")
        except Exception as e:
            failed.append("heat-history"); print(f"FAIL heat-history: {e}", file=sys.stderr)
    def multi(item):
        name, job = item
        try:
            keys = job()
            for k, data in keys.items():
                data = tw(data)
                data["_mirror"] = {"source": "AIHOT", "sourceUrl": "https://aihot.news", "fetchedAt": now}
                with open(os.path.join(OUT, f"{k}.json"), "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
            print(f"ok {name}: {len(keys)} keys")
        except Exception as e:
            failed.append(name); print(f"FAIL {name}: {e}", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=2) as ex: list(ex.map(multi, MULTI.items()))
    if not any(k.startswith("lb-") for k in failed):
        try: lb_snapshot(now)
        except Exception as e:
            failed.append("lb-snapshot"); print(f"FAIL lb-snapshot（沿用上一份完整快照）: {e}", file=sys.stderr)
    shutil.rmtree(LBTMP, ignore_errors=True)
    if failed: sys.exit(2)

if __name__ == "__main__": main()
