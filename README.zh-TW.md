# TranscriptFlow

[![English](README.md)][![繁體中文](README.zh-TW.md)]

---

韌性 AI 資料管道，將原始逐字稿轉換為可搜尋、可摘要、向量化的知識庫。

TranscriptFlow 將 YouTube／SRT 字幕檔案轉換為語意區塊、LLM 生成的摘要與標籤、批次嵌入向量，以及 LanceDB 向量索引。專為長篇逐字稿與批次任務設計，注重可觀測性、重試機制與可復原性。

## 專案起源

TranscriptFlow 源自一個非常實際的保存問題。

一個我追蹤多年的心理學頻道宣布將停止更新，並計畫在約一年後刪除其影片存檔。該頻道大約每天發布一部影片，持續超過三年，留下了大量長篇內容。這些內容很有價值，但難以搜尋：標題通常與實際討論內容關聯鬆散，而 YouTube 搜尋不足以讓我在數百部影片中找到特定的想法、案例、來賓或主題。

大約在同一時期，我開始實驗 OpenClaw，並進一步了解了 LanceDB、嵌入向量與向量檢索。這讓我萌生了將字幕檔案轉換為可搜尋知識庫的想法。該專案經過多次 AI 輔助探索、除錯、失敗嘗試與重新設計而成長。

早期的做法是讓 LLM 直接對逐字稿進行分段。實際上，這對於我手中的模型與工作負載來說過於不穩定：出現了幻覺邊界與不可能的時間戳，包括僅有幾十分鐘的逐字稿卻產生了 `1:20:xx` 的時間軸。那次失敗塑造了目前的設計方向。TranscriptFlow 避免依賴 LLM 進行結構性時間邊界的判斷，而是採用更具確定性的、嵌入輔助的區塊切割管道，並具備驗證、重試與可稽核性。

最終成果不僅僅是一個摘要工具，而是一個可復原的逐字稿到向量資料庫的管道，用於將大量字幕檔案轉換為 RAG 就緒的知識庫。

## 工程亮點

TranscriptFlow 旨在展示產品級 AI 管道的設計，而非單一的提示詞封裝：

- 具備明確檔案狀態的可復原多階段處理
- 區塊層級重試，保留成功結果
- 模型層級診斷：吞吐量、延遲、失敗次數與錯誤分布
- 長時間摘要任務的原子化檢查點寫入
- 對部分摘要、部分嵌入向量與不匹配的資料庫記錄進行失敗關閉驗證
- 基於穩定檔案 ID 與區塊 ID 的冪等 LanceDB 寫入（合併插入語意）
- 具備斷路器保護的自適應嵌入批次處理
- 資料完整性、陳舊任務與跨檔案一致性的稽核工具

## 近期強化（v1.8）

2026 年 5 月完成全面的程式碼審查（25 項發現：7 項重大、12 項重要、6 項建議）：

- **安全性**：`sanitize_api_url()` 現在在 IP 檢查前先解析 DNS，並使用 `ipaddress.is_private` 完整涵蓋 RFC 1918 + CGNAT，關閉 DNS 欺騙繞過途徑。API 金鑰快取消除了每次記錄讀取設定檔的操作。
- **模組安全性**：`finalize.py` 與 `summarize.py` 使用延遲初始化——匯入模組不再因環境變數未設定而崩潰。
- **資料完整性**：斷路器後備方案改為拋出 `CircuitBreakerOpenError`，而非靜默回傳 `None` 並標示 `success=True`。語意切割中移除了進度恢復功能，以防止重新執行時產生不正確的中斷點。
- **並發性**：共享的 `BatchEmbeddingClient` 單例消除了同一嵌入端點的重複斷路器。`save_status()` 鎖定機制統一使用 `_locked_read_write()`。
- **程式碼品質**：合併重複的 `extract_participants()`；移除手動驗證檢查，改用 Pydantic `ConfigSchema`；Smart Merge 診斷重構為呼叫正式程式碼；清除無用程式碼。

### 近期強化（v1.9）

2026 年 5 月完成第二次全面審查（24 項發現）：

- **安全性**：所有測試設定檔中的硬編碼 API 金鑰已移除。子程序環境變數縮減為僅允許清單。`_health_check()` 現在在失敗時正確回傳 `False`。
- **匯入安全性**：`llm_client.py` 與 `summarize_pipeline.py` 在匯入時能優雅處理缺少設定檔的情況。
- **正確性**：移除 `release_phase_slot()`（原為空操作）。修正 `batch_audit`（維度分數聚合、陳舊診斷變數）。損壞的 JSON 修復改為跳過並發出警告。單一嵌入視窗失敗改為軟失敗。
- **無用程式碼**：移除 `ResilientEmbeddingClient`（完全未使用，使用 `urllib` 而非 `requests`）、`_escape_sql_literal()` 與 `get_resilient_client()` 工廠函式。
- **測試**：修正兩個失敗的測試。新增 `conftest.py`、`pytest.ini` 與 `.github/workflows/test.yml`（CI/CD）。

## 為何存在

大多數逐字稿工具止步於「摘要這個檔案」。TranscriptFlow 將逐字稿視為資料管道問題，特別是當來源檔案庫龐大、雜亂且不易透過標題或元資料搜尋時：

- 保留語意邊界，而非按固定長度分割
- 透過可復原的狀態機處理大量檔案
- 重試失敗區塊而不丟棄成功結果
- 追蹤模型錯誤、耗時、進度與批次健康狀態
- 產出 RAG 就緒的記錄，用於語意搜尋與代理記憶

## 適用範圍

TranscriptFlow 提供一條從字幕檔案到 LanceDB 向量資料庫的靈活、容錯路徑：

```text
SRT 字幕 -> 語意區塊 -> 摘要/標籤 -> 嵌入向量 -> LanceDB
```

輸入的字幕可來自 Whisper、YouTube 字幕或內嵌字幕匯出。輸出為結構化資料，適用於語意搜尋、檢索增強生成、個人知識系統、研究檔案或代理記憶。

適用情境：

- 有大量長篇逐字稿需要處理
- 內容難以僅透過標題搜尋
- 檔案庫混合不同主題、講者或內容風格
- 需要可中斷恢復性（完整任務可能耗費數小時或數天）
- 希望模型失敗是可見的，而非靜默地破壞輸出

## 不適用範圍

TranscriptFlow 不是一鍵歸檔工具，也不會為你下載影片。它需要字幕檔案與清單檔案。它也不假設 LLM 在時間戳分段上是足夠可靠的。LLM 被用於它們最擅長的部分：在逐字稿已被分割為驗證後的區塊之後，進行摘要、標記與元資料提取。

## 主要功能

- **Smart Merge 3.0 語意區塊切割**：重疊字幕視窗、嵌入餘弦相似度、百分位數斷點、最小跨度驗證與雜訊過濾。
- **四階段管道**：區塊切割、摘要、嵌入向量、LanceDB 插入。
- **區塊與重試追蹤**：區塊層級重試保留成功結果，每模型診斷。
- **檢查點安全恢復**：僅當來源區塊文字雜湊仍相符時，才重複使用已完成的摘要。
- **失敗關閉寫入**：部分摘要、部分嵌入回應、無效向量與不相容的 LanceDB 架構會停止管道，而非靜默丟棄記錄。
- **冪等向量索引**：LanceDB 寫入使用穩定的 `file_id` 與 `chunk_id` 欄位，搭配合併插入語意，避免重新執行時產生重複列。
- **看門狗自動化**：掃描批次狀態檔案、推進可執行的工作、重設逾時任務、管理階段並發數。
- **OpenAI 相容 API**：支援 OpenAI、LiteLLM Proxy、OpenRouter、vLLM 等。

## 可調參數

TranscriptFlow 旨在適應不同的逐字稿檔案庫，而非假設一種完美的區塊切割策略。最重要的控制項位於 `scripts/config.example.json`：

- `chunking.smart_merge_window_size`：每次合併視窗中考慮的字幕條目數
- `chunking.smart_merge_strong_pct`：強語意邊界的百分位數閾值
- `chunking.smart_merge_weak_pct`：較弱候選邊界的百分位數閾值
- `chunking.smart_merge_min_sentences`：接受區塊邊界前的最小句子數
- `chunking.smart_merge_noise_drop_len`：應丟棄的短雜訊段落長度
- `chunking.smart_merge_noise_weak_len`：視為弱邊界候選的短段落長度
- `chunking.min_chunks` / `chunking.max_chunks`：區塊數量的下限與上限
- `summarization.models`：用於摘要與標籤生成的有序聊天模型清單
- `summarization.max_retries`：每個區塊的重試次數
- `summarization.concurrency`：摘要工作者的並發數
- `embedding.model`：嵌入模型名稱
- `embedding.expected_dim`：驗證與 LanceDB 架構的預期向量維度
- `embedding.batch_max_size`：最大嵌入批次大小
- `phase_concurrency`：看門狗各階段的並發限制
- `watchdog.max_working_time_sec`：任務被視為卡住前的逾時秒數

這些參數使管道適用於不同節奏的檔案庫：訪談、講座、播客、座談會、課程逐字稿或混合長篇媒體。

## 架構

```text
SRT 檔案 + 主清單
        |
        v
batch_status_*.json
        |
        v
auto_watchdog.py
        |
        +--> summarize.py --phase chunking
        |       parse_srt.py -> semantic_chunk.py
        |
        +--> summarize.py --phase summarizing
        |       summarize_pipeline.py
        |
        +--> summarize.py --phase embedding
        |       batch_embedding.py
        |
        +--> summarize.py --phase db_inserting
                finalize.py -> LanceDB
```

## 快速開始（正式管道）

```bash
git clone https://github.com/samson910022/TranscriptFlow.git
cd TranscriptFlow

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp scripts/config.example.json config.json
```

編輯 `.env` 與 `config.json`，設定 API 端點、模型名稱、輸出目錄與 LanceDB 路徑。

```bash
set -a && source .env && set +a

python3 scripts/state_manager.py init_batch 0 0
python3 scripts/auto_watchdog.py
```

> **注意**：使用 `set -a && source .env && set +a` 而非 `export $(grep -v '^#' .env | xargs)`，以避免 shell 去除 JSON 值中的雙引號。

## 驗證

在變更管道行為前，先執行本地回歸測試套件：

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
```

回歸測試涵蓋：設定檔載入、Smart Merge 小型檔案輸出形狀、狀態檔 sidecar 鎖定、檢查點恢復安全性、部分摘要阻擋、嵌入回應驗證、記錄驗證與 LanceDB 合併插入冪等性。

如需端到端即時驗證，初始化一個小型批次，將輸出指向臨時目錄，然後依序執行各階段：

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

## 測試管道（方案 A — 單次執行）

快速測試參數，不需寫入 LanceDB：

```bash
python3 scripts/chunk_test_runner.py --file-id 11
python3 scripts/chunk_test_runner.py --file-id 11 --chunk-params '{"window_size":3}'
python3 scripts/chunk_test_runner.py --file-id 11 --models '["NV-deepseek-v4-flash"]'
```

## 測試套件（方案 B — 多組設定比較）

建立一個 JSON 套件來比較多組參數：

```json
{
    "name": "視窗大小比較",
    "files": [0, 11],
    "configs": [
        {"label": "baseline",   "chunking": {"smart_merge_window_size": 5, ...}, "models": [...]},
        {"label": "window_3",   "chunking": {"smart_merge_window_size": 3}},
        {"label": "strong_0_03","chunking": {"smart_merge_strong_pct": 0.03}}
    ]
}
```

## 設定

TranscriptFlow 按以下順序讀取設定：

1. 環境變數
2. `config.json`（專案根目錄）
3. `scripts/config.example.json`

請勿提交 `.env`、`config.json`、`test_params_suite.json`、產出檔案或 LanceDB 資料。

## 逐字稿品質檢查

多模型組 LLM 評估，找出轉錄錯誤或不合邏輯的段落。支援並行模型組、共識式嚴重性聚合與每模型計時統計。

```bash
python3 scripts/srt_quality_check.py --file-id 3
python3 scripts/srt_quality_check.py --file-id 0-2
python3 scripts/srt_quality_check.py --file-id 3 --interactive
python3 scripts/srt_quality_check.py --input sample.srt
```

### 共識嚴重性

| 共識比例 | 標籤 | 意義 |
|---|---|---|
| <20% | ✅ 乾淨 | 多數檢查者認為沒問題 |
| 20–60% | 🟡 可疑 | 信號分歧 |
| ≥60% | 🔴 有問題 | 多模型強烈一致 |

## 授權

MIT
