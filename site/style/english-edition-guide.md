# AINEWS English edition: rules for generated text

Used only by `scripts/english-aihot.py` (the sync job's English pass). It adds to, and never replaces, the voice rules in
`rewrite-guide.md` (sections "共同原則", "去 AI 味", "English" and "English edition (AINEWS)"), which the script puts in front of
this file. The Chinese rewrite prompt does not read this file, so the Chinese edition is unaffected.

## Hard rules (a checker rejects any output that breaks the first four)
1. No Chinese, Japanese or Korean characters anywhere, and no fullwidth punctuation (use ASCII `: , ( ) ? !`). Every name is written in Latin letters.
2. Names, numbers, versions, prices and dates stay exactly as in the source. Do not add a number, a claim or a name that the source does not contain. Do not convert currencies. Do not round.
3. Chinese numerals are written out in digits (萬 = ten thousand, 億 = hundred million: 800億 is 80 billion, 3萬 is 30,000), and the unit words are English (yuan, not 元).
4. The string "AIHOT" never appears. The site is AINEWS in English (AI熱點 is AINEWS).
5. US spelling. Plain newsroom verbs (said, released, cut, raised, opened). No hype words, no emoji, no exclamation marks.
6. Write the English from the facts. Never translate word by word, never keep Chinese word order. If a sentence only makes sense as a translation, rewrite it from scratch.
7. If an English `originalTitle` is given, it is the publisher's own headline: take names, spellings and wording from it, but still write a headline in AINEWS style.

## Chinese companies, products and people
- Use the established English name when there is one: 月之暗面 Moonshot AI, 智譜 Zhipu AI, 字節跳動 ByteDance, 騰訊 Tencent, 阿里巴巴 Alibaba, 阿里雲 Alibaba Cloud, 百度 Baidu, 華為 Huawei, 小米 Xiaomi, 豆包 Doubao, 通義千問 Qwen, 文心 ERNIE, 騰訊混元 Tencent Hunyuan, 階躍星辰 StepFun, 面壁智能 ModelBest, 深度求索 DeepSeek, 崑崙萬維 Kunlun Tech.
- A person's name with an @handle: use the Latin spelling of the handle when it is clearly the name (卡茲克 with @Khazix0918 is Khazix). Otherwise pinyin without tone marks, given name after family name as in the source order, each part capitalized (邵猛 is Shao Meng).
- Anything else: pinyin, capitalized, with a short English descriptor only when the source gives one. Never leave characters in place.

## Headlines
- Sentence case, at most 12 words (14 when a name needs the room), no trailing period, present tense for news ("DeepSeek releases V4.1-Flash-1017 and opens a search API").
- Lead with who did what. Keep the one number that matters. No question headlines, no teasers.

## Summaries and digests
- Summary: one to three short sentences, the news first. Digest (story pages): two to four sentences per paragraph; keep the paragraph breaks (a blank line) of the source.
- No concluding sentence ("this shows...", "overall..."), no scene-setting opener.

## Small things that keep the edition consistent
- Money and quantities: `$50 billion`, `80 billion yuan`, `37,999 yuan`, `128 GB`, `1.05M tokens`. Digits first, the unit or sign after; thousands separators from 1,000 up. Never "50 billion dollars".
- `open-source` is the adjective ("an open-source model"), `open-sources` / `open-sourced` the verb, `open weights` the noun phrase.
- Product and model names are exactly what the source writes. Do not complete, correct or "improve" a name from memory (a Chinese model name that has no English form in the source is described, not guessed).
- Dates without a year stay without a year ("October 16"). Times keep their zone as the source gives it ("00:00 Taiwan time").
- A company or person that the source names only in Chinese gets the established English name (see above) or pinyin, once and the same way everywhere.

## Source names
- Keep the structure `Publisher: Section (kind)` and the @handle. Use ASCII punctuation.
- Descriptors: 公眾號 is "WeChat account", 網頁 is "Web", 更新日誌 is "Changelog", 官方博客 is "Official Blog", 官網動態 is "News", 熱帖 is "Hot Posts", 發表成果 is "Publications".
- Use the outlet's own English name when it has one: 量子位 QbitAI, 機器之心 Synced, IT之家 IT Home, 36氪 36Kr, 澎湃新聞 The Paper, 財新 Caixin, 虎嗅 Huxiu, 極客公園 GeekPark, 雷峰網 Leiphone, 品玩 PingWest, 愛範兒 ifanr, 少數派 sspai. Otherwise a clean pinyin or English rendering, never characters.

## Model board
- A benchmark `what` is one plain sentence saying what the test measures ("Has a model finish a whole engineering task on its own in a command-line environment."). No score claims.
- Labels are short noun phrases in sentence case ("Coding", "Knowledge Q&A", "Human preference", "xHigh reasoning").
