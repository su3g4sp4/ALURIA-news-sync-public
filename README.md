# AI熱點 資料同步

AI熱點網站的定時資料同步工作（GitHub Actions）。網站程式在另一個私人倉庫。

- `.github/workflows/sync.yml`：每 10 分鐘醒來，距離上次成功同步不到 25 分鐘就結束，實際約每 30 分鐘同步一次。
- `scripts/`：同步、改寫和檢查用的程式，跟網站倉庫的 `scripts/` 同一份；改了那邊要一起更新這裡。
- `site/style/rewrite-guide.md`：自動改寫標題與摘要的風格規則（含去 AI 味規則）。
- 金鑰只放在本倉庫的 Actions secrets（`DATABASE_URL`、`GEMINI_API_KEY`、`CRON_SECRET`），程式和執行紀錄裡都沒有。

授權：MIT，見 `LICENSE`。
