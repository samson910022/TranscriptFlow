# TranscriptFlow

Resilient AI data pipeline for turning raw transcripts into searchable, summarized, vectorized knowledge.

TranscriptFlow converts YouTube/SRT subtitle files into semantic chunks, LLM-generated summaries and tags, batched embeddings, and LanceDB vector indexes. It is designed for long transcripts and batch jobs where observability, retries, and recoverability matter.

## Project Origin

TranscriptFlow started from a very practical preservation problem.

A psychology channel I followed for years announced that it would stop updating and planned to take down its video archive about a year later. The channel had published roughly one video per day for more than three years, leaving behind a large body of long-form material. The content was valuable, but difficult to search: titles were often only loosely related to the actual discussion, and YouTube search was not enough when I wanted to find a specific idea, example, guest, or topic buried somewhere inside hundreds of videos.

Around the same time, I started experimenting with OpenClaw and learned more about LanceDB, embeddings, and vector retrieval. That gave me the idea to turn subtitle files into a searchable knowledge base. The project grew through many rounds of AI-assisted exploration, debugging, failed attempts, and redesign.

One early approach was to ask an LLM to segment transcripts directly. In practice, this was too unstable for the models and workloads I had available: hallucinated boundaries and impossible timestamps appeared, including cases where a transcript only lasted tens of minutes but the generated timeline contained values such as `1:20:xx`. That failure shaped the current design. TranscriptFlow avoids relying on LLMs for structural time boundaries and instead uses a more deterministic, embedding-assisted chunking pipeline with validation, retries, and auditability.

The result is not just a summarizer. It is a resilient transcript-to-vector-database pipeline for turning large subtitle archives into RAG-ready knowledge.

## Engineering Highlights

TranscriptFlow is built to demonstrate production-oriented AI pipeline design rather than a single prompt wrapper:

- recoverable multi-phase processing with explicit file states
- chunk-level retries that preserve successful work
- model-level diagnostics for throughput, latency, failures, and error distribution
- atomic checkpoint writes for long-running summarization jobs
- fail-closed validation for partial summaries, partial embeddings, and mismatched DB records
- idempotent LanceDB writes keyed by stable file and chunk identifiers
- adaptive embedding batching with circuit-breaker protection
- audit tooling for data integrity, stale jobs, and cross-file consistency

## Why It Exists

Most transcript tools stop at "summarize this file." TranscriptFlow treats transcripts as a data pipeline problem, especially when the source archive is large, messy, and not easily searchable by title or metadata:

- preserve semantic boundaries instead of splitting by fixed length
- process many files through a recoverable state machine
- retry failed chunks without throwing away successful work
- track model errors, elapsed time, progress, and batch health
- produce RAG-ready records for semantic search and agent memory

## What It Is

TranscriptFlow provides a flexible, fault-tolerant path from subtitle files to a LanceDB vector database:

```text
SRT subtitles -> semantic chunks -> summaries/tags -> embeddings -> LanceDB
```

The input subtitles can come from Whisper, YouTube captions, or embedded subtitle exports. The output is structured data suitable for semantic search, retrieval-augmented generation, personal knowledge systems, research archives, or agent memory.

It is useful when:

- you have many long transcripts to process
- the content is hard to search by title alone
- the archive mixes different topics, speakers, or content styles
- you need resumability because the full job may take hours or days
- you want model failures to be visible instead of silently corrupting output

## What It Is Not

TranscriptFlow is not a one-click archival tool and does not download videos for you. It expects subtitle files and a manifest. It also does not assume that LLMs are reliable enough to own timestamp segmentation directly. LLMs are used where they are strongest: summarization, tagging, and metadata extraction after the transcript has already been split into validated chunks.
## Key Features

- **Smart Merge 3.0 semantic chunking**: overlapping subtitle windows, embedding cosine similarity, percentile breakpoints, minimum-span validation, and noise filtering.
- **Four-phase pipeline**: chunking, summarization, embedding, and LanceDB insertion.
- **Chunk & retry tracking**: chunk-level retries that preserve successful work, per-model diagnostics.
- **Checkpoint-safe resumability**: completed summaries are reused only when the source chunk text hash still matches.
- **Fail-closed writes**: partial summarization, partial embedding responses, invalid vectors, and incompatible LanceDB schemas stop the pipeline instead of silently dropping records.
- **Idempotent vector indexing**: LanceDB writes use stable `file_id` and `chunk_id` fields with merge-upsert semantics to avoid duplicate rows on reruns.
- **Watchdog automation**: scans batch status files, advances eligible work, resets timed-out jobs, and manages phase concurrency.
- **OpenAI-compatible API**: works with OpenAI, LiteLLM Proxy, OpenRouter, vLLM, etc.

## Tunable Parameters

TranscriptFlow is designed to adapt to different transcript archives rather than assuming one perfect chunking strategy. The most important controls live in `scripts/config.example.json`:

- `chunking.smart_merge_window_size`: number of subtitle entries considered in each merge window
- `chunking.smart_merge_strong_pct`: percentile threshold for strong semantic boundaries
- `chunking.smart_merge_weak_pct`: percentile threshold for weaker candidate boundaries
- `chunking.smart_merge_min_sentences`: minimum span before a chunk boundary is accepted
- `chunking.smart_merge_noise_drop_len`: short noisy segments to discard
- `chunking.smart_merge_noise_weak_len`: short segments treated as weak boundary candidates
- `chunking.min_chunks` / `chunking.max_chunks`: lower and upper bounds for chunk counts
- `summarization.models`: ordered list of chat models used for summary/tag generation
- `summarization.max_retries`: retry budget per chunk
- `summarization.concurrency`: summarization worker concurrency
- `embedding.model`: embedding model name
- `embedding.expected_dim`: expected vector dimension for validation and LanceDB schema
- `embedding.batch_max_size`: maximum embedding batch size
- `phase_concurrency`: per-phase concurrency limits for the watchdog
- `watchdog.max_working_time_sec`: timeout before a stuck job is reset

These parameters make the pipeline suitable for archives with different rhythms: interviews, lectures, podcasts, panel discussions, course transcripts, or mixed long-form media.

## Architecture

```text
SRT files + master manifest
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

## Quick Start (Production Pipeline)

```bash
git clone https://github.com/samson1357924/TranscriptFlow.git
cd TranscriptFlow

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp scripts/config.example.json config.json
```

Edit `.env` and `config.json` for your API endpoint, model names, output directory, and LanceDB path.

```bash
set -a && source .env && set +a

python3 scripts/state_manager.py init_batch 0 0
python3 scripts/auto_watchdog.py
```

> **Note**: Use `set -a && source .env && set +a` instead of `export $(grep -v '^#' .env | xargs)` to avoid shell stripping double quotes from JSON values.

## Validation

Run the local regression suite before changing pipeline behavior:

```bash
.venv/bin/pytest -q
.venv/bin/python -m compileall -q scripts tests
```

The regression tests cover config loading, Smart Merge small-file output shape, status-file sidecar locking, checkpoint resume safety, partial summarization blocking, embedding response validation, record validation, and LanceDB merge-upsert idempotency.

For a live end-to-end check, initialize a small batch, point outputs to a disposable directory, and run each phase:

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

Repeat the phase commands for another file id when validating multi-file behavior. A successful DB validation should show one row per unique `chunk_id`, not duplicate rows after reruns.

## Test Pipeline (方案 A — Single Run)

For quick parameter testing without LanceDB writes:

```bash
python3 scripts/chunk_test_runner.py --file-id 11
python3 scripts/chunk_test_runner.py --file-id 11 --chunk-params '{"window_size":3}'
python3 scripts/chunk_test_runner.py --file-id 11 --models '["NV-deepseek-v4-flash"]'
```

Output: `.txt` report + `.json` structured data per run, containing:
- File info, params, participants
- All segments sorted by time (excluded chunks flagged with reason +边界 percentile ranking & cosine)
- Summary per chunk with model name, tags, original text

## Test Suite (方案 B — Multi-config Comparison)

For comparing multiple parameter sets across files, create a suite JSON:

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

Run:

```bash
python3 scripts/chunk_test_suite.py --suite my_suite.json
```

Outputs individual `.txt` + `.json` per `(file × config)`, plus a comparison report with:
- Parameter cross-reference table
- Quantitative stats (kept/excluded counts per config)
- Full segment listings with boundary strength
- Difference analysis (which segments changed status between configs)

## Configuration

TranscriptFlow reads settings in this order:

1. Environment variables
2. `config.json` (project root)
3. `scripts/config.example.json`

Important environment variables:

```bash
OPENAI_BASE_URL=https://api.openai.com
OPENAI_API_KEY=replace-with-your-key
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_EXPECTED_DIM=3072
SRT_OUTPUT_DIR=./output
SRT_DB_PATH=./lancedb
SRT_MASTER_FILE=./examples/master_file_manifest.example.json
```

**Note**: Prefer setting `summarization.models` in `config.json` over `SUMMARIZATION_MODELS` env var, as shell variable parsing can strip JSON double quotes.

Do not commit `.env`, `config.json`, `test_params_suite.json`, generated output, or LanceDB data.

## SRT Quality Check

Multi-model-group LLM evaluation to find transcription errors or illogical segments. Supports parallel model groups, consensus-based severity aggregation, and per-model timing statistics.

```bash
python3 scripts/srt_quality_check.py --file-id 3
python3 scripts/srt_quality_check.py --file-id 0-2          # batch range
python3 scripts/srt_quality_check.py --file-id 3 --interactive  # human-in-the-loop
python3 scripts/srt_quality_check.py --input sample.srt
```

### Sliding Window

Windows of 10 entries with 5-entry overlap (configurable via `srt_quality.window_size` and `srt_quality.stride` in `config.json`, or `--window-size` / `--stride` CLI flags). Boundary rows (first/last `stride` entries) receive supplementary wider windows to ensure equal double-review coverage.

### Model Groups

Multiple model groups can run simultaneously with independent concurrency settings:

```json
"srt_quality": {
  "concurrency": [5, 7, 7],
  "model_groups": [
    ["oci-openai.gpt-oss-120b", "NV-gpt-oss-120b"],
    ["oci-meta.llama-3.3-70b-instruct"],
    ["oci-cohere.command-a-03-2025"]
  ]
}
```

- Each group lists models called **in parallel** per window (load distribution).
- Groups execute **concurrently** via `ThreadPoolExecutor`.
- Per-group concurrency caps total concurrent LLM calls within that group.
- Fallback: single `models` or `summarization.models` if `model_groups` is absent.

### Consensus Severity

Flagged lines from all groups are merged into a single report with consensus-based severity:

| Consensus Ratio | Tag | Meaning |
|---|---|---|
| <20% | ✅ Clean | Most inspectors agree it's fine |
| 20–60% | 🟡 Questionable | Mixed signals |
| ≥60% | 🔴 Problem | Strong multi-model agreement |

Consensus thresholds configurable via `srt_quality.consensus_thresholds` (default `[0.2, 0.6]`).

### Output Structure

```
{paths.output_dir}/srt_quality_report/
├── {id}_srt_quality_report_merged_{timestamp}.txt    ← consensus report with stats
└── group_raw/
    ├── {id}_srt_quality_report_group0_{timestamp}.txt
    ├── {id}_srt_quality_report_group1_{timestamp}.txt
    └── {id}_srt_quality_report_group2_{timestamp}.txt
```

The merged report header includes per-model average response time, completed segments count, and per-group wall-clock timing.

### CLI Flags

| Flag | Description |
|---|---|
| `--file-id` | Single ID (`3`) or range (`0-2`) |
| `--input` | Direct `.srt` file (single-model only) |
| `--interactive` | Human-in-the-loop correction mode |
| `--window-size` | Override `srt_quality.window_size` |
| `--stride` | Override `srt_quality.stride` |
| `--concurrency` | Override concurrency (applies to all groups) |

## B+ Chunk Quality Evaluation

Evaluate chunk boundary and noise handling quality from test suite JSON outputs:

```bash
python3 scripts/evaluate_chunks.py --reports /path/to/output/*.json
python3 scripts/evaluate_chunks.py --reports /path/to/dir --sample 10
```

Scores (total 100): boundary reasonableness (coherence 25 + breakpoint correctness 25) + noise handling 50. Outputs cross-config comparison with best-param recommendation.

Evaluation model config: `evaluation.chunk_models` in `config.json` (fallback: `summarization.models`).

## Summary Fidelity Evaluation

Assess whether LLM summaries faithfully reflect chunk content, independent of chunk quality:

```bash
python3 scripts/evaluate_summary_fidelity.py --reports /path/to/output/*.json
python3 scripts/evaluate_summary_fidelity.py --reports /path/to/dir --sample 5
```

Scores (0-25 each): factual accuracy, completeness, neutrality, overall quality. Detects hallucinated content and missing key points per model.

Evaluation model config: `evaluation.fidelity_models` in `config.json` (fallback: `summarization.models`).

## Manifest Generator

Scan the data directory and auto-generate `master_file_manifest.json`:

```bash
python3 scripts/generate_manifest.py                    # uses config.json paths
python3 scripts/generate_manifest.py --dry-run           # preview only
python3 scripts/generate_manifest.py --data-dir /path --output /path/manifest.json
```

## Unified LLM Client

All LLM API calls are consolidated through `scripts/llm_client.py`:

```python
from llm_client import call_llm, get_models

# Simple call
result = call_llm(prompt="...", model="gpt-4.1-mini",
                  system_prompt="You are a helpful assistant.")

# With model fallback list
result = call_llm(prompt="...", models=["model-a", "model-b"])
```

Features: unified retry (exponential backoff), model round-robin on retry, JSON response parsing, shared timeout and API key configuration.

## State Machine

```text
undone
  -> chunking -> queueing_1
  -> summarizing -> queueing_2
  -> embedding -> queueing_3
  -> db_inserting -> done

failed -> undone
failed_permanent
```

## Roadmap

- [ ] Provider-specific examples for LiteLLM, OpenRouter, vLLM
- [ ] Integration tests with mocked chat and embedding endpoints
- [ ] CLI ergonomics around batch initialization and phase selection

## License

MIT
