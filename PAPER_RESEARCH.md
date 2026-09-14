# Polymarket 純模擬研究（2026-09-14）

## 使用入口

這個分支新增獨立的 `karb.paper_ev` 入口。它以 Python 標準函式庫執行，
完全不載入私鑰、不讀取 .env，也不呼叫下單、簽名、轉換或合併 API。

**請使用以下入口；原本的 `karb run`、即時交易與舊 Dashboard 尚未接上這套模型。**
舊執行器顯示的 expected_profit 不能當作這套研究的測試結果。

Windows PowerShell（Python 3.12）：

```powershell
git clone --branch codex/polymarket-paper-ev https://github.com/jack885198/karb.git karb-paper
cd karb-paper
$env:PYTHONPATH = "src"
py -3.12 -m unittest discover -s tests -p test_paper_ev.py -v
py -3.12 -m karb.paper_ev --seconds 1800 --markets 10 --shares 20 --cash 1000
```

Linux/macOS：

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_paper_ev.py -v
PYTHONPATH=src python3 -m karb.paper_ev --seconds 1800 --markets 10 --shares 20 --cash 1000
```

不需要 pip install、錢包或交易 API key。Ctrl+C 可停止。
預設為 60 秒；網路請求與兩腿確認可能使最後一次操作超出指定秒數。
GitHub Actions 只跑一次 60 秒樣本，不提供持續監控。

## 計算邏輯

- 僅處理標準二元 YES/NO，同一 conditionId、不同 token；Negative Risk 明確跳過。
- 費率必須有明確資料。只接受 exponent=1、takerOnly=true 的費表，
  或明確 feesEnabled=false；未知費率不猜測為零。
- 逐檔成交量乘成交價，逐檔計算 share × rate × price × (1-price)。
- 費用每檔向上捨入至五位小數，是保守的現金等值估計；
  並未模擬實際買入時扣 token 的規則，也不保證淨份數可實際 merge。
- 純報價淨空間 = 份數 - 買入金額 - 費用 - 每組交易固定模型成本 0.05。
  固定成本是研究假設，不是目前鏈上實際收費。
- 只接受兩秒內的簿記時間，兩腿相差不得超過一秒；需同步本機時鐘。
- 每次發現超過 min-net（預設 0.10）的候選，等待 latency-ms（預設 500），
  重新讀簿，按原始每腿限價獨立作 FOK-like 深度檢查。
- 兩腿都模擬成交：扣除虛擬現金，保留配對部位；不假設 merge 已確認，
  本輪不再使用這筆資金，也不重複交易同一市場。
- 單腿模擬成交：嘗試以當時買盤估算回補；若深度不足則保留未解部位。
  回補尚未加入額外網路延遲，因此此部分仍可能樂觀。
- API 錯誤、資料不足、費率不支援與沒有機會都分別記錄。

## 輸出與驗收

`paper-results/observations.jsonl`：包含市場費表、原始深度、計算及延遲後深度。
`paper-results/summary.json`：報價樣本、候選數、虛擬現金與未結部位。
API 無法讀取或完全沒有可用報價時，退出碼為 2；不能當作零風險或測試成功。
單次只取 Gamma 前 100 個市場內的限定子集，不代表全市場掃描。

人工測試 fixtures 的正利潤不代表真實市场正 EV。
Paper 模式不模擬撮合排隊、链上回滾或實際 merge，不會產生實際損益。
`certified_positive_ev` 永遠為 false，直到另有校準後的獨立樣本研究。

## 已修正的舊模型

`ArbitrageOpportunity.expected_profit_usd` 改為：
`shares * (1 - combined_cost)`，這是毛利金額，仍未扣費。
原先份數乘報酬率的公式有單位錯誤。

## 後續必要工作

1. 依目前 CLOB 協議處理實際淨 token 與費用捨入。
2. 蒐集可用時序深度，校準兩腿成交、回補與鏈上確認。
3. 統一舊即時與輪詢路徑的新版模型後再評估啟用；本分支不啟用實盤。
4. 標準 Negative Risk 轉換需要獨立的事件完整性與轉換費用測試。
