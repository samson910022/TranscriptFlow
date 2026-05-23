"""
summarize_pipeline.py
直接對 OpenAI-compatible API 發送 requests，同時產生每個 chunk 的摘要與標籤。
支援斷點續傳（漸進式 Output 寫入）。
"""
import json
import os
import argparse
import re
import requests
import fcntl
import tempfile
import shutil
from datetime import datetime
import time
import threading
import glob
import hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from logger_config import get_logger
from config_loader import get_api_config, get_env_or_config, get_nested_config
from state_manager import _locked_read_write, set_status_file
from llm_client import call_llm as _call_llm

logger = get_logger('summarize_pipeline')

# Graceful module-level initialization — import never crashes
try:
    _api_cfg = get_api_config()
    API_BASE_URL = _api_cfg['base_url']
    CHAT_ENDPOINT = _api_cfg['chat_completions_path']
    API_KEY = _api_cfg['api_key']
except Exception:
    _api_cfg = None
    API_BASE_URL = None
    CHAT_ENDPOINT = None
    API_KEY = None

if API_KEY:
    MODELS = get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])
    MAX_RETRIES = get_env_or_config('MAX_RETRIES', 'summarization.max_retries', 3)
    CONCURRENCY = int(get_env_or_config('CONCURRENCY', 'summarization.concurrency', 5))
    TIMEOUT_SEC = int(get_env_or_config('TIMEOUT_SEC', 'summarization.timeout_sec', 120))
else:
    MODELS = ["gpt-4.1-mini"]
    MAX_RETRIES = 3
    CONCURRENCY = 5
    TIMEOUT_SEC = 120

# 跨 worker 共享模型計數器（執行緒安全）
_model_counter = 0
_model_counter_lock = threading.Lock()

def _get_next_model():
    global _model_counter
    with _model_counter_lock:
        model = MODELS[_model_counter % len(MODELS)]
        _model_counter += 1
    return model

SYSTEM_PROMPT = """你是一個摘要助理。請根據以下文字區塊，直接輸出摘要內容，不要使用「這段文字」、「本文」、「該段落」等引導詞。

1. 摘要（150-300 字，繁體中文，客觀濃縮核心論點）
2. 標籤（3-8 個，每個 1-3 詞，用於語意檢索）

只輸出 JSON，不要 markdown 格式、不要額外說明：
{"summary": "...", "tags": ["...", "...", ...]}"""


class CheckpointManager:
    """
    處理漸進式寫入與原子性操作的 Checkpoint 管理器。
    """
    def __init__(self, output_file: str, checkpoint_dir: str = None):
        self.output_file = output_file
        self.checkpoint_dir = checkpoint_dir or os.path.join(
            os.path.dirname(output_file) or '.',
            '.checkpoints'
        )
        self.temp_file = None
        self.lock_file = output_file + '.lock'
        self.completed_chunks = {}
        self._ensure_checkpoint_dir()

    @staticmethod
    def chunk_fingerprint(chunk: dict) -> str:
        text = chunk.get("text_content", "")
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    
    def _ensure_checkpoint_dir(self):
        if not os.path.exists(self.checkpoint_dir):
            os.makedirs(self.checkpoint_dir, exist_ok=True)
    
    def load_existing_results(self) -> dict:
        loaded_results = []
        if os.path.exists(self.output_file):
            try:
                with open(self.output_file, 'r', encoding='utf-8') as f:
                    loaded_results.extend(json.load(f))
            except json.JSONDecodeError:
                logger.warning("現有輸出檔案 JSON 格式錯誤，將重新處理")
            except Exception as e:
                logger.warning(f"載入現有結果失敗: {e}，將重新處理")

        checkpoint_pattern = os.path.join(os.path.dirname(self.output_file) or '.', 'checkpoint_*.tmp')
        for checkpoint_path in sorted(glob.glob(checkpoint_pattern), key=os.path.getmtime, reverse=True):
            try:
                with open(checkpoint_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content:
                    continue
                if content.startswith('[') and not content.endswith(']'):
                    logger.warning(f"Corrupt checkpoint {checkpoint_path}: missing closing bracket, skipping")
                    continue
                checkpoint_results = json.loads(content)
                if isinstance(checkpoint_results, list):
                    loaded_results.extend(checkpoint_results)
                    logger.info(f"從暫存 checkpoint 載入 {len(checkpoint_results)} 個 chunks: {checkpoint_path}")
                    break
            except Exception as e:
                logger.debug(f"略過不可讀 checkpoint {checkpoint_path}: {e}")

        self.completed_chunks = {
            r.get('chunk_id', r.get('id')): r
            for r in loaded_results
            if r.get('chunk_id', r.get('id')) and r.get('status') == 'done'
        }
        if self.completed_chunks:
            logger.info(f"從既有結果載入 {len(self.completed_chunks)} 個已完成 chunks")
        return self.completed_chunks
    
    def get_pending_chunks(self, all_chunks: list) -> list:
        for chunk in all_chunks:
            chunk_id = chunk.get('chunk_id', chunk.get('id'))
            existing = self.completed_chunks.get(chunk_id)
            if existing and existing.get("source_text_hash") != self.chunk_fingerprint(chunk):
                logger.warning(f"Chunk {chunk_id} text changed; ignoring stale completed result")
                self.completed_chunks.pop(chunk_id, None)
        pending = [
            chunk for chunk in all_chunks 
            if chunk.get('chunk_id', chunk.get('id')) not in self.completed_chunks
        ]
        logger.info(f"待處理 chunks: {len(pending)}/{len(all_chunks)}")
        return pending
    
    def start_incremental_write(self):
        directory = os.path.dirname(self.output_file) or '.'
        fd, self.temp_file = tempfile.mkstemp(
            suffix='.tmp', 
            prefix='checkpoint_', 
            dir=directory
        )
        os.close(fd)
        logger.debug(f"建立暫時檔案: {self.temp_file}")
    
    def write_chunk_result(self, result: dict):
        chunk_id = result.get('chunk_id', result.get('id'))
        
        lock_acquired = False
        for attempt in range(3):
            try:
                with open(self.lock_file, 'w') as lock_f:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                    try:
                        with open(self.temp_file, 'a+', encoding='utf-8') as f:
                            file_size = os.path.getsize(self.temp_file)
                            if file_size == 0:
                                f.write('[')
                            else:
                                f.seek(0, 2)
                                f.write(',')
                            json.dump(result, f, ensure_ascii=False)
                            f.flush()
                            os.fsync(f.fileno())
                            self.completed_chunks[chunk_id] = result
                    finally:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                    break
            except BlockingIOError:
                wait_time = 2**attempt
                logger.debug(f"Lock busy for {chunk_id}, retrying in {wait_time}s... (attempt {attempt+1}/3)")
                time.sleep(wait_time)
        
        if not lock_acquired:
            logger.error(f"Failed to acquire lock for chunk {chunk_id} after 3 attempts.")
            return

        logger.debug(f"已寫入 chunk: {chunk_id}")
    
    def finalize(self, all_results: list):
        if self.temp_file and os.path.exists(self.temp_file):
            try:
                temp_results = []
                try:
                    with open(self.temp_file, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        if content.startswith('['):
                            content += ']'
                            temp_results = json.loads(content)
                        else:
                            temp_results = all_results
                except Exception:
                    logger.debug("無法讀取暫存結果，使用 all_results")
                    temp_results = all_results
                
                final_results = list(all_results)
                final_result_ids = {r.get('chunk_id', r.get('id')) for r in final_results}
                for result in temp_results:
                    chunk_id = result.get('chunk_id', result.get('id'))
                    if chunk_id and chunk_id not in final_result_ids:
                        final_results.append(result)
                        final_result_ids.add(chunk_id)
                for chunk_id, result in self.completed_chunks.items():
                    if chunk_id not in final_result_ids:
                        final_results.append(result)
                
                # M2: Lock 改用 LOCK_NB + 指數退避重試
                lock_acquired = False
                for attempt in range(3):
                    try:
                        with open(self.lock_file, 'w') as lock_f:
                            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            try:
                                with open(self.output_file, 'w', encoding='utf-8') as f:
                                    json.dump(final_results, f, ensure_ascii=False, indent=2)
                                logger.info(f"原子性寫入完成，共 {len(final_results)} 個結果")
                            finally:
                                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                            lock_acquired = True
                            break
                    except BlockingIOError:
                        wait_time = 2**attempt
                        time.sleep(wait_time)
                
                if not lock_acquired:
                    logger.error(f"Failed to acquire final lock for {self.output_file} after 3 attempts.")
                
            except Exception as e:
                logger.error(f"最終寫入失敗，使用備援寫入: {e}")
                with open(self.output_file, 'w', encoding='utf-8') as f:
                    json.dump(all_results, f, ensure_ascii=False, indent=2)
            finally:
                # M4: Lock cleanup 移入 finally
                if os.path.exists(self.lock_file):
                    try:
                        os.remove(self.lock_file)
                    except Exception:
                        pass
                if os.path.exists(self.temp_file):
                    try:
                        os.remove(self.temp_file)
                    except Exception:
                        pass
                self.temp_file = None
        else:
            with open(self.output_file, 'w', encoding='utf-8') as f:
                json.dump(all_results, f, ensure_ascii=False, indent=2)
        
        if os.path.exists(self.lock_file):
            try:
                os.remove(self.lock_file)
            except Exception:
                pass
    
    def __enter__(self):
        self.start_incremental_write()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.temp_file:
            try:
                if os.path.exists(self.temp_file):
                    os.remove(self.temp_file)
            except Exception:
                pass


def call_llm(text: str, model: str) -> dict:
    return _call_llm(prompt=text, model=model, system_prompt=SYSTEM_PROMPT)


def process_chunk(chunk: dict, stats: dict) -> dict:
    if API_KEY is None:
        raise RuntimeError('API key 未設定 - 請設定 OPENAI_API_KEY 環境變數或 config.json api.api_key')
    chunk_id = chunk.get("chunk_id", "?")
    errors = []
    start_time = time.time()
    for attempt in range(1, MAX_RETRIES + 1):
        model = _get_next_model()
        attempt_start = time.time()
        try:
            result = call_llm(chunk["text_content"], model)
            summary = result.get("summary", "")
            tags = result.get("tags", [])
            elapsed = time.time() - attempt_start
            elapsed_total = time.time() - start_time
            logger.info(f"[Success] {chunk_id} ({model}) | tags={len(tags)} | {elapsed:.1f}s")
            stats["success"] += 1
            if model not in stats["model_stats"]:
                stats["model_stats"][model] = 0
            stats["model_stats"][model] += 1
            return {**chunk, "summary": summary, "tags": tags, "status": "done",
                    "model_used": model, "retry_count": attempt - 1, "errors": errors,
                    "source_text_hash": CheckpointManager.chunk_fingerprint(chunk),
                    "elapsed_sec": round(elapsed, 2), "elapsed_total_sec": round(elapsed_total, 2),
                    "char_count": len(chunk.get("text_content", ""))}
        except Exception as exc:
            elapsed = time.time() - attempt_start
            errors.append({"model": model, "message": str(exc)})
            logger.warning(f"[Error] {chunk_id} failed with {model} (attempt {attempt}, {elapsed:.1f}s): {exc}")
            error_key = f"{type(exc).__name__}: {str(exc)[:80]}"
            stats["error_distribution"][error_key] = stats["error_distribution"].get(error_key, 0) + 1
            if attempt == MAX_RETRIES:
                stats["failed"] += 1
                elapsed_total = time.time() - start_time
                return {**chunk, "status": "failed", "errors": errors, "retry_count": MAX_RETRIES,
                        "source_text_hash": CheckpointManager.chunk_fingerprint(chunk),
                        "elapsed_sec": round(elapsed, 2), "elapsed_total_sec": round(elapsed_total, 2),
                        "char_count": len(chunk.get("text_content", ""))}
    elapsed_total = time.time() - start_time
    return {**chunk, "status": "failed", "errors": [{"model": "unknown", "message": "unexpected"}],
            "retry_count": MAX_RETRIES, "source_text_hash": CheckpointManager.chunk_fingerprint(chunk),
            "elapsed_sec": 0, "elapsed_total_sec": round(elapsed_total, 2),
            "char_count": len(chunk.get("text_content", ""))}


def save_detailed_stats(output_path: str, results: list, stats: dict):
    stats_path = output_path.replace('_chunks_output.json', '_chunks_detailed_stats.json')
    
    # M3: Migrate detailed stats to .progress/
    out_dir = os.path.dirname(stats_path) or '.'
    prog_dir = os.path.join(out_dir, '.progress')
    os.makedirs(prog_dir, exist_ok=True)
    stats_path = os.path.join(prog_dir, os.path.basename(stats_path))
    
    stats["timestamp"] = datetime.now().isoformat()
    stats["total_chunks"] = len(results)
    try:
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        logger.info(f"📊 詳細統計已儲存至：{stats_path}")
    except Exception as e:
        logger.error(f"儲存統計失敗: {e}")


class ZeroChunksError(Exception):
    pass

def _chunk_entry(ch):
    entry = {
        "chunk_id": ch.get("chunk_id"),
        "status": ch.get("status", "failed"),
        "model_used": ch.get("model_used", ""),
        "retry_count": ch.get("retry_count", 0),
        "errors": ch.get("errors", []),
        "char_count": ch.get("char_count", len(ch.get("text_content", ""))),
        "elapsed_sec": ch.get("elapsed_sec"),
    }
    if ch.get("status") == "done":
        s = ch.get("summary", "")
        entry["summary_preview"] = s[:100] if s else ""
    elif ch.get("errors"):
        ps = [f'[{e.get("model","?")}] {e.get("message","")}' for e in ch["errors"]]
        entry["summary_preview"] = " | ".join(ps)[:100]
    return entry

def _incremental_write_chunk(batch_file, file_id, ch):
    set_status_file(batch_file)
    entry = _chunk_entry(ch)
    def _upsert(data):
        for item in data:
            if item.get("file_id") == file_id:
                chunks = item.get("chunks", [])
                for i, c in enumerate(chunks):
                    if c.get("chunk_id") == entry.get("chunk_id"):
                        chunks[i] = entry
                        break
                else:
                    chunks.append(entry)
                item["chunks"] = chunks
                item["total_chunks"] = len(chunks)
                return data
        return None
    _locked_read_write(_upsert)


def main(input_file: str, output_file: str, batch_file: str = None):
    global _model_counter
    if API_KEY is None:
        raise RuntimeError('API key 未設定 - 請設定 OPENAI_API_KEY 環境變數或 config.json api.api_key')
    _model_counter = 0

    # [Hybrid] Step 1: 基礎設施檢查 (B1 方案)
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"輸入檔案不存在：{input_file}")
    
    try:
        with open(input_file, "r", encoding="utf-8") as f:
            chunks = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"輸入檔案 JSON 格式錯誤：{input_file} (Error: {e})")
    except Exception as e:
        raise RuntimeError(f"讀取輸入檔案失敗：{input_file} (Error: {e})")
    
    if not isinstance(chunks, list):
        raise ValueError(f"輸入檔案內容必須是 JSON 陣列，但得到 {type(chunks).__name__}: {input_file}")
    
    # [Hybrid] Step 2: 語意錯誤處理 (B2 方案)
    total_chunks = len(chunks)
    if total_chunks == 0:
        logger.error("❌ 錯誤：輸入檔案包含 0 個 chunks，無法進行處理")
        error_stats = {
            "timestamp": datetime.now().isoformat(),
            "total_chunks": 0,
            "success": 0,
            "failed": 0,
            "model_stats": {},
            "error_distribution": {"ZERO_CHUNKS_ERROR": 1},
            "fatal_error": {
                "type": "ZeroChunksError",
                "message": "輸入檔案不包含任何 chunk 數據，無法繼續處理",
                "input_file": input_file,
                "suggestion": "請檢查上游 chunking 流程，確認是否正確生成 chunk 數據"
            }
        }
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        stats_path = output_file.replace('_chunks_output.json', '_chunks_detailed_stats.json')
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(error_stats, f, ensure_ascii=False, indent=2)
        logger.info(f"錯誤統計已儲存至：{stats_path}")
        raise ZeroChunksError(f"輸入檔案 '{input_file}' 不包含任何 chunks，請檢查上游 chunking 流程")
    
    logger.info(f"Loaded {total_chunks} chunks, starting {CONCURRENCY} workers...")
    
    checkpoint_manager = CheckpointManager(output_file)
    checkpoint_manager.load_existing_results()
    pending_chunks = checkpoint_manager.get_pending_chunks(chunks)
    
    if not pending_chunks:
        logger.info("所有 chunks 已完成處理，無須重複執行")
        checkpoint_manager.finalize(list(checkpoint_manager.completed_chunks.values()))
        save_detailed_stats(output_file, list(checkpoint_manager.completed_chunks.values()), {
            "success": len(checkpoint_manager.completed_chunks),
            "failed": 0,
            "model_stats": {},
            "error_distribution": {},
        })
        return
    
    stats = {"success": 0, "failed": 0, "model_stats": {}, "error_distribution": {}}
    stats["success"] = len(checkpoint_manager.completed_chunks)
    results = list(checkpoint_manager.completed_chunks.values())

    _file_id = None
    if batch_file:
        try:
            _file_id = int(os.path.basename(input_file).split('_')[0])
        except Exception:
            logger.debug(f"無法從 {input_file} 解析 file_id")
            pass

    checkpoint_manager.start_incremental_write()
    try:
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futures = {ex.submit(process_chunk, ch, stats): ch for ch in pending_chunks}
            for fut in as_completed(futures):
                result = fut.result()
                results.append(result)
                if result.get("status") == "done":
                    checkpoint_manager.write_chunk_result(result)
                if _file_id is not None:
                    try:
                        _incremental_write_chunk(batch_file, _file_id, result)
                    except Exception:
                        logger.debug(f"增量寫入失敗: {_file_id} {result.get('chunk_id', '?')}")
                        pass
    finally:
        checkpoint_manager.finalize(results)
    
    save_detailed_stats(output_file, results, stats)
    done = sum(1 for r in results if r["status"] == "done")
    failed = sum(1 for r in results if r["status"] == "failed")
    logger.info(f"Pipeline finished. {done} done, {failed} failed → {output_file}")

    if batch_file:
        try:
            input_basename = os.path.basename(input_file)
            file_id = int(input_basename.split('_')[0])
            set_status_file(batch_file)
            def _write_chunks(data):
                for item in data:
                    if item.get("file_id") == file_id:
                        item["chunks"] = [_chunk_entry(ch) for ch in results]
                        item["total_chunks"] = len(results)
                        return data
                return None
            _locked_read_write(_write_chunks)
            logger.info(f"已回寫 {len(results)} 個 chunk 狀態到 batch_status (file_id: {file_id})")
        except Exception as e:
            logger.warning(f"回寫 chunk 狀態到 batch_status 失敗: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch", type=str, default=None)
    args = parser.parse_args()
    main(args.input, args.output, args.batch)
