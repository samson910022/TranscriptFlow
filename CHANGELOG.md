# Changelog

## v1.2 (2026-05-13): LLM Quality Evaluation & SRT Checker

### 🚀 New Features

- **`scripts/srt_quality_check.py`** — SRT 字幕品質檢查。滑窗 10 句、重疊 5 句送 LLM 評分（連貫性 / 邏輯合理性 / 語句品質 / 時間合理性），跨窗彙整標記（≥2 窗 🔴 有問題 / 1 窗 🟡 存疑）。支援批次報告與互動修正模式（替換/編輯/保留）
- **`scripts/evaluate_chunks.py` (B+ 方案)** — LLM 評估分段品質。掃描方案 A/B 的 JSON 輸出，對每個 chunk 評分：邊界合理性（內部連貫性 25 + 斷點合理與否 25）+ 雜訊處理 50 = 總分 100。自動跨 config 比較並推薦最佳參數
- **`scripts/evaluate_summary_fidelity.py`** — 摘要忠實度獨立評估。對每個 chunk 的原文 + 摘要送 LLM 評分（事實正確性 / 完整性 / 中立性 / 整體品質），擷取幻覺內容與遺漏關鍵點，跨 model 統計
- **`scripts/generate_manifest.py`** — 掃描 data_dir 自動產生 master_file_manifest.json

### 📦 New Scripts (v1.2)

- `srt_quality_check.py` — SRT 品質檢查
- `evaluate_chunks.py` — B+ Chunk 品質評分
- `evaluate_summary_fidelity.py` — 摘要忠實度評估
- `generate_manifest.py` — 總表產生器

## v1.1 (2026-05-13): Test Pipeline & Boundary Diagnostics

### 🚀 New Features

- **`scripts/chunk_test_runner.py` (方案 A)** — 獨立測試腳本，直接 from SRT → chunking → summarizing → `.txt` 報告，跳過 LanceDB 與 watchdog 生產管線。支援 CLI 覆蓋 chunking 參數與 summarization models
- **`scripts/chunk_test_suite.py` (方案 B)** — 多組參數比較測試套件。讀取單一 suite JSON，依序執行多組 `(file_id × config)` 組合，輸出結構化 `.json` 側檔與跨組比對報告（含量化統計、分段邊界差異分析）
- **邊界強度資訊** — `smart_merge_3_0()` 回傳每個 segment 的上/下邊界強度百分位排名與 cosine 相似度，同時輸出於 `.txt` 報告與結構化 `.json`
- **被排除片段回傳** — `smart_merge_3_0()` 第三回傳值 `discarded_chunks`，記錄被雜訊過濾丟棄的段落及其排除原因 (`noise_too_short` / `noise_weak_links`)

### 🔧 Improvements

- **`summarize.py` phase_summarizing 進入時清除舊 output cache** — 避免 CheckpointManager 殘留 `_chunks_output.json` 導致 chunks 被跳過
- **`summarize.py` phase_db_inserting 空 records 時 raise** — 防止全數 chunk 失敗仍被標記 `done` 的靜默錯誤
- **`semantic_chunk.py` `seg` 改寫** — 移除 loop 內重複的 `entries[start_idx:end_idx+1]` 計算，改用 `_chunk_base` 統一產生 chunk dict
- **`.env` 移除 `SUMMARIZATION_MODELS`** — 避免 shell `export $(xargs)` 剝掉雙引號導致 JSON 解析失敗，統一由 `config.json` 管理

### 🐛 Bug Fixes

- **`test_config_loader.py`** — 修正測試隔離問題，使用 `monkeypatch.setattr` 切換 `CONFIG_PATH` 而非依賴環境變數

## v1.0 (2026-05-13): TranscriptFlow Production Baseline

### 🚀 Core Pipeline

- **Smart Merge 3.0** — 語意分段演算法：重疊窗口 embedding、cosine similarity 相似度、百分位數斷點、最小區間驗證、雜訊過濾
- **四階段管線**：chunking → summarizing → embedding → LanceDB insert
- **11 狀態機**：`undone → chunking → queueing_1 → summarizing → queueing_2 → embedding → queueing_3 → db_inserting → done`，含 `failed → undone` 重試與 `failed_permanent` 終止狀態
- **Watchdog 自動化**：`auto_watchdog.py` 掃描 batch_status 檔案，自動推進可執行工作、重設逾時任務、管理相位並發（`phase_concurrency` 設定）
- **Atomic checkpoint writes**：`CheckpointManager` 支援斷點續傳，`fcntl` 檔案鎖定確保併發安全
- **Chunk-level retry**：失敗 chunk 獨立重試、跨模型輪換、`max_retries` 上限
- **Model diagnostics**：記錄 per-model 成功率、chars/sec、錯誤分布，輸出 `model_diagnostics_{file_id}.json`
- **Batch audit tooling**：`batch_audit.py` 六維度稽核（Schema、Timeliness、Error Visibility、Data Integrity、Model Performance、Cross-File）

### 🔧 Configuration

- **統一配置加載**：`config_loader.py` 支援環境變數優先、`config.json` 後備、巢狀 key 存取
- **雙軌 `chars_per_sec`**：`overall` (wall-clock) 與 `actual` (pure processing time)
- **OpenAI-compatible API**：支援 LiteLLM Proxy、OpenRouter、vLLM 等
- **`.env` / `config.json` 分離**：機密存於 `.env`，非機密存於 `config.json`

### 📦 Scripts

- `parse_srt.py` — SRT 字幕解析
- `semantic_chunk.py` — Smart Merge 3.0 語意分段
- `summarize_pipeline.py` — LLM 摘要生成（多模型輪循、retry、incremental write）
- `summarize.py` — 各 phase 入口
- `batch_embedding.py` — Batch embedding client
- `finalize.py` — LanceDB 寫入
- `state_manager.py` — 狀態管理
- `auto_watchdog.py` — 自動化監控
- `run_wrapper.sh` — 統一啟動腳本
