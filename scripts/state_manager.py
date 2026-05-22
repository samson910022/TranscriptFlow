import json
import os
import time
from datetime import datetime
import sys
import re
import fcntl
import tempfile

# 優先從環境變數讀取專案根目錄，預設為 scripts/ 的上一層。
PROJECT_ROOT = os.environ.get('SRT_PROJECT_ROOT', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 預設狀態檔位置
STATUS_FILE = os.environ.get('BATCH_STATUS_FILE', os.path.join(PROJECT_ROOT, 'file_manifest_status.json'))

def get_status_path():
    if os.path.isabs(STATUS_FILE):
        return STATUS_FILE
    return os.path.join(PROJECT_ROOT, STATUS_FILE)

def set_status_file(filename):
    global STATUS_FILE
    STATUS_FILE = filename

def _locked_read_write(operation_func):
    path = get_status_path()
    if not os.path.exists(path):
        dir_path = os.path.dirname(path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        with open(path, 'w') as f:
            json.dump([], f)

    MAX_FALLBACK_ATTEMPTS = 10
    lock_path = path + '.lock'
    for attempt in range(MAX_FALLBACK_ATTEMPTS):
        tmp_path = None
        lock_f = None
        try:
            lock_f = open(lock_path, 'w')
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    f.seek(0)
                    data = json.load(f)
                new_data = operation_func(data)
                if new_data is not None:
                    dir_path = os.path.dirname(path) or '.'
                    with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False,
                                                     prefix='.status_tmp_', suffix='.json') as tf:
                        tmp_path = tf.name
                        json.dump(new_data, tf, indent=2, ensure_ascii=False)
                        tf.flush()
                        os.fsync(tf.fileno())
                    os.replace(tmp_path, path)
                    tmp_path = None
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
            return
        except BlockingIOError:
            if attempt < MAX_FALLBACK_ATTEMPTS - 1:
                time.sleep(min(2 ** attempt, 30))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"狀態檔 JSON 格式錯誤: {path}") from exc
        finally:
            if lock_f is not None:
                lock_f.close()
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    raise RuntimeError(f"無法取得檔案鎖定（逾時）: {path}")

def load_status(file_id=None):
    path = get_status_path()
    if not os.path.exists(path):
        return []
    if file_id is None:
        with open(path, 'r') as f:
            data = json.load(f)
        return data
    with open(path, 'r') as f:
        data = json.load(f)
    if file_id is not None:
        for item in data:
            if item["file_id"] == int(file_id):
                return item
        return None

def save_status(data):
    path = get_status_path()
    dir_path = os.path.dirname(path) or '.'
    lock_path = path + '.lock'
    lock_f = None
    tmp_path = None
    try:
        lock_f = open(lock_path, 'w')
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False,
                                         prefix='.status_tmp_', suffix='.json') as tf:
            tmp_path = tf.name
            json.dump(data, tf, indent=2, ensure_ascii=False)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if lock_f is not None:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
            lock_f.close()
        if tmp_path is not None:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

# ── 相位並發控制 ──────────────────────────────────────────────────────────
_PHASE_SLOTS_FILE = None
_PHASE_SLOTS_LOCK_FILE = None

def _get_phase_concurrency():
    return get_nested_config('phase_concurrency', {
        "phase1_chunking": 1,
        "phase2_summarizing": 1,
        "phase3_embedding": 2,
        "phase4_db_insert": 1
    })

def _get_phase_slots_file():
    global _PHASE_SLOTS_FILE
    if _PHASE_SLOTS_FILE is None:
        output_dir = os.environ.get('SRT_OUTPUT_DIR',
            get_nested_config('paths.output_dir', './output'))
        _PHASE_SLOTS_FILE = os.path.join(output_dir, '.phase_slots.json')
    return _PHASE_SLOTS_FILE

def _get_phase_slots_lock_file():
    global _PHASE_SLOTS_LOCK_FILE
    if _PHASE_SLOTS_LOCK_FILE is None:
        output_dir = os.environ.get('SRT_OUTPUT_DIR',
            get_nested_config('paths.output_dir', './output'))
        _PHASE_SLOTS_LOCK_FILE = os.path.join(output_dir, '.phase_slots.lock')
    return _PHASE_SLOTS_LOCK_FILE

def _atomic_update_phase_slots(path, update_fn):
    lock_path = _get_phase_slots_lock_file()
    for attempt in range(3):
        lock_f = None
        try:
            lock_f = open(lock_path, 'w')
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with open(path, 'r') as f:
                    slots = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                slots = {}
            new_slots = update_fn(slots)
            if new_slots is False:
                return False
            dir_path = os.path.dirname(path) or '.'
            os.makedirs(dir_path, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, delete=False,
                                             prefix='.phase_slots_tmp_', suffix='.json') as tf:
                tmp_path = tf.name
                json.dump(new_slots, tf, indent=2)
            os.replace(tmp_path, path)
            return True
        except BlockingIOError:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return False
        except Exception:
            return False
        finally:
            if lock_f is not None:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_UN)
                    lock_f.close()
                except Exception:
                    pass
    return False

def acquire_phase_slot(file_id, phase):
    concurrency = _get_phase_concurrency()
    max_slots = concurrency.get(phase, 1)
    path = _get_phase_slots_file()
    _acquired = [False]
    def _acquire(slots):
        phase_data = slots.get(phase, {"slots": [], "queue": []})
        if file_id in phase_data.get("slots", []):
            _acquired[0] = True
            return slots
        if len(phase_data.get("slots", [])) < max_slots:
            phase_data.setdefault("slots", []).append(file_id)
            slots[phase] = phase_data
            _acquired[0] = True
            return slots
        queue = phase_data.setdefault("queue", [])
        if file_id not in queue:
            queue.append(file_id)
        slots[phase] = phase_data
        return slots
    _atomic_update_phase_slots(path, _acquire)
    return _acquired[0]

def release_phase_slot(phase):
    path = _get_phase_slots_file()
    def _release(slots):
        phase_data = slots.get(phase, {"slots": [], "queue": []})
        return slots
    _atomic_update_phase_slots(path, _release)

def release_file_phase_slot(file_id, phase):
    path = _get_phase_slots_file()
    def _release(slots):
        phase_data = slots.get(phase, {"slots": [], "queue": []})
        slots_list = phase_data.get("slots", [])
        queue = phase_data.get("queue", [])
        if file_id in slots_list:
            slots_list.remove(file_id)
            if queue:
                next_file = queue.pop(0)
                slots_list.append(next_file)
            phase_data["slots"] = slots_list
            phase_data["queue"] = queue
            slots[phase] = phase_data
        elif file_id in queue:
            queue.remove(file_id)
            phase_data["queue"] = queue
            slots[phase] = phase_data
        return slots
    return _atomic_update_phase_slots(path, _release)

# 基礎路徑配置
# 從 config_loader 讀取路徑配置
from config_loader import get_nested_config, get_env_or_config
_paths_cfg = get_nested_config('paths')
_default_master = _paths_cfg.get('master_file', './examples/master_file_manifest.example.json')
MASTER_FILE = os.environ.get('SRT_MASTER_FILE', _default_master)
SRT_DATA_DIR = os.environ.get('SRT_DATA_DIR', _paths_cfg.get('data_dir', './examples/srt'))
WORK_DIR = os.environ.get('SRT_WORK_DIR', os.path.join(PROJECT_ROOT, 'structured_yt_data'))

def init_batch(start_idx, end_idx):
    master_file = MASTER_FILE
    global STATUS_FILE
    
    output_dir = os.environ.get('SRT_OUTPUT_DIR', 
        './output')
    STATUS_FILE = os.path.join(output_dir, f'batch_status_{start_idx}_{end_idx}.json')
    path = STATUS_FILE

    master_path = MASTER_FILE
    if os.path.exists(path):
        print(f"[Info] Batch file {STATUS_FILE} already exists. Resuming existing batch. (Skipping overwrite)")
        return

    if not os.path.exists(master_path):
        print(f"[Error] Master file not found at {master_path}")
        sys.exit(1)

    # 讀取 master file
    with open(master_path, 'r') as f:
        master = json.load(f)
    files = master.get("files", [])

    # 路徑模板替換函數
    def _resolve_path(path_str):
        return path_str.replace('{SRT_DATA_DIR}', SRT_DATA_DIR).replace('{SRT_PROJECT_ROOT}', PROJECT_ROOT)

    status_data = []
    for idx in range(start_idx, min(end_idx + 1, len(files))):
        file_info = files[idx]
        path_srt = _resolve_path(file_info["path_srt"])
        path_mp3 = _resolve_path(file_info["path_mp3"])
        status = "undone"
        error_log = []

        if not os.path.exists(path_srt):
            status = "failed_permanent"
            error_log.append({"time": datetime.now().isoformat(), "from_status": "init", "error": f"SRT Not Found - {path_srt}"})
        elif not os.path.exists(path_mp3):
            status = "failed_permanent"
            error_log.append({"time": datetime.now().isoformat(), "from_status": "init", "error": f"MP3 Not Found - {path_mp3}"})

        status_data.append({
            "file_id": file_info["id"],
            "filename_srt": file_info["filename_srt"],
            "filename_mp3": file_info["filename_mp3"],
            "file_path": path_srt,
            "status": status,
            "last_updated": datetime.now().isoformat(),
            "retry_count": 0 if status != "failed_permanent" else 1,
            "error_log": error_log,
            "total_chunks": 0,
            "chunks": []
        })

    save_status(status_data)
    print(f"Initialized {len(status_data)} files in {STATUS_FILE}. "
          f"Pre-flight checked: {len([x for x in status_data if x['status'] == 'failed_permanent'])} failed permanently.")

_VALID_TRANSITIONS = {
    'undone': {'chunking'},
    'chunking': {'queueing_1', 'summarizing', 'undone'},
    'queueing_1': {'summarizing', 'undone'},
    'summarizing': {'queueing_2', 'embedding', 'undone'},
    'queueing_2': {'embedding', 'undone'},
    'embedding': {'queueing_3', 'db_inserting', 'undone'},
    'queueing_3': {'db_inserting', 'undone'},
    'db_inserting': {'done', 'undone'},
    'done': set(),
    'failed': {'undone'},
    'failed_permanent': set(),
}

def _write_failed_report(file_id, item):
    report_dir = os.path.join(os.path.dirname(get_status_path()), '.failed_reports')
    os.makedirs(report_dir, exist_ok=True)
    report = {
        "timestamp": datetime.now().isoformat(),
        "file_id": file_id,
        "status": "failed_permanent",
        "error_log": item.get('error_log', []),
        "chunks": item.get('chunks', [])
    }
    report_path = os.path.join(report_dir, f"failed_file_{file_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(report_path, 'w', encoding='utf-8') as rf:
        json.dump(report, rf, ensure_ascii=False, indent=2)

def update_state(file_id, new_status, error_msg=None, used_model=None):
    result_msg = []
    def modify(data):
        for item in data:
            if item["file_id"] == int(file_id):
                old_status = item["status"]

                if new_status in ('failed', 'failed_permanent'):
                    pass
                elif new_status == 'undone' and old_status in ('done', 'failed_permanent'):
                    result_msg.append(f"[System] Cannot retry from terminal state {old_status}")
                    return data
                elif old_status != new_status:
                    valid = _VALID_TRANSITIONS.get(old_status, set())
                    if new_status not in valid:
                        result_msg.append(f"[System] Invalid transition: {old_status} -> {new_status}")
                        return data

                target_status = new_status

                chunks_list = item.get("chunks", [])
                if target_status == "failed_permanent" and chunks_list:
                    all_processed = all(c.get("status") in ("done", "failed", "failed_permanent") for c in chunks_list)
                    if all_processed and not all(c.get("status") == "failed_permanent" for c in chunks_list):
                        target_status = old_status
                        result_msg.append(f"File {file_id} keeping {old_status} (not all chunks failed_permanent).")

                if old_status == 'failed' and target_status == 'undone':
                    item['retry_count'] = item.get('retry_count', 0) + 1
                    if item['retry_count'] >= 3:
                        target_status = 'failed_permanent'

                item["status"] = target_status
                item["last_updated"] = datetime.now().isoformat()

                if error_msg:
                    log_entry = {"time": datetime.now().isoformat(), "from_status": old_status, "error": error_msg}
                    if used_model: log_entry["used_model"] = used_model
                    item["error_log"].append(log_entry)

                if target_status == "failed_permanent":
                    _write_failed_report(file_id, item)

                result_msg.append(f"File {file_id} updated: {old_status} -> {item['status']} (Retry: {item.get('retry_count', 0)})")
                return data
        result_msg.append(f"File ID {file_id} not found in {STATUS_FILE}.")
        return None

    _locked_read_write(modify)
    for m in result_msg: print(m)

def print_summary():
    data = load_status()
    if not data:
        print("No data found.")
        return
    counts = {}
    for item in data:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    print("\n=== Batch Summary ===")
    for st, c in counts.items(): print(f"- {st}: {c}")
    print("\n=== Actionable Items (Limit 5 per status) ===")
    for target_st in ["undone", "chunking", "queueing_1", "summarizing", "queueing_2", "embedding", "queueing_3", "db_inserting", "failed", "failed_permanent"]:
        items = [str(x['file_id']) for x in data if x['status'] == target_st][:5]
        if items: print(f"[{target_st}] -> {', '.join(items)}")

def get_worker_model(file_id):
    data = load_status()
    for item in data:
        if item["file_id"] == int(file_id):
            models = get_nested_config('summarization.models', ["gpt-4.1-mini"])
            idx = (item["file_id"] + item["retry_count"]) % len(models)
            model = models[idx]
            print(model)
            return model
    print(f"[Error] File ID {file_id} not found in {STATUS_FILE}.")
    sys.exit(1)

def check_watchdog():
    watchdog_timeout_sec = get_env_or_config('WATCHDOG_MAX_WORKING_TIME', 'watchdog.max_working_time_sec', 600)
    def watchdog_logic(data):
        now = datetime.now()
        changed = False
        for item in data:
            if item["status"] not in ["undone", "done", "failed_permanent", "queueing_1", "queueing_2", "queueing_3"]:
                last_updated = datetime.fromisoformat(item["last_updated"])
                if (now - last_updated).total_seconds() >= watchdog_timeout_sec:
                    print(f"[Watchdog] File {item['file_id']} stuck in {item['status']} for >{watchdog_timeout_sec}s. Resetting to undone.")
                    item["error_log"].append({
                        "time": now.isoformat(), "from_status": item["status"],
                        "error": f"Watchdog timeout (>{watchdog_timeout_sec}s)", "used_model": "watchdog"
                    })
                    item["status"] = "undone"
                    item["retry_count"] = item.get("retry_count", 0) + 1
                    item["last_updated"] = now.isoformat()
                    if item["retry_count"] >= 3: item["status"] = "failed_permanent"
                    changed = True
        return data if changed else None
    _locked_read_write(watchdog_logic)

if __name__ == "__main__":
    if len(sys.argv) > 1:
        cmd = sys.argv[1]

        if len(sys.argv) > 2 and sys.argv[2].endswith('.json'):
            STATUS_FILE = sys.argv[2]
            args_offset = 3
        else:
            args_offset = 2

        if cmd == "init_batch":
            start = int(sys.argv[args_offset])
            end = int(sys.argv[args_offset+1])
            init_batch(start, end)
        elif cmd == "watchdog":
            check_watchdog()
        elif cmd == "get_worker_model" and len(sys.argv) >= args_offset + 1:
            file_id = sys.argv[args_offset]
            get_worker_model(file_id)
        elif cmd == "update" and len(sys.argv) >= args_offset + 2:
            file_id = sys.argv[args_offset]
            status = sys.argv[args_offset+1]
            err = sys.argv[args_offset+2] if len(sys.argv) > args_offset+2 else None
            model = sys.argv[args_offset+3] if len(sys.argv) > args_offset+3 else None
            update_state(file_id, status, err, model)
        elif cmd == "summary":
            print_summary()
