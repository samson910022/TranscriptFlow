# Changelog

## v1.7 (2026-05-19): Multi-Group Consensus, Parallel Groups & Timing Stats

### 🚀 New Features

- **`srt_quality_check.py`: Multi-model-group parallel execution** — `model_groups` config supports multiple model lists; each group's models are called simultaneously per window for load distribution. Groups execute in parallel via `ThreadPoolExecutor`.
- **Nested parallelism model** — `concurrency` controls the number of **windows** processed simultaneously; all models within a group fire concurrently per window via an inner thread pool. Total concurrent LLM calls ≈ `concurrency × len(models)`. Fixes the bottleneck where a flat pool serialized multi-model groups.
- **Consensus merge report** — Cross-group flagged lines are merged into a single report. Severity determined by configurable thresholds (`consensus_thresholds`, default `[0.2, 0.6]`): <20% ✅ clean, 20–60% 🟡 questionable, ≥60% 🔴 problem.
- **Per-group independent concurrency** — `srt_quality.concurrency` accepts an array (e.g., `[9, 3, 9]`) for per-group concurrency. Single integer remains backward-compatible.
- **Batch file ID range** — `--file-id 0-2` syntax for processing a range of files.
- **Supplementary boundary windows** — First/last `stride` rows now get extra wider windows to ensure equal double-review coverage alongside middle rows.
- **Model timing statistics** — Merged report header includes per-model average response time, completed segments count, per-group wall-clock time, and total elapsed.

### 🔧 Improvements

- `line_coverage_counts()` now computes coverage from actual windows (including supplementaries) instead of formula.
- `aggregate_flags()` and merged report row validation use per-window entry count instead of fixed `window_size`.
- `.gitignore` updated: `*.docx` ignored.
- `config.example.json` updated with `srt_quality` block.
- Evaluation scripts (`evaluate_chunks.py`, `evaluate_summary_fidelity.py`) default output to subdirs under `output_dir`.

## v1.6 (2026-05-16): Pipeline Recovery, Idempotent Writes & Live Validation

### Reliability

- **Status writes use a sidecar lock file** — `_locked_read_write()` now locks a stable `.lock` file instead of the JSON file that is replaced by `os.replace()`, preventing stale-inode lost updates under concurrent workers.
- **Phase slot writes are atomic** — phase concurrency counters now use a sidecar lock plus tempfile replacement.
- **Project root detection fixed** — script defaults now resolve to the repository root instead of walking above the checkout in direct import scenarios.

### Recovery & Fail-Closed Behavior

- **Summarization resume is content-aware** — completed chunk summaries include `source_text_hash`; stale cached summaries are ignored when source chunk text changes.
- **Partial summarization is blocked** — any failed chunk in a summary output prevents the embedding phase from continuing.
- **Checkpoint finalization preserves failed chunks** — final output now keeps both successful and failed results so downstream validation can detect partial failure.
- **Embedding validation is strict** — batch embedding responses fail closed when vector dimensions mismatch or the API returns fewer embeddings than requested.

### LanceDB Integrity

- **Stable record identity** — records now include `file_id` and `chunk_id`.
- **Schema preflight** — LanceDB writes validate required fields, vector shape, finite values, and existing table schema before writing.
- **Idempotent merge-upsert** — DB insertion uses `merge_insert("chunk_id")` instead of append-only writes or title-based delete/add.
- **Chunk-centric deduplication** — duplicate filtering now prefers `chunk_id`, avoiding accidental drops when titles or start times collide.

### Validation

- Added regression coverage for Smart Merge output shape, status-file locking, checkpoint resume, partial summarization blocking, embedding partial-response failure, record validation, and LanceDB upsert idempotency.
- Live validation completed for file IDs 1 and 2:
  - ID 1: 43 chunks summarized, embedded, and written.
  - ID 2: 32 chunks summarized, embedded, and written.
  - LanceDB validation: 75 rows, 75 unique chunk IDs.

## v1.5 (2026-05-14): API Key Security, Session Pool, Atomic Writes & Code Split

### 🔒 Security

- **API key 從 `config.json` 移除** — `api_key` 僅存於 `.env`（`chmod 600`），由 `OPENAI_API_KEY` 環境變數提供
- **`.env` 權限強化** — 安裝後自動設定 `600`（僅所有者可讀）

### ⚡ Performance

- **`requests.Session()` 連線池** — `llm_client.py` 與 `batch_embedding.py` 共用模組級 `Session()`，減少 TCP 連線建置成本
- **`save_status()` 原子寫入** — 改為 `tempfile.NamedTemporaryFile` + `os.replace`，崩潰時不再損壞狀態檔
- **`_locked_read_write()` 原子寫入** — 同上，分離讀取鎖與檔案寫入，寫入階段使用 tempfile + rename

### 🧹 Code Quality

- **`smart_merge_3_0()` 拆分為 5 個獨立函數** — `_embed_windows()`、`_compute_similarities()`、`_resolve_breakpoints()`、`_build_bp_meta()`、`_build_segments()`。主函數從 194 行降至約 25 行，每個子函數可獨立測試

## v1.4 (2026-05-14): Circuit Breaker, Lock Fix, JSON Integrity & Unified LLM Client

### 🐛 Bug Fixes

- **`batch_embedding.py`: CircuitBreaker bypassed** — `generate_batch_embeddings()` 直接 try/except 跳過熔斷器，已改為提取 `_do_request()` 內層函數，透過 `self.circuit_breaker.call()` 保護 API 呼叫
- **`summarize_pipeline.py`: checkpoint JSON 損壞** — `write_chunk_result()` 第一個 chunk 寫入時缺少 `[` 陣列括號。已修正：首筆寫 `[` + JSON，後續寫 `,` + JSON，`finalize()` 讀取時補 `]`
- **`state_manager.py`: blocking lock 無 timeout** — `_locked_read_write()` 3 次 `LOCK_NB` 失敗後 fallback 到阻塞鎖，可永久卡死。已改為 LOCK_NB 重試 10 次（間隔逐次遞增，max 30s），逾時拋 `RuntimeError`

### 🔧 Improvements

- **`scripts/llm_client.py` — 統一 LLM 客戶端** — 新增共用模組，提供 `call_llm()` 與 `get_models()`，消除 5 個檔案（`summarize_pipeline.py`、`chunk_test_runner.py`、`evaluate_chunks.py`、`evaluate_summary_fidelity.py`、`srt_quality_check.py`）中重複的 LLM 呼叫實作，淨減 62 行程式碼
- **`.env.example` 清理** — 移除已廢棄的 `SUMMARIZATION_MODELS`，加註說明改由 `config.json` 管理

## v1.3 (2026-05-14): Parallel Quality Check, Evaluation Model Config & Code Cleanup

### 🚀 New Features

- **`srt_quality_check.py` 並行處理** — 支援 `--concurrency` 參數，透過 `ThreadPoolExecutor` 同時送多窗給 LLM 評分。預設 5，可從 `config.json` `srt_quality.concurrency` 設定
- **評估腳本獨立 model 設定** — `srt_quality_check.py` 讀取 `srt_quality.models`，`evaluate_chunks.py` 讀取 `evaluation.chunk_models`，`evaluate_summary_fidelity.py` 讀取 `evaluation.fidelity_models`，各自未設定時 fallback 至 `summarization.models`

### 🔧 Improvements

- **`.gitignore` 加入 `.progress/` / `.failed_reports/`** — 防禦性保護動態產生目錄
- **死亡狀態清理** — 移除 `_VALID_TRANSITIONS` 中的 `working` 與 `verified-1`（無任何程式碼會轉入），刪除 `parse_validator_output()` 函數與對應 CLI 入口
- **`auto_watchdog.py` / `batch_audit.py` 同步清理** — 移除 `working` 相關的死碼分支
- **`validate_config()` 變數重新命名** — `cw`→`window_size`、`mc`→`min_chunks_val`、`mxc`→`max_chunks_val`、`edim`→`embed_dim`、`pc`→`participant_chunks_val`、`mr`→`max_retries_val`

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
