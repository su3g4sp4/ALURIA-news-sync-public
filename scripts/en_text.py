"""AINEWS English edition: pure helpers (no network, no files) shared by english-aihot.py and its tests.

What lives here: the Chinese-character test the whole English pipeline hangs on, punctuation clean-up, the number guard
(an English text may not contain a number the Chinese source does not), the source-name rules, hashing and JSON parsing.
"""
import hashlib, json, re

# ---- Chinese (CJK) characters -------------------------------------------------------------------------------------
# A superset of the site's leak scanner (U+3000-303F, U+3040-30FF, U+3400-4DBF, U+4E00-9FFF, U+FF00-FFEF): radicals, bopomofo,
# Hangul, compatibility ideographs, vertical forms and the supplementary ideograph planes too.
CJK_RE = re.compile(
    "[⺀-⿟　-〿぀-ヿ㄀-ㄯ㄰-㆏㇀-㇯㈀-㏿㐀-䶿一-鿿"
    "ꥠ-꥿가-힯豈-﫿︰-﹏＀-￯\U00020000-\U0003134f]")
# Letters of those scripts (ideographs, kana, bopomofo, hangul), without the punctuation and fullwidth forms CJK_RE also covers. A text with fullwidth
# punctuation only is fixed by ascii_punct() alone and needs no model.
IDEO_RE = re.compile("[⺀-⿟぀-ヿ㄀-㆏㇀-㇯㈀-㏿㐀-䶿一-鿿ꥠ-꥿가-힯豈-﫿𠀀-𱍏]")
# The only Chinese text English mode may show: the language-switch label.
ALLOW_EXACT = frozenset({"中文"})

def has_cjk(s):
    return isinstance(s, str) and s not in ALLOW_EXACT and bool(CJK_RE.search(s))

def has_ideograph(s):
    return isinstance(s, str) and s not in ALLOW_EXACT and bool(IDEO_RE.search(s))

def cjk_sample(s, n=6):
    return "".join(dict.fromkeys(CJK_RE.findall(s)))[:n]

def safe(s):
    """For logs that are published: the string with its Chinese characters replaced by '?'."""
    return CJK_RE.sub("?", s) if isinstance(s, str) and s not in ALLOW_EXACT else s

def safe_key(s):
    """A unit key for a published log: a key that has Chinese characters (a source name, a board label) is shown as its hash."""
    return "#" + h_any(s) if has_cjk(s) else s

def iter_strings(obj, path=""):
    """Every string value (never a key) with its JSON path."""
    if isinstance(obj, str): yield path, obj
    elif isinstance(obj, list):
        for i, x in enumerate(obj): yield from iter_strings(x, f"{path}[{i}]")
    elif isinstance(obj, dict):
        for k, x in obj.items(): yield from iter_strings(x, f"{path}.{k}" if path else str(k))

def find_cjk(obj):
    """[(path, string)] for every string value that still holds Chinese (outside the allow-list)."""
    return [(p, s) for p, s in iter_strings(obj) if has_cjk(s)]

_BRAND_ONE = re.compile(r"\bAIHOT\b", re.I)
_BRAND_TWO = re.compile(r"\bAI HOT\b")   # the two-word spelling, capitals only: "AI hot topics" and "Shanghai hotel" are plain English

def brand(s):
    """The upstream name never shows: the standalone word AIHOT (or AI HOT in capitals) is AINEWS here. Words that merely contain the letters
    (Shanghai hotel, Thai hot pot) and ordinary phrases (AI hot topics) are left alone."""
    return _BRAND_TWO.sub("AINEWS", _BRAND_ONE.sub("AINEWS", s)) if isinstance(s, str) else s

# ---- punctuation --------------------------------------------------------------------------------------------------
_PUNCT = {
    "：": ": ", "；": "; ", "，": ", ", "、": ", ", "。": ". ", "！": "! ", "？": "? ", "（": " (", "）": ") ", "［": " [", "］": "] ",
    "【": " [", "】": "] ", "「": '"', "」": '"', "『": '"', "』": '"', "《": '"', "》": '"', "〈": '"', "〉": '"',
    "　": " ", "〜": "~", "～": "~", "・": " · ", "‧": " · ", "﹑": ", ", "〔": " (", "〕": ") ", "〖": " [", "〗": "] ", "￥": "CNY ", "￡": "GBP ",
}

# Characters no published text needs and Postgres jsonb (or a UTF-8 file) refuses: NUL and the other C0/C1 controls (newline and tab stay),
# lone surrogates, U+FFFE and U+FFFF.
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff￾￿]")

def strip_ctrl(s):
    return _CTRL_RE.sub("", s) if isinstance(s, str) else s

_UNSTORABLE_RE = re.compile("[\x00\ud800-\udfff]")

def has_nul(obj):
    """True when a string (value or key) anywhere in `obj` holds NUL, which jsonb can not store, or a lone surrogate, which UTF-8 can not write."""
    if isinstance(obj, str): return bool(_UNSTORABLE_RE.search(obj))
    if isinstance(obj, list): return any(has_nul(x) for x in obj)
    if isinstance(obj, dict): return any(has_nul(k) or has_nul(v) for k, v in obj.items())
    return False

def ascii_punct(s, keep_empty=False):
    """Fullwidth forms and CJK punctuation become ASCII; spacing is tidied; control characters are dropped. Chinese ideographs are left for the checker.
    keep_empty=True keeps an empty pair of brackets (a label that is only "（）" stays "()" instead of vanishing)."""
    if not isinstance(s, str): return s
    s = strip_ctrl(s)
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E and ch not in _PUNCT: ch = chr(o - 0xFEE0)  # fullwidth ASCII (letters, digits, signs)
        out.append(_PUNCT.get(ch, ch))
    t = "".join(out)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"[ \t]+([,.;:!?\)\]])", r"\1", t)
    t = re.sub(r"([\(\[])[ \t]+", r"\1", t)
    if not keep_empty: t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()

def clean(s):
    """Model output to the text we store: ASCII punctuation, no upstream name."""
    return ascii_punct(brand(s))

# ---- numbers ------------------------------------------------------------------------------------------------------
_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000}
_BIG = {"萬": 10 ** 4, "万": 10 ** 4, "億": 10 ** 8, "亿": 10 ** 8, "兆": 10 ** 12}
_ZH_NUM = re.compile(r"[零〇一二兩两三四五六七八九十百千萬万億亿兆]+(?:[點点][零〇一二三四五六七八九]+)?")
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SRC_NUM = re.compile(rf"({_NUM})\s*((?:[千百][萬万億亿兆]|[萬万億亿兆千百成])?)")   # 3千萬 is 3 x 1000 x 10000
_MULT = {"萬": 1e4, "万": 1e4, "億": 1e8, "亿": 1e8, "兆": 1e12, "千": 1e3, "百": 1e2, "成": 10.0}
_EN_NUM = re.compile(_NUM)
# Magnitude words and letters after a number. A number followed by one of these stands for the SCALED value only (500 million is 5e8, never 500).
_EN_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12, "bn": 1e9, "tn": 1e12, "k": 1e3, "m": 1e6, "b": 1e9}
_EN_SCALE_WORD = re.compile(r"\s*(thousand|million|billion|trillion|bn|tn)\b", re.I)
_EN_SCALE_LETTER = re.compile(r"(k|m|b)(?![A-Za-z])", re.I)
_SRC_LATIN_SCALE = re.compile(rf"({_NUM})(?:\s*(thousand|million|billion|trillion|bn|tn)\b|(k|m|b)(?![A-Za-z]))", re.I)

def _canon(v):
    return float(f"{v:.8g}")

def _zh_value(tok):
    """A run of Chinese numerals as a number, or None."""
    tok = tok.strip()
    dec = None
    m = re.match(r"(.+?)[點点]([零〇一二三四五六七八九]+)$", tok)
    if m:
        tok, frac = m.group(1), m.group(2)
        dec = float("0." + "".join(str(_DIG[c]) for c in frac))
    if not tok: return None
    if all(c in _DIG for c in tok):
        v = float("".join(str(_DIG[c]) for c in tok))  # 二〇二六 is read digit by digit; a lone 三 is 3
    else:
        total = section = num = 0
        for c in tok:
            if c in _DIG: num = _DIG[c]
            elif c in _UNIT: section += (num or 1) * _UNIT[c]; num = 0
            elif c in _BIG: section += num; total += (section or 1) * _BIG[c]; section = num = 0
            else: return None
        v = float(total + section + num)
    return v + dec if dec is not None else v

def source_values(text):
    """Every number the Chinese (or English) source can justify, as plain values: digits as written, digits scaled by 萬/億/千/百/成,
    and spelled-out Chinese numerals. Permissive on purpose: it only has to catch numbers the source never had."""
    vals = set()
    for m in _SRC_NUM.finditer(text or ""):
        base = float(m.group(1).replace(",", ""))
        vals.add(_canon(base))
        if m.group(2):
            mul = 1.0
            for ch in m.group(2): mul *= _MULT[ch]
            vals.add(_canon(base * mul))
    for m in _SRC_LATIN_SCALE.finditer(text or ""):     # 1.05M, 235B, 4 million written in Latin letters inside the source
        vals.add(_canon(float(m.group(1).replace(",", "")) * _EN_SCALE[(m.group(2) or m.group(3)).lower()]))
    for m in _ZH_NUM.finditer(text or ""):
        v = _zh_value(m.group(0))
        if v is not None: vals.add(_canon(v))
    for m in re.finditer(r"([零〇一二兩两三四五六七八九十]+)成", text or ""):   # 三成 is 30 percent
        v = _zh_value(m.group(1))
        if v is not None: vals.add(_canon(v * 10))
    return vals

def _en_candidates(text):
    """[(token, {values it may stand for})]: a bare number stands for itself; a number followed by 'million', 'billion', 'k', 'B' and so on
    stands for the scaled value only, so $500 million can not hide behind a source that says 500 億 (which is $50 billion)."""
    out = []
    for m in _EN_NUM.finditer(text or ""):
        base = float(m.group(0).replace(",", ""))
        tail = text[m.end():m.end() + 12]
        mm = _EN_SCALE_WORD.match(tail) or _EN_SCALE_LETTER.match(tail)
        out.append((m.group(0) + (mm.group(0) if mm else ""), {_canon(base * _EN_SCALE[mm.group(1).lower()]) if mm else _canon(base)}))
    return out

def numbers_ok(src, out):
    """[] when every number in the English text is justified by the source (digits as written, scaled by 萬/億/千/百/成 or by million, billion, k, B...,
    or spelled in Chinese numerals); else the offending tokens, a scaled number shown with its scale word."""
    allowed = source_values(src)
    return [tok for tok, cands in _en_candidates(out) if not (cands & allowed)]

# ---- hashing / JSON -----------------------------------------------------------------------------------------------
def h_item(title, summary="", reason=""):
    """Same formula as rewrite-aihot.py's h(): an item's text key. A reason joins the hash only when there is one."""
    extra = f"\n{reason}" if reason else ""
    return hashlib.sha256(f"{title}\n{summary}{extra}".encode()).hexdigest()[:16]

def h_any(*parts):
    return hashlib.sha256("\x1f".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:16]

def parse_json_text(text):
    """The JSON array or object in a model's text (code fences and chatter around it are ignored); None when there is none."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try: return json.loads(t)
    except ValueError: pass
    for a, b in (("[", "]"), ("{", "}")):
        i, j = t.find(a), t.rfind(b)
        if 0 <= i < j:
            try: return json.loads(t[i:j + 1])
            except ValueError: continue
    return None

# ---- source names -------------------------------------------------------------------------------------------------
# Systematic descriptors of AIHOT's source labels, in the Traditional forms the mirror stores (the Simplified forms too).
PHRASES = [
    ("排除企業/客戶案例", "excl. customer stories"), ("排除企业/客户案例", "excl. customer stories"),
    ("失準報告與通報", "Misalignment Reports"), ("失准报告与通报", "Misalignment Reports"),
    ("事故復盤 + 工程實踐", "Postmortems + Engineering Practice"), ("事故复盘 + 工程实践", "Postmortems + Engineering Practice"),
    ("官方團隊博客", "Official Team Blog"), ("官方团队博客", "Official Team Blog"),
    ("官方工程博客", "Official Engineering Blog"),
    ("研究與產品博客", "Research and Product Blog"), ("研究与产品博客", "Research and Product Blog"),
    ("模型與工程博客", "Model and Engineering Blog"), ("模型与工程博客", "Model and Engineering Blog"),
    ("產品與工程博客", "Product and Engineering Blog"), ("产品与工程博客", "Product and Engineering Blog"),
    ("開發者博客", "Developer Blog"), ("开发者博客", "Developer Blog"), ("工程博客", "Engineering Blog"),
    ("官方博客", "Official Blog"), ("官方文章", "Official Articles"), ("完整文章", "Full Articles"),
    ("官網動態", "News"), ("官网动态", "News"), ("發表成果", "Publications"), ("发表成果", "Publications"),
    ("開發者關係", "DevRel"), ("开发者关系", "DevRel"), ("更新日誌", "Changelog"), ("更新日志", "Changelog"),
    ("熱點 RSS", "Hot RSS"), ("热点 RSS", "Hot RSS"), ("熱帖", "Hot Posts"), ("热帖", "Hot Posts"),
    ("公眾號", "WeChat account"), ("公众号", "WeChat account"), ("網頁", "Web"), ("网页", "Web"),
    ("人工智能", "AI"), ("人工智慧", "AI"), ("IT之家", "IT Home"), ("博客", "Blog"), ("官網", "Website"), ("官网", "Website"),
]
_PHRASES = sorted(PHRASES, key=lambda p: -len(p[0]))

def det_name(name):
    """The English form of a source name when the systematic descriptors alone settle it, else None (a model must write it)."""
    t = name
    for zh, en in _PHRASES: t = t.replace(zh, en)
    t = re.sub(r"(?<=[A-Za-z0-9\)])·(?=[A-Za-z0-9\(])", " · ", t)
    t = ascii_punct(t)
    t = re.sub(r"(?<=[A-Za-z0-9\)])(?=\()", " ", t)
    return t if not has_cjk(t) else None

_HANDLE = re.compile(r"@\w{2,}")

def name_ok(src, out):
    """[] when a generated source name is usable; else the reasons. Handles of the source must survive."""
    errs = []
    if not out or not isinstance(out, str): return ["empty"]
    if has_cjk(out): errs.append(f"Chinese characters {cjk_sample(out)}")
    if not re.match(r"[A-Za-z0-9]", out): errs.append("must start with a Latin letter or digit")
    if len(out) > 140: errs.append("too long")
    for hd in _HANDLE.findall(src):
        if hd not in out: errs.append(f"lost handle {hd}")
    if re.search(r"AIHOT", out, re.I): errs.append("AIHOT")
    return errs

# ---- glossary -----------------------------------------------------------------------------------------------------
# Terms the source writes in mainland or Taiwan wording that English readers expect in one fixed form. site/style/terms.json
# (the site's own glossary: zh, en, avoid) is merged with these; a prompt only carries the pairs its batch actually contains.
EXTRA_TERMS = [
    ("智能體", "AI agent"), ("智慧體", "AI agent"), ("令牌", "token"), ("詞元", "token"), ("算力", "compute"), ("大模型", "large model"),
    ("閉源", "closed-source"), ("開放權重", "open weights"), ("上下文窗口", "context window"), ("上下文視窗", "context window"), ("幻覺", "hallucination"),
    ("蒸餾", "distillation"), ("微調", "fine-tuning"), ("多模態", "multimodal"), ("統一內存", "unified memory"), ("統一記憶體", "unified memory"),
    ("商務部", "Ministry of Commerce"), ("科技部", "Ministry of Science and Technology"), ("工信部", "Ministry of Industry and Information Technology"),
    ("國務院", "State Council"), ("網信辦", "Cyberspace Administration of China"), ("京東", "JD.com"), ("英偉達", "Nvidia"), ("輝達", "Nvidia"),
    ("微軟", "Microsoft"), ("谷歌", "Google"), ("蘋果", "Apple"), ("亞馬遜", "Amazon"), ("特斯拉", "Tesla"), ("甲骨文", "Oracle"),
]

def load_terms(path):
    """[(zh form, English)] from the site glossary file (every written form, wanted and avoided), plus EXTRA_TERMS; [] when unreadable."""
    pairs = list(EXTRA_TERMS)
    try:
        with open(path, encoding="utf-8") as f: doc = json.load(f)
        for t in doc.get("terms") or []:
            en = (t.get("en") or "").split(" / ")[0].split(" (")[0].strip()
            if not en: continue
            for zh in [t.get("zh")] + [re.sub(r"（.*", "", a) for a in t.get("avoid") or []]:
                if isinstance(zh, str) and len(zh) >= 2: pairs.append((zh, en))
    except (OSError, ValueError, AttributeError): pass
    seen, out = set(), []
    for zh, en in pairs:
        if zh not in seen: seen.add(zh); out.append((zh, en))
    return out

def glossary(text, terms, limit=16):
    """'zh = English' lines for the glossary terms that occur in `text`, longest first, at most `limit`."""
    hit = [(zh, en) for zh, en in terms if zh in text]
    hit.sort(key=lambda p: -len(p[0]))
    kept, covered = [], ""
    for zh, en in hit:
        if zh in covered: continue          # 智能體 inside a longer hit is not listed twice
        kept.append((zh, en)); covered += zh
        if len(kept) >= limit: break
    return "\n".join(f"{zh} = {en}" for zh, en in kept)

# Section labels of the daily, weekly and monthly reports: one English wording, the same the website uses elsewhere
# (apps/web/app/lib/i18n-phrases.ts). Anything else is written by the model once and cached.
SECTION_LABELS = {
    "模型發布/更新": "Model releases", "模型發佈/更新": "Model releases", "模型发布/更新": "Model releases",
    "產品發布/更新": "Product launches", "產品發佈/更新": "Product launches", "产品发布/更新": "Product launches",
    "行業動態": "Industry", "行业动态": "Industry", "產業動態": "Industry", "产业动态": "Industry", "技巧與觀點": "Tips & opinion", "技巧与观点": "Tips & opinion",
    "論文研究": "Research papers", "论文研究": "Research papers",
}

# ---- shapes of the published keys (docs/english-keys.md) ------------------------------------------------------------
# The generator checks every top-level entry against these before it writes (one malformed entry is dropped, never the whole key);
# scripts/test_en_text.py checks the files again before they are saved.
KEYS = ("en-items", "en-stories", "en-reports", "en-names", "en-lb", "en-lb-text")

def shape_errors(key, doc):
    """[] when `doc` has the documented shape for mirror key `key`."""
    errs = []
    def is_str(v, nonempty=True): return isinstance(v, str) and (v.strip() != "" or not nonempty)
    def only(d, allowed, where):
        extra = set(d) - set(allowed)
        if extra: errs.append(f"{where}: unexpected {sorted(extra)}")
    if not isinstance(doc, dict): return [f"{key}: not an object"]
    if key == "en-items":
        for i, v in doc.items():
            if not isinstance(v, dict) or not is_str(v.get("t")): errs.append(f"en-items.{i}: t missing"); continue
            only(v, ("t", "s", "r"), f"en-items.{i}")
            for f in ("s", "r"):
                if f in v and not is_str(v[f]): errs.append(f"en-items.{i}.{f} empty")
    elif key == "en-stories":
        for k, v in doc.items():
            if not isinstance(v, dict) or not is_str(v.get("title")): errs.append(f"en-stories.{k}: title missing"); continue
            only(v, ("title", "latest", "digest", "timeline"), f"en-stories.{k}")
            for f in ("latest", "digest"):
                if f in v and not is_str(v[f]): errs.append(f"en-stories.{k}.{f} empty")
            if not isinstance(v.get("timeline"), dict): errs.append(f"en-stories.{k}.timeline missing")
            else:
                for i, e in v["timeline"].items():
                    if not isinstance(e, dict) or not is_str(e.get("t")): errs.append(f"en-stories.{k}.timeline.{i}: t missing")
                    else: only(e, ("t", "s"), f"en-stories.{k}.timeline.{i}")
    elif key == "en-reports":
        def entry(x, where, need_title):
            if not isinstance(x, dict) or not is_str(x.get("id")): errs.append(f"{where}: id missing"); return
            only(x, ("id", "title", "summary", "reason"), where)
            if need_title and "title" not in x: return   # an item the generator gave up on carries its id only
            if "title" in x and not is_str(x["title"]): errs.append(f"{where}.title empty")
        for k, r in doc.items():
            if not re.fullmatch(r"daily-\d{4}-\d{2}-\d{2}|weekly-\d{4}-W\d{2}|monthly-\d{4}-\d{2}", k): errs.append(f"en-reports.{k}: bad key"); continue
            if not isinstance(r, dict): errs.append(f"en-reports.{k}: not an object"); continue
            only(r, ("headline", "overview", "lead", "sections", "flashes"), f"en-reports.{k}")
            for f in ("headline", "overview"):
                if f in r and not is_str(r[f]): errs.append(f"en-reports.{k}.{f} empty")
            if "lead" in r:
                if not isinstance(r["lead"], dict): errs.append(f"en-reports.{k}.lead")
                else: only(r["lead"], ("title", "leadParagraph"), f"en-reports.{k}.lead")
            for si, s in enumerate(r.get("sections") or []):
                # label is absent when the Chinese section has none (an empty string is never written)
                if not isinstance(s, dict) or ("label" in s and not is_str(s["label"])): errs.append(f"en-reports.{k}.sections[{si}].label"); continue
                only(s, ("label", "summary", "items"), f"en-reports.{k}.sections[{si}]")
                for ii, x in enumerate(s.get("items") or []): entry(x, f"en-reports.{k}.sections[{si}].items[{ii}]", True)
            for fi, x in enumerate(r.get("flashes") or []): entry(x, f"en-reports.{k}.flashes[{fi}]", True)
    elif key in ("en-names", "en-lb-text"):
        for k, v in doc.items():
            if not is_str(k) or not is_str(v): errs.append(f"{key}: bad pair {k[:30]!r}")
    elif key == "en-lb":
        for k, v in doc.items():
            if not isinstance(v, dict) or not is_str(v.get("name")): errs.append(f"en-lb.{k}: name missing"); continue
            only(v, ("name", "what", "operator"), f"en-lb.{k}")
    return errs
