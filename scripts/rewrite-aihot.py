#!/usr/bin/env python3
"""AI熱點：用 Gemini 免費額度把鏡像來的標題與摘要改寫成自然的台灣繁中與英文（風格見 site/style/rewrite-guide.md）。

只改新的或內容變了的條目：快取以「條目 id + 原文雜湊」為鍵，存在 Neon（aihot_mirror 的 rewrites 鍵）。
每次最多 MAX_CALLS 次請求、每次 BATCH 則、請求之間間隔 PACE 秒；任何錯誤或額度用完就停，
沒改到的條目網站直接顯示轉換後的原文。永遠以退出碼 0 結束（不讓改寫失敗擋住同步）。
"""
import hashlib, json, os, re, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = os.path.join(ROOT, "data/mirror")
PREV = os.environ.get("REWRITES_PREV", "/tmp/rewrites-prev.json")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
KEY = os.environ.get("GEMINI_API_KEY", "")
BATCH, MAX_CALLS, PACE = 10, int(os.environ.get("REWRITE_MAX_CALLS", "5")), 6.5
GUIDE = open(os.path.join(ROOT, "site/style/rewrite-guide.md"), encoding="utf-8").read()

def load(name):
    try: return json.load(open(os.path.join(M, f"{name}.json"), encoding="utf-8"))
    except Exception: return None

def idof(x):
    if x.get("id"): return x["id"]
    m = re.search(r"/items/([A-Za-z0-9_-]+)", (x.get("links") or {}).get("aihot", ""))
    return m.group(1) if m else None

def candidates():
    seen = {}
    def add(x):
        i = idof(x)
        if i and x.get("title") and i not in seen: seen[i] = {"id": i, "title": x["title"], "summary": x.get("summary") or "", "reason": x.get("reason") or "", "originalTitle": x.get("originalTitle") or ""}
    for x in (load("items") or {}).get("items", []): add(x)
    for x in (load("hot-topics") or {}).get("items", []): add(x)
    for k in ("daily-latest", "weekly-latest", "monthly-latest"):
        r = (load(k) or {}).get("report") or {}
        for s in r.get("sections", []):
            for x in s.get("items", []): add(x)
        for x in r.get("flashes", []): add(x)
        # 週報、月報的導言（overview）也送去改寫，英文版才有得用；id 以報告種類為鍵。
        if r.get("overview"):
            t = (r.get("lead") or {}).get("title") or r.get("headline") or ""
            seen[f"overview:{k}"] = {"id": f"overview:{k}", "title": t or k, "summary": r["overview"], "reason": "", "originalTitle": ""}
    return list(seen.values())

def h(c):
    # reason（為何值得看）只在有內容時進雜湊：沒有 reason 的舊快取不會因為這次改版全部重跑。
    extra = f"\n{c['reason']}" if c.get("reason") else ""
    return hashlib.sha256(f"{c['title']}\n{c['summary']}{extra}".encode()).hexdigest()[:16]

SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "id": {"type": "STRING"}, "zhTitle": {"type": "STRING"}, "zhSummary": {"type": "STRING"}, "enTitle": {"type": "STRING"}, "enSummary": {"type": "STRING"}, "enReason": {"type": "STRING"}},
    "required": ["id", "zhTitle", "zhSummary", "enTitle", "enSummary", "enReason"]}}

def call(batch):
    prompt = (GUIDE + "\n\n## 任務\n下面每則有 id、title、summary（已是繁中，可能還帶大陸用語）和 originalTitle（英文原標題，可參考）。"
              "有的還有 reason（為何值得看，一句話）。每則輸出 zhTitle、zhSummary（台灣繁中）與 enTitle、enSummary、enReason（英文；enReason 是 reason 的自然英文，一句話），id 原樣帶回。summary 或 reason 是空的就輸出空字串。只輸出 JSON。\n\n"
              + json.dumps(batch, ensure_ascii=False))
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": SCHEMA}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
    with urllib.request.urlopen(req, timeout=60) as r: d = json.loads(r.read())
    text = d["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text), d.get("usageMetadata", {})

NUM = re.compile(r"\d+(?:[.,]\d+)?")
def sane(c, out):
    """拒收數字對不上的改寫：原標題的數字都要保留；改寫裡不能出現原文沒有的數字（防止編造）。"""
    src = set(NUM.findall(c["title"] + " " + c["summary"] + " " + c.get("reason", "") + " " + c["originalTitle"]))
    keep = set(NUM.findall(c["title"]))
    zh = set(NUM.findall(out["zhTitle"] + " " + out["zhSummary"]))
    en = set(NUM.findall(out["enTitle"] + " " + out["enSummary"] + " " + (out.get("enReason") or "")))
    return keep <= zh and zh <= src and en <= src | {n.replace(",", "") for n in src} and out["zhTitle"].strip() and out["enTitle"].strip()

def save(cache):
    os.makedirs(M, exist_ok=True)
    json.dump({"model": MODEL, "entries": cache}, open(os.path.join(M, "rewrites.json"), "w", encoding="utf-8"), ensure_ascii=False)

def main():
    try: cache = json.load(open(PREV, encoding="utf-8")).get("entries", {})
    except Exception: cache = {}
    now = datetime.now(timezone.utc)
    cands = candidates()
    todo = [c for c in cands if cache.get(c["id"], {}).get("h") != h(c)]
    print(f"candidates {len(cands)}, cached {len(cands) - len(todo)}, to rewrite {len(todo)}")
    calls = tokens = done = rejected = 0
    if KEY:
        for i in range(0, len(todo), BATCH):
            if calls >= MAX_CALLS: break
            if calls: time.sleep(PACE)  # 只在兩次請求之間等，最後一次後不空等（省 Actions 分鐘數）
            batch = todo[i:i + BATCH]
            try:
                outs, usage = call(batch); calls += 1; tokens += usage.get("totalTokenCount", 0)
            except urllib.error.HTTPError as e:
                print(f"gemini HTTP {e.code}; stop (fallback to original text)"); break
            except Exception as e:
                print(f"gemini error {type(e).__name__}; stop (fallback to original text)"); break
            by = {o.get("id"): o for o in outs if isinstance(o, dict)}
            for c in batch:
                o = by.get(c["id"])
                if o and sane(c, o):
                    cache[c["id"]] = {"h": h(c), "seen": now.isoformat(timespec="seconds"), "zh": {"title": o["zhTitle"].strip(), "summary": o["zhSummary"].strip()}, "en": {"title": o["enTitle"].strip(), "summary": o["enSummary"].strip(), "reason": (o.get("enReason") or "").strip() if c.get("reason") else ""}}
                    done += 1
                else:
                    # 記下被擋的版本，原文沒變就不再重送（省額度與 Actions 分鐘數）；網站顯示原文。
                    cache[c["id"]] = {"h": h(c), "seen": now.isoformat(timespec="seconds"), "skip": True}
                    rejected += 1
            save(cache)  # 每批寫一次：步驟逾時被砍也保住已改好的
    live = {c["id"] for c in cands}
    cutoff = (now - timedelta(days=14)).isoformat()
    for k, v in list(cache.items()):
        if k in live: v["seen"] = now.isoformat(timespec="seconds")
        elif v.get("seen", "") < cutoff: del cache[k]
    save(cache)
    print(f"calls {calls}, tokens {tokens}, rewritten {done}, rejected {rejected}, cache {len(cache)}")

if __name__ == "__main__":
    try: main()
    except Exception as e: print(f"rewrite skipped: {type(e).__name__}", file=sys.stderr)
