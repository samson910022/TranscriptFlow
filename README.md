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
- **11-state workflow**: explicit file states from `undone` through `done`, including retryable and permanent failure states.
- **Watchdog automation**: scans batch status files, advances eligible work, resets timed-out jobs, and manages phase concurrency.
- **Chunk-level retry**: failed chunks can be retried independently across models.
- **Model diagnostics**: records success/failure counts, elapsed time, throughput, and common error patterns.
- **Batch audit tooling**: checks structure, timeliness, data integrity, error visibility, model performance, and cross-file consistency.
- **OpenAI-compatible API support**: works with OpenAI, LiteLLM Proxy, OpenRouter, vLLM, Ollama-compatible servers, or any service exposing compatible `/v1/chat/completions` and `/v1/embeddings` endpoints.

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

## Quick Start

```bash
git clone https://github.com/samson1357924/TranscriptFlow.git
cd TranscriptFlow

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
cp scripts/config.example.json scripts/config.json
```

Edit `.env` and `scripts/config.json` for your API endpoint, model names, output directory, and LanceDB path.

```bash
export $(grep -v '^#' .env | xargs)

python3 scripts/state_manager.py init_batch 0 0
python3 scripts/auto_watchdog.py
```

## Configuration

TranscriptFlow reads settings in this order:

1. Environment variables
2. `scripts/config.json`
3. `scripts/config.example.json`

Important environment variables:

```bash
OPENAI_BASE_URL=https://api.openai.com
OPENAI_API_KEY=replace-with-your-key
SUMMARIZATION_MODELS='["gpt-4.1-mini"]'
EMBEDDING_MODEL=text-embedding-3-large
EMBEDDING_EXPECTED_DIM=3072
SRT_OUTPUT_DIR=./output
SRT_DB_PATH=./lancedb
SRT_MASTER_FILE=./examples/master_file_manifest.example.json
```

LiteLLM remains supported because it exposes the same OpenAI-compatible interface:

```bash
OPENAI_BASE_URL=http://localhost:4000
OPENAI_API_KEY=your-litellm-key
```

Legacy `LITELLM_PROXY_URL` and `LITELLM_PROXY_KEY` are still accepted for existing local setups, but new deployments should prefer `OPENAI_BASE_URL` and `OPENAI_API_KEY`.

Do not commit `.env`, `scripts/config.json`, generated output, or LanceDB data.

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

The watchdog advances queued work, resets stale active jobs, and prevents terminal states from being retried accidentally.

## Example Manifest

The batch initializer expects a manifest JSON file that maps file IDs to SRT files. See [examples/master_file_manifest.example.json](examples/master_file_manifest.example.json).

## Open Source Status

This repository was prepared from a personal AI-assisted engineering project. The architecture, reliability model, workflow design, and final review are human-owned; AI agents were used as implementation accelerators for scaffolding, refactoring, debugging, and documentation.

## Roadmap

- Extract the OpenAI-compatible request layer into a dedicated client module.
- Add provider-specific examples for LiteLLM, OpenRouter, vLLM, and local Ollama-compatible servers.
- Add integration tests with mocked chat and embedding endpoints.
- Add CLI ergonomics around batch initialization and phase selection.

## License

MIT
