#!/usr/bin/env python3
"""AINEWS English edition: native English text for everything the mirror shows in Chinese.

Runs after fetch-aihot-mirror.py and rewrite-aihot.py in the sync job. Reads the mirror files in data/mirror (and, only when it still has
work to do, older days and report archives from Neon), asks the free-tier Gemini models (Flash-Lite and Gemma, which have separate
quotas) for English, checks every answer, and writes these mirror keys (shapes and sizes: docs/english-keys.md):

  en-items    { itemId: { t, s?, r? } }
  en-stories  { storyKey: { title, latest?, digest?, timeline: { itemId: { t, s? } } } }
  en-reports  { "daily-YYYY-MM-DD" | "weekly-YYYY-Www" | "monthly-YYYY-MM": the report's text fields at the same JSON paths }
  en-names    { original source display name: English display name }
  en-lb       { benchmark source key: { name, what?, operator? } }
  en-lb-text  { original Chinese string of the model board data: English }
  en-meta     coverage and call counts (monitoring; no Chinese in it either)
  en-cache    internal: everything generated so far, gzip+base64, keyed by text hash (nothing is translated twice)

Fail closed: an answer with Chinese characters, a number the source does not have, or a missing field is retried (the reason goes
back to the model); after that the key is left out, never filled with Chinese. The script always exits 0 so it can not block the sync.
With EN_CACHE_REQUIRED=1 (the workflow) a missing cache file means Neon could not be read: the run then writes nothing.
Cost: $0 as long as the project that owns GEMINI_API_KEY has no billing attached (then only the free tier answers). Per-run and per-day call caps
(Flash-Lite 24 and 240, Gemma 8 and 150) and a pace under the free per-minute limits; the day is the Pacific day, which is when Google resets
its daily quota. The Flash-Lite share is the same pool the Chinese rewrite step (rewrite-aihot.py, up to 5 calls a run) draws from, so the
daily cap leaves room for it. Whatever is skipped is logged, never silent. Everything printed here is safe to publish: Chinese characters,
database host names and secrets never reach the log.
Order of work: priority class first (P0 hot list, P1 selected 48 h, P2 all-stream 48 h, P3 older selected, P4 the rest of the 14 days), then
names, story pages, reports, board text, items; newest first.
"""
import base64, gzip, json, os, re, subprocess, sys, threading, time, urllib.error, urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import en_text as T

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = os.environ.get("EN_MIRROR_DIR") or os.path.join(ROOT, "data/mirror")
CACHE_PREV = os.environ.get("EN_CACHE_PREV", "/tmp/en-cache-prev.json")
CACHE_REQUIRED = os.environ.get("EN_CACHE_REQUIRED") == "1"   # the workflow sets it: a missing cache file then means "Neon could not be read", not "first run"
REWRITES_FILES = [os.path.join(M, "rewrites.json"), os.environ.get("REWRITES_PREV", "/tmp/rewrites-prev.json")]
API_BASE = os.environ.get("EN_API_BASE", "https://generativelanguage.googleapis.com/v1beta")
KEY = os.environ.get("GEMINI_API_KEY", "")

def env_int(name, default):
    try: return int(os.environ.get(name, default))
    except ValueError: return default

def env_float(name, default):
    try: return float(os.environ.get(name, default))
    except ValueError: return default

ITEM_DAYS = env_int("EN_ITEM_DAYS", 14)                       # how far back items get English (the 14-day trim of the all-* keys)
REPORT_KEEP = {"daily": env_int("EN_KEEP_DAILY", 14), "weekly": env_int("EN_KEEP_WEEKLY", 8), "monthly": env_int("EN_KEEP_MONTHLY", 3)}
DEADLINE_SEC = env_int("EN_DEADLINE", 480)                    # no new call starts after this many seconds
MAX_TRIES = 3                                                 # attempts per unit in one run (the reason of each failure goes back to the model)
MAX_FAILS = 3                                                 # runs that may fail on a unit before it waits for its source text to change
BATCH = {"name": 40, "lbsrc": 12, "lbtext": 40, "item": env_int("EN_BATCH", 10), "story": 3, "report": 2}
KIND_RANK = {"name": 0, "story": 1, "report": 2, "lbsrc": 3, "lbtext": 4, "item": 5}   # inside one priority class: names, story pages, reports, board text, then the items
RANK_ITEMS, RANK_DAY, RANK_STORY, RANK_REPORT = 0, 1, 2, 3   # which text an item id keeps when several sources carry it

NOW = datetime.fromisoformat(os.environ["EN_NOW"].replace("Z", "+00:00")) if os.environ.get("EN_NOW") else datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat(timespec="seconds")

def pacific(dt):
    """`dt` on the Pacific clock (Google resets its daily quota at midnight there); a fixed UTC-8 where the time zone data is missing."""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        return dt.astimezone(timezone(timedelta(hours=-8)))

QUOTA_DAY = pacific(NOW).strftime("%Y-%m-%d")
def next_quota_reset():
    p = pacific(NOW)
    return (p + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")

TODAY = (NOW + timedelta(hours=8)).strftime("%Y-%m-%d")
WINDOW0 = (NOW + timedelta(hours=8) - timedelta(days=ITEM_DAYS)).strftime("%Y-%m-%d")
START = time.time()

def log(*a): print(*a, flush=True)

_ANNOTATED = set()
def annotate(level, msg):
    """A GitHub Actions annotation (shows on the run summary page; harmless anywhere else), once per message. It is cleaned like every log line."""
    line = f"::{level} title=English edition::" + re.sub(r"[\r\n%]+", " ", T.safe(msg))
    if line not in _ANNOTATED: _ANNOTATED.add(line); log(line)

def ts_of(iso):
    try: return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception: return 0.0

def tpe_day(iso):
    t = ts_of(iso) if isinstance(iso, str) else 0.0
    return datetime.fromtimestamp(t + 8 * 3600, timezone.utc).strftime("%Y-%m-%d") if t else TODAY

def at_of(x): return x.get("discoveredAt") or x.get("publishedAt") or x.get("latestAt") or ""

def within_48h(x):
    t = ts_of(at_of(x))
    return bool(t) and NOW.timestamp() - t < 48 * 3600

# ---- style guide for the prompts -------------------------------------------------------------------------------------
def read_text(path):
    try:
        with open(path, encoding="utf-8") as f: return f.read()
    except OSError: return ""

def guide():
    g = read_text(os.path.join(ROOT, "site/style/rewrite-guide.md"))
    secs = [s for s in re.split(r"\n(?=## )", g) if s.startswith(("## 共同原則", "## 去 AI 味", "## English"))]
    secs = [s.split("- Each channel has its own register")[0].rstrip() if s.startswith("## English edition") else s for s in secs]  # channel registers are for the social posts
    extra = read_text(os.path.join(ROOT, "site/style/english-edition-guide.md"))
    text = "\n\n".join(secs + [extra]).strip()
    # Lines that only make sense for the Traditional Chinese edition (fullwidth punctuation) would fight the English rules.
    return "\n".join(l for l in text.split("\n") if "全形標點" not in l)

GUIDE = guide()
TERMS = T.load_terms(os.path.join(ROOT, "site/style/terms.json"))

# ---- mirror files and Neon -------------------------------------------------------------------------------------------
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return None

def disk(name): return read_json(os.path.join(M, f"{name}.json"))

def disk_keys(pattern):
    try: return sorted(f[:-5] for f in os.listdir(M) if f.endswith(".json") and re.fullmatch(pattern, f[:-5]))
    except OSError: return []

def neon(sql):
    """Tab-separated rows from Neon, or None (no database, or a failed read: the caller carries on without)."""
    url = os.environ.get("DATABASE_URL")
    if not url: return None
    try:
        r = subprocess.run(["psql", url, "-Atqc", sql, "-F", "\t"], capture_output=True, text=True, encoding="utf-8", timeout=120)
    except Exception as e:
        log(f"neon read skipped ({type(e).__name__})"); return None
    if r.returncode != 0:
        log("neon read failed:", T.safe(hide_db(r.stderr or "", url).strip()[:160])); return None
    return r.stdout

def hide_db(text, url):
    """The connection string, its host name, user name and password are not printed (the job log of the public sync repository is public)."""
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        parts = [url] + [x for x in (u.hostname, u.username, u.password) if x and len(x) >= 3]
    except Exception:
        parts = [url]
    for x in parts: text = text.replace(x, "<db>")
    return text

def neon_docs(keys):
    """{key: parsed json} for mirror keys held in Neon (keys are plain slugs)."""
    keys = [k for k in keys if re.fullmatch(r"[A-Za-z0-9-]+", k)]   # weekly-2026-W41 has a capital W
    if not keys: return {}
    out = neon("SELECT key, data::text FROM aihot_mirror WHERE key IN (" + ",".join(f"'{k}'" for k in keys) + ")")
    docs = {}
    for line in (out or "").splitlines():
        if "\t" in line:
            k, d = line.split("\t", 1)
            try: docs[k] = json.loads(d)
            except ValueError: pass
    return docs

# ---- the cache ------------------------------------------------------------------------------------------------------
def fresh_cache():
    return {"v": 1, "txt": {}, "ids": {}, "fail": {}, "names": {}, "stories": {}, "reports": {}, "lb": {}, "lbtext": {}, "days": {}, "lbrun": None, "usage": {}}

def load_cache():
    try:
        with open(CACHE_PREV, encoding="utf-8") as f: raw = f.read()
    except OSError:
        if CACHE_REQUIRED:
            # Starting from an empty cache would publish shorter en-* keys than the ones in Neon; stay out of the way instead.
            annotate("warning", "en-cache could not be read from Neon; nothing written this run (the Chinese data is not affected)"); sys.exit(0)
        return fresh_cache()
    if not raw.strip(): return fresh_cache()   # Neon answered and holds no en-cache yet: the very first run
    try:
        d = json.loads(raw)
        if "z" in d: d = json.loads(gzip.decompress(base64.b64decode(d["z"])))
        if d.get("v") != 1: raise ValueError("version")
        for k, v in fresh_cache().items(): d.setdefault(k, v)
        return d
    except Exception as e:
        # Writing from an empty cache would shrink the published keys; stay out of the way instead.
        annotate("warning", f"en-cache unreadable ({type(e).__name__}); nothing written this run (the Chinese data is not affected)"); sys.exit(0)

def dump(name, obj):
    os.makedirs(M, exist_ok=True)
    tmp = os.path.join(M, f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, os.path.join(M, f"{name}.json"))

# ---- gathering what needs English --------------------------------------------------------------------------------------
def idof(x):
    """The item id: `id`, else the id in links.aihot (/items/<id>). None when there is none (a null, a missing key or a non-string never raises)."""
    if not isinstance(x, dict): return None
    i = x.get("id")
    if isinstance(i, (str, int)) and not isinstance(i, bool) and str(i): return str(i)
    links = x.get("links")
    aihot = links.get("aihot") if isinstance(links, dict) else None
    m = re.search(r"/items/([A-Za-z0-9_-]+)", aihot) if isinstance(aihot, str) else None
    return m.group(1) if m else None

def names_in(obj, out):
    if isinstance(obj, dict):
        s = obj.get("source")
        if isinstance(s, dict) and isinstance(s.get("name"), str): out.add(s["name"])
        sn = obj.get("sourceNames")
        if isinstance(sn, list): out.update(x for x in sn if isinstance(x, str))
        for k, v in obj.items():
            if k != "_mirror": names_in(v, out)
    elif isinstance(obj, list):
        for v in obj: names_in(v, out)

class Sources:
    """Everything this run knows that may need English, with the priority of each text."""
    def __init__(self, C):
        self.C = C
        self.texts = {}        # text hash -> {title, summary, reason, orig, prio, order}
        self.ids = {}          # item id -> [text hash, TPE day, rank]
        self.names = set()     # source display names seen
        self.stories = {}      # story key -> {title, latest, digest, h, prio, reports: [(id, text hash)]}
        self.reports = {}      # report key -> {kind, key, fields, sections, flashes, h, prio, has_lead}
        self.day_hashes = {}   # all-YYYY-MM-DD -> [text hash]
        self.day_len = {}      # all-YYYY-MM-DD -> number of items in the stored day (the same count Neon reports)
        self.hot = []          # hot-list entries (their headline is the event headline, not the headline of the item the entry points to)
        self.hot_h = {}        # hot entry id -> text hash of its headline
        self.bad = []          # records that could not be read (malformed upstream data): skipped one by one, never the whole run
        self.lb_src = {}       # benchmark source key -> {name, operator, description, h}
        self.lb_strings = set()
        self.lb_run = None     # upstream round of the model board
        self.lb_boards = []    # board and model documents read (their strings need English too)
        self.lb_needs_neon = False
        self.report_window = {}      # report key -> kind, for the issues that should have English
        self.pending_reports = []    # (kind, key, prio) of those not on disk this run

    def guard(self, what, fn, *a):
        """Run one record's reader; a malformed record is counted and skipped, the rest of the run goes on."""
        try: return fn(*a)
        except Exception as e:
            self.bad.append(f"{what}: {type(e).__name__}")
            return None

    def add_item(self, x, prio, rank, daykey=None):
        """Register the text of an item. rank None: the text is translated but no item id is bound to it (hot-list headlines)."""
        if not isinstance(x, dict): return None
        title = x.get("title")
        if not isinstance(title, str) or not title.strip(): return None
        def txt(k): return x[k] if isinstance(x.get(k), str) else ""
        summary, reason, orig = txt("summary"), txt("reason"), txt("originalTitle")
        orig = orig if orig and not T.has_cjk(orig) else ""
        h = T.h_item(title, summary, reason)
        at = at_of(x)
        t = self.texts.get(h)
        if t is None:
            self.texts[h] = {"title": title, "summary": summary, "reason": reason, "orig": orig, "prio": prio, "order": ts_of(at)}
        else:
            t["prio"] = min(t["prio"], prio)
            t["order"] = max(t["order"], ts_of(at))
            if not t["orig"]: t["orig"] = orig
        i = idof(x) if rank is not None else None
        if i:
            day = tpe_day(at) if at else TODAY
            cur = self.ids.get(i)
            if cur is None or rank < cur[2] or (rank == cur[2] and cur[1] < day): self.ids[i] = [h, day, rank]
        if daykey: self.day_hashes.setdefault(daykey, []).append(h)
        return h

    def add_day(self, key, doc):
        items = (doc or {}).get("items")
        items = items if isinstance(items, list) else []
        self.day_len[key] = len(items)
        for x in items:
            if isinstance(x, dict): self.guard("all-stream item", self.add_item, x, 2 if within_48h(x) else 4, RANK_DAY, key)
        names_in(doc, self.names)

    def add_selected(self, doc):
        items = (doc or {}).get("items")
        for x in items if isinstance(items, list) else []:
            if isinstance(x, dict): self.guard("selected item", self.add_item, x, 1 if within_48h(x) else 3, RANK_ITEMS)
        names_in(doc, self.names)

    def add_hot(self, doc):
        items = (doc or {}).get("items")
        self.hot = [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []
        names_in(doc, self.names)

    def finish_hot(self):
        """A hot-list entry carries the headline of the EVENT (the same text as its story page title), which is not the headline of the item the entry
        points to. Its English is therefore never stored under the item id in en-items; the API reads it from en-stories[<hot id>].title
        (docs/english-keys.md). The headline is translated like any other text; the item behind the id keeps its own text, which is raised
        to the hot priority when the two headlines are the same."""
        for x in self.hot:
            self.guard("hot entry", self.finish_hot_entry, x)

    def finish_hot_entry(self, x):
        i = idof(x)
        cur = self.ids.get(i) if i else None
        if cur and self.texts.get(cur[0], {}).get("title") == x.get("title"): self.texts[cur[0]]["prio"] = 0
        h = self.add_item(x, 0, None)
        if i and h: self.hot_h[i] = h

    def add_stories(self, doc):
        for group, prio in (("byItem", 1), ("byStory", 3)):
            stories = (doc or {}).get(group)
            for key, st in (stories if isinstance(stories, dict) else {}).items():
                if isinstance(st, dict): self.guard("story", self.add_story, key, st, prio)
        names_in(doc, self.names)

    def add_story(self, key, st, prio):
        def txt(k): return st[k] if isinstance(st.get(k), str) else ""
        reports = st.get("reports")
        reps = [(idof(r), self.add_item(r, prio, RANK_STORY)) for r in (reports if isinstance(reports, list) else []) if isinstance(r, dict)]
        title, latest, digest = txt("title"), txt("latest"), txt("digest")
        self.stories[key] = {"title": title, "latest": latest, "digest": digest, "h": T.h_any("story", title, latest, digest), "prio": prio, "reports": reps}

    def add_report(self, kind, key, doc, prio):
        r = (doc or {}).get("report")
        if not isinstance(r, dict): return
        def txt(d, k): return d[k] if isinstance(d.get(k), str) and d[k] else ""
        fields = {k: txt(r, k) for k in ("headline", "overview") if txt(r, k)}
        lead = r["lead"] if isinstance(r.get("lead"), dict) else {}
        has_lead = bool(lead)
        if txt(lead, "title"): fields["leadTitle"] = lead["title"]
        if txt(lead, "leadParagraph"): fields["leadParagraph"] = lead["leadParagraph"]
        def recs(xs):
            # An entry with no id (no `id`, no links.aihot) can not be matched by the page: it is left out of the English report (and costs no call).
            return [{"id": idof(x), "h": self.add_item(x, prio, RANK_REPORT), "title": x["title"]}
                    for x in (xs if isinstance(xs, list) else []) if isinstance(x, dict) and isinstance(x.get("title"), str) and x["title"] and idof(x)]
        sections = [{"label": txt(s, "label"), "summary": txt(s, "summary"), "items": recs(s.get("items"))}
                    for s in (r.get("sections") if isinstance(r.get("sections"), list) else []) if isinstance(s, dict)]
        flashes = recs(r.get("flashes"))
        text = json.dumps([fields, [(s["label"], s["summary"]) for s in sections]], ensure_ascii=False, sort_keys=True)
        self.reports[f"{kind}-{key}"] = {"kind": kind, "key": key, "fields": fields, "sections": sections, "flashes": flashes, "prio": prio, "has_lead": has_lead, "h": T.h_any("report", text)}
        names_in(doc, self.names)

    def add_lb(self, sources_doc, docs):
        srcs = (sources_doc or {}).get("sources") if isinstance(sources_doc, dict) else None
        for key, s in (srcs if isinstance(srcs, dict) else {}).items():
            if isinstance(s, dict):
                def txt(k): return s[k] if isinstance(s.get(k), str) else ""
                self.lb_src[key] = {"name": txt("name"), "operator": txt("operator"), "description": txt("description"),
                                    "h": T.h_any("lb", s.get("name"), s.get("operator"), s.get("description"))}
        docs = [d for d in docs if isinstance(d, dict)]
        self.lb_boards = list(docs)
        # Every Chinese string of the board and model documents gets an entry in en-lb-text (benchmark names and descriptions also
        # have a richer one in en-lb, keyed by source key), so the API can translate any string it meets in those documents.
        for d in docs:
            for _, s in T.iter_strings({k: v for k, v in d.items() if k != "_mirror"}):
                if T.has_cjk(s) and not re.match(r"https?://", s): self.lb_strings.add(s)

def seed_from_rewrites(C, S):
    """English the earlier rewrite step already wrote for the same Chinese text (same id, same hash) is reused once it passes the checks."""
    rw = None
    for p in REWRITES_FILES:
        d = read_json(p)
        if isinstance(d, dict) and isinstance(d.get("entries"), dict): rw = d["entries"]; break
    if not rw: return 0
    n = 0
    for i, (h, _, _) in S.ids.items():
        e = rw.get(i)
        t = S.texts.get(h)
        if h in C["txt"] or not t or not isinstance(e, dict) or e.get("h") != h or not isinstance(e.get("en"), dict): continue
        en = e["en"]
        out = check_item(t, {"t": en.get("title"), "s": en.get("summary"), "r": en.get("reason")})
        if isinstance(out, dict): C["txt"][h] = {**out, "u": NOW_ISO}; n += 1
    return n

# ---- checking what the models write -----------------------------------------------------------------------------------
def check_item(t, o):
    """The dict to store, or an error string for an item answer."""
    if not isinstance(o, dict): return "answer is not an object"
    ti, su, rs = (T.clean(o.get(k)) if isinstance(o.get(k), str) else "" for k in ("t", "s", "r"))
    if not t["summary"]: su = ""
    if not t["reason"]: rs = ""
    errs = []
    if not ti: errs.append("t (headline) is empty")
    if t["summary"] and not su: errs.append("s (summary) is empty but the input has a summary")
    if t["reason"] and not rs: errs.append("r (reason) is empty but the input has a reason")
    for k, v in (("t", ti), ("s", su), ("r", rs)):
        if T.has_cjk(v): errs.append(f"{k} still has Chinese characters ({T.cjk_sample(v)}); write them in English")
    # English runs about twice as long as the Chinese; the limits only catch runaway answers (a 900-character Chinese summary may need 1,900).
    if len(ti) > max(220, 4 * len(t["title"])) or len(su) > max(1200, 3 * len(t["summary"])) or len(rs) > max(500, 4 * len(t["reason"])): errs.append("too long")
    bad = T.numbers_ok(f"{t['title']} {t['summary']} {t['reason']} {t['orig']}", f"{ti} {su} {rs}")
    if bad: errs.append(f"numbers not in the source: {', '.join(bad[:5])}; use only numbers the source has")
    if errs: return "; ".join(errs)
    return {"t": ti, **({"s": su} if su else {}), **({"r": rs} if rs else {})}

def check_strings(src, out):
    """Shared check for answers made of plain strings (a dict name -> text): no Chinese, no new numbers."""
    errs = [f"{k} still has Chinese characters ({T.cjk_sample(v)})" for k, v in out.items() if T.has_cjk(v)]
    bad = T.numbers_ok(" ".join(src), " ".join(v for v in out.values() if isinstance(v, str)))
    if bad: errs.append(f"numbers not in the source: {', '.join(bad[:5])}")
    return errs

# ---- tasks ------------------------------------------------------------------------------------------------------------
class Task:
    __slots__ = ("kind", "key", "data", "prio", "order", "tries", "feedback")
    def __init__(self, kind, key, data, prio, order=0.0):
        self.kind, self.key, self.data, self.prio, self.order, self.tries, self.feedback = kind, key, data, prio, order, 0, ""

def fail_key(kind, key, h=""):
    return {"item": "i:", "name": "n:", "lbsrc": "l:", "lbtext": "t:", "story": "s:", "report": "r:"}[kind] + key + (h if kind in ("lbsrc", "story", "report") else "")

def given_up(C, kind, key, h=""):
    f = C["fail"].get(fail_key(kind, key, h))
    return bool(f and f[0] >= MAX_FAILS)

def build_tasks(C, S):
    tasks = []
    for n in sorted(S.names):
        if n in C["names"] or not T.has_cjk(n): continue
        d = T.det_name(n)
        if d is not None:
            if d: C["names"][n] = d    # a name that is only fullwidth brackets would come out empty: nothing is stored for it
            continue
        if not given_up(C, "name", n): tasks.append(Task("name", n, {"name": n}, 0))
    # The model board is read far less than the news: it waits behind the hot list and the last 48 hours (priority 2), not behind the backlog.
    for k, s in S.lb_src.items():
        c = C["lb"].get(k)
        if (c and c.get("h") == s["h"]) or given_up(C, "lbsrc", k, s["h"]): continue
        tasks.append(Task("lbsrc", k, s, 2))
    for s in sorted(S.lb_strings):
        if s in C["lbtext"]: continue
        if not T.has_ideograph(s):  # only fullwidth punctuation: no model needed ("（）" stays "()"; a string with nothing left is not stored)
            v = T.ascii_punct(s) or T.ascii_punct(s, keep_empty=True)
            if v: C["lbtext"][s] = v
            continue
        if not given_up(C, "lbtext", s): tasks.append(Task("lbtext", s, {"text": s}, 2))
    for h, t in S.texts.items():
        if h in C["txt"] or given_up(C, "item", h): continue
        if not any(T.has_ideograph(t[k]) for k in ("title", "summary", "reason")):
            # Already English (a source that publishes in English; at most fullwidth punctuation to fix): no model call, only the same clean-up and checks.
            out = check_item(t, {"t": t["title"], "s": t["summary"], "r": t["reason"]})
            if isinstance(out, dict): C["txt"][h] = {**out, "u": NOW_ISO}; continue
        tasks.append(Task("item", h, t, t["prio"], -t["order"]))
    for key, st in S.stories.items():
        c = C["stories"].get(key)
        if (c and c.get("h") == st["h"]) or given_up(C, "story", key, st["h"]) or not (st["title"] or st["digest"]): continue
        tasks.append(Task("story", key, st, st["prio"]))
    for key, r in S.reports.items():
        c = C["reports"].get(key)
        if (c and c.get("h") == r["h"] and c.get("f") is not None) or given_up(C, "report", key, r["h"]) or not (r["fields"] or r["sections"]): continue
        tasks.append(Task("report", key, r, r["prio"]))
    tasks.sort(key=lambda t: (t.prio, KIND_RANK[t.kind], t.order))
    return tasks

# ---- prompts and schemas ------------------------------------------------------------------------------------------------
def S_(t="STRING"): return {"type": t}
def obj(props, req=None):
    req = list(props) if req is None else req
    return {"type": "OBJECT", "properties": props, **({"required": req} if req else {})}
def arr(item): return {"type": "ARRAY", "items": item}
SCHEMA = {
    "item": arr(obj({"n": S_("INTEGER"), "t": S_(), "s": S_(), "r": S_()})),
    "story": arr(obj({"n": S_("INTEGER"), "title": S_(), "latest": S_(), "digest": S_()})),
    "report": arr(obj({"n": S_("INTEGER"), "fields": arr(obj({"k": S_(), "en": S_()})),
                       "sections": arr(obj({"label": S_(), "summary": S_()}))}, ["n", "fields", "sections"])),
    "name": arr(obj({"n": S_("INTEGER"), "en": S_()})),
    "lbsrc": arr(obj({"n": S_("INTEGER"), "name": S_(), "what": S_(), "operator": S_()}, ["n", "name", "what"])),
    "lbtext": arr(obj({"n": S_("INTEGER"), "en": S_()})),
}
EXAMPLE_NAMES = ('X：邵猛 (@shao__meng) -> "X: Shao Meng (@shao__meng)"; 公眾號：數字生命卡茲克 -> "WeChat account: Digital Life Khazix"; '
                 'X：卡茲克 (@Khazix0918) -> "X: Khazix (@Khazix0918)"; X：Peter McCrory（Anthropic 首席經濟學家，@PeterMcCrory） -> "X: Peter McCrory (Anthropic chief economist, @PeterMcCrory)"; '
                 '公眾號：騰訊混元 -> "WeChat account: Tencent Hunyuan"; DeepSeek：API 更新日誌 -> "DeepSeek: API Changelog"; 量子位 -> "QbitAI"')
TASKS = {
    "item": ("Write the AINEWS English edition entry for each input item. Each item has: n (echo it back), title (headline, Traditional Chinese), summary (may be empty), "
             "reason (one line on why it matters; may be empty), originalTitle (the publisher's own English headline, only when there is one) and, on a retry, feedback (what was wrong with your last answer: fix it).\n"
             'Return a JSON array with one object per item: {"n", "t", "s", "r"}. t = English headline (always). s = English summary, "" when the input summary is empty. r = the reason as one English sentence, "" when the input reason is empty.'),
    "story": ("Write the AINEWS English edition of each story page. Each input has n (echo it back), title, latest (the latest development, one line; may be empty), digest (the story summary, paragraphs separated by a blank line; may be empty) and, on a retry, feedback.\n"
              'Return a JSON array with one object per input: {"n", "title", "latest", "digest"}. Keep the paragraph breaks of the digest (a blank line between paragraphs). Use "" for any field whose input is empty.'),
    "report": ("Write the AINEWS English edition of each daily, weekly or monthly report. Each input has n (echo it back), report (daily, weekly or monthly), fields (a list of {k, text}: the headline, lead title, lead paragraph or overview of the report) and sections (label and summary each; summary may be empty; when useLabel is given, use exactly that label) and, on a retry, feedback.\n"
               'Return a JSON array with one object per input: {"n", "fields": [one {"k", "en"} for every input field, same k, same order], "sections": [same number and order as the input, each {"label", "summary"}]}. '
               'Every field and every section with text in the input must have English text; none may be left empty. k = headline or leadTitle are headlines; k = leadParagraph or overview are two to four sentences. Use "" only for a section summary that is empty in the input.'),
    "name": ("Write the English display name of each news source. Each input has n (echo it back), name and, on a retry, feedback. Keep the structure Publisher: Section (kind) and keep every @handle exactly. Use ASCII punctuation (a colon followed by a space, plain parentheses). "
             f"Examples: {EXAMPLE_NAMES}.\n"
             'Return a JSON array with one object per input: {"n", "en"}. The name must start with a Latin letter or digit and contain no Chinese character.'),
    "lbsrc": ("Write the English name and a one-sentence description for each AI benchmark. Each input has n (echo it back), key, name (keep a name that is already in Latin letters exactly as given; translate only the Chinese part), operator (the group that runs it) and description (Chinese). "
              'Return a JSON array with one object per input: {"n", "name", "what", "operator"}. what = one plain sentence saying what the benchmark measures, in the present tense, no scores, no hype. '
              'operator = the input operator exactly as given, unless it contains Chinese characters; then write it in English.'),
    "lbtext": ("Translate each short label or sentence from an AI model leaderboard into plain English. Each input has n (echo it back) and text. Glossary: 编程/編程 Coding, 科研 Science, 专业办公/專業辦公 Professional work, 知识问答/知識問答 Knowledge Q&A, 真人盲选/真人盲選 Human preference, 综合/綜合 Overall, 推理 reasoning. "
               'Return a JSON array with one object per input: {"n", "en"}. Keep names, versions and numbers exactly; labels stay short noun phrases.'),
}

# One worked example per kind that carries news text: native English from the facts, not a translation of the sentences.
EXAMPLES = {
    "item": ("Example. Input: " + json.dumps({"n": 1, "title": "DeepSeek-V4.1-Flash-1017 發佈，多基準成績提升且 API 價格不變",
                                              "summary": "DeepSeek 將 V4.1 Flash 升級至 DeepSeek-V4.1-Flash-1017，網頁端與 APP 同步更新，API 調用方式與價格不變，仍用 deepseek-flash 訪問。",
                                              "reason": "原文列出各基準分數並說明 API 調用與價格不變，讀者可直接評估新版模型是否值得切換。"}, ensure_ascii=False)
             + "\nOutput: " + json.dumps({"n": 1, "t": "DeepSeek releases V4.1-Flash-1017 with higher benchmark scores and unchanged API pricing",
                                          "s": "DeepSeek upgraded V4.1 Flash to DeepSeek-V4.1-Flash-1017 on the web and in the app. API calls and prices stay the same, and the model is still reached as deepseek-flash.",
                                          "r": "The changelog lists the benchmark scores and confirms API usage and pricing are unchanged, so developers can judge whether the switch is worth it."}) + "\n"),
    "story": ("Example. Input: " + json.dumps({"n": 1, "title": "Meta 刪除涉 AI 訓練抓取爭議條款", "latest": "Meta 刪除商業服務條款中“用於訓練 AI”相關表述",
                                               "digest": "Meta 在引發爭議後再次更新商業條款，刪除了被解讀為允許其抓取新聞網站等內容訓練 AI 的措辭。\n\n法律專家曾警告，新措辭可能讓 Meta 繞過 robots.txt 等屏蔽指令。"}, ensure_ascii=False)
              + "\nOutput: " + json.dumps({"n": 1, "title": "Meta drops the AI-training wording from its business terms", "latest": "Meta removes the \"training AI\" language from its commercial terms",
                                           "digest": "Meta updated its business terms again after the backlash and deleted the wording that readers took to let it scrape news sites and other content to train AI.\n\nLegal experts had warned the new language could let Meta get around blocking instructions such as robots.txt."}) + "\n"),
}

def prompt_for(kind, batch):
    rows = []
    for i, t in enumerate(batch, 1):
        d = t.data
        if kind == "item":
            r = {"n": i, "title": d["title"], "summary": d["summary"], "reason": d["reason"]}
            if d["orig"]: r["originalTitle"] = d["orig"]
        elif kind == "story": r = {"n": i, "title": d["title"], "latest": d["latest"], "digest": d["digest"]}
        elif kind == "report":
            r = {"n": i, "report": d["kind"], "fields": [{"k": k, "text": v} for k, v in d["fields"].items()],
                 "sections": [{"label": s["label"], "summary": s["summary"], **({"useLabel": T.SECTION_LABELS[s["label"]]} if s["label"] in T.SECTION_LABELS else {})} for s in d["sections"]]}
        elif kind == "name": r = {"n": i, "name": d["name"]}
        elif kind == "lbsrc": r = {"n": i, "key": t.key, "name": d["name"], "operator": d["operator"], "description": d["description"]}
        else: r = {"n": i, "text": d["text"]}
        if t.feedback: r["feedback"] = t.feedback
        rows.append(r)
    body = json.dumps(rows, ensure_ascii=False)
    gl = T.glossary(body, TERMS) if kind in ("item", "story", "report") else ""
    return (f"{GUIDE}\n\n## Task\n{TASKS[kind]}\n{EXAMPLES.get(kind, '')}"
            + (f"Glossary (use these English terms):\n{gl}\n" if gl else "")
            + "Hard rules: no Chinese characters anywhere in the output, no number or fact that is not in the input, the string AIHOT never appears. Answer with the JSON array only.\n\n"
            f"INPUT:\n{body}")

# ---- applying answers -------------------------------------------------------------------------------------------------
def apply_answer(C, t, o):
    """Check one answer. None when it is stored, else the error text that goes back to the model."""
    if not isinstance(o, dict): return "answer is not an object"
    k, d = t.kind, t.data
    if k == "item":
        r = check_item(d, o)
        if isinstance(r, dict): C["txt"][t.key] = {**r, "u": NOW_ISO}; return None
        return r
    if k == "story":
        out = {f: T.clean(o.get(f)) if isinstance(o.get(f), str) else "" for f in ("title", "latest", "digest")}
        errs = [f"{f} is empty but the input has it" for f in out if d[f] and not out[f]]
        errs += check_strings([d["title"], d["latest"], d["digest"]], out)
        if errs: return "; ".join(errs)
        C["stories"][t.key] = {"h": d["h"], **{f: v for f, v in out.items() if v}, "u": NOW_ISO}; return None
    if k == "report":
        fo = o.get("fields")
        if isinstance(fo, list):   # [{k, en}, ...]
            fo = {e.get("k"): e.get("en") for e in fo if isinstance(e, dict) and isinstance(e.get("k"), str)}
        elif not isinstance(fo, dict): fo = {}
        out_f = {f: T.clean(fo.get(f)) if isinstance(fo.get(f), str) else "" for f in d["fields"]}
        secs = o.get("sections") if isinstance(o.get("sections"), list) else []
        errs = [f"fields.{f} is empty but the input has it" for f in d["fields"] if not out_f[f]]
        if len(secs) != len(d["sections"]): errs.append(f"sections must have {len(d['sections'])} entries, you gave {len(secs)}")
        out_s = []
        if not errs:
            for s, e in zip(d["sections"], secs):
                e = e if isinstance(e, dict) else {}
                label = "" if not s["label"] else (T.SECTION_LABELS.get(s["label"]) or (T.clean(e.get("label")) if isinstance(e.get("label"), str) else ""))   # no label in the source: none written
                summ = T.clean(e.get("summary")) if isinstance(e.get("summary"), str) and s["summary"] else ""
                if s["label"] and not label: errs.append("a section label is empty")
                if s["summary"] and not summ: errs.append("a section summary is empty but the input has one")
                out_s.append({"label": label, "summary": summ})
        if not errs:
            flat = {**{f"fields.{f}": v for f, v in out_f.items()}, **{f"label{i}": s["label"] for i, s in enumerate(out_s)}, **{f"summary{i}": s["summary"] for i, s in enumerate(out_s)}}
            errs = check_strings(list(d["fields"].values()) + [x for s in d["sections"] for x in (s["label"], s["summary"])], flat)
        if errs: return "; ".join(dict.fromkeys(errs))
        C["reports"].setdefault(t.key, {}).update({"h": d["h"], "f": out_f, "sec": out_s, "u": NOW_ISO}); return None   # "out" (the published object) stays until the new one is assembled
    if k == "name":
        v = T.clean(o.get("en")) if isinstance(o.get("en"), str) else ""
        errs = T.name_ok(d["name"], v)
        if errs: return "; ".join(errs)
        C["names"][d["name"]] = v; return None
    if k == "lbsrc":
        name = d["name"] if d["name"] and not T.has_cjk(d["name"]) else (T.clean(o.get("name")) if isinstance(o.get("name"), str) else "")
        what = T.clean(o.get("what")) if isinstance(o.get("what"), str) else ""
        op = T.clean(o.get("operator")) if T.has_cjk(d["operator"]) and isinstance(o.get("operator"), str) else ""   # only an operator that has Chinese needs English
        errs = (["name is empty"] if not name else []) + (["what is empty but the input has a description"] if d["description"] and not what else [])
        errs += (["operator is empty but the input operator has Chinese characters"] if T.has_cjk(d["operator"]) and not op else [])
        errs += check_strings([d["name"], d["description"], d["operator"]], {"name": name, "what": what, "operator": op})
        if errs: return "; ".join(errs)
        # "o" / "oo": the original name and operator when they had Chinese characters, so they can also be listed in en-names
        C["lb"][t.key] = {"h": d["h"], "name": name, **({"what": what} if what else {}), **({"operator": op} if op else {}),
                          **({"o": d["name"]} if T.has_cjk(d["name"]) else {}), **({"oo": d["operator"]} if op else {})}; return None
    v = T.clean(o.get("en")) if isinstance(o.get("en"), str) else ""
    errs = ([] if v else ["en is empty"]) + check_strings([d["text"]], {"en": v})
    if errs: return "; ".join(errs)
    C["lbtext"][d["text"]] = v; return None

# ---- models ----------------------------------------------------------------------------------------------------------
def json_schema(s):
    """Gemini's schema (OBJECT, STRING...) as JSON Schema (object, string...), for Gemma, which has no JSON mode."""
    if isinstance(s, list): return [json_schema(x) for x in s]
    if not isinstance(s, dict): return s
    return {k: (v.lower() if k == "type" and isinstance(v, str) else json_schema(v)) for k, v in s.items()}

class RateLimited(Exception):
    def __init__(self, daily=False): super().__init__(); self.daily = daily   # daily: the per-day quota is gone, not only the per-minute one
class ModelBroken(Exception): pass

class Model:
    def __init__(self, name, model, gemma, pace, tpm, run_cap, day_cap, workers=1, kinds=None):
        self.name, self.model, self.gemma, self.pace, self.tpm, self.run_cap, self.day_cap = name, model, gemma, pace, tpm, run_cap, day_cap
        self.workers = workers   # calls in flight at once; the pace still spaces their starts
        self.kinds = kinds       # the only kinds of work this model takes while another model can take the rest (None: everything)
        self.calls = self.errors = 0
        self.blocked = ""
        self.next_at = 0.0
        self.win = deque()       # [start time, tokens] of the calls of the last minute
        self.lock = threading.Lock()

    def available(self, usage):
        if self.blocked: return False
        if self.calls >= self.run_cap: self.blocked = "run cap"; return False
        if usage["calls"].get(self.name, 0) >= self.day_cap: self.blocked = "daily cap"; return False
        return True

    def wait_turn(self, est, deadline):
        """Pace the calls under the free per-minute limits (requests, and tokens for Gemma). Returns the window entry to fill in with the
        real token count, or None when the run's time is up. Safe with several workers on one model."""
        while True:
            with self.lock:
                now = time.time()
                while self.win and now - self.win[0][0] > 60: self.win.popleft()
                wait = max(self.next_at - now, 0.0)
                if self.tpm and self.win and sum(t for _, t in self.win) + est > self.tpm: wait = max(wait, 60 - (now - self.win[0][0]) + 0.5)
                if wait <= 0:
                    if now > deadline: return None
                    self.next_at = now + self.pace
                    entry = [now, est]
                    self.win.append(entry)
                    return entry
            if time.time() + wait > deadline: return None
            time.sleep(min(wait, 5))

    def call(self, prompt, schema, entry):
        gen = {"temperature": 0.3, "maxOutputTokens": 8192}
        if self.gemma:  # no system turn or JSON mode: everything in one turn, the schema asked for in words
            text = prompt + "\n\nAnswer with one JSON value only, no code fence and no other text, matching this JSON Schema:\n" + json.dumps(json_schema(schema))
            body = {"contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": gen}
        else:
            body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {**gen, "responseMimeType": "application/json", "responseSchema": schema}}
        req = urllib.request.Request(f"{API_BASE}/models/{self.model}:generateContent", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": KEY})
        try:
            with urllib.request.urlopen(req, timeout=100) as r: d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try: body = e.read(4000).decode("utf-8", "replace")
                except Exception: body = ""
                raise RateLimited(daily=bool(re.search(r"PerDay|per day|RequestsPerDay", body, re.I)))
            if e.code in (400, 401, 403, 404): raise ModelBroken(f"HTTP {e.code}")
            raise
        parts = ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        used = (d.get("usageMetadata") or {}).get("totalTokenCount")
        if used: entry[1] = used
        return T.parse_json_text(text)

def make_models(C):
    u = C.setdefault("usage", {})
    day = QUOTA_DAY   # the Pacific day: Google's daily quota resets at midnight there, not at midnight UTC
    if u.get("day") != day: u["day"], u["calls"] = day, {}
    u.setdefault("calls", {}); u.setdefault("cool", {})
    ms = []
    lite = os.environ.get("EN_LITE_MODEL") or os.environ.get("GEMINI_MODEL") or "gemini-3.1-flash-lite"
    gemma = os.environ.get("EN_GEMMA_MODEL", "gemma-4-26b-a4b-it")   # "" turns Gemma off
    # Flash-Lite writes the story pages and reports; Gemma (weaker, 15K tokens a minute) takes headlines, summaries, names and board text.
    if lite: ms.append(Model("lite", lite, False, env_float("EN_LITE_PACE", 6.5), env_int("EN_LITE_TPM", 200000), env_int("EN_LITE_CALLS", 24), env_int("EN_LITE_DAILY", 240), env_int("EN_LITE_WORKERS", 2)))
    if gemma: ms.append(Model("gemma", gemma, True, env_float("EN_GEMMA_PACE", 6.5), env_int("EN_GEMMA_TPM", 12000), env_int("EN_GEMMA_CALLS", 8), env_int("EN_GEMMA_DAILY", 150), 1, ("item", "name", "lbsrc", "lbtext")))
    for m in ms:
        cool = u["cool"].get(m.name)
        if cool and cool > NOW_ISO: m.blocked = f"cooling down until {cool}"
    return ms

class Queue:
    def __init__(self, tasks): self.tasks, self.lock = list(tasks), threading.Lock()
    def take(self, only=None):
        """The next batch: the first task (of the kinds in `only`, if given) and the tasks of its kind behind it, up to the batch size of that kind."""
        with self.lock:
            head = next((t for t in self.tasks[:400] if only is None or t.kind in only), None)
            if head is None: return []
            batch, rest = [], []
            for i, t in enumerate(self.tasks):
                if t.kind == head.kind and len(batch) < BATCH[head.kind] and i < 400: batch.append(t)
                else: rest.append(t)
            self.tasks = rest
            return batch
    def put_back(self, tasks):
        with self.lock:
            self.tasks = sorted(list(tasks) + self.tasks, key=lambda t: (t.prio, KIND_RANK[t.kind], t.order))

# ---- the run ---------------------------------------------------------------------------------------------------------
class Run:
    def __init__(self, C, S, tasks):
        self.C, self.S, self.q = C, S, Queue(tasks)
        self.lock = threading.Lock()
        self.deadline = START + DEADLINE_SEC
        self.models = make_models(C)
        self.done = self.retried = self.failed = self.batches = 0
        self.failures = []     # (kind, key, reason)
        self.rejects = []      # (model, kind, key, reason): the first answers a check turned down, for tuning the prompts
        self.stop = {}         # model -> why it stopped

    def give_up(self, t, why):
        with self.lock:
            k = fail_key(t.kind, t.key, t.data.get("h", "") if isinstance(t.data, dict) else "")
            f = self.C["fail"].get(k, [0, ""])
            self.C["fail"][k] = [f[0] + 1, NOW_ISO]
            self.failed += 1
            self.failures.append((t.kind, t.key, why[:140]))

    def retry(self, tasks, why):
        again = []
        for t in tasks:
            t.tries += 1
            if t.tries >= MAX_TRIES: self.give_up(t, why)
            else: again.append(t); self.retried += 1
        if again: self.q.put_back(again)

    def attempt(self, m, batch):
        kind = batch[0].kind
        est = int(sum(len(json.dumps(t.data, ensure_ascii=False)) for t in batch) / 1.6) + 1800 + len(GUIDE) // 3
        entry = m.wait_turn(est, self.deadline)
        if entry is None:
            self.q.put_back(batch); m.blocked = "run deadline"; return
        with self.lock:
            if not m.available(self.C["usage"]):   # another worker took the last call this model may make
                self.q.put_back(batch); return
            m.calls += 1
            u = self.C["usage"]["calls"]; u[m.name] = u.get(m.name, 0) + 1
        try:
            out = m.call(prompt_for(kind, batch), SCHEMA[kind], entry)
        except RateLimited as e:
            # A per-minute limit cools the model for 30 minutes; the per-day quota being gone keeps it quiet until Google resets it (midnight Pacific),
            # so the Chinese rewrite step that shares the Flash-Lite quota is not starved by more English calls.
            m.blocked = "daily quota used up (429)" if e.daily else "rate limited (429)"
            with self.lock: self.C["usage"]["cool"][m.name] = next_quota_reset() if e.daily else (NOW + timedelta(minutes=30)).isoformat(timespec="seconds")
            self.q.put_back(batch); return
        except ModelBroken as e:
            m.blocked = str(e); self.q.put_back(batch); return
        except Exception as e:
            m.errors += 1
            if m.errors >= 3: m.blocked = f"{type(e).__name__} x3"
            self.retry(batch, f"model call failed ({type(e).__name__})"); return
        m.errors = 0
        if isinstance(out, dict):
            lists = [v for v in out.values() if isinstance(v, list)]
            out = lists[0] if len(out) == 1 and lists else [out]
        if not isinstance(out, list):
            self.retry(batch, "the answer was not a JSON array"); return
        by = {}
        for o in out:   # n may come back as 3 or "3"
            if isinstance(o, dict):
                try: by[int(o.get("n"))] = o
                except (TypeError, ValueError): pass
        again = []
        for i, t in enumerate(batch, 1):
            with self.lock:
                err = apply_answer(self.C, t, by[i]) if i in by else "missing from the answer"
                if err is None: self.done += 1; continue
            t.tries += 1
            t.feedback = f"Your last answer was rejected: {err}"
            if len(self.rejects) < 30: self.rejects.append((m.name, kind, str(t.key)[:40], err[:160], json.dumps(by.get(i), ensure_ascii=False)[:300]))
            if t.tries >= MAX_TRIES: self.give_up(t, err)
            else: again.append(t); self.retried += 1
        if again: self.q.put_back(again)

    def only_for(self, m):
        """The kinds this model should take: its own list while Flash-Lite can still take the rest, everything once it can not."""
        if m.kinds is None: return None
        return m.kinds if any(o.kinds is None and o.available(self.C["usage"]) for o in self.models if o is not m) else None

    def worker(self, m):
        while time.time() < self.deadline and m.available(self.C["usage"]):
            batch = self.q.take(self.only_for(m))
            if not batch: break
            self.attempt(m, batch)
            with self.lock:
                self.batches += 1
                if self.batches % 4 == 0: write_all(self.C, self.S, self)   # a step killed by its timeout still keeps what was done
        if not m.blocked and time.time() >= self.deadline: m.blocked = "run deadline"
        self.stop[m.name] = m.blocked or "queue empty"

    def go(self):
        if not KEY or not self.q.tasks:
            for m in self.models: self.stop[m.name] = "no work" if KEY else "no GEMINI_API_KEY"
            return
        threads = [threading.Thread(target=self.worker, args=(m,), daemon=True) for m in self.models for _ in range(m.workers)]
        for th in threads: th.start()
        for th in threads: th.join()

# ---- outputs -----------------------------------------------------------------------------------------------------------
def assemble_report(C, key, r):
    """The English report object (same JSON paths as the mirror's report), or None while something it needs is still missing."""
    c = C["reports"].get(key)
    if not c or c.get("h") != r["h"] or c.get("f") is None: return None
    def item(x):
        e = C["txt"].get(x["h"]) if x["h"] else None
        if e and e.get("t"): return {"id": x["id"], "title": e["t"], **({"summary": e["s"]} if e.get("s") else {}), **({"reason": e["r"]} if e.get("r") else {})}
        return {"id": x["id"]} if given_up(C, "item", str(x["h"])) else None   # gave up on it: the entry carries the id only, the page leaves it out
    f = c["f"]
    out = {k: f[k] for k in ("headline", "overview") if k in f}
    if r["has_lead"]: out["lead"] = {k: f[v] for k, v in (("title", "leadTitle"), ("leadParagraph", "leadParagraph")) if v in f}
    out["sections"] = []
    for s, es in zip(r["sections"], c["sec"]):
        items = [item(x) for x in s["items"]]
        if None in items: return None
        out["sections"].append({**({"label": es["label"]} if es["label"] else {}), **({"summary": es["summary"]} if es["summary"] else {}), "items": items})   # no empty strings: a section without a label has no label key
    out["flashes"] = [item(x) for x in r["flashes"]]
    return None if None in out["flashes"] else out

def project(C, S):
    en_items = {}
    for i, v in sorted(C["ids"].items()):
        # While the English for a changed Chinese text is still being written, the English of the previous text keeps serving the id.
        e = C["txt"].get(v[0]) or (C["txt"].get(v[3]) if len(v) > 3 else None)
        if e and e.get("t") and v[1] >= WINDOW0: en_items[i] = {k: e[k] for k in ("t", "s", "r") if e.get(k)}
    by_title = {t["title"]: C["txt"][h]["t"] for h, t in S.texts.items() if h in C["txt"] and C["txt"][h].get("t")}
    en_stories = {}
    for key, st in sorted(S.stories.items()):
        c = C["stories"].get(key)
        if not c or c.get("h") != st["h"] or not c.get("title"): continue
        # The same Chinese line as an item headline reads the same in English wherever it appears.
        title = by_title.get(st["title"], c["title"])
        latest = by_title.get(st["latest"], c.get("latest", "")) if st["latest"] else ""
        timeline = {}
        for i, h in st["reports"]:
            e = C["txt"].get(h)
            if i and e and e.get("t"): timeline[i] = {"t": e["t"], **({"s": e["s"]} if e.get("s") else {})}
        en_stories[key] = {"title": title, **({"latest": latest} if latest else {}), **({"digest": c["digest"]} if c.get("digest") else {}), "timeline": timeline}
    # The hot list shows the event headline, which is the story page title: en-stories[<hot id>].title. A hot entry with no story page in the
    # mirror (its story could not be fetched) still gets that title here, with an empty timeline.
    for i, h in sorted(S.hot_h.items()):
        e = C["txt"].get(h)
        if i not in S.stories and i not in en_stories and e and e.get("t"): en_stories[i] = {"title": e["t"], "timeline": {}}
    en_reports = {}
    for key, r in sorted(S.reports.items()):
        out = assemble_report(C, key, r)
        if out is not None: C["reports"][key]["out"] = out
    # The English of an issue stays published while its Chinese text changed and the new English is not written yet, and for a kind of report
    # whose index could not be read this run (a failed fetch must not make the English reports vanish from Neon).
    kinds_read = set(S.report_window.values())
    for key in sorted(set(S.report_window) | {k for k in C["reports"] if k.split("-")[0] not in kinds_read}):
        c = C["reports"].get(key)
        if c and c.get("out") is not None: en_reports[key] = c["out"]
    en_lb = {k: {x: v[x] for x in ("name", "what", "operator") if v.get(x)} for k, v in sorted(C["lb"].items())}
    en_names = dict(C["names"])
    for v in C["lb"].values():   # benchmark names and operators that had Chinese characters are source names too
        for orig, en in ((v.get("o"), v.get("name")), (v.get("oo"), v.get("operator"))):
            if orig and en and T.has_cjk(orig): en_names[orig] = en
    return {"en-items": en_items, "en-stories": en_stories, "en-reports": en_reports, "en-names": dict(sorted((k, v) for k, v in en_names.items() if v)),
            "en-lb": en_lb, "en-lb-text": dict(sorted((k, v) for k, v in C["lbtext"].items() if v))}

def scrub_outputs(outs):
    """Defence in depth, one top-level entry at a time: an entry that still holds Chinese or the upstream name, has a character jsonb refuses (NUL),
    or does not have the documented shape is dropped; the other entries of the key are published. Returns the number dropped."""
    dropped = 0
    for name, d in outs.items():
        for e, v in list(d.items()):
            why = None
            if T.find_cjk(v): why = "Chinese characters"
            elif any(re.search(r"AIHOT", s, re.I) for _, s in T.iter_strings(v)): why = "the upstream name"
            elif T.has_nul({e: v}): why = "a NUL character"
            else:
                errs = T.shape_errors(name, {e: v})
                if errs: why = "shape: " + errs[0]
            if why:
                del d[e]; dropped += 1
                log(f"DROPPED {name} entry {T.safe_key(e)[:60]} ({T.safe(why)[:120]})")
    if dropped: annotate("warning", f"{dropped} English entr{'y' if dropped == 1 else 'ies'} failed the checks and were left out of this run's keys (see the DROPPED lines)")
    return dropped

def prune(C, S):
    for i in [i for i, v in C["ids"].items() if v[1] < WINDOW0]: del C["ids"][i]
    used = {v[0] for v in C["ids"].values()} | {v[3] for v in C["ids"].values() if len(v) > 3}
    cut = (NOW - timedelta(days=3)).isoformat(timespec="seconds")
    for h in [h for h, e in C["txt"].items() if h not in used and h not in S.texts and e.get("u", "") < cut]: del C["txt"][h]
    for k in [k for k, v in C["fail"].items() if v[1] < cut]: del C["fail"][k]
    for k in [k for k in C["days"] if k[4:] < WINDOW0]: del C["days"][k]
    kinds_read = set(S.report_window.values())     # a kind whose index could not be read this run keeps what it has
    for k in [k for k in C["reports"] if k.split("-")[0] in kinds_read and k not in S.report_window]: del C["reports"][k]
    if S.stories:
        for k in [k for k in C["stories"] if k not in S.stories]: del C["stories"][k]

def write_all(C, S, run=None):
    outs = project(C, S)
    dropped = scrub_outputs(outs)
    if not S.stories: outs.pop("en-stories")        # nothing read this run: the key in Neon stays as it was
    if not S.report_window: outs.pop("en-reports")
    for k, v in outs.items(): dump(k, v)
    cov = {"items": {"have": len(outs["en-items"]), "ids": len(C["ids"])}, "stories": {"have": len(outs.get("en-stories", {})), "of": len(S.stories)},
           "reports": {"have": len(outs.get("en-reports", {})), "of": len(S.report_window)}, "names": len(outs["en-names"]),
           "lb": {"sources": len(outs["en-lb"]), "strings": len(outs["en-lb-text"])}}
    meta = {"updatedAt": NOW_ISO, "windowDays": ITEM_DAYS, "coverage": cov, "usage": C.get("usage", {}),
            "itemsWithoutEnglish": {f"P{p}": n for p, n in sorted(pending_items(C, S).items())},
            "skipped": {"records": len(S.bad), "entries": dropped}}   # malformed upstream records left out, finished entries that failed the checks
    # en-meta is published like the other keys, so it carries no Chinese either: a failure names its unit by key or hash, the reason is stripped of the rejected text.
    if run: meta["run"] = {"done": run.done, "failed": run.failed, "retried": run.retried, "stopped": run.stop,
                           "failures": [[kind, T.safe_key(key), T.safe(why)] for kind, key, why in run.failures[:40]]}
    dump("en-meta", meta)
    blob = base64.b64encode(gzip.compress(json.dumps(C, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace"), 6)).decode()
    dump("en-cache", {"v": 1, "z": blob})
    return cov

# ---- main --------------------------------------------------------------------------------------------------------------
def load_mirror(S, C):
    """What the mirror step wrote this run (files in data/mirror)."""
    sel = disk("items")
    if sel: S.guard("selected items", S.add_selected, sel)
    S.guard("hot list", S.add_hot, disk("hot-topics"))
    S.guard("stories", S.add_stories, disk("stories"))
    for key in disk_keys(r"all-\d{4}-\d{2}-\d{2}"):
        if key[4:] >= WINDOW0: S.guard("all-stream day", S.add_day, key, disk(key))
    for kind, index_key, field in (("daily", "dailies", "date"), ("weekly", "weeklies", "week"), ("monthly", "monthlies", "month")):
        latest = disk(f"{kind}-latest")
        index = (disk(index_key) or {}).get("items") if isinstance(disk(index_key), dict) else None
        keys = [x[field] for x in (index if isinstance(index, list) else []) if isinstance(x, dict) and isinstance(x.get(field), str) and x[field]]
        lead_report = (latest or {}).get("report") if isinstance(latest, dict) else None
        for n, k in enumerate(keys[:REPORT_KEEP[kind]]):
            S.report_window[f"{kind}-{k}"] = kind
            doc = latest if isinstance(lead_report, dict) and lead_report.get(field) == k else disk(f"{kind}-{k}")
            if doc: S.guard("report", S.add_report, kind, k, doc, 1 if n == 0 else 3)
            else: S.pending_reports.append((kind, k, 1 if n == 0 else 3))
    meta = disk("lb-meta")
    S.lb_run = meta.get("version") if isinstance(meta, dict) else None
    src = disk("lb-sources")
    docs = [d for d in (disk(f"lb-{b}") for b in ("overall", "coding", "reasoning", "professional", "knowledge")) if d] + [d for d in (disk(k) for k in disk_keys(r"lb-model-.+")) if d]
    S.lb_needs_neon = bool(S.lb_run) and not src and C.get("lbrun") != S.lb_run
    if src or docs: S.guard("model board", S.add_lb, src, docs)

def sweep_names(S):
    """Every source display name held in Neon (stored days and report archives), found by the database itself: a name that is not in
    this run's files must still get its English, because the About page and the source lists show names from all stored days."""
    if not os.environ.get("DATABASE_URL"): return
    out = neon("SELECT DISTINCT n #>> '{}' FROM aihot_mirror, LATERAL jsonb_path_query(data, 'lax $.**.source.name') AS n "
               "WHERE key ~ '^(all|daily|weekly|monthly)-[0-9]'")
    found = [x for x in (out or "").splitlines() if x.strip()]
    if found: log(f"source names in Neon: {len(found)}")
    S.names.update(found)

def fetch_more(S, C, spare_items):
    """From Neon, only when this run's calls could reach it: older all-* days (newest first, as many as the spare calls cover),
    report archives without a finished English object, and the model board of a new round."""
    if not os.environ.get("DATABASE_URL"): return
    last_old = (datetime.fromisoformat(TODAY) - timedelta(days=2)).strftime("%Y-%m-%d")
    on_disk = set(disk_keys(r"all-.+"))
    counts = {}
    for line in (neon("SELECT key, jsonb_array_length(data->'items') FROM aihot_mirror WHERE key ~ '^all-[0-9]'") or "").splitlines():
        if "\t" in line:
            k, n = line.split("\t"); counts[k] = int(n or 0)
    take, budget = [], spare_items
    for k in sorted(counts, reverse=True):
        if budget <= 0: break
        if WINDOW0 <= k[4:] <= last_old and k not in on_disk and C["days"].get(k) != counts[k]: take.append(k); budget -= max(counts[k], 1)
    if take:
        for k, d in neon_docs(take).items(): S.guard("older all-stream day", S.add_day, k, d)
        log(f"read {len(take)} older day(s) from Neon: {', '.join(take)}")
    need = [(kind, k, prio) for kind, k, prio in S.pending_reports if not (C["reports"].get(f"{kind}-{k}") or {}).get("out")]
    if need:
        docs = neon_docs([f"{kind}-{k}" for kind, k, _ in need])
        for kind, k, prio in need:
            if f"{kind}-{k}" in docs: S.guard("report archive", S.add_report, kind, k, docs[f"{kind}-{k}"], prio)
        log(f"read {len(docs)} report archive(s) from Neon")
    if S.lb_needs_neon:
        docs = neon_docs((neon("SELECT key FROM aihot_mirror WHERE key LIKE 'lb-model-%' OR key = 'lb-sources'") or "").split())
        models = [d for k, d in docs.items() if k.startswith("lb-model-")]
        if docs.get("lb-sources") or models: S.guard("model board", S.add_lb, docs.get("lb-sources"), S.lb_boards + models)

def pending_items(C, S):
    n = {}
    for h, t in S.texts.items():
        if h not in C["txt"]: n[t["prio"]] = n.get(t["prio"], 0) + 1
    return n

def summary(run, queued, C):
    by = {}
    for t in run.q.tasks: by.setdefault(t.kind, {}).setdefault(f"P{t.prio}", 0); by[t.kind][f"P{t.prio}"] += 1
    log(f"queued {queued} unit(s); not attempted this run: " + ("none" if not run.q.tasks else "; ".join(f"{k} " + ", ".join(f"{p}={n}" for p, n in sorted(v.items())) for k, v in sorted(by.items()))))
    log("calls this run: " + ", ".join(f"{m.name} {m.calls}/{m.run_cap} (today {C['usage']['calls'].get(m.name, 0)}/{m.day_cap})" for m in run.models) + "; stopped: " + ", ".join(f"{k}: {v}" for k, v in run.stop.items()))
    log(f"answers stored {run.done}, retried {run.retried}, given up {run.failed}")
    log("items still without English by priority (P0 hot list, P1 selected 48h, P2 all-stream 48h, P3 older selected, P4 rest): "
        + (", ".join(f"P{p}={n}" for p, n in sorted(pending_items(C, run.S).items())) or "none"))
    for kind, key, why in run.failures[:25]: log(f"  gave up on {kind} {T.safe_key(str(key))[:60]}: {T.safe(why)}")   # the log is public: a unit named by Chinese text is shown by its hash
    for n, (model, kind, key, why, raw) in enumerate(run.rejects):
        log(T.safe(f"  rejected ({model}) {kind} {T.safe_key(key)}: {why}" + (f" | answer: {raw}" if n < 6 else "")))

def mark_done_days(C, S):
    """A day older than yesterday never changes: once all its items have English (or were given up on) it is not read from Neon again."""
    cutoff = (datetime.fromisoformat(TODAY) - timedelta(days=2)).strftime("%Y-%m-%d")
    for key, hs in S.day_hashes.items():
        if key[4:] <= cutoff and all(h in C["txt"] or given_up(C, "item", h) for h in hs): C["days"][key] = S.day_len.get(key, len(hs))

def main():
    C = load_cache()
    S = Sources(C)
    load_mirror(S, C)
    if not (S.texts or S.stories or S.report_window or S.hot):
        annotate("warning", "no mirror data on disk; the English pass had nothing to do"); return
    S.finish_hot()
    sweep_names(S)
    reused = seed_from_rewrites(C, S)
    cap_calls = sum(max(0, min(m.run_cap, m.day_cap - C["usage"].get("calls", {}).get(m.name, 0))) for m in make_models(C) if not m.blocked) if KEY else 0
    spare = cap_calls * BATCH["item"] - sum(1 for h in S.texts if h not in C["txt"])
    if spare > 0:
        fetch_more(S, C, spare); S.finish_hot(); reused += seed_from_rewrites(C, S)
    for i, v in S.ids.items():   # remember which text each id has; a richer source keeps it
        cur = C["ids"].get(i)
        if v[1] >= WINDOW0 and (cur is None or v[2] <= cur[2]):
            old = None if cur is None else (cur[0] if cur[0] != v[0] else (cur[3] if len(cur) > 3 else None))
            C["ids"][i] = v[:3] + ([old] if old and v[0] not in C["txt"] else [])
    tasks = build_tasks(C, S)
    if S.bad:
        kinds = {}
        for b in S.bad: kinds[b] = kinds.get(b, 0) + 1
        annotate("warning", f"{len(S.bad)} malformed upstream record(s) were skipped one by one: " + ", ".join(f"{k} x{n}" for k, n in sorted(kinds.items())))
    if not KEY: annotate("warning", "GEMINI_API_KEY is not set: no new English was written this run")
    log(f"items: {len(S.texts)} text(s) for {len(S.ids)} id(s), {sum(1 for h in S.texts if h in C['txt'])} already in English ({reused} of them from the rewrite step); "
        f"stories {len(S.stories)}, reports read {len(S.reports)}/{len(S.report_window)}, source names {len(S.names)}, board sources {len(S.lb_src)}")
    run = Run(C, S, tasks)
    run.go()
    summary(run, len(tasks), C)
    if S.lb_src and all(C["lb"].get(k, {}).get("h") == s["h"] for k, s in S.lb_src.items()) and all(s in C["lbtext"] for s in S.lb_strings): C["lbrun"] = S.lb_run
    mark_done_days(C, S)
    prune(C, S)
    log("coverage:", json.dumps(write_all(C, S, run), separators=(",", ":")))

if __name__ == "__main__":
    try: main()
    except Exception as e:
        # Never blocks the sync (exit 0), but it is not silent: a red annotation on the run, and en-meta says so for whoever watches it.
        import traceback
        print(T.safe(f"english text skipped: {type(e).__name__}: {e}"), file=sys.stderr)
        print(T.safe(traceback.format_exc()), file=sys.stderr)
        annotate("error", f"the English pass crashed ({type(e).__name__}); the Chinese data is not affected and Neon keeps the last good English")
        try: dump("en-meta", {"updatedAt": NOW_ISO, "error": type(e).__name__})
        except Exception: pass
