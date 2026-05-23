# TranscriptFlow

具有韌性的 AI 資料管線，將原始字幕轉換為可搜尋、摘要、向量化的知識庫。

TranscriptFlow 將 YouTube／SRT 字幕檔案轉換為語意段落、LLM 生成的摘要與標籤、批次嵌入向量，以及 LanceDB 向量索引。專為大量字幕與批次作業設計，注重可觀測性、重試與復原能力。

## 專案起源

TranscriptFlow 源自一個實際的 preservation 問題。

一個我追蹤多年的心理學頻道宣布停止更新，並計劃大約一年後刪除影片存檔。該頻道三年多來每天發布約一支影片，累積了大量長篇內容。這些內容很有價值，但難以搜尋：標題往往只與實際討論鬆散相關，YouTube 搜尋不足以找到藏在數百部影片中的特定想法、案例、來賓或主題。

大約同期我開始實驗 OpenClaw，並了解 LanceDB、嵌入向量與向量檢索。這啟發我將字幕檔案轉換為可搜尋的知識庫。這個專案經歷了多次 AI 輔助的探索、除錯、失敗嘗試與重新設計。

早期的做法是直接讓 LLM 分段字幕，但對於可用的模型與工作負載來說太不穩定：出現幻覺邊界與不可能的 timestamp，例如僅數十分鐘的字幕卻產生 `1:20:xx` 的時間軸。這次失敗塑造了目前的設計——TranscriptFlow 避免依賴 LLM 處理結構性時間邊界，而是採用更確定性的、嵌入輔助的分段管線，搭配驗證、重試與稽核。

結果不僅僅是一個摘要工具，而是一個具韌性的「字幕→向量資料庫」管線，可將大量字幕存檔轉換為 RAG 就緒的知識庫。

## 工程亮點

TranscriptFlow 旨在展示生產級的 AI 管線設計：

- 可復原的多階段處理，搭配明確的檔案狀態
- 區塊級重試，保留已成功的工作
- 模型級診斷：吞吐量、延遲、失敗率、錯誤分布
- 長篇摘要作業的原子性 checkpoint 寫入
- 關閉即失敗（fail-closed）驗證：部分摘要、部分嵌入、資料庫記錄不匹配
- 基於穩定檔案與區塊識別碼的 idempotent LanceDB 寫入
- 自適應嵌入批次處理，附電路斷路器保護
- 資料完整性、停滯作業與跨檔案一致性的稽核工具

## 近期強化 (v1.8)

2026 年 5 月完成全面程式碼審查（25 項發現：7 項嚴重、12 項重要、6 項建議）：

- **安全性**：`sanitize_api_url()` 現先解析 DNS 再檢查 IP，使用 `ipaddress.is_private` 完整涵蓋 RFC 1918 + CGNAT，修補 DNS 假冒繞過漏洞。API 金鑰快取消除每次日誌記錄的設定檔讀取。
- **模組安全**：`finalize.py` 與 `summarize.py` 採用延遲初始化——匯入這些模組不再因環境變數未設定而崩潰。
- **資料完整性**：電路斷路器後備機制改為拋出 `CircuitBreakerOpenError`，而非靜默回傳 `None` 卻標記 `success=True`。語意分段移除進度續傳功能，防止重新執行時產生錯誤斷點。
- **並發安全**：共用 `BatchEmbeddingClient` 單例消除針對同一嵌入端點的重複電路斷路器。`save_status()` 鎖定行為統一與 `_locked_read_write()` 一致。
- **程式碼品質**：重複的 `extract_participants()` 合併；手動驗證檢查移除，改由 Pydantic `ConfigSchema` 處理；Smart Merge 診斷工具改為呼叫正式程式碼；移除死亡程式碼。

## 近期強化 (v1.9)

2026 年 5 月完成第二波全面審查（24 項發現）：

- **安全性**：所有測試設定檔中的硬編碼 API key 移除。子程序環境變數限縮為白名單。`_health_check()` 現在正確地在失敗時回傳 `False`。
- **匯入安全**：`llm_client.py` 與 `summarize_pipeline.py` 優雅處理設定檔缺失的情況（與 v1.8 模式一致）。
- **正確性**：移除無效的 `release_phase_slot()`。修正 `batch_audit`（dim_scores 聚合、過時 diag 變數）。損壞 JSON 修復改為跳過+警告。單一嵌入視窗失敗改為軟失敗。
- **死亡程式碼**：移除完全未使用的 `ResilientEmbeddingClient`（使用 `urllib` 而非 `requests`）、`_escape_sql_literal()` 與 `get_resilient_client()` 工廠函數。
- **測試**：兩個失敗測試已修復。新增 `conftest.py`、`pytest.ini` 與 `.github/workflows/test.yml`（CI/CD）。

## 為什麼存在

多數字幕工具止於「摘要這個檔案」。TranscriptFlow 將字幕視為資料管線問題，特別是當來源存檔龐大、雜亂、難以透過標題或 metadata 搜尋時：

- 保留語意邊界，而非固定長度分割
- 透過可復原的狀態機處理大量檔案
- 重試失敗區塊，不捨棄已成功的工作
- 追蹤模型錯誤、耗時、進度與批次健康狀態
- 產出 RAG 就緒的記錄，用於語意搜尋與代理記憶

## 這是什麼

TranscriptFlow 提供一條從字幕檔案到 LanceDB 向量資料庫的彈性、容錯路徑：

```text
SRT 字幕 -> 語意段落 -> 摘要/標籤 -> 嵌入向量 -> LanceDB
```

輸入字幕可來自 Whisper、YouTube 字幕或內嵌字幕匯出。輸出為結構化資料，適用於語意搜尋、檢索增強生成、個人知識系統、研究存檔或代理記憶。

適用場景：

- 有大量長篇字幕需要處理
- 內容難以僅透過標題搜尋
- 存檔混合不同主題、講者或內容風格
- 需可續傳性，因為完整作業可能耗費數小時或數天
- 希望模型失敗可被看見，而非靜默破壞輸出

## 這不是什麼

TranscriptFlow 不是一鍵封存工具，也不會為你下載影片。它需要字幕檔案與 manifest。它也不假設 LLM 足夠可靠以直接處理 timestamp 分段。LLM 被用在其最擅長的領域：摘要、標籤生成與 metadata 提取——在字幕已被分割為驗證過的區塊之後。

## 主要功能

- **Smart Merge 3.0 語意分段**：重疊字幕視窗、嵌入向量 cosine similarity、百分位數斷點、最小區間驗證、雜訊過濾
- **四階段管線**：分段、摘要、嵌入、LanceDB 寫入
- **區塊與重試追蹤**：區塊級重試保留已成功工作，per-model 診斷
- **Checkpoint 安全續傳**：僅在來源區塊文字 hash 仍相符時重複使用已完成摘要
- **關閉即失敗寫入**：部分摘要、部分嵌入回應、無效向量、不相容的 LanceDB schema 會停止管線而非靜默丟棄記錄
- **Idempotent 向量索引**：LanceDB 寫入使用穩定 `file_id` 與 `chunk_id` 欄位搭配 merge-upsert 語義，避免重新執行時產生重複列
- **Watchdog 自動化**：掃描 batch status 檔案、推進可執行工作、重設逾時作業、管理階段並發
- **OpenAI-compatible API**：支援 OpenAI、LiteLLM Proxy、OpenRouter、vLLM 等

## 可調參數

TranscriptFlow 設計為適應不同字幕存檔，而非假設單一分段策略。最重要的控制項在 `scripts/config.example.json` 中。

參數包括視窗大小、強弱斷點百分位數、最小句子數、雜訊過濾長度、摘要模型清單、重試次數、並發數、嵌入模型與維度等。

## 架構

```text
SRT 檔案 + master manifest
        │
        ▼
batch_status_*.json
        │
        ▼
auto_watchdog.py
        │
        ├──> summarize.py --phase chunking
        │        parse_srt.py → semantic_chunk.py
        │
        ├──> summarize.py --phase summarizing
        │        summarize_pipeline.py
        │
        ├──> summarize.py --phase embedding
        │        batch_embedding.py
        │
        └──> summarize.py --phase db_inserting
                 finalize.py → LanceDB
```

## 快速開始（正式管線）

```bash
git clone https://github.com/samson1357924/TranscriptFlow.git
cd TranscriptFlow
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp scripts/config.example.json config.json
```

編輯 `.env` 與 `config.json` 設定 API 端點、模型名稱、輸出目錄與 LanceDB 路徑。

```bash
set -a && source .env && set +a
python3 scripts/state_manager.py init_batch 0 0
python3 scripts/auto_watchdog.py
```

> **注意**：使用 `set -a && source .env && set +a` 而非 `export $(grep -v '^#' .env | xargs)`，以避免 shell 剝掉 JSON 值的雙引號。

## 驗證

執行區域回歸測試套件：

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
```

回歸測試涵蓋設定載入、Smart Merge 小檔案輸出形狀、狀態檔 sidecar 鎖定、checkpoint 續傳安全、部分摘要阻斷、嵌入回應驗證、記錄驗證與 LanceDB merge-upsert idempotency。

即時端到端驗證：

```bash
set -a && source .env && set +a
export SRT_OUTPUT_DIR="$PWD/output/live_validation"
export SRT_DB_PATH="$PWD/output/live_validation_db"
python3 scripts/state_manager.py init_batch 1 2
python3 scripts/summarize.py --id 1 --batch "$SRT_OUTPUT_DIR/batch_status_1_2.json" --phase chunking
python3 scripts/summarize.py --id 1 --batch "$SRT_OUTPUT_DIR/batch_status_1_2.json" --phase summarizing
python3 scripts/summarize.py --id 1 --batch "$SRT_OUTPUT_DIR/batch_status_1_2.json" --phase embedding
python3 scripts/summarize.py --id 1 --batch "$SRT_OUTPUT_DIR/batch_status_1_2.json" --phase db_inserting
```

## 測試管線（方案 A — 單次執行）

```bash
python3 scripts/chunk_test_runner.py --file-id 11
python3 scripts/chunk_test_runner.py --file-id 11 --chunk-params '{"window_size":3}'
```

輸出：`.txt` 報告 + `.json` 結構化資料。

## 設定

TranscriptFlow 依以下順序讀取設定：

1. 環境變數
2. `config.json`（專案根目錄）
3. `scripts/config.example.json`

重要環境變數：

```bash
OPENAI_BASE_URL=https://api.openai.com
OPENAI_API_KEY=replace-with-your-key
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_EXPECTED_DIM=3072
SRT_OUTPUT_DIR=./output
SRT_DB_PATH=./lancedb
SRT_MASTER_FILE=./examples/master_file_manifest.example.json
```

請勿提交 `.env`、`config.json`、產出檔案或 LanceDB 資料。

## 更多功能

- **SRT 品質檢查**：多模型群組 LLM 評估，共識嚴重度彙總，互動修正模式
- **B+ Chunk 品質評估**：邊界合理性 + 雜訊處理評分，跨設定比較
- **摘要忠實度評估**：事實正確性、完整性、中立性、整體品質
- **Manifest 產生器**：掃描 data_dir 自動產生 `master_file_manifest.json`
- **統一 LLM 用戶端**：`scripts/llm_client.py` 提供 `call_llm()`、`get_models()`、`extract_participants()`、`get_embedding_client()`

## 狀態機

```text
undone
  → chunking → queueing_1
  → summarizing → queueing_2
  → embedding → queueing_3
  → db_inserting → done

failed → undone
failed_permanent
```

## 路線圖

- [ ] LiteLLM、OpenRouter、vLLM 的 provider 專屬範例
- [ ] 使用 mock API 的整合測試
- [ ] CLI 批次初始化與階段選擇的 ergonomics 改善

## 授權

MIT
