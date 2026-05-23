#!/usr/bin/env python3
"""
auto_watchdog.py - Smart Merge 3.0 自動流水線監控
功能:
1. 自動監控 batch_status 檔案處理。
2. 每五分鐘彙報進度。
3. 重複問題偵測:累積 3 次異常(如 API 失敗),透過 logger 終端機推播警告,喚醒 Agent 排查。
4. 片段失敗重試:失敗片段重試若再次失敗,觸發嚴重警告,交由 Agent 判斷是否重新切檔。
"""

import os
import sys
import json
import time
import subprocess
import traceback
from datetime import datetime
import requests

from logger_config import get_logger
from state_manager import update_state, set_status_file, load_status, acquire_phase_slot, release_file_phase_slot
from config_loader import get_env_or_config, get_api_config

logger = get_logger('auto_watchdog')

# 從 config.json 讀取路徑
OUTPUT_DIR = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
PROGRESS_DIR = os.path.join(OUTPUT_DIR, '.progress')
os.makedirs(PROGRESS_DIR, exist_ok=True)
MAX_WORKING_TIME_SEC = get_env_or_config('WATCHDOG_MAX_WORKING_TIME', 'watchdog.max_working_time_sec', 600)
CHECK_INTERVAL_SEC = 30
REPORT_INTERVAL_SEC = int(os.getenv('REPORT_INTERVAL_SEC', get_env_or_config('REPORT_INTERVAL_SEC', 'monitoring.heartbeat_interval_sec', 300)))
# 並行度上限 (H4)
MAX_CONCURRENCY = 3
# 追蹤目前執行中的 processes (list of {"proc": Popen, "file_id": int, "phase": str})
active_processes = []

# 全域失敗計數器(不限時間,只要累積 3 次就警報)
RECENT_FAILURES = []

def _normalize_batch_data(data):
    if isinstance(data, dict) and 'files' in data:
        return data['files']
    if isinstance(data, list):
        return data
    return []

def _get_next_phase(status):
    mapping = {
        'undone': ('chunking', 'phase1_chunking'),
        'queueing_1': ('summarizing', 'phase2_summarizing'),
        'queueing_2': ('embedding', 'phase3_embedding'),
        'queueing_3': ('db_inserting', 'phase4_db_insert'),
    }
    return mapping.get(status, (None, None))

def _check_environment():
    """從 config.json 讀取必要配置（不再強制要求環境變數）"""
    _api_cfg = get_api_config()
    if not _api_cfg['api_key']:
        logger.error("❗❗ config.json api.api_key 未設定")
        sys.exit(1)
    if not _api_cfg['base_url']:
        logger.error("❗❗ config.json api.base_url 未設定")
        sys.exit(1)
    logger.info(f"✅ API 配置就緒 (base_url: {_api_cfg['base_url']})")

def _reset_phase_slots():
    from state_manager import _get_phase_slots_file, _get_phase_slots_lock_file
    import json, fcntl, tempfile, os
    data_path = _get_phase_slots_file()
    lock_path = _get_phase_slots_lock_file()
    base = {"phase1_chunking": {"slots": [], "queue": []}, "phase2_summarizing": {"slots": [], "queue": []}, "phase3_embedding": {"slots": [], "queue": []}, "phase4_db_insert": {"slots": [], "queue": []}}
    try:
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                dir_path = os.path.dirname(data_path) or '.'
                os.makedirs(dir_path, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False,
                                                 prefix='.phase_slots_tmp_', suffix='.json') as tf:
                    tmp_path = tf.name
                    json.dump(base, tf)
                os.replace(tmp_path, data_path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        logger.info("✅ 相位槽已重置（啟動清理）")
    except Exception as e:
        logger.warning(f"⚠️ 無法重置相位槽: {e}")

def _health_check():
    _api_cfg = get_api_config()
    base_url = _api_cfg['base_url'].rstrip('/')
    if not base_url:
        return False
    try:
        headers = {"Authorization": f"Bearer {_api_cfg['api_key']}"}
        r = requests.get(f"{base_url}/health", headers=headers, timeout=5)
        if r.status_code == 200:
            logger.info("✅ 伺服器健康檢查通過")
            return True
        logger.warning(f"健康檢查返回非預期狀態碼: {r.status_code}")
        return False
    except Exception as e:
        logger.warning(f"伺服器健康檢查: {e}")
    return False

def _register_failure(error_msg: str):
    global RECENT_FAILURES
    RECENT_FAILURES.append(error_msg)
    logger.error(f"❗ 捕獲失敗 (累計 {len(RECENT_FAILURES)} 次): {error_msg}")

    if len(RECENT_FAILURES) >= 3:
        logger.warning(
            "🚨 [重複問題偵測] 累積達 3 次失敗!\n"
            "請 Agent 立即介入:\n"
            "1. 測試伺服器與模型通訊。\n"
            "2. 若伺服器正常,檢查片段是否有問題。\n"
            "3. 無異常則將該片段 retry + 1 重新排隊。"
        )
        RECENT_FAILURES.clear()

# 增量對比暫存
LAST_REPORT_STATS = {
    "done_files": 0,
    "llm_success": 0,
    "timestamp": time.time()
}

def _report_progress():
    global LAST_REPORT_STATS
    now_ts = time.time()
    batch_files = [os.path.join(OUTPUT_DIR, f) for f in os.listdir(OUTPUT_DIR) if f.startswith('batch_status_') and f.endswith('.json')]

    total_files = 0
    done_files = 0
    working_files = 0
    failed_files = 0
    failed_permanent_files = []

    overall_stats = {
        "llm_success": 0,
        "llm_failed": 0,
        "model_performance": {},
        "errors": {},
        "top_failed_models": []
    }

    for bf in batch_files:
        try:
            with open(bf, 'r', encoding='utf-8') as f:
                data = _normalize_batch_data(json.load(f))
                total_files += len(data)
                for item in data:
                    status = item.get('status')
                    if status == 'done': done_files += 1
                    elif status and status.startswith('failed'): failed_files += 1

                    if status == 'failed_permanent':
                        failed_permanent_files.append(item.get('file_id'))

                    chunks = item.get('chunks', [])
                    for chunk in chunks:
                        if chunk.get('status') == 'done':
                            overall_stats["llm_success"] += 1
                            model = chunk.get('model_used', 'unknown')
                            if model not in overall_stats["model_performance"]:
                                overall_stats["model_performance"][model] = {"success": 0, "failed": 0}
                            overall_stats["model_performance"][model]["success"] += 1
                        elif chunk.get('status') in ('failed', 'failed_permanent'):
                            overall_stats["llm_failed"] += 1
                            err = chunk.get('error', 'unknown')
                            overall_stats["errors"][err] = overall_stats["errors"].get(err, 0) + 1

                    if not chunks:
                        file_id = item.get('file_id')
                        stats_file = os.path.join(OUTPUT_DIR, f"{file_id}_chunks_detailed_stats.json")
                        if os.path.exists(stats_file):
                            try:
                                with open(stats_file, 'r', encoding='utf-8') as sf:
                                    s = json.load(sf)
                                    overall_stats["llm_success"] += s.get("success", 0)
                                    overall_stats["llm_failed"] += s.get("failed", 0)
                                    for m, count in s.get("model_stats", {}).items():
                                        if m not in overall_stats["model_performance"]:
                                            overall_stats["model_performance"][m] = {"success": 0, "failed": 0}
                                        overall_stats["model_performance"][m]["success"] += count
                                    for err, count in s.get("error_distribution", {}).items():
                                        overall_stats["errors"][err] = overall_stats["errors"].get(err, 0) + count
                            except Exception:
                                logger.debug(f"無法讀取統計檔 {stats_file}")
        except Exception:
            logger.debug(f"無法讀取 batch 檔案 {bf}")

    delta_files = done_files - LAST_REPORT_STATS["done_files"]
    delta_chunks = overall_stats["llm_success"] - LAST_REPORT_STATS["llm_success"]
    time_diff_min = (now_ts - LAST_REPORT_STATS["timestamp"]) / 60

    throughput = (delta_chunks / time_diff_min) if time_diff_min > 0.016 else 0

    logger.info("==================================================")
    logger.info(f"📊 [深度報告] 總檔案進度: {done_files}/{total_files} (處理中: {working_files})")
    logger.info(f"⚡ [增量對比] 檔案: +{delta_files} | 摘要: +{delta_chunks} | 吞吐量: {throughput:.1f} chunks/min")
    logger.info(f"📈 LLM 摘要統計: 成功 {overall_stats['llm_success']} | 失敗 {overall_stats['llm_failed']}")

    if failed_permanent_files:
        logger.warning(f"🚨 永久失敗檔案: {failed_permanent_files}")

    if overall_stats["model_performance"]:
        logger.info("--- 模型成功率排行 ---")
        for m, p in overall_stats["model_performance"].items():
            total = p["success"] + p["failed"]
            rate = (p["success"] / total * 100) if total > 0 else 0
            logger.info(f"  - {m}: {rate:.1f}% ({p['success']}/{total})")

    if overall_stats["errors"]:
        logger.info("--- 錯誤原因分布 (前 3) ---")
        for err, count in sorted(overall_stats["errors"].items(), key=lambda x: x[1], reverse=True)[:3]:
            logger.info(f"  - {err}: {count} 次")

    model_failure_counts = {}
    for m, p in overall_stats["model_performance"].items():
        if p["failed"] > 0:
            model_failure_counts[m] = p["failed"]

    sorted_failed_models = sorted(model_failure_counts.items(), key=lambda x: x[1], reverse=True)
    overall_stats["top_failed_models"] = [
        {"model": m, "failed_count": c} for m, c in sorted_failed_models
    ]

    if overall_stats["top_failed_models"]:
        logger.info("--- 失敗模型排行榜 (Top Failed Models) ---")
        for idx, item in enumerate(overall_stats["top_failed_models"], 1):
            logger.info(f" - #{idx} {item['model']}: {item['failed_count']} 次失敗")

    if failed_files > 0 or overall_stats["llm_failed"] > 0:
        logger.info("💡 [AGENT_PROMPT] 偵測到失敗，請檢查上述『錯誤原因』。若特定模型成功率低於 50%，建議從 config.json 移除。")

    status_file_path = os.path.join(OUTPUT_DIR, "top_failed_models_status.json")
    try:
        with open(status_file_path, 'w', encoding='utf-8') as sf:
            json.dump(overall_stats["top_failed_models"], sf, ensure_ascii=False, indent=2)
        logger.debug(f"📝 失敗模型排行榜已寫入：{status_file_path}")
    except Exception as e:
        logger.warning(f"⚠️ 寫入失敗模型排行榜狀態檔失敗：{e}")

    logger.info("==================================================")

    LAST_REPORT_STATS = {
        "done_files": done_files,
        "llm_success": overall_stats["llm_success"],
        "timestamp": now_ts
    }

def check_and_reset_if_needed(batch_file, active_file_ids=None):
    STUCKABLE_STATES = {'chunking', 'summarizing', 'embedding', 'db_inserting'}
    try:
        with open(batch_file, 'r', encoding='utf-8') as f:
            data = _normalize_batch_data(json.load(f))
    except Exception:
        logger.debug(f"無法讀取 batch 檔案 {batch_file} 用於卡住檢測")
        return
    updated = False
    for item in data:
        file_id = item.get('file_id')
        if active_file_ids and file_id in active_file_ids:
            continue
        status = item.get('status')
        last_updated = item.get('last_updated')
        if not last_updated: continue
        try:
            last_dt = datetime.fromisoformat(last_updated.replace('Z', '+00:00')).replace(tzinfo=None)
            time_diff = (datetime.now() - last_dt).total_seconds()
            if status in STUCKABLE_STATES and time_diff > MAX_WORKING_TIME_SEC:
                logger.warning(f"⚠️ 檔案 {file_id} 卡住超過 {time_diff:.0f} 秒,重置為 undone。")
                set_status_file(batch_file)
                update_state(file_id, 'undone')
                updated = True
        except Exception:
            logger.debug(f"無法解析時間戳: 檔案 {file_id}, last_updated={last_updated}")

def _retry_chunk(file_id, chunk, batch_file):
    from summarize_pipeline import call_llm
    from config_loader import get_nested_config
    models = get_nested_config('summarization.models', [])
    if not models:
        models = ["gpt-4.1-mini"]
    max_retries = get_nested_config('summarization.max_retries', 3)
    chunk_text = chunk.get('text_content', '')
    for attempt in range(1, max_retries + 1):
        model = models[(attempt - 1) % len(models)]
        try:
            result = call_llm(chunk_text, model)
            chunk['summary'] = result.get('summary', '')
            chunk['tags'] = result.get('tags', [])
            chunk['status'] = 'done'
            chunk['model_used'] = model
            chunk['retry_count'] = chunk.get('retry_count', 0) + 1
            chunk['error'] = None
            return True
        except Exception as e:
            logger.warning(f"Chunk {chunk.get('chunk_id')} retry {attempt} failed with {model}: {e}")
            chunk['error'] = str(e)
            chunk['retry_count'] = chunk.get('retry_count', 0) + 1
    chunk['status'] = 'failed_permanent'
    return False

def run_failed_chunk_retry(batch_file):
    try:
        with open(batch_file, 'r', encoding='utf-8') as f:
            data = _normalize_batch_data(json.load(f))
    except Exception:
        logger.debug(f"無法讀取 batch 檔案 {batch_file} 用於重試")
        return False
    any_retried = False
    for item in data:
        chunks = item.get('chunks', [])
        file_id = item.get('file_id')
        retry_candidates = [c for c in chunks if c.get('status') == 'failed' and c.get('retry_count', 0) < 3]
        if not retry_candidates:
            continue
        logger.info(f"🔄 檢測到 {len(retry_candidates)} 個失敗 chunk 需重試:檔案 {file_id}")
        set_status_file(batch_file)
        for chunk in retry_candidates:
            success = _retry_chunk(file_id, chunk, batch_file)
            if not success:
                logger.warning(f"Chunk {chunk.get('chunk_id')} 重試失敗,已設為 failed_permanent")
        def save_chunks(data):
            for it in data:
                if it['file_id'] == file_id:
                    it['chunks'] = chunks
                    return data
            return None
        from state_manager import _locked_read_write
        _locked_read_write(save_chunks)
        all_permanent = all(c.get('status') == 'failed_permanent' for c in chunks)
        if all_permanent:
            update_state(file_id, 'failed_permanent')
        any_retried = True
    if not any_retried:
        import glob
        legacy_files = glob.glob(os.path.join(OUTPUT_DIR, 'failed_chunks_*.json'))
        for legacy_file in legacy_files:
            try:
                with open(legacy_file, 'r', encoding='utf-8') as f:
                    legacy_data = json.load(f)
                if isinstance(legacy_data, dict):
                    lfid = legacy_data.get('file_id')
                else:
                    lfid = int(os.path.basename(legacy_file).split('_')[2].split('.')[0])
                logger.info(f"🔄 舊式失敗片段檔案 {legacy_file}, 檔案 {lfid}")
                set_status_file(batch_file)
                cmd = ['python3', os.path.join(os.path.dirname(__file__), 'summarize_retry.py'), '--file-id', str(lfid), '--batch', batch_file]
                env = os.environ.copy()
                res = subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)
                if res.returncode == 0:
                    logger.info("✅ 片段重試成功")
                    update_state(lfid, 'done')
                else:
                    _register_failure(f"File {lfid} 片段重試執行失敗")
                    update_state(lfid, 'failed')
                os.remove(legacy_file)
                any_retried = True
            except Exception as e:
                logger.error(f"處理舊式失敗檔案 {legacy_file} 錯誤: {e}")
    return any_retried

def run_batch_processor(batch_file):
    try:
        with open(batch_file, 'r', encoding='utf-8') as f:
            data = _normalize_batch_data(json.load(f))
    except Exception:
        logger.debug(f"無法讀取 batch 檔案 {batch_file} 用於 processor")
        return
    if run_failed_chunk_retry(batch_file): return

    launched = 0
    for item in data:
        if len(active_processes) >= MAX_CONCURRENCY:
            break
        status = item.get('status')
        next_status, phase = _get_next_phase(status)
        if next_status and acquire_phase_slot(item.get('file_id'), phase):
            file_id = item.get('file_id')
            set_status_file(batch_file)
            update_state(file_id, next_status)
            if next_status == 'chunking':
                from state_manager import _locked_read_write
                def _record_start(data):
                    for it in data:
                        if it.get('file_id') == file_id:
                            it['diagnostics'] = {'started_at': datetime.now().isoformat()}
                            return data
                    return None
                _locked_read_write(_record_start)
            logger.info(f"🚀 [Auto] 啟動 {next_status}: 檔案 {file_id}...")
            cmd = ['bash', os.path.join(os.path.dirname(__file__), 'run_wrapper.sh'),
                   '--id', str(file_id), '--batch', batch_file, '--phase', next_status]
            env = {k: os.environ[k] for k in ('PATH', 'HOME', 'SRT_PROJECT_ROOT', 'SRT_OUTPUT_DIR', 'SRT_DB_PATH', 'SRT_MASTER_FILE', 'SRT_DATA_DIR', 'TRANSCRIPTFLOW_CONFIG') if k in os.environ}
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            active_processes.append({"proc": proc, "file_id": file_id, "phase": phase})
            launched += 1

def main():
    _check_environment()
    _health_check()
    _reset_phase_slots()
    logger.info("🛡️ Smart Merge Watchdog 已啟動 (非阻塞模式)。")
    last_report_time = time.time()
    
    while True:
        try:
            now = time.time()
            if now - last_report_time >= REPORT_INTERVAL_SEC:
                _report_progress()
                last_report_time = now
            
            for entry in active_processes[:]:
                proc = entry["proc"]
                if proc.poll() is not None:
                    retcode = proc.returncode
                    file_id = entry["file_id"]
                    phase = entry["phase"]
                    if retcode == 0:
                        logger.info(f"✅ 任務完成:檔案 {file_id}")
                        batch_files = [os.path.join(OUTPUT_DIR, f) for f in os.listdir(OUTPUT_DIR) if f.startswith('batch_status_') and f.endswith('.json')]
                        for bf in batch_files:
                            try:
                                with open(bf, 'r', encoding='utf-8') as f:
                                    bdata = _normalize_batch_data(json.load(f))
                                for bitem in bdata:
                                    if bitem.get('file_id') == file_id:
                                        cur_status = bitem.get('status')
                                        set_status_file(bf)
                                        if cur_status == 'chunking':
                                            update_state(file_id, 'queueing_1')
                                        elif cur_status == 'summarizing':
                                            update_state(file_id, 'queueing_2')
                                        elif cur_status == 'embedding':
                                            update_state(file_id, 'queueing_3')
                                        elif cur_status == 'db_inserting':
                                            update_state(file_id, 'done')
                                        break
                            except Exception:
                                logger.debug(f"無法處理完成任務: 檔案 {file_id}, batch {bf}")
                    else:
                        logger.error(f"❌ 任務失敗,檔案 {file_id}, 退出碼: {retcode}")
                        batch_files = [os.path.join(OUTPUT_DIR, f) for f in os.listdir(OUTPUT_DIR) if f.startswith('batch_status_') and f.endswith('.json')]
                        for bf in batch_files:
                            try:
                                with open(bf, 'r', encoding='utf-8') as f:
                                    bdata = _normalize_batch_data(json.load(f))
                                if any(i.get('file_id') == file_id for i in bdata):
                                    set_status_file(bf)
                                    update_state(file_id, 'failed')
                                    break
                            except Exception:
                                logger.debug(f"無法處理失敗任務: 檔案 {file_id}, batch {bf}")
                    release_file_phase_slot(file_id, phase)
                    active_processes.remove(entry)

            if len(active_processes) < MAX_CONCURRENCY:
                batch_files = [os.path.join(OUTPUT_DIR, f) for f in os.listdir(OUTPUT_DIR) if f.startswith('batch_status_') and f.endswith('.json')]
                active_ids = {e["file_id"] for e in active_processes}
                for bf in batch_files:
                    try:
                        with open(bf, 'r', encoding='utf-8') as f:
                            bdata = _normalize_batch_data(json.load(f))
                        for item in bdata:
                            fid = item.get('file_id')
                            if fid in active_ids:
                                continue
                            if item.get('status') == 'failed':
                                set_status_file(bf)
                                update_state(fid, 'undone')
                    except Exception:
                        logger.debug(f"無法處理失敗重置: batch {bf}")
                for bf in batch_files: check_and_reset_if_needed(bf, active_ids)
                for bf in batch_files:
                    try:
                        with open(bf, 'r', encoding='utf-8') as f:
                            bdata = _normalize_batch_data(json.load(f))
                        if any(_get_next_phase(i.get('status'))[0] for i in bdata):
                            run_batch_processor(bf)
                    except Exception:
                        logger.debug(f"無法啟動 processor: batch {bf}")
            
            time.sleep(CHECK_INTERVAL_SEC)
        except KeyboardInterrupt:
            logger.info("🛑 Watchdog 已停止。")
            break
        except Exception as e:
            logger.error(f"Watchdog 主迴圈錯誤: {e}\n{traceback.format_exc()}")
            time.sleep(10)

if __name__ == '__main__':
    main()
