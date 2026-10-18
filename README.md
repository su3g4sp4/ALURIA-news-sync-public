# AI熱點 資料同步

AI熱點網站的定時資料同步工作（GitHub Actions）。網站程式在另一個私人倉庫。

- `.github/workflows/sync.yml`：每 10 分鐘醒來，距離上次成功同步不到 25 分鐘就結束，實際約每 30 分鐘同步一次。
- `scripts/`：同步、改寫和檢查用的程式，跟網站倉庫的 `scripts/` 同一份；改了那邊要一起更新這裡。
- `site/style/rewrite-guide.md`：自動改寫標題與摘要的風格規則（含去 AI 味規則）。
- 英文版（AINEWS）：同一個工作裡，中文存庫之後由 `scripts/english-aihot.py` 用 Gemini 免費額度寫英文（`en_text.py` 是輔助、`test_en_text.py` 是存庫前的檢查，樣本在 `tests/fixtures/`，英文寫法規則在 `site/style/english-edition-guide.md` 與 `terms.json`）。手動執行時勾選 `english_sample` 只在內建樣本上試跑，不碰資料庫。執行紀錄是公開的，英文步驟只印次數、模型種類與狀態碼。
- 金鑰只放在本倉庫的 Actions secrets（`DATABASE_URL`、`GEMINI_API_KEY`、`CRON_SECRET`），程式和執行紀錄裡都沒有。

授權：MIT，見 `LICENSE`。
