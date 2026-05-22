"""
chunk_test_runner.py — 獨立參數測試腳本

解析 SRT → 語意分段 → 提取參與者 → LLM 摘要
不寫入 LanceDB，全部輸出到 .txt 報告

用法:
    python3 scripts/chunk_test_runner.py --file-id 11
    python3 scripts/chunk_test_runner.py --file-id 11 --chunk-params '{"window_size":3,"strong_pct":0.01}'
    python3 scripts/chunk_test_runner.py --file-id 11 --models '["NV-deepseek-v4-flash"]'
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import List, Dict
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import get_env_or_config, get_nested_config
from parse_srt import parse_srt, SubtitleEntry
from semantic_chunk import smart_merge_3_0
from logger_config import get_logger
from llm_client import call_llm as _call_llm, extract_participants

logger = get_logger('chunk_test_runner')
MAX_RETRIES = get_env_or_config('MAX_RETRIES', 'summarization.max_retries', 3)

SYSTEM_PROMPT = """你是一個摘要助理。請根據以下文字區塊，直接輸出摘要內容，不要使用「這段文字」、「本文」、「該段落」等引導詞。

1. 摘要（150-300 字，繁體中文，客觀濃縮核心論點）
2. 標籤（3-8 個，每個 1-3 詞，用於語意檢索）

只輸出 JSON，不要 markdown 格式、不要額外說明：
{"summary": "...", "tags": ["...", "...", ...]}"""


def load_manifest_entry(file_id: int) -> dict:
    master_path = get_env_or_config('SRT_MASTER_FILE', 'paths.master_file',
                                     './examples/master_file_manifest.example.json')
    with open(master_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    files = manifest.get('files', [])
    for entry in files:
        if entry.get('id') == file_id:
            srt_path = entry['path_srt']
            if not os.path.exists(srt_path):
                raise FileNotFoundError(f"SRT not found: {srt_path}")
            return entry
    raise ValueError(f"File ID {file_id} not found in manifest")


def get_models(override: list = None) -> List[str]:
    if override:
        return override
    return get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])


def call_llm(text: str, model: str) -> dict:
    return _call_llm(prompt=text, model=model, system_prompt=SYSTEM_PROMPT)


def summarize_chunk(chunk: dict, models: List[str]) -> dict:
    text = chunk['text_content']
    errors = []
    for attempt in range(1, MAX_RETRIES + 1):
        model = models[(attempt - 1) % len(models)]
        try:
            result = call_llm(text, model)
            chunk['summary'] = result.get('summary', '')
            chunk['tags'] = result.get('tags', [])
            chunk['status'] = 'done'
            chunk['model_used'] = model
            chunk['errors'] = errors
            return chunk
        except Exception as exc:
            errors.append({"model": model, "message": str(exc)})
            logger.warning(f"Chunk {chunk.get('chunk_id')} failed with {model} (attempt {attempt}): {exc}")
    chunk['status'] = 'failed'
    chunk['model_used'] = errors[-1]['model'] if errors else 'unknown'
    chunk['errors'] = errors
    return chunk


DEFAULT_CHUNK_PARAMS = {
    "window_size": get_env_or_config('SMART_MERGE_WINDOW_SIZE', 'chunking.smart_merge_window_size', 5),
    "strong_pct": get_env_or_config('SMART_MERGE_STRONG_PCT', 'chunking.smart_merge_strong_pct', 0.02),
    "weak_pct": get_env_or_config('SMART_MERGE_WEAK_PCT', 'chunking.smart_merge_weak_pct', 0.05),
    "min_sentences": get_env_or_config('SMART_MERGE_MIN_SENTENCES', 'chunking.smart_merge_min_sentences', 8),
    "noise_drop_len": get_env_or_config('SMART_MERGE_NOISE_DROP_LEN', 'chunking.smart_merge_noise_drop_len', 2),
    "noise_weak_len": get_env_or_config('SMART_MERGE_NOISE_WEAK_LEN', 'chunking.smart_merge_noise_weak_len', 3),
    "min_chunks": get_env_or_config('MIN_CHUNKS', 'chunking.min_chunks', 2),
    "max_chunks": get_env_or_config('MAX_CHUNKS', 'chunking.max_chunks', 200),
}


def write_report(file_id: int, entry: dict, params: dict, models: List[str],
                 participants: List[str], kept_chunks: List[Dict],
                 discarded_chunks: List[Dict], output_path: str,
                 all_segments: List[Dict] = None):
    if all_segments is None:
        all_segments = []
        for ch in kept_chunks:
            all_segments.append({**ch, '_sort_key': ch.get('start_time', '')})
        for ch in discarded_chunks:
            all_segments.append({**ch, '_sort_key': ch.get('start_time', '')})
        all_segments.sort(key=lambda x: x['_sort_key'])

    lines = []
    _sep = lambda: lines.append('')
    _hline = lambda: lines.append('=' * 80)

    _hline()
    lines.append(f"{'測試報告':^80}")
    _hline()
    _sep()

    lines.append(f"{'檔案名稱':<20}{entry.get('filename_srt', 'N/A')}")
    lines.append(f"{'檔案 ID':<20}{file_id}")
    lines.append(f"{'執行時間':<20}{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _sep()

    kept_count = len(kept_chunks)
    dropped_count = len(discarded_chunks)
    _hline()
    lines.append(f"{'組態參數':^80}")
    _hline()
    for k, v in params.items():
        lines.append(f"  {k:<30}{v}")
    _sep()
    lines.append(f"  summarization.models          {', '.join(models)}")
    _sep()

    _hline()
    lines.append(f"{'參與者':^80}")
    _hline()
    if participants:
        for p in participants:
            lines.append(f"  - {p}")
    else:
        lines.append("  (未偵測到參與者)")
    _sep()

    _hline()
    title = f"分段結果（共 {len(all_segments)} 段：{kept_count} 段納入，{dropped_count} 段排除）"
    lines.append(f"{title:^80}")
    _hline()
    _sep()

    for idx, seg in enumerate(all_segments, 1):
        is_dropped = seg.get('dropped', False)
        status_tag = "❌ 已排除" if is_dropped else "✅ 已納入"
        lines.append(f"--- 區段 {idx}/{len(all_segments)} ({status_tag}) ---")
        lines.append(f"  區塊 ID:      {seg.get('chunk_id', 'N/A')}")
        lines.append(f"  時間區間:      {seg.get('start_time', '')} → {seg.get('end_time', '')}")
        lines.append(f"  邊界類型:      {seg.get('boundary_type', 'N/A')}")
        lines.append(f"  字幕行數:      {seg.get('entry_count', 'N/A')}")

        lb = seg.get('left_boundary', {})
        rb = seg.get('right_boundary', {})
        def _fmt_bp(bp, side):
            pct = bp.get('strength_pct')
            cos = bp.get('cosine')
            if pct is None:
                return f"{bp.get('label', '-')}"
            return f"{pct}% (cosine {cos})"
        lines.append(f"  上邊界(起點):  {_fmt_bp(lb, 'left')}")
        lines.append(f"  下邊界(終點):  {_fmt_bp(rb, 'right')}")

        if is_dropped:
            lines.append(f"  排除原因:      {seg.get('drop_reason', 'N/A')}")
            _sep()
            lines.append("  原始內容:")
            for line in seg.get('text_content', '').split('  '):
                lines.append(f"    {line.strip()}")
        else:
            model_used = seg.get('model_used', 'N/A')
            summary = seg.get('summary', '')
            tags = seg.get('tags', [])
            original = seg.get('text_content', '')
            _sep()
            lines.append("  原文:")
            for line in original.split('  '):
                lines.append(f"    {line.strip()}")
            _sep()
            lines.append(f"  摘要（{model_used}）:")
            if summary:
                for s_line in summary.split('\n'):
                    lines.append(f"    {s_line}")
            else:
                lines.append("    (摘要失敗)")
            _sep()
            if tags:
                lines.append(f"  標籤: {', '.join(tags)}")
            else:
                lines.append("  標籤: (無)")
        _sep()
        _sep()

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    logger.info(f"報告已寫入: {output_path}")


def main():
    parser = argparse.ArgumentParser(description='TranscriptFlow 參數測試腳本')
    parser.add_argument('--file-id', type=int, required=True, help='要處理的檔案 ID')
    parser.add_argument('--output', default=None, help='輸出 .txt 路徑（預設自動產生）')
    parser.add_argument('--chunk-params', type=str, default=None,
                        help='JSON 字串，覆蓋 chunking 參數')
    parser.add_argument('--models', type=str, default=None,
                        help='JSON 陣列，指定 summarization models')
    parser.add_argument('--concurrency', type=int, default=None,
                        help='並行摘要數（預設從 config.json 讀取）')
    args = parser.parse_args()

    # 1. 載入 manifest
    logger.info(f"載入檔案 ID {args.file_id}...")
    entry = load_manifest_entry(args.file_id)
    srt_path = entry['path_srt']
    logger.info(f"SRT: {srt_path}")

    # 2. 解析 chunk 參數
    params = dict(DEFAULT_CHUNK_PARAMS)
    if args.chunk_params:
        overrides = json.loads(args.chunk_params)
        params.update(overrides)
        logger.info(f"Chunk params overridden: {overrides}")

    models = get_models()
    if args.models:
        models = json.loads(args.models)
        logger.info(f"Models overridden: {models}")

    concurrency = args.concurrency or get_env_or_config(
        'CHUNK_TEST_CONCURRENCY', 'chunk_test.concurrency', 1)

    # 3. 解析 SRT
    logger.info("解析 SRT...")
    subtitles = parse_srt(srt_path)
    logger.info(f"解析完成：{len(subtitles)} 條字幕")

    # 4. 語意分段
    logger.info("執行 Smart Merge 3.0 語意分段...")
    window_size = int(params['window_size'])
    strong_pct = float(params['strong_pct'])
    weak_pct = float(params['weak_pct'])
    min_sentences = int(params['min_sentences'])
    noise_drop_len = int(params['noise_drop_len'])
    noise_weak_len = int(params['noise_weak_len'])

    kept_chunks, failed_indices, discarded_chunks = smart_merge_3_0(
        entries=subtitles,
        file_id=args.file_id,
        window_size=window_size,
        min_sentences=min_sentences,
        high_pct=weak_pct,
        low_pct=strong_pct,
        noise_drop_len=noise_drop_len,
        noise_weak_len=noise_weak_len,
    )
    logger.info(f"分段完成：{len(kept_chunks)} 段納入，{len(discarded_chunks)} 段排除")
    if failed_indices:
        logger.warning(f"向量化失敗窗口：{len(failed_indices)} 個")

    # 5. 提取參與者
    logger.info("提取參與者...")
    participants = extract_participants(kept_chunks)
    logger.info(f"參與者: {participants}")

    # 6. 摘要每個 chunk（並行）
    logger.info(f"開始摘要 {len(kept_chunks)} 個 chunks（並行 {concurrency}，{len(models)} 個模型輪循）...")
    lock = threading.Lock()
    done_count = [0]
    total = len(kept_chunks)
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(summarize_chunk, ch, models): ch for ch in kept_chunks}
        for fut in as_completed(futures):
            ch = futures[fut]
            with lock:
                done_count[0] += 1
                status = fut.result().get('status', '?')
                logger.info(f"  [{done_count[0]}/{total}] {ch['chunk_id']} ({status})")

    done_count = sum(1 for ch in kept_chunks if ch.get('status') == 'done')
    failed_count = sum(1 for ch in kept_chunks if ch.get('status') == 'failed')
    logger.info(f"摘要完成：{done_count} 成功，{failed_count} 失敗")

    # 7. 建立合併排序的分段列表
    all_segments = []
    for ch in kept_chunks:
        all_segments.append({**ch, '_sort_key': ch.get('start_time', '')})
    for ch in discarded_chunks:
        all_segments.append({**ch, '_sort_key': ch.get('start_time', '')})
    all_segments.sort(key=lambda x: x['_sort_key'])

    # 8. 輸出 .txt 報告
    output_dir = args.output or get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_path = os.path.join(output_dir, f"{args.file_id}_test_report_{timestamp}.txt")
    write_report(args.file_id, entry, params, models, participants,
                 kept_chunks, discarded_chunks, output_path, all_segments)

    # 9. 輸出結構化 JSON（供 test suite 比對用）
    json_path = output_path.replace('.txt', '.json')
    structured = {
        "file_id": args.file_id,
        "filename": entry.get('filename_srt', ''),
        "timestamp": timestamp,
        "params": params,
        "models": models,
        "participants": participants,
        "total_kept": len(kept_chunks),
        "total_discarded": len(discarded_chunks),
        "segments": [
            {
                "chunk_id": seg.get('chunk_id'),
                "start_time": seg.get('start_time'),
                "end_time": seg.get('end_time'),
                "entry_count": seg.get('entry_count'),
                "boundary_type": seg.get('boundary_type'),
                "left_boundary": seg.get('left_boundary'),
                "right_boundary": seg.get('right_boundary'),
                "dropped": seg.get('dropped', False),
                "drop_reason": seg.get('drop_reason'),
                "text_content": seg.get('text_content'),
                "status": seg.get('status'),
                "model_used": seg.get('model_used'),
                "summary": seg.get('summary'),
                "tags": seg.get('tags'),
            }
            for seg in all_segments
        ],
        "summary_stats": {
            "done": done_count,
            "failed": failed_count,
        },
    }
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(structured, f, ensure_ascii=False, indent=2)
    logger.info(f"結構化資料已寫入: {json_path}")
    logger.info(f"全部完成！")


if __name__ == '__main__':
    main()
