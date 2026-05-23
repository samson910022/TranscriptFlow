import os
import sys
import json
import argparse
import shutil
import tempfile
import fcntl
import math
from datetime import datetime
from typing import List, Dict, Tuple
import pyarrow as pa
import pandas as pd
import time
import lancedb

# Local imports
from state_manager import update_state, set_status_file
from logger_config import get_logger
from config_loader import get_env_or_config, validate_config, check_required_env_vars, sanitize_api_url, ensure_secure_permissions

logger = get_logger('finalize')

# Environment / config (lazy: validated on first write_to_db call)
_CONFIG_INITIALIZED = False
PROJECT_ROOT = None
OUTPUT_DIR = None
DB_FINAL = None
TABLE_NAME = None
BACKUP_DIR = None
EXPECTED_DIM = None

def _ensure_config():
    global _CONFIG_INITIALIZED, PROJECT_ROOT, OUTPUT_DIR, DB_FINAL, TABLE_NAME, BACKUP_DIR, EXPECTED_DIM
    if _CONFIG_INITIALIZED:
        return
    required_ok, missing = check_required_env_vars()
    if not required_ok:
        raise RuntimeError(f'必要環境變數未設定:{missing}')
    validation_errors = validate_config()
    if validation_errors:
        raise ValueError('配置驗證失敗:\n' + '\n'.join(f' - {e}' for e in validation_errors))
    PROJECT_ROOT = os.getenv('SRT_PROJECT_ROOT', os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../')))
    OUTPUT_DIR = os.getenv('SRT_OUTPUT_DIR', './output')
    DB_FINAL = get_env_or_config('SRT_DB_PATH', 'paths.db_path', os.path.join(OUTPUT_DIR, 'lance_test_db'))
    TABLE_NAME = get_env_or_config('SRT_TABLE_NAME', 'tables.final_db', 'psychology_kb')
    BACKUP_DIR = get_env_or_config('SRT_BACKUP_DIR', 'paths.backup_dir', './output/lance_backup')
    EXPECTED_DIM = get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072)
    _CONFIG_INITIALIZED = True


def deduplicate_records(records: List[Dict]) -> List[Dict]:
    """按穩定 chunk_id 去重。
    舊版以 file_name + start_time 去重，標題或時間重疊時可能誤刪合法 chunk。
    """
    seen = set()
    deduped = []
    for rec in records:
        key = rec.get('chunk_id') or (rec['file_name'], rec['start_time'])
        if key not in seen:
            seen.add(key)
            deduped.append(rec)

    if len(deduped) != len(records):
        logger.warning(f"Deduplicated: {len(records)} → {len(deduped)} records (removed {len(records)-len(deduped)} duplicates)")

    return deduped


def _validate_records(records: List[Dict]) -> Tuple[bool, str]:
    required = {
        "chunk_id", "file_id", "file_name", "start_time", "end_time", "summary", "text_content",
        "tags", "participants", "vector", "boundary_type"
    }
    string_fields = {"chunk_id", "file_name", "start_time", "end_time", "summary", "text_content", "boundary_type"}
    for idx, rec in enumerate(records):
        missing = sorted(required - set(rec))
        if missing:
            return False, f"Record {idx} missing fields: {missing}"
        if not isinstance(rec["file_id"], int):
            return False, f"Record {idx} file_id must be an integer"
        for field in string_fields:
            if not isinstance(rec[field], str):
                return False, f"Record {idx} {field} must be a string"
        if not isinstance(rec["tags"], list) or not all(isinstance(x, str) for x in rec["tags"]):
            return False, f"Record {idx} tags must be a list of strings"
        if not isinstance(rec["participants"], list) or not all(isinstance(x, str) for x in rec["participants"]):
            return False, f"Record {idx} participants must be a list of strings"
        vector = rec["vector"]
        if not isinstance(vector, list) or len(vector) != EXPECTED_DIM:
            return False, f"Record {idx} vector dimension mismatch: {len(vector) if isinstance(vector, list) else 'not-list'} != {EXPECTED_DIM}"
        if not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in vector):
            return False, f"Record {idx} vector contains non-finite or non-numeric values"
    return True, "ok"


def _required_schema() -> pa.Schema:
    return pa.schema([
        ("chunk_id", pa.string()),
        ("file_id", pa.int64()),
        ("file_name", pa.string()),
        ("start_time", pa.string()),
        ("end_time", pa.string()),
        ("summary", pa.string()),
        ("text_content", pa.string()),
        ("tags", pa.list_(pa.string())),
        ("participants", pa.list_(pa.string())),
        ("vector", pa.list_(pa.float32(), EXPECTED_DIM)),
        ("boundary_type", pa.string())
    ])


def _schema_field_names(schema) -> set:
    return {field.name for field in schema}


def _preflight_table_schema(table) -> Tuple[bool, str]:
    try:
        existing_schema = table.schema
        if callable(existing_schema):
            existing_schema = existing_schema()
    except Exception as exc:
        return False, f"Unable to inspect existing table schema: {exc}"

    existing_fields = _schema_field_names(existing_schema)
    required_fields = _schema_field_names(_required_schema())
    missing = sorted(required_fields - existing_fields)
    if missing:
        return False, f"Existing LanceDB table schema is missing fields {missing}; run a migration before idempotent write"
    return True, "ok"


def write_to_db(records: List[Dict], allow_delete: bool = False) -> Tuple[bool, str]:
    _ensure_config()
    records = deduplicate_records(records)
    if not records:
        return True, "No records to write"

    ok, validation_msg = _validate_records(records)
    if not ok:
        return False, validation_msg

    os.makedirs(DB_FINAL, exist_ok=True)
    try:
        db = lancedb.connect(DB_FINAL)
        raw = db.list_tables()
        table_names = raw.tables

        if TABLE_NAME not in table_names:
            db.create_table(TABLE_NAME, schema=_required_schema())

        table = db.open_table(TABLE_NAME)
        ok, schema_msg = _preflight_table_schema(table)
        if not ok:
            return False, schema_msg

        merge = table.merge_insert("chunk_id")
        merge.when_matched_update_all()
        merge.when_not_matched_insert_all()
        if allow_delete:
            file_ids = sorted({r["file_id"] for r in records})
            conditions = [f"file_id = {file_id}" for file_id in file_ids]
            delete_condition = " OR ".join(conditions)
            merge.when_not_matched_by_source_delete(delete_condition)
        merge.execute(records)
        logger.info(f"LanceDB merge upsert: {len(records)} records (allow_delete={allow_delete})")
        return True, f"Written {len(records)} records to {TABLE_NAME}"
    except Exception as e:
        return False, str(e)

if __name__ == '__main__':
    logger.error("finalize.py is no longer a standalone entry point. Use summarize.py --phase db_inserting instead.")
    sys.exit(1)
