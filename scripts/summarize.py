"""
summarize.py - 串起 SRT → Chunk → Summarize → Participants → Vector → Finalize
"""
import os
import sys
import json
import subprocess
import requests
import time
from datetime import datetime
import numpy as np
from typing import List, Dict

from parse_srt import parse_srt, SubtitleEntry
from semantic_chunk import semantic_chunk
from logger_config import get_logger
from config_loader import get_env_or_config, get_api_config
from llm_client import extract_participants, get_embedding_client
from state_manager import update_state, load_status, set_status_file, _locked_read_write

logger = get_logger('summarize')

PROJECT_ROOT = os.getenv('SRT_PROJECT_ROOT', os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
PYTHON_EXE = 'python3'

# Lazy config initialization (avoid module-level side effects)
_LAZY_CONFIG = None

def _get_lazy_config():
    global _LAZY_CONFIG
    if _LAZY_CONFIG is None:
        _api_cfg = get_api_config()
        api_key = _api_cfg['api_key']
        if not api_key:
            raise RuntimeError('API key 未設定 - 請在 config.json api.api_key 或環境變數 OPENAI_API_KEY 中設定')
        _LAZY_CONFIG = {
            "API_BASE_URL": _api_cfg['base_url'],
            "EMBEDDING_API_BASE": _api_cfg['embedding_url'],
            "API_KEY": api_key,
            "CHAT_ENDPOINT": _api_cfg['chat_completions_path'],
            "EMBEDDINGS_ENDPOINT": _api_cfg['embeddings_path'],
            "MODELS_ENDPOINT": _api_cfg['models_path'],
            "MAX_RETRIES": get_env_or_config('MAX_RETRIES', 'summarization.max_retries', 3),
        }
    return _LAZY_CONFIG

# Module-level aliases for backward compat (lazy-populated by _init_lazy_globals)
API_BASE_URL = None
EMBEDDING_API_BASE = None
API_KEY = None
CHAT_ENDPOINT = None
EMBEDDINGS_ENDPOINT = None
MODELS_ENDPOINT = None
MAX_RETRIES = None

def _init_lazy_globals():
    global API_BASE_URL, EMBEDDING_API_BASE, API_KEY, CHAT_ENDPOINT
    global EMBEDDINGS_ENDPOINT, MODELS_ENDPOINT, MAX_RETRIES
    if API_BASE_URL is not None:
        return
    cfg = _get_lazy_config()
    API_BASE_URL = cfg["API_BASE_URL"]
    EMBEDDING_API_BASE = cfg["EMBEDDING_API_BASE"]
    API_KEY = cfg["API_KEY"]
    CHAT_ENDPOINT = cfg["CHAT_ENDPOINT"]
    EMBEDDINGS_ENDPOINT = cfg["EMBEDDINGS_ENDPOINT"]
    MODELS_ENDPOINT = cfg["MODELS_ENDPOINT"]
    MAX_RETRIES = cfg["MAX_RETRIES"]

# ── Pre-flight health checks ───────────────────────────────────────────────────
def _check_health() -> bool:
    """Ping the OpenAI-compatible API before starting the pipeline."""
    all_ok = True

    # Check API via models endpoint
    try:
        headers = {"Authorization": f"Bearer {API_KEY}"}
        models_url = API_BASE_URL.rstrip('/') + MODELS_ENDPOINT
        resp = requests.get(models_url, headers=headers, timeout=10)
        if resp.status_code == 200:
            logger.info(f"[Health] OpenAI-compatible API OK (models endpoint)")
        else:
            logger.error(f"[Health] API returned {resp.status_code}: {resp.text[:200]}")
            all_ok = False
    except Exception as e:
        logger.error(f"[Health] API UNREACHABLE: {e}")
        all_ok = False

    # Check embedding endpoint via BatchEmbeddingClient (goes through circuit breaker)
    client = get_embedding_client()
    try:
        emb_result = client.generate_batch_embeddings(["health_check"])
        if emb_result.success and emb_result.embeddings and len(emb_result.embeddings) == 1:
            emb_dim = len(emb_result.embeddings[0])
            expected_dim = get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072)
            logger.info(f"[Health] Embedding server OK: model={client.model}, dim={emb_dim}")
            if emb_dim != expected_dim:
                logger.error(f"[Health] ❌ Embedding dim {emb_dim} != {expected_dim} - check embedding.expected_dim")
                all_ok = False
        else:
            logger.error(f"[Health] Embedding server FAILED: {emb_result.error or 'unknown error'}")
            all_ok = False
    except Exception as e:
        logger.error(f"[Health] Embedding server UNREACHABLE: {e}")
        all_ok = False

    return all_ok

# ── Step 4: Participants ──────────────────────────────────────────────────────
def generate_embedding_local(text: str, expected_dim: int = None):
    """Generate a single embedding vector using get_embedding_client()."""
    client = get_embedding_client()
    expected_dim = expected_dim or get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072)
    try:
        embeddings = client.generate_batch_embeddings([text])
        if embeddings.success and embeddings.embeddings and len(embeddings.embeddings) == 1:
            emb = embeddings.embeddings[0]
            if len(emb) != expected_dim:
                logger.error(f"Embedding dimension mismatch: got {len(emb)}, expected {expected_dim}.")
                return None
            return emb
        else:
            logger.error(f"Batch embedding failed for text snippet '{text[:30]}...': {embeddings.error or 'unknown error'}")
            return None
    except Exception as e:
        logger.error(f"Embedding request failed for text snippet '{text[:30]}...': {e}")
        return None

def _validate_env():
    """驗證必須的配置是否存在。"""
    from config_loader import check_required_env_vars
    ok, missing = check_required_env_vars()
    if not ok:
        raise RuntimeError(f"缺少必要配置：{', '.join(missing)}")

def phase_chunking(file_id, batch_file):
    _init_lazy_globals()
    _validate_env()
    progress_file = os.path.join(
        get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output'),
        '.progress', f'embedding_progress_{file_id}.json'
    )
    if os.path.exists(progress_file):
        os.remove(progress_file)
    if batch_file:
        set_status_file(batch_file)
    if not _check_health():
        raise RuntimeError("Pre-flight health check FAILED")

    master_path = os.getenv('SRT_MASTER_FILE', os.path.join(PROJECT_ROOT, 'master_file_manifest.json'))
    with open(master_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    entry = next((e for e in manifest.get('files', []) if e['id'] == file_id), None)
    if not entry:
        raise RuntimeError(f"File ID {file_id} not found in master manifest")
    srt_path = entry['path_srt']
    title = entry.get('title', entry.get('filename_srt', f'file_{file_id}'))

    if not os.path.exists(srt_path):
        raise RuntimeError(f"SRT file not found: {srt_path}")

    subtitles = parse_srt(srt_path)
    threshold_env = os.getenv('THRESHOLD', 'auto')
    try:
        threshold = float(threshold_env)
    except ValueError:
        threshold = threshold_env
    chunks = semantic_chunk(subtitles, file_id, threshold=threshold)

    output_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
    os.makedirs(output_dir, exist_ok=True)
    chunk_file = os.path.join(output_dir, f"{file_id}_chunks_input.json")
    with open(chunk_file, 'w', encoding='utf-8') as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    participants = extract_participants(chunks)
    meta_file = os.path.join(output_dir, f"{file_id}_meta.json")
    with open(meta_file, 'w', encoding='utf-8') as f:
        json.dump({"title": title, "participants": participants}, f, ensure_ascii=False)

    if batch_file:
        set_status_file(batch_file)
        def _write_chunk_segments(data):
            for item in data:
                if item.get("file_id") == file_id:
                    segments = []
                    for ch in chunks:
                        segments.append({
                            "chunk_id": ch.get("chunk_id", ""),
                            "status": "pending",
                            "text_preview": ch.get("text_content", "")[:100],
                        })
                    item["chunks"] = segments
                    item["total_chunks"] = len(segments)
                    return data
            return None
        _locked_read_write(_write_chunk_segments)

    logger.info(f"[{file_id}] Phase 1 (chunking) complete: {len(chunks)} chunks")


def phase_summarizing(file_id, batch_file):
    _init_lazy_globals()
    output_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
    chunk_file = os.path.join(output_dir, f"{file_id}_chunks_input.json")
    summarized_fn = os.path.join(output_dir, f"{file_id}_chunks_output.json")

    if not os.path.exists(chunk_file):
        raise RuntimeError(f"Chunk file not found: {chunk_file}")
    if os.path.exists(summarized_fn):
        logger.info(f"[{file_id}] Reusing existing summary output for checkpoint resume: {summarized_fn}")

    pipeline_script = os.path.join(os.path.dirname(__file__), 'summarize_pipeline.py')
    cmd = [PYTHON_EXE, pipeline_script, "--input", chunk_file, "--output", summarized_fn]
    if batch_file:
        cmd.extend(["--batch", batch_file])
    logger.info(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(f"[{file_id}] summarize_pipeline exit code {result.returncode}")

    if not os.path.exists(summarized_fn):
        raise RuntimeError(f"Summarize produced no output: {summarized_fn}")

    with open(summarized_fn, 'r', encoding='utf-8') as f:
        summarized_data = json.load(f)
    _validate_summarized_results(file_id, summarized_data)
    done_count = sum(1 for ch in summarized_data if ch.get('status') == 'done')

    logger.info(f"[{file_id}] Phase 2 (summarizing) complete: {done_count}/{len(summarized_data)} chunks done")


def _validate_summarized_results(file_id, summarized_data):
    done_count = sum(1 for ch in summarized_data if ch.get('status') == 'done')
    failed_count = sum(1 for ch in summarized_data if ch.get('status') in ('failed', 'failed_permanent'))
    if done_count == 0:
        raise RuntimeError(f"All {len(summarized_data)} chunks failed summarization")
    if failed_count:
        raise RuntimeError(f"{failed_count}/{len(summarized_data)} chunks failed summarization; blocking next phase")


def phase_embedding(file_id, batch_file):
    _init_lazy_globals()
    output_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
    summarized_fn = os.path.join(output_dir, f"{file_id}_chunks_output.json")
    meta_file = os.path.join(output_dir, f"{file_id}_meta.json")

    if not os.path.exists(summarized_fn):
        raise RuntimeError(f"Summarized file not found: {summarized_fn}")

    with open(summarized_fn, 'r', encoding='utf-8') as f:
        summarized_data = json.load(f)

    participants = []
    title = f"id_{file_id}"
    if os.path.exists(meta_file):
        with open(meta_file, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        participants = meta.get("participants", [])
        title = meta.get("title", title)

    records = []
    total = len([ch for ch in summarized_data if ch.get('status') == 'done' and ch.get('summary', '').strip()])
    skipped_done = 0
    for idx, ch in enumerate(summarized_data):
        if ch.get('status') != 'done':
            continue
        summary = ch['summary']
        if not summary or not summary.strip():
            skipped_done += 1
            continue
        vector = generate_embedding_local(summary)
        if vector is None:
            skipped_done += 1
            continue
        vector = vector.tolist()
        records.append({
            "chunk_id": ch.get("chunk_id", f"{file_id}_{idx}"),
            "file_id": file_id,
            "file_name": title,
            "start_time": ch['start_time'],
            "end_time": ch['end_time'],
            "summary": summary,
            "text_content": ch['text_content'],
            "tags": list(ch.get('tags', [])),
            "participants": list(participants),
            "vector": vector,
            "boundary_type": ch.get('boundary_type', 'unknown'),
        })
        if batch_file:
            set_status_file(batch_file)
            def _record_embed_progress(data):
                for item in data:
                    if item.get("file_id") == file_id:
                        diag = item.get("diagnostics") or {}
                        diag["phase3_progress"] = f"{len(records)}/{total}"
                        diag["phase3_embedded"] = len(records)
                        item["diagnostics"] = diag
                        return data
                return None
            _locked_read_write(_record_embed_progress)

    records_file = os.path.join(output_dir, f"{file_id}_records.json")
    with open(records_file, 'w', encoding='utf-8') as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    if len(records) != total:
        raise RuntimeError(f"[{file_id}] Embedding incomplete: {len(records)}/{total} records generated ({skipped_done} skipped)")

    logger.info(f"[{file_id}] Phase 3 (embedding) complete: {len(records)} records")


def phase_db_inserting(file_id, batch_file):
    _init_lazy_globals()
    output_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')

    records_file = os.path.join(output_dir, f"{file_id}_records.json")

    if not os.path.exists(records_file):
        raise RuntimeError(f"Records file not found: {records_file}")

    with open(records_file, 'r', encoding='utf-8') as f:
        records = json.load(f)

    if not records:
        raise RuntimeError(f"[{file_id}] No records to write — all chunks failed or empty")

    from finalize import write_to_db
    success, msg = write_to_db(records, allow_delete=True)
    if not success:
        raise RuntimeError(f"LanceDB write failed: {msg}")
    logger.info(f"[{file_id}] LanceDB write successful")

    backup_dir = os.path.join(output_dir, 'lance_backup')
    os.makedirs(backup_dir, exist_ok=True)
    file_name = records[0].get('file_name', f'id_{file_id}')
    backup_path = os.path.join(
        backup_dir,
        f'{file_id}_{os.path.splitext(os.path.basename(file_name))[0]}.json'
    )
    with open(backup_path, 'w', encoding='utf-8') as bf:
        json.dump(
            [{k: v for k, v in rec.items() if k != 'vector'} for rec in records],
            bf, ensure_ascii=False, indent=2
        )
    logger.info(f"[{file_id}] JSON backup: {backup_path}")

    if batch_file:
        from collections import Counter, defaultdict
        set_status_file(batch_file)

        chunks_data = []
        started_at = None
        elapsed = None
        model_stats = defaultdict(lambda: {"success": 0, "fail": 0, "chars": 0, "errors": defaultdict(int), "total_time": 0.0})
        models_out = {}
        top_failed = []

        def _write_diagnostics(data):
            nonlocal chunks_data, started_at, elapsed, model_stats, models_out, top_failed
            for item in data:
                if item.get("file_id") == file_id:
                    chunks_data = item.get("chunks", [])
                    started_at = (item.get("diagnostics") or {}).get("started_at")
                    if started_at:
                        try:
                            elapsed = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
                        except Exception:
                            logger.exception("解析 started_at 時間戳失敗")

                    model_stats.clear()
                    for ch in chunks_data:
                        m = ch.get("model_used", "?")
                        if ch.get("status") == "done":
                            model_stats[m]["success"] += 1
                        elif ch.get("status") in ("failed", "failed_permanent"):
                            model_stats[m]["fail"] += 1
                        model_stats[m]["chars"] += ch.get("char_count", 0)
                        model_stats[m]["total_time"] += ch.get("elapsed_sec", 0) or 0
                        for e in (ch.get("errors") or []):
                            if isinstance(e, dict):
                                err_model = e.get("model", m)
                                model_stats[err_model]["errors"][e.get("message", str(e))[:120]] += 1

                    models_out.clear()
                    for m, s in list(model_stats.items()):
                        total = s["success"] + s["fail"]
                        rate = round(s["success"] / total * 100, 1) if total else 0
                        cps_elapsed = round(s["chars"] / elapsed, 1) if elapsed and elapsed > 0 else None
                        cps_actual = round(s["chars"] / s["total_time"], 1) if s["total_time"] > 0 else None
                        all_fail = s["fail"] + sum(s["errors"].values())
                        models_out[m] = {
                            "success": s["success"],
                            "fail": all_fail,
                            "success_rate": round(s["success"] / (s["success"] + all_fail) * 100, 1) if (s["success"] + all_fail) else 0,
                            "chars_per_sec_overall": cps_elapsed,
                            "chars_per_sec_actual": cps_actual,
                        }
                        if s["errors"]:
                            models_out[m]["top_errors"] = [
                                {"msg": msg, "count": cnt}
                                for msg, cnt in sorted(s["errors"].items(), key=lambda x: -x[1])[:10]
                            ]

                    top_failed = sorted(
                        [{"model": m, "fail_count": models_out[m]["fail"],
                          "top_errors": models_out[m].get("top_errors", [])}
                         for m, s in model_stats.items() if models_out[m]["fail"] > 0],
                        key=lambda x: -x["fail_count"]
                    )

                    total_chars = sum(len(r.get("summary", "")) for r in records)
                    item["diagnostics"] = {
                        "summary": {
                            "started_at": started_at,
                            "elapsed_sec": round(elapsed) if elapsed else None,
                            "records": len(records),
                            "total_chars": total_chars,
                            "chars_per_sec": round(total_chars / elapsed, 1) if elapsed and records else None,
                        },
                        "models": models_out,
                        "top_failed_models": top_failed,
                    }
                    return data
            return None
        _locked_read_write(_write_diagnostics)

        # Write separate model diagnostics file with real per-model stats
        try:
            model_diag_path = os.path.join(output_dir, f"model_diagnostics_{file_id}.json")
            model_diag = {}
            for m in model_stats:
                model_diag[m] = {
                    "success": models_out[m]["success"],
                    "fail": models_out[m]["fail"],
                    "success_rate": models_out[m]["success_rate"],
                    "chars_per_sec_actual": models_out[m].get("chars_per_sec_actual"),
                    "top_errors": models_out[m].get("top_errors", []),
                }

            with open(model_diag_path, 'w', encoding='utf-8') as df:
                json.dump(model_diag, df, ensure_ascii=False, indent=2)
            logger.info(f"Model diagnostics saved: {model_diag_path}")
        except Exception as e:
            logger.warning(f"Failed to write model diagnostics: {e}")

    for fn in [
        f"{file_id}_chunks_input.json",
        f"{file_id}_chunks_output.json",
        f"{file_id}_meta.json",
        f"{file_id}_records.json",
    ]:
        path = os.path.join(output_dir, fn)
        if os.path.exists(path):
            os.remove(path)

    logger.info(f"[{file_id}] Phase 4 (db_inserting) complete: {len(records)} records")

if __name__ == '__main__':
    import argparse
    import traceback
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', type=int, required=True)
    parser.add_argument('--batch', type=str, default='')
    parser.add_argument('--phase', type=str, required=True, choices=['chunking', 'summarizing', 'embedding', 'db_inserting'])
    args = parser.parse_args()

    file_id = args.id
    try:
        if args.phase == 'chunking':
            phase_chunking(file_id, args.batch)
        elif args.phase == 'summarizing':
            phase_summarizing(file_id, args.batch)
        elif args.phase == 'embedding':
            phase_embedding(file_id, args.batch)
        elif args.phase == 'db_inserting':
            phase_db_inserting(file_id, args.batch)
        logger.info(f"✅ [{file_id}] Phase {args.phase} completed successfully.")
    except Exception:
        traceback.print_exc()
        sys.exit(1)
