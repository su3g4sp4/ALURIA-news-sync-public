"""Checks for the AINEWS English text pipeline (scripts/en_text.py and scripts/english-aihot.py).

Run: python3 -m unittest scripts/test_en_text.py

What it proves:
  1. The Chinese-character test, punctuation clean-up, number guard and source-name rules behave on the cases the site leaked.
  2. tests/fixtures/en-sample.json (the shape sample the API reads) has no Chinese in any value and follows docs/english-keys.md.
  3. The whole generator runs against a small mirror and a local stand-in for the Gemini API (no network): output with Chinese,
     with an invented number or with a missing field is retried and then left out; nothing Chinese is ever written.
  4. When data/mirror holds generated en-* keys (the sync job, right after the generation step), none of them contains Chinese
     outside the allow-list and all follow the shapes. The workflow runs this before the keys are saved.
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading, unittest, unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import en_text as T

SCRIPT = os.path.join(HERE, "english-aihot.py")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "en-sample.json")
MIRROR = os.environ.get("EN_MIRROR_DIR") or os.path.join(ROOT, "data", "mirror")
KEYS = T.KEYS

# ---- shapes (docs/english-keys.md): the checker lives in en_text.py, the generator uses it on every entry before it writes ------------
shape_errors = T.shape_errors

def cjk_in_values(doc):
    return T.find_cjk(doc)

# ---- 1. pure helpers --------------------------------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_cjk_detection(self):
        for s in ("你好", "AI熱點", "X：Arena (@arena)", "MarkTechPost（RSS）", "價格：", "、", "。", "ｆｕｌｌ", "한국어", "ひらがな", "カタカナ", "𠀀"):
            self.assertTrue(T.has_cjk(s), s)
        for s in ("", "plain English", "naïve café", "Tom's “quote” — dash", "37,999 yuan", "😀"):
            self.assertFalse(T.has_cjk(s), s)

    def test_ideographs_are_told_apart_from_fullwidth_punctuation(self):
        self.assertFalse(T.has_ideograph("AI：news（RSS）"))
        for s in ("價格", "カナ", "한국"): self.assertTrue(T.has_ideograph(s), s)

    def test_allow_list_is_only_the_language_label(self):
        self.assertEqual(T.ALLOW_EXACT, frozenset({"中文"}))
        self.assertFalse(T.has_cjk("中文"))
        self.assertTrue(T.has_cjk("中文版"))

    def test_ascii_punct_on_the_leaked_names(self):
        self.assertEqual(T.ascii_punct("X：Arena (@arena)"), "X: Arena (@arena)")
        self.assertEqual(T.ascii_punct("MarkTechPost（RSS）"), "MarkTechPost (RSS)")
        self.assertEqual(T.ascii_punct("Microsoft：Official Blog（RSS）"), "Microsoft: Official Blog (RSS)")
        self.assertEqual(T.ascii_punct("ａｂｃ１２３"), "abc123")
        self.assertEqual(T.ascii_punct("a，b；c"), "a, b; c")
        self.assertFalse(T.has_cjk(T.ascii_punct("Title：「quoted」（x）、y。")))

    def test_brand(self):
        self.assertNotIn("AIHOT", T.clean("via AIHOT and AI HOT and aihot"))
        self.assertEqual(T.clean("AIHOT news"), "AINEWS news")

    def test_numbers_guard_accepts_faithful_english(self):
        ok = [("800 億元融資", "a raise of 80 billion yuan"), ("年化營收約 500 億美元", "annualized revenue of about $50 billion"),
              ("37999 元", "37,999 yuan"), ("降低 40%", "40% lower"), ("每百萬 $4 和 $20", "$4 and $20 per million"),
              ("5 元/千次", "5 yuan per 1,000 calls"), ("上下文 1.05M", "a 1.05M context window"), ("3萬用戶", "30,000 users"),
              ("三個模型", "3 models"), ("2026 年 11 月 1 日", "November 1, 2026"), ("營收增長 12 倍", "revenue grew 12x"),
              ("低 83 倍的成本 1/83", "at 83 times lower cost, 1/83"), ("DeepSeek-V4.1-Flash-1017", "DeepSeek-V4.1-Flash-1017"),
              ("三成用戶", "30% of users"), ("千萬級", "10 million"), ("兩萬五千人", "25,000 people")]
        for src, en in ok:
            self.assertEqual(T.numbers_ok(src, en), [], (src, en))

    def test_numbers_guard_rejects_invented_numbers(self):
        self.assertEqual(T.numbers_ok("成本降低 40%", "cost down 45%"), ["45"])
        self.assertEqual(T.numbers_ok("發佈新模型", "released 3 new models"), ["3"])
        self.assertEqual(T.numbers_ok("800 億", "8 billion"), ["8 billion"])

    def test_numbers_guard_checks_the_magnitude_not_only_the_digits(self):
        # 億 is 10^8: 500 億 is $50 billion, never $500 million or $500 billion (the same digits at the wrong scale)
        for src, en in (("營收 500 億美元", "revenue of $500 million"), ("營收 500 億美元", "revenue of $500 billion"), ("營收 500 億美元", "revenue of $5 billion"),
                        ("融資 3 億美元", "raised $3 million"), ("融資 3 億美元", "raised $30 million"), ("融資 3 億美元", "raised $3 billion"),
                        ("估值 20 億美元", "valued at $20B"), ("200 億", "$200B"), ("擁有 3 千萬用戶", "30 thousand users"), ("市值 1.5 兆", "$1.5 billion")):
            self.assertNotEqual(T.numbers_ok(src, en), [], (src, en))
        for src, en in (("營收 500 億美元", "revenue of $50 billion"), ("營收 500 億美元", "revenue of $50bn"), ("融資 3 億美元", "raised $300 million"),
                        ("融資 3 億美元", "raised $0.3 billion"), ("200 億美元", "$20B"), ("擁有 3 千萬用戶", "30 million users"), ("市值 1.5 兆", "$1.5 trillion"),
                        ("市值 1.5 兆", "1.5tn"), ("3 億", "300,000,000"), ("30B 參數", "30B parameters"), ("30B 參數", "30 billion parameters"),
                        ("Qwen3-8B", "Qwen3-8B"), ("上下文 128K", "a 128K context"), ("上下文 128K", "128,000 tokens"), ("raises $4 million", "raised $4 million")):
            self.assertEqual(T.numbers_ok(src, en), [], (src, en))

    def test_brand_only_touches_the_standalone_word(self):
        for s in ("Shanghai hotel", "Thai hot pot", "AI hot topics", "Saigon hot spring", "Hanoi hotels", "ai hot takes"):
            self.assertEqual(T.clean(s), s, s)
        self.assertEqual(T.clean("AI HOT"), "AINEWS")
        self.assertEqual(T.clean("via AIHOT today"), "via AINEWS today")
        self.assertEqual(T.clean("aihot.virxact.com"), "AINEWS.virxact.com")
        self.assertEqual(T.clean("(AIHOT)"), "(AINEWS)")
        self.assertEqual(T.clean("AIHOT's list"), "AINEWS's list")

    def test_control_characters_are_dropped_and_nul_is_detected(self):
        self.assertEqual(T.clean("a\x00b\x01c\x7fd\ud800e"), "abcde")
        self.assertEqual(T.clean("para one\n\npara two\ttab"), "para one\n\npara two\ttab")    # newlines and tabs stay
        self.assertTrue(T.has_nul({"a": ["x\x00"]}) and T.has_nul({"k\x00": 1}))
        self.assertFalse(T.has_nul({"a": ["x"]}))

    def test_empty_brackets_can_be_kept(self):
        self.assertEqual(T.ascii_punct("（）"), "")
        self.assertEqual(T.ascii_punct("（）", keep_empty=True), "()")
        self.assertEqual(T.ascii_punct("Name（）"), "Name")

    def test_source_names_by_descriptor_only(self):
        self.assertEqual(T.det_name("DeepSeek：API 更新日誌"), "DeepSeek: API Changelog")
        self.assertEqual(T.det_name("X：Arena (@arena)"), "X: Arena (@arena)")
        self.assertEqual(T.det_name("MarkTechPost（RSS）"), "MarkTechPost (RSS)")
        self.assertEqual(T.det_name("IT之家·人工智能"), "IT Home · AI")
        self.assertEqual(T.det_name("OpenAI：官網動態（RSS · 排除企業/客戶案例）"), "OpenAI: News (RSS · excl. customer stories)")
        self.assertEqual(T.det_name("Anthropic：Newsroom（網頁）"), "Anthropic: Newsroom (Web)")
        self.assertIsNone(T.det_name("公眾號：數字生命卡茲克"))     # a name only a model can write
        self.assertIsNone(T.det_name("X：邵猛 (@shao__meng)"))
        self.assertIsNone(T.det_name("量子位"))

    def test_name_checker(self):
        self.assertEqual(T.name_ok("X：邵猛 (@shao__meng)", "X: Shao Meng (@shao__meng)"), [])
        self.assertTrue(T.name_ok("X：邵猛 (@shao__meng)", "X: Shao Meng"))                 # lost the handle
        self.assertTrue(T.name_ok("量子位", "量子位"))                                       # still Chinese
        self.assertTrue(T.name_ok("量子位", ""))
        self.assertEqual(T.name_ok("量子位", "QbitAI"), [])

    def test_glossary_only_lists_terms_present(self):
        terms = T.load_terms(os.path.join(ROOT, "site", "style", "terms.json"))
        g = T.glossary("開發者用智能體寫程式碼，令牌用量下降", terms)
        self.assertIn("智能體 = AI agent", g)
        self.assertIn("令牌 = token", g)
        self.assertNotIn("伺服器", g)
        self.assertEqual(T.glossary("plain text", terms), "")

    def test_json_parsing_of_model_text(self):
        self.assertEqual(T.parse_json_text('```json\n[{"n": 1}]\n```'), [{"n": 1}])
        self.assertEqual(T.parse_json_text('Here you go: [{"n": 2}] done'), [{"n": 2}])
        self.assertIsNone(T.parse_json_text("no json here"))

    def test_item_hash_matches_the_rewrite_step(self):
        # the real h() of rewrite-aihot.py, imported: if that formula changes, this goes red
        import importlib.util
        spec = importlib.util.spec_from_file_location("rewrite_aihot_under_test", os.path.join(HERE, "rewrite-aihot.py"))
        rw = importlib.util.module_from_spec(spec); spec.loader.exec_module(rw)
        for title, summary, reason in (("t", "s", ""), ("t", "s", "r"), ("標題", "", ""), ("標題", "摘要", "原因"), ("", "", "")):
            self.assertEqual(T.h_item(title, summary, reason), rw.h({"title": title, "summary": summary, "reason": reason}), (title, summary, reason))
        self.assertEqual(rw.h({"title": "t", "summary": "s"}), T.h_item("t", "s"))   # a candidate without a reason key

# ---- 2. the sample the API reads ----------------------------------------------------------------------------------------
class TestSampleFixture(unittest.TestCase):
    def setUp(self):
        if not os.path.exists(FIXTURE): self.skipTest("tests/fixtures/en-sample.json is not part of this checkout (the sync job checks out scripts and site/style only)")
        with open(FIXTURE, encoding="utf-8") as f: self.doc = json.load(f)

    def test_one_example_of_each_key(self):
        self.assertEqual(set(self.doc), set(KEYS) | {"en-meta"})

    def test_no_chinese_in_any_value(self):
        self.assertEqual(cjk_in_values(self.doc), [])

    def test_shapes(self):
        for k in KEYS: self.assertEqual(shape_errors(k, self.doc[k]), [], k)

    def test_report_keys_follow_the_mirror_keys(self):
        self.assertEqual(sorted(self.doc["en-reports"]), ["daily-2026-10-18", "monthly-2026-09", "weekly-2026-W41"])

    def test_no_upstream_name(self):
        self.assertFalse([p for p, s in T.iter_strings(self.doc) if re.search("AIHOT", s, re.I)])

# ---- 3. the generator against a stand-in for Gemini -------------------------------------------------------------------------
NOW = "2026-10-18T03:00:00Z"
AT = "2026-10-18T01:30:00.000Z"

def item(i, title, summary="", reason="", src="X：邵猛 (@shao__meng)", orig="", selected=True, at=AT):
    return {"id": i, "title": title, "originalTitle": orig, "summary": summary, "reason": reason, "source": {"name": src}, "publishedAt": at, "discoveredAt": at,
            "category": "ai-models", "score": 80, "selected": selected, "links": {"original": f"https://example.com/{i}"}}

def mini_mirror(d, marked=True):
    """A small mirror in `d`. With marked=True some items carry words the stand-in model reacts to (answer in Chinese, invent a number);
    marked=False is the clean sample the workflow can run against the real models."""
    def w(name, obj):
        with open(os.path.join(d, f"{name}.json"), "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
    sel = [item("sel-1", "DeepSeek 發佈 V4.1 並降價 40%", "DeepSeek 將 V4.1 升級，API 價格降低 40%，網頁端同步更新。", "讀者可以評估是否切換。"),
           item("failfirst-1", ("FAILFIRST " if marked else "") + "騰訊混元開源 Hy Memory 2.0", "騰訊混元開源記憶框架 Hy Memory 2.0。"),
           item("badnum-1", ("BADNUM " if marked else "") + "月之暗面發佈 Kimi K3", "月之暗面發佈 Kimi K3 模型。"),
           item("failalways-1", ("FAILALWAYS " if marked else "") + "智譜發佈 GLM 5.3", "智譜發佈新模型。"),
           item("units-1", "卡茲克實測華碩 ProArt 創16 RTX Spark 電腦，37999 元實現本地 Token 自由",
                "作者分享受邀參加英偉達 RTX Spark 全球開售活動後，深度使用一週華碩 ProArt 創16（128G 統一內存+1T，京東售價 37999 元）的體驗，開售時 128G 版本在京東幾分鐘內被搶空。",
                "作者親測 RTX Spark 上的本地模型部署路徑，並把整套流程封裝成 WorkBuddy 專家，讀者可以照著復現本地 Token 自由的用法。", src="X：卡茲克 (@Khazix0918)"),
           item("units-2", "OpenAI 年化營收約 500 億美元並洽談新融資，DeepSeek 傳出融資 800 億元", "OpenAI 年化營收約 500 億美元，正在洽談新一輪融資；DeepSeek 被曝接近完成至少 800 億元融資，Anthropic 同期發佈 3 款新模型。",
                src="公眾號：數字生命卡茲克")]
    w("items", {"items": sel})
    day = [item("english-1", "Mistral releases Large 4", "Mistral released Large 4 with an open license.", "", src="Mistral：官方博客（RSS）"),
           item("english-2", "Mistral：Large 4 is out", "Open weights：yes.", ""),
           item("day-2", "阿里雲發佈通義千問 Qwen3.8 系列", "阿里雲開源 3 款 Qwen3.8 模型。", "", src="公眾號：數字生命卡茲克", selected=False)] + sel
    w("all-2026-10-18", {"date": "2026-10-18", "items": day})
    hot = {"items": [{"rank": 1, "id": "hot-1", "title": "騰訊混元發佈並開源 Hy Memory 2.0", "source": {"name": "DeepSeek：API 更新日誌"}, "links": {"aihot": "https://aihot.news/items/hot-1", "original": "https://example.com/hot", "story": "https://x/story/uuid-1"},
                    "sourceCount": 2, "participantCount": 5, "sourceNames": ["X：邵猛 (@shao__meng)", "MarkTechPost（RSS）"], "latestAt": AT}]}
    w("hot-topics", hot)
    story = {"publicId": "hot-1", "title": "騰訊混元發佈並開源 Hy Memory 2.0", "latest": "騰訊混元開源 Hy Memory 2.0", "digest": "騰訊混元於 10 月 16 日開源 Hy Memory 2.0。\n\n包含 3 個元件。",
             "reports": [{"id": "rep-1", "title": "騰訊混元開源 Hy Memory 2.0", "summary": "騰訊混元團隊開源 Hy Memory 2.0。", "source": {"name": "X：邵猛 (@shao__meng)", "firstParty": False}, "publishedAt": AT}],
             "storyline": [], "related": []}
    w("stories", {"byItem": {"hot-1": story}, "byStory": {}})
    rep = lambda i, t, s: {"title": t, "summary": s, "source": {"name": "Anthropic：Newsroom（網頁）"}, "links": {"aihot": f"https://aihot.news/items/{i}", "original": f"https://example.com/{i}"}, "publishedAt": AT}
    daily = {"report": {"date": "2026-10-18", "generatedAt": AT, "lead": {"title": "DeepSeek 發佈 V4.1 並降價 40%", "leadParagraph": "DeepSeek 將 V4.1 升級，API 價格降低 40%。"},
                        "sections": [{"label": "模型發佈/更新", "items": [rep("sel-1", "DeepSeek 發佈 V4.1 並降價 40%", "DeepSeek 將 V4.1 升級，API 價格降低 40%，網頁端同步更新。")]}],
                        "flashes": [{"title": "阿里雲發佈通義千問 Qwen3.8 系列", "source": {"name": "公眾號：數字生命卡茲克"}, "links": {"aihot": "https://aihot.news/items/day-2", "original": "https://example.com/day-2"}, "publishedAt": AT}]}}
    w("daily-latest", daily); w("dailies", {"items": [{"date": "2026-10-18", "leadTitle": "DeepSeek 發佈 V4.1 並降價 40%"}]})
    weekly = {"report": {"week": "2026-W41", "headline": "OpenAI 向全部 ChatGPT 用戶推出 GPT-6", "overview": "本週 OpenAI 推出 GPT-6，DeepSeek 傳出融資 800 億元。",
                         "sections": [{"label": "AI實驗室競逐企業部署", "summary": "兩家公司各成立一家合資公司。", "items": [rep("wk-1", "Anthropic 發佈 Claude Haiku 5.5", "Anthropic 發佈 Claude Haiku 5.5，成本降低約 75%。")]}]}}
    w("weekly-latest", weekly); w("weeklies", {"items": [{"week": "2026-W41", "headline": "OpenAI 向全部 ChatGPT 用戶推出 GPT-6"}]})
    w("monthlies", {"items": []})
    w("lb-sources", {"runId": "run-1", "sources": {"arena-creative-writing": {"name": "Arena 创作盲选", "operator": "LMArena", "description": "真人盲選故事、詩歌和其他創意表達。"},
                                                   "lhtb": {"name": "LHTB", "operator": "Tencent HY / Lehigh 等", "description": "在較長時間內持續使用終端。"}}})
    w("lb-meta", {"version": "run-1"})
    w("lb-overall", {"key": "overall", "entries": [], "more": [], "pending": [{"model": {"slug": "m", "name": "M"}, "reason": "編程評測還不到 3 類"}]})

LATIN = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*|\d[\d,.]*")

class Stub(BaseHTTPRequestHandler):
    """A stand-in for the Gemini generateContent endpoint: answers from the Latin words and numbers of the input."""
    calls = []
    seen_first = set()
    def log_message(self, *a): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["contents"][0]["parts"][0]["text"]
        rows, _ = json.JSONDecoder().raw_decode(prompt.split("INPUT:\n", 1)[1])
        Stub.calls.append({"model": self.path, "prompt": prompt, "rows": rows})
        out = [self.answer(r) for r in rows]
        data = json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(out)}]}}], "usageMetadata": {"totalTokenCount": 100}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def fake(self, zh, marker=None):
        if not zh: return ""
        words = [w for w in LATIN.findall(zh) if w not in ("FAILFIRST", "BADNUM", "FAILALWAYS")]
        return "Stub: " + " ".join(words or ["news"])

    def answer(self, r):
        n = r["n"]
        if "digest" in r:
            return {"n": n, "title": self.fake(r["title"]), "latest": self.fake(r["latest"]), "digest": "\n\n".join(self.fake(p) for p in r["digest"].split("\n\n"))}
        if "fields" in r:
            return {"n": n, "fields": [{"k": f["k"], "en": self.fake(f["text"])} for f in r["fields"]],
                    "sections": [{"label": s.get("useLabel") or "Stub section", "summary": self.fake(s["summary"])} for s in r["sections"]]}
        if "description" in r:
            return {"n": n, "name": r["name"] if not T.has_cjk(r["name"]) else "Stub benchmark", "what": "Measures " + self.fake(r["description"]), "operator": "Tencent HY / Lehigh and others" if T.has_cjk(r["operator"]) else r["operator"]}
        if "text" in r:
            return {"n": n, "en": self.fake(r["text"])}
        if "name" in r and "title" not in r:
            hd = re.findall(r"@\w+", r["name"])
            return {"n": n, "en": "WeChat account: Stub Name" if r["name"].startswith("公眾號") else "X: Stub Name" + (f" ({hd[0]})" if hd else "")}
        title = r["title"]
        if "FAILALWAYS" in title: return {"n": n, "t": "還是中文", "s": "仍然是中文", "r": ""}
        if "FAILFIRST" in title and title not in Stub.seen_first:
            Stub.seen_first.add(title); return {"n": n, "t": "中文標題", "s": self.fake(r["summary"]), "r": ""}
        if "BADNUM" in title: return {"n": n, "t": self.fake(title) + " 99999", "s": self.fake(r["summary"]), "r": ""}
        return {"n": n, "t": self.fake(title), "s": self.fake(r["summary"]), "r": self.fake(r["reason"])}

class TestGeneratorEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Stub.calls, Stub.seen_first = [], set()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.tmp = tempfile.mkdtemp(prefix="en-test-")
        cls.mirror = os.path.join(cls.tmp, "mirror"); os.makedirs(cls.mirror)
        mini_mirror(cls.mirror)
        cls.cache = os.path.join(cls.tmp, "cache-prev.json")
        cls.run1 = cls.run_generator()
        cls.calls1 = list(Stub.calls)
        shutil.copy(os.path.join(cls.mirror, "en-cache.json"), cls.cache)
        cls.out1 = {k: cls.read(k) for k in KEYS + ("en-meta",)}
        Stub.calls.clear()
        cls.run2 = cls.run_generator()
        cls.calls2 = list(Stub.calls)
        cls.out2 = {k: cls.read(k) for k in KEYS + ("en-meta",)}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def read(cls, key):
        with open(os.path.join(cls.mirror, f"{key}.json"), encoding="utf-8") as f: return json.load(f)

    @classmethod
    def run_generator(cls):
        env = {**os.environ, "EN_MIRROR_DIR": cls.mirror, "EN_CACHE_PREV": cls.cache, "EN_CACHE_REQUIRED": "0", "EN_API_BASE": f"http://127.0.0.1:{cls.server.server_port}/v1beta",
               "GEMINI_API_KEY": "test-key", "EN_NOW": NOW, "EN_LITE_PACE": "0", "EN_GEMMA_PACE": "0", "EN_GEMMA_MODEL": "gemma-test", "EN_BATCH": "3",
               "REWRITES_PREV": os.path.join(cls.tmp, "none.json"), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        env.pop("DATABASE_URL", None)
        r = subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        return r

    def test_script_exits_zero_and_logs(self):
        self.assertEqual(self.run1.returncode, 0, self.run1.stderr)
        self.assertIn("calls this run:", self.run1.stdout)
        self.assertIn("items still without English by priority", self.run1.stdout)

    def test_every_key_is_written_with_the_documented_shape(self):
        for k in KEYS: self.assertEqual(shape_errors(k, self.out1[k]), [], k)

    def test_no_chinese_in_any_generated_value(self):
        for k, doc in self.out1.items(): self.assertEqual(cjk_in_values(doc), [], k)
        for k, doc in self.out2.items(): self.assertEqual(cjk_in_values(doc), [], k)

    def test_items_get_english(self):
        it = self.out1["en-items"]
        for i in ("sel-1", "day-2", "wk-1"):
            self.assertIn(i, it, i)
        self.assertTrue(it["sel-1"]["t"].startswith("Stub:"))
        self.assertIn("40", it["sel-1"]["t"])                      # numbers of the source survive

    def test_hot_list_headline_lives_in_the_story_not_under_the_item_id(self):
        # the hot entry's headline is the event headline; en-items[<hot id>] would be the headline of the item behind the id
        self.assertNotIn("hot-1", self.out1["en-items"])
        self.assertTrue(self.out1["en-stories"]["hot-1"]["title"].startswith("Stub:"))

    def test_nothing_printed_has_chinese(self):
        for run in (self.run1, self.run2): self.assertEqual(T.find_cjk({"out": run.stdout, "err": run.stderr}), [])
        self.assertIn("rejected", self.run1.stdout)                  # the rejected answers are still logged, with '?' for the Chinese

    def test_item_that_is_already_english_costs_no_model_call(self):
        self.assertEqual(self.out1["en-items"]["english-1"]["t"], "Mistral releases Large 4")
        self.assertFalse([c for c in self.calls1 + self.calls2 if "english-1" in json.dumps(c["rows"])])

    def test_english_with_fullwidth_punctuation_is_fixed_without_a_model(self):
        self.assertEqual(self.out1["en-items"]["english-2"], {"t": "Mistral: Large 4 is out", "s": "Open weights: yes."})
        self.assertFalse([c for c in self.calls1 + self.calls2 if "english-2" in json.dumps(c["rows"])])

    def test_chinese_answer_is_retried_with_the_reason_then_accepted(self):
        self.assertIn("failfirst-1", self.out1["en-items"])
        retried = [c for c in self.calls1 if "Your last answer was rejected" in c["prompt"] and "Chinese characters" in c["prompt"]]
        self.assertTrue(retried)

    def test_answers_that_stay_bad_are_left_out_never_filled(self):
        for i in ("failalways-1", "badnum-1"):
            self.assertNotIn(i, self.out1["en-items"], i)
            self.assertNotIn(i, self.out2["en-items"], i)
        self.assertIn("numbers not in the source", "".join(c["prompt"] for c in self.calls1))

    def test_second_run_asks_only_for_what_is_missing(self):
        asked = json.dumps([c["rows"] for c in self.calls2], ensure_ascii=False)
        self.assertNotIn("sel-1", asked)
        self.assertNotIn("DeepSeek 發佈 V4.1", asked)               # finished items come from the cache
        self.assertIn("FAILALWAYS", asked)                          # failed units are retried on the next run
        self.assertEqual(self.out2["en-items"]["sel-1"], self.out1["en-items"]["sel-1"])
        self.assertLess(len(self.calls2), len(self.calls1))

    def test_source_names(self):
        n = self.out1["en-names"]
        self.assertEqual(n["DeepSeek：API 更新日誌"], "DeepSeek: API Changelog")              # settled by descriptor rules, no model
        self.assertEqual(n["MarkTechPost（RSS）"], "MarkTechPost (RSS)")
        self.assertTrue(n["X：邵猛 (@shao__meng)"].endswith("(@shao__meng)"))
        self.assertTrue(n["公眾號：數字生命卡茲克"].startswith("WeChat account:"))
        self.assertTrue(n["Mistral：官方博客（RSS）"].startswith("Mistral: Official Blog"))
        self.assertNotIn("DeepSeek: API Changelog", json.dumps([c["rows"] for c in self.calls1]))

    def test_story_has_title_latest_digest_and_timeline(self):
        st = self.out1["en-stories"]["hot-1"]
        self.assertTrue(st["title"] and st["latest"] and st["digest"])
        self.assertEqual(st["digest"].count("\n\n"), 1)                                      # paragraph breaks kept
        self.assertIn("rep-1", st["timeline"])

    def test_reports(self):
        r = self.out1["en-reports"]
        self.assertEqual(sorted(r), ["daily-2026-10-18", "weekly-2026-W41"])
        d = r["daily-2026-10-18"]
        self.assertTrue(d["lead"]["title"] and d["lead"]["leadParagraph"])
        self.assertEqual(d["sections"][0]["label"], "Model releases")                        # fixed wording for the standard labels
        self.assertEqual(d["sections"][0]["items"][0]["id"], "sel-1")
        self.assertEqual(d["flashes"][0]["id"], "day-2")
        w = r["weekly-2026-W41"]
        self.assertTrue(w["headline"] and w["overview"] and w["sections"][0]["label"])

    def test_model_board(self):
        lb = self.out1["en-lb"]["arena-creative-writing"]
        self.assertTrue(lb["name"] and lb["what"])
        self.assertEqual(self.out1["en-lb"]["lhtb"]["operator"], "Tencent HY / Lehigh and others")
        self.assertEqual(self.out1["en-names"]["Arena 创作盲选"], lb["name"])                      # benchmark names with Chinese are source names too
        self.assertEqual(self.out1["en-names"]["Tencent HY / Lehigh 等"], "Tencent HY / Lehigh and others")
        self.assertIn("編程評測還不到 3 類", self.out1["en-lb-text"])
        self.assertIn("3", self.out1["en-lb-text"]["編程評測還不到 3 類"])

    def test_meta_reports_coverage_and_what_was_left(self):
        m = self.out1["en-meta"]
        self.assertIn("coverage", m); self.assertIn("usage", m); self.assertIn("run", m)
        self.assertIn("itemsWithoutEnglish", m)
        self.assertGreaterEqual(m["usage"]["calls"].get("lite", 0) + m["usage"]["calls"].get("gemma", 0), 1)

    def test_the_stand_in_was_the_only_endpoint(self):
        self.assertTrue(self.calls1)
        self.assertTrue(all("/models/" in c["model"] for c in self.calls1))

    def test_cache_is_written_and_readable(self):
        with open(os.path.join(self.mirror, "en-cache.json"), encoding="utf-8") as f: c = json.load(f)
        self.assertEqual(c["v"], 1); self.assertIn("z", c)

# ---- 3b. the parts that read Neon, against a stand-in for the database ----------------------------------------------------
def load_generator(mirror_dir, cache):
    """english-aihot.py as a module (its settings are read at import, so they are set around it)."""
    import importlib.util
    names = ("EN_MIRROR_DIR", "EN_NOW", "EN_CACHE_PREV", "GEMINI_API_KEY")
    keep = {k: os.environ.get(k) for k in names}
    os.environ.update({"EN_MIRROR_DIR": mirror_dir, "EN_NOW": NOW, "EN_CACHE_PREV": cache, "GEMINI_API_KEY": ""})
    try:
        spec = importlib.util.spec_from_file_location("english_aihot_under_test", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in keep.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
    return mod

class TestNeonReads(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="en-neon-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = os.path.join(self.tmp, "m"); os.makedirs(self.dir)
        mini_mirror(self.dir)
        self.mod = load_generator(self.dir, os.path.join(self.tmp, "none.json"))
        self.sql, self.names, self.old_days, self.docs, self.lb_keys = [], [], {}, {}, []
        self.mod.neon = self.fake_neon
        patcher = unittest.mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://example.invalid/db"})
        patcher.start(); self.addCleanup(patcher.stop)

    def fake_neon(self, sql):
        self.sql.append(sql)
        if "jsonb_path_query" in sql: return "\n".join(self.names) + "\n"
        if "jsonb_array_length" in sql: return "".join(f"{k}\t{len(d['items'])}\n" for k, d in self.old_days.items())
        if "WHERE key IN" in sql:
            keys = re.findall(r"'([A-Za-z0-9-]+)'", sql)
            return "".join(f"{k}\t{json.dumps(self.docs[k], ensure_ascii=False)}\n" for k in keys if k in self.docs)
        if "lb-model-%" in sql: return "\n".join(self.lb_keys) + "\n"
        return ""

    def sources(self):
        C = self.mod.fresh_cache(); S = self.mod.Sources(C); self.mod.load_mirror(S, C)
        return C, S

    def test_every_source_name_neon_holds_is_collected(self):
        self.names = ["X：舊日期的來源 (@old_one)", "Plain Name", ""]
        _, S = self.sources()
        self.mod.sweep_names(S)
        self.assertIn("X：舊日期的來源 (@old_one)", S.names); self.assertIn("Plain Name", S.names); self.assertNotIn("", S.names)
        self.assertTrue(any("jsonb_path_query" in q for q in self.sql))

    def test_nothing_is_read_without_a_database(self):
        del os.environ["DATABASE_URL"]
        C, S = self.sources()
        self.mod.sweep_names(S); self.mod.fetch_more(S, C, 100)
        self.assertEqual(self.sql, [])

    def test_older_days_are_read_newest_first_inside_the_window_and_the_spare_budget(self):
        def day(d, n): return {"items": [item(f"{d}-{i}", f"舊聞 {d} {i}", "摘要", at="2026-10-12T04:00:00.000Z") for i in range(n)]}
        self.old_days = {"all-2026-10-17": day("d17", 2), "all-2026-10-16": day("d16", 3), "all-2026-10-15": day("d15", 5), "all-2026-09-01": day("d01", 2)}
        self.docs = dict(self.old_days)
        C, S = self.sources()
        C["days"]["all-2026-10-17"] = 2                    # finished earlier: not read again
        self.mod.fetch_more(S, C, 4)
        self.assertEqual(sorted(S.day_hashes), ["all-2026-10-15", "all-2026-10-16", "all-2026-10-18"])
        self.assertTrue(all(S.texts[h]["prio"] == 4 for k in ("all-2026-10-15", "all-2026-10-16") for h in S.day_hashes[k]))   # older than 48 h: the rest of the window
        self.assertFalse([k for k in S.day_hashes if k.endswith("09-01")])                                                     # outside the 14-day window

    def test_report_archives_not_on_disk_are_read(self):
        with open(os.path.join(self.dir, "dailies.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"date": "2026-10-18"}, {"date": "2026-10-17"}]}, f)
        old = {"report": {"date": "2026-10-17", "lead": {"title": "舊日報標題", "leadParagraph": "舊日報導言。"}, "sections": [{"label": "行業動態", "items": [item("old-1", "舊日報條目", "條目摘要")]}]}}
        self.docs = {"daily-2026-10-17": old}
        C, S = self.sources()
        self.assertEqual([(k, d) for k, d, _ in S.pending_reports], [("daily", "2026-10-17")])
        self.mod.fetch_more(S, C, 10)
        self.assertIn("daily-2026-10-17", S.reports)
        self.assertEqual(S.reports["daily-2026-10-17"]["sections"][0]["items"][0]["id"], "old-1")

    def test_a_weekly_archive_with_a_capital_in_its_key_is_read(self):
        with open(os.path.join(self.dir, "weeklies.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"week": "2026-W41"}, {"week": "2026-W40"}]}, f)
        self.docs = {"weekly-2026-W40": {"report": {"week": "2026-W40", "headline": "上週標題", "overview": "上週導言。", "sections": []}}}
        C, S = self.sources()
        self.mod.fetch_more(S, C, 10)
        self.assertIn("weekly-2026-W40", S.reports)
        self.assertTrue(any("weekly-2026-W40" in q for q in self.sql))

    def test_model_board_of_a_new_round_is_read_when_it_is_not_on_disk(self):
        os.remove(os.path.join(self.dir, "lb-sources.json"))
        self.lb_keys = ["lb-sources", "lb-model-m1"]
        self.docs = {"lb-sources": {"sources": {"vals-tax": {"name": "Vals Tax", "operator": "Vals AI", "description": "稅務專業問題"}}},
                     "lb-model-m1": {"overall": {"pendingReason": "編程評測還不到 3 類"}, "evidence": [{"items": [{"config": "Max 推理", "name": "Harvey 法律 Agent 基准"}]}]}}
        C, S = self.sources()
        self.assertTrue(S.lb_needs_neon)
        self.mod.fetch_more(S, C, 10)
        self.assertIn("vals-tax", S.lb_src)
        self.assertIn("Max 推理", S.lb_strings); self.assertIn("Harvey 法律 Agent 基准", S.lb_strings)

# ---- 3c. the checks applied to each model answer ----------------------------------------------------------------------------
class TestAnswerChecks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="en-answer-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.mod = load_generator(tmp, os.path.join(tmp, "none.json"))
        self.C = self.mod.fresh_cache()

    def report_task(self):
        data = {"kind": "daily", "fields": {"leadTitle": "DeepSeek 降價 40%", "leadParagraph": "DeepSeek 將 API 價格降低 40%。"},
                "sections": [{"label": "模型發佈/更新", "summary": "", "items": []}], "h": "hh"}
        return self.mod.Task("report", "daily-2026-10-18", data, 1)

    def test_report_fields_come_back_as_a_list_of_pairs(self):
        t = self.report_task()
        ok = {"n": 1, "fields": [{"k": "leadTitle", "en": "DeepSeek cuts prices 40%"}, {"k": "leadParagraph", "en": "DeepSeek cut its API prices by 40%."}], "sections": [{"label": "x", "summary": ""}]}
        self.assertIsNone(self.mod.apply_answer(self.C, t, ok))
        self.assertEqual(self.C["reports"]["daily-2026-10-18"]["f"]["leadParagraph"], "DeepSeek cut its API prices by 40%.")
        self.assertEqual(self.C["reports"]["daily-2026-10-18"]["sec"][0]["label"], "Model releases")

    def test_report_with_an_empty_field_is_refused_and_the_reason_names_it(self):
        t = self.report_task()
        bad = {"n": 1, "fields": [{"k": "leadTitle", "en": "DeepSeek cuts prices 40%"}, {"k": "leadParagraph", "en": ""}], "sections": [{"label": "x", "summary": ""}]}
        err = self.mod.apply_answer(self.C, t, bad)
        self.assertIn("leadParagraph", err)
        self.assertNotIn("daily-2026-10-18", self.C["reports"])

    def test_report_in_the_older_object_form_is_still_understood(self):
        t = self.report_task()
        old = {"n": 1, "fields": {"leadTitle": "DeepSeek cuts prices 40%", "leadParagraph": "DeepSeek cut API prices by 40%."}, "sections": [{"label": "x", "summary": ""}]}
        self.assertIsNone(self.mod.apply_answer(self.C, t, old))

    def test_report_with_a_wrong_number_of_sections_is_refused(self):
        t = self.report_task()
        bad = {"n": 1, "fields": [{"k": "leadTitle", "en": "a 40%"}, {"k": "leadParagraph", "en": "b 40%"}], "sections": []}
        self.assertIn("sections must have 1", self.mod.apply_answer(self.C, t, bad))

    def item_task(self, title, summary="", reason=""):
        return self.mod.Task("item", "h1", {"title": title, "summary": summary, "reason": reason, "orig": "", "prio": 1, "order": 0}, 1)

    def test_item_answers(self):
        t = self.item_task("OpenAI 年化營收約 500 億美元", "OpenAI 年化營收約 500 億美元，洽談新融資。")
        self.assertIsNone(self.mod.apply_answer(self.C, t, {"n": 1, "t": "OpenAI reaches $50 billion in annualized revenue", "s": "OpenAI's annualized revenue is about $50 billion, and it is in talks on new funding.", "r": ""}))
        self.assertEqual(self.C["txt"]["h1"]["t"], "OpenAI reaches $50 billion in annualized revenue")
        self.assertNotIn("r", self.C["txt"]["h1"])           # no reason in the source: none stored
        self.assertIn("numbers not in the source", self.mod.apply_answer(self.C, self.item_task("OpenAI 年化營收約 500 億美元"), {"n": 1, "t": "OpenAI reaches $5 billion", "s": "", "r": ""}))
        self.assertIn("Chinese characters", self.mod.apply_answer(self.C, self.item_task("OpenAI 發佈新模型"), {"n": 1, "t": "OpenAI 發佈 a model", "s": "", "r": ""}))
        self.assertIn("summary", self.mod.apply_answer(self.C, self.item_task("OpenAI 發佈新模型", "摘要"), {"n": 1, "t": "OpenAI releases a model", "s": "", "r": ""}))

    def test_long_chinese_summaries_may_have_long_english(self):
        zh = "摘要" * 450                                            # 900 characters
        t = self.item_task("標題", zh)
        long_en = "A sentence of plain English that goes on. " * 40   # about 1,640 characters
        self.assertIsNone(self.mod.apply_answer(self.C, t, {"n": 1, "t": "Headline", "s": long_en, "r": ""}))

    def test_nul_and_other_control_characters_never_reach_the_stored_text(self):
        t = self.item_task("OpenAI 發佈新模型", "摘要")
        self.assertIsNone(self.mod.apply_answer(self.C, t, {"n": 1, "t": "OpenAI re\x00leases a \x07model", "s": "A sum\x00mary.", "r": ""}))
        self.assertEqual(self.C["txt"]["h1"], {"t": "OpenAI releases a model", "s": "A summary.", "u": self.C["txt"]["h1"]["u"]})

    def test_a_number_at_the_wrong_scale_is_refused_with_the_reason(self):
        t = self.item_task("OpenAI 年化營收約 500 億美元")
        err = self.mod.apply_answer(self.C, t, {"n": 1, "t": "OpenAI revenue reaches $500 million", "s": "", "r": ""})
        self.assertIn("numbers not in the source: 500 million", err)
        self.assertIsNone(self.mod.apply_answer(self.C, t, {"n": 1, "t": "OpenAI revenue reaches $50 billion", "s": "", "r": ""}))

    def test_a_section_without_a_label_gets_none(self):
        data = {"kind": "weekly", "fields": {"headline": "標題"}, "sections": [{"label": "", "summary": "", "items": []}], "h": "hh"}
        t = self.mod.Task("report", "weekly-2026-W41", data, 1)
        ok = {"n": 1, "fields": [{"k": "headline", "en": "A headline"}], "sections": [{"label": "Invented label", "summary": ""}]}
        self.assertIsNone(self.mod.apply_answer(self.C, t, ok))
        self.assertEqual(self.C["reports"]["weekly-2026-W41"]["sec"][0]["label"], "")

    def test_a_published_entry_with_the_upstream_name_is_dropped(self):
        outs = {"en-items": {"a": {"t": "Fine"}, "b": {"t": "via AIHOT"}, "c": {"t": "中文版"}}}
        self.assertEqual(self.mod.scrub_outputs(outs), 2)
        self.assertEqual(list(outs["en-items"]), ["a"])

# ---- 3d. malformed upstream data, quota, publishing rules -----------------------------------------------------------------
class RateLimitStub(Stub):
    """Answers 429 (the per-day quota is gone) for every call."""
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        data = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}).encode()
        self.send_response(429); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

def read_cache(path):
    import base64, gzip
    with open(path, encoding="utf-8") as f: c = json.load(f)
    return json.loads(gzip.decompress(base64.b64decode(c["z"])))

class GeneratorHarness(unittest.TestCase):
    handler = Stub

    @classmethod
    def setUpClass(cls):
        Stub.calls, Stub.seen_first = [], set()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.tmp = tempfile.mkdtemp(prefix="en-harness-")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); shutil.rmtree(cls.tmp, ignore_errors=True)

    def new_mirror(self, name):
        d = os.path.join(self.tmp, name); os.makedirs(d, exist_ok=True)
        return d

    def write(self, d, name, obj):
        with open(os.path.join(d, f"{name}.json"), "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)

    def read(self, d, key):
        with open(os.path.join(d, f"{key}.json"), encoding="utf-8") as f: return json.load(f)

    def run_generator(self, d, **env_extra):
        cache = os.path.join(d, "cache-prev.json")
        if os.path.exists(os.path.join(d, "en-cache.json")): shutil.copy(os.path.join(d, "en-cache.json"), cache)
        env = {**os.environ, "EN_MIRROR_DIR": d, "EN_CACHE_PREV": cache, "EN_CACHE_REQUIRED": "0", "EN_API_BASE": f"http://127.0.0.1:{self.server.server_port}/v1beta",
               "GEMINI_API_KEY": "test-key", "EN_NOW": NOW, "EN_LITE_PACE": "0", "EN_GEMMA_PACE": "0", "EN_GEMMA_MODEL": "gemma-test", "EN_BATCH": "3",
               "REWRITES_PREV": os.path.join(self.tmp, "none.json"), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **env_extra}
        env.pop("DATABASE_URL", None)
        return subprocess.run([sys.executable, SCRIPT], env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)

class TestMalformedUpstreamData(GeneratorHarness):
    """Odd shapes in the upstream documents skip the odd record, never the whole run."""
    def odd_mirror(self, name):
        d = self.new_mirror(name); mini_mirror(d)
        rep = lambda i, t, aihot: {"title": t, "summary": t + " 摘要", "source": {"name": "Anthropic：Newsroom（網頁）"}, "links": {"aihot": aihot, "original": f"https://example.com/{i}"}, "publishedAt": AT}
        daily = {"report": {"date": "2026-10-18", "generatedAt": AT, "lead": {"title": "DeepSeek 發佈 V4.1 並降價 40%", "leadParagraph": "DeepSeek 將 V4.1 升級，API 價格降低 40%。"},
                            "sections": [{"label": "", "items": [rep("sel-1", "DeepSeek 發佈 V4.1 並降價 40%", "https://aihot.news/items/sel-1"),
                                                                 rep("x", "沒有編號的條目 40", None),                       # links.aihot is null
                                                                 {"title": "連 links 都沒有的條目", "summary": "摘要"}]},      # no links at all
                                         {"label": "行業動態", "items": [rep("sel-2", "OpenAI 發佈 GPT-6", "https://aihot.news/items/sel-2")]}],
                            "flashes": [rep("day-2", "阿里雲發佈通義千問 Qwen3.8 系列", "https://aihot.news/items/day-2"), rep("y", "沒編號的快訊", None)]}}
        self.write(d, "daily-latest", daily)
        self.write(d, "weekly-latest", {"report": {"week": "2026-W41", "headline": "OpenAI 向全部 ChatGPT 用戶推出 GPT-6", "overview": "本週 OpenAI 推出 GPT-6。", "lead": ["not", "a", "dict"],
                                                   "sections": [{"label": "行業動態", "summary": "兩家公司。", "items": "oops"}, "oops"]}})
        hot = self.read(d, "hot-topics")
        hot["items"] += [{"rank": 2, "id": "hot-2", "title": "OpenAI 發佈 GPT-6 並調降價格 50%", "source": {"name": "X：Arena (@arena)"}, "links": {"aihot": None, "story": None}, "sourceNames": "oops"},
                         {"rank": 3, "id": None, "title": "沒有編號的熱榜", "links": None}]
        self.write(d, "hot-topics", hot)
        sel = self.read(d, "items")
        sel["items"] += [{"id": 7, "title": None}, {"id": "odd-1", "title": "標題 5", "summary": ["not", "text"], "reason": 3, "discoveredAt": 12345, "links": "oops"}, "oops", None]
        self.write(d, "items", sel)
        stories = self.read(d, "stories")
        stories["byItem"]["hot-9"] = {"title": 5, "latest": None, "digest": ["x"], "reports": "oops"}
        stories["byItem"]["hot-8"] = "oops"
        self.write(d, "stories", stories)
        self.write(d, "lb-overall", {"key": "overall", "entries": [], "excluded": [{"name": "（）", "reason": "（）"}], "pending": [{"model": {"slug": "m", "name": "M"}, "reason": "編程評測還不到 3 類"}]})
        return d

    def test_odd_records_are_skipped_and_everything_else_is_written(self):
        d = self.odd_mirror("odd")
        r = self.run_generator(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("english text skipped", r.stderr)
        for k in KEYS: self.assertEqual(shape_errors(k, self.read(d, k)), [], k)
        self.assertEqual(T.find_cjk({"o": r.stdout, "e": r.stderr}), [])
        items = self.read(d, "en-items")
        for i in ("sel-1", "sel-2", "day-2", "odd-1"): self.assertIn(i, items, i)
        # reports: the entries without an id are left out, a section without a label has no label key, the other sections are complete
        daily = self.read(d, "en-reports")["daily-2026-10-18"]
        self.assertEqual([x["id"] for x in daily["sections"][0]["items"]], ["sel-1"])
        self.assertNotIn("label", daily["sections"][0])
        self.assertEqual(daily["sections"][1]["label"], "Industry")
        self.assertEqual([x["id"] for x in daily["flashes"]], ["day-2"])
        self.assertTrue(self.read(d, "en-reports")["weekly-2026-W41"]["headline"])
        # a punctuation-only board string stays punctuation, never an empty value
        lbt = self.read(d, "en-lb-text")
        self.assertEqual(lbt["（）"], "()")
        self.assertTrue(all(lbt.values()))
        # the hot entry whose story page does not exist still gets its headline (en-stories[<hot id>].title); it is not stored as an item
        st = self.read(d, "en-stories")
        self.assertTrue(st["hot-2"]["title"]); self.assertEqual(st["hot-2"]["timeline"], {})
        self.assertNotIn("hot-2", items)
        self.assertIn("hot-1", st)
        self.assertEqual(self.read(d, "en-meta")["skipped"]["entries"], 0)

    def test_a_document_of_the_wrong_type_is_skipped_with_a_visible_warning(self):
        d = self.new_mirror("wrongtype"); mini_mirror(d)
        self.write(d, "hot-topics", [1, 2, 3])                         # a list where an object is expected
        r = self.run_generator(d)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("::warning title=English edition::", r.stdout)
        self.assertIn("malformed upstream record(s)", r.stdout)
        self.assertGreaterEqual(self.read(d, "en-meta")["skipped"]["records"], 1)
        self.assertIn("sel-1", self.read(d, "en-items"))               # the rest of the run went on

class TestReportsStayPublished(GeneratorHarness):
    def test_reports_survive_a_missing_index_and_a_changed_chinese_text(self):
        d = self.new_mirror("reports"); mini_mirror(d)
        self.assertEqual(self.run_generator(d).returncode, 0)
        first = self.read(d, "en-reports")
        self.assertEqual(sorted(first), ["daily-2026-10-18", "weekly-2026-W41"])
        # run 2: the daily and weekly sources are gone (a failed fetch) while a monthly index exists: kinds not read keep their English
        for name in ("dailies", "daily-latest", "weeklies", "weekly-latest"): os.remove(os.path.join(d, f"{name}.json"))
        self.write(d, "monthlies", {"items": [{"month": "2026-09", "headline": "九月"}]})
        self.write(d, "monthly-latest", {"report": {"month": "2026-09", "headline": "九月的大事", "overview": "九月發生了很多事。", "sections": []}})
        self.assertEqual(self.run_generator(d).returncode, 0)
        second = self.read(d, "en-reports")
        self.assertEqual(sorted(second), ["daily-2026-10-18", "monthly-2026-09", "weekly-2026-W41"])
        self.assertEqual(second["daily-2026-10-18"], first["daily-2026-10-18"])
        self.assertEqual(second["weekly-2026-W41"], first["weekly-2026-W41"])
        # run 3: the Chinese lead of the daily changed and no call may be made: the previous English keeps being published
        self.write(d, "dailies", {"items": [{"date": "2026-10-18", "leadTitle": "改過的導言"}]})
        daily = {"report": {"date": "2026-10-18", "generatedAt": AT, "lead": {"title": "改過的標題 40", "leadParagraph": "改過的導言。"}, "sections": [], "flashes": []}}
        self.write(d, "daily-latest", daily)
        self.assertEqual(self.run_generator(d, EN_LITE_CALLS="0", EN_GEMMA_CALLS="0").returncode, 0)
        third = self.read(d, "en-reports")
        self.assertEqual(third["daily-2026-10-18"], first["daily-2026-10-18"])

class TestDailyQuota(GeneratorHarness):
    handler = RateLimitStub

    def test_a_per_day_429_keeps_the_model_quiet_until_the_pacific_reset(self):
        d = self.new_mirror("quota"); mini_mirror(d)
        r = self.run_generator(d, EN_LITE_MODEL="lite-test", EN_GEMMA_MODEL="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("daily quota used up (429)", r.stdout)
        cache = read_cache(os.path.join(d, "en-cache.json"))
        self.assertEqual(cache["usage"]["day"], "2026-10-17")           # 03:00 UTC on the 18th is still the 17th in Pacific time
        self.assertIn(cache["usage"]["cool"]["lite"], ("2026-10-18T07:00:00+00:00", "2026-10-18T08:00:00+00:00"))   # midnight Pacific (PDT or the fixed UTC-8 fallback)
        # the next run, an hour later, makes no call at all
        r2 = self.run_generator(d, EN_LITE_MODEL="lite-test", EN_GEMMA_MODEL="", EN_NOW="2026-10-18T04:00:00Z")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("cooling down until", r2.stdout)

class TestUnits(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="en-units-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.mod = load_generator(tmp, os.path.join(tmp, "none.json"))

    def test_idof_never_raises(self):
        i = self.mod.idof
        self.assertEqual(i({"id": "a1"}), "a1")
        self.assertEqual(i({"id": 7}), "7")
        self.assertEqual(i({"links": {"aihot": "https://aihot.news/items/b2"}}), "b2")
        for odd in ({"links": {"aihot": None}}, {"links": None}, {"links": "x"}, {"links": {"aihot": 5}}, {"id": ""}, {"id": None}, {"id": True}, {}, None, "x", []):
            self.assertIsNone(i(odd), odd)

    def test_guard_counts_and_carries_on(self):
        S = self.mod.Sources(self.mod.fresh_cache())
        self.assertIsNone(S.guard("thing", lambda: 1 / 0))
        self.assertEqual(S.guard("thing", lambda: 5), 5)
        self.assertEqual(S.bad, ["thing: ZeroDivisionError"])

    def test_scrub_drops_one_entry_not_the_key(self):
        outs = {"en-items": {"a": {"t": "Fine"}, "b": {"t": "via AIHOT"}, "c": {"t": "中文版"}, "d": {"t": "has \x00 nul"}, "e": {"s": "no headline"}, "f": {"t": "Fine too", "x": "extra"}},
                "en-reports": {"daily-2026-10-18": {"sections": [{"items": [{"id": None, "title": "x"}]}]}, "weekly-2026-W41": {"headline": "ok", "sections": []}, "bad-key": {"headline": "x"}},
                "en-lb-text": {"（）": "", "x": "()"}}
        self.assertEqual(self.mod.scrub_outputs(outs), 8)
        self.assertEqual(list(outs["en-items"]), ["a"])
        self.assertEqual(list(outs["en-reports"]), ["weekly-2026-W41"])
        self.assertEqual(list(outs["en-lb-text"]), ["x"])

    def test_database_name_and_credentials_are_not_printed(self):
        url = "postgresql://user_abc:secretpw@ep-quiet-sun-123.us-east-2.aws.neon.tech/db?sslmode=require"
        msg = f'psql: error: connection to server at "ep-quiet-sun-123.us-east-2.aws.neon.tech" failed for user "user_abc" with {url} password secretpw'
        out = self.mod.hide_db(msg, url)
        for secret in ("ep-quiet-sun", "user_abc", "secretpw", "neon.tech"): self.assertNotIn(secret, out)

    def test_quota_day_is_the_pacific_day(self):
        self.assertEqual(self.mod.QUOTA_DAY, "2026-10-17")
        self.assertIn(self.mod.next_quota_reset(), ("2026-10-18T07:00:00+00:00", "2026-10-18T08:00:00+00:00"))

# ---- 3e. the workflow keeps the Chinese data first and the English keys apart ------------------------------------------------
class TestSyncWorkflow(unittest.TestCase):
    def setUp(self):
        path = os.path.join(ROOT, ".github", "workflows", "tibo-sync.yml")
        if not os.path.exists(path): self.skipTest(".github is not part of this checkout (the sync job checks out scripts, site/style and tests/fixtures only)")
        with open(path, encoding="utf-8") as f: self.text = f.read()
        self.steps = {}
        for chunk in re.split(r"\n      - ", self.text.split("    steps:\n", 1)[1].split("\n  english-sample:", 1)[0]):
            m = re.search(r"name: (.+)", chunk)
            if m: self.steps[m.group(1).strip()] = chunk

    def step(self, prefix):
        found = [(n, c) for n, c in self.steps.items() if n.startswith(prefix)]
        self.assertEqual(len(found), 1, f"{prefix}: {[n for n, _ in found]}")
        return list(self.steps).index(found[0][0]), found[0][1]

    def test_chinese_keys_are_saved_before_the_slow_English_step_and_without_en_keys(self):
        save, body = self.step("Save mirror to Neon")
        english, _ = self.step("English edition")
        self.assertLess(save, english)
        self.assertRegex(body, r"en-\*\) continue")
        self.assertLess(self.step("Check model board snapshot")[0], english)

    def test_english_keys_have_their_own_save_and_a_failed_check_keeps_cache_and_meta(self):
        check_i, check = self.step("Check the English keys")
        save_i, save = self.step("Save the English keys")
        self.assertLess(self.step("English edition")[0], check_i)
        self.assertLess(check_i, save_i)
        self.assertNotRegex(check, r"rm -f data/mirror/en-\*")                 # no glob: en-cache and en-meta stay
        for k in ("en-items", "en-stories", "en-reports", "en-names", "en-lb.json", "en-lb-text"): self.assertIn(k, check)
        self.assertRegex(save, r"for f in data/mirror/en-\*\.json")
        self.assertNotIn("BEGIN", save)                                          # one failing key does not lose the others
        self.assertIn("continue-on-error: true", check)

    def test_a_failed_English_check_or_save_still_fails_the_job_with_its_own_message(self):
        self.assertRegex(self.text, r"steps\.encheck\.outcome == 'failure'")
        notify = self.text.split("Notify failure", 1)[1]
        self.assertIn("EN_CHECK", notify)
        self.assertIn("EN_SAVE", notify)

# ---- 4. whatever the sync job just generated ----------------------------------------------------------------------------
class TestGeneratedKeys(unittest.TestCase):
    """Runs on data/mirror in the workflow, before the keys are saved to Neon. Skips when the generator wrote nothing."""
    def test_generated_keys(self):
        found = {k: p for k in KEYS if os.path.exists(p := os.path.join(MIRROR, f"{k}.json"))}
        if not found: self.skipTest("no generated en-* keys in data/mirror")
        for k, p in found.items():
            with open(p, encoding="utf-8") as f: doc = json.load(f)
            self.assertEqual(shape_errors(k, doc), [], k)
            bad = cjk_in_values(doc)
            self.assertEqual(bad, [], f"{k} still has Chinese: {bad[:3]}")
            self.assertFalse([p2 for p2, s in T.iter_strings(doc) if re.search("AIHOT", s, re.I)], f"{k} shows the upstream name")
        for name in ("en-meta",):
            p = os.path.join(MIRROR, f"{name}.json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f: self.assertEqual(cjk_in_values(json.load(f)), [], name)

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--mini-mirror":
        os.makedirs(sys.argv[2], exist_ok=True)
        mini_mirror(sys.argv[2], marked=False)
        print("sample mirror written to", sys.argv[2])
    else:
        unittest.main()
