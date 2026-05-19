"""
srt_quality_check.py — SRT 字幕品質檢查（滑窗評分 + 多 group + 共識合併）

用法:
    python3 scripts/srt_quality_check.py --file-id 3
    python3 scripts/srt_quality_check.py --file-id 0-2
    python3 scripts/srt_quality_check.py --input sample.srt
    python3 scripts/srt_quality_check.py --file-id 3 --interactive
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import get_api_config, get_env_or_config, get_nested_config
from parse_srt import parse_srt
from logger_config import get_logger
from llm_client import call_llm as _call_llm

logger = get_logger('srt_quality_check')

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = 3
TIMEOUT_SEC = 120


# ---------------------------------------------------------------------------
# Manifest 載入
# ---------------------------------------------------------------------------

def load_manifest_entry(file_id: int) -> str:
    master_path = get_env_or_config('SRT_MASTER_FILE', 'paths.master_file',
                                     './examples/master_file_manifest.example.json')
    with open(master_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    files = manifest.get('files', [])
    for entry in files:
        if entry.get('id') == file_id:
            path = entry.get('path_srt')
            if not os.path.exists(path):
                raise FileNotFoundError(f"SRT not found: {path}")
            title = entry.get('filename_srt', str(file_id))
            return path, title
    raise ValueError(f"File ID {file_id} not found in manifest")


# ---------------------------------------------------------------------------
# 滑窗 & LLM 調用
# ---------------------------------------------------------------------------

def build_windows(entries, window_size=10, stride=5):
    windows = []
    for i in range(0, len(entries), stride):
        win = entries[i:i + window_size]
        if len(win) < window_size // 2:
            break
        windows.append((i, win))

    if len(entries) > window_size:
        head_end = min(window_size + stride, len(entries))
        if head_end > window_size:
            windows.append((0, entries[0:head_end]))
        tail_start = max(0, len(entries) - window_size - stride)
        if tail_start > 0:
            windows.append((tail_start, entries[tail_start:len(entries)]))

    return windows


def format_window(entries, start_idx):
    lines = []
    for offset, e in enumerate(entries):
        lines.append(f"[{offset + 1}] {e.start_time} \u2192 {e.end_time}: {e.text}")
    return '\n'.join(lines)


def call_llm_single(window_text: str, model: str) -> dict:
    start_time = time.time()
    json_example = '''{
  "flagged_entries": {
    "3": {"severity": "major", "issue": "\u539f\u6587:'...' \u932f\u5b57\u6216\u8f49\u9304\u932f\u8aa4"}
  }
}'''

    prompt = f"""你是一個字幕審查助手。以下是某段對話字幕。

只抓兩種問題：
1. 內容明顯不合邏輯、前後矛盾、完全無法理解
2. 錯字或轉錄錯誤導致文意嚴重偏離

填充詞、不完整句子、口語重複都是正常的對話特徵，請忽略。

字幕：
{window_text}

請以 JSON 格式回覆，key 為行號（從 1 開始），每個 entry 包含 severity（"major"/"minor"）和 issue 描述：
{json_example}"""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = _call_llm(prompt=prompt, models=[model],
                             system_prompt="你是一個專業的字幕品質審查員。請嚴格但公正地找出有問題的行。")
            flagged = data.get('flagged_entries', {})
            for key in flagged:
                int(key)
            return {"flagged_entries": flagged, "elapsed": time.time() - start_time}
        except Exception as e:
            logger.warning(f"LLM call attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return {"flagged_entries": {}, "elapsed": time.time() - start_time}


def run_one_group(windows, models, concurrency, group_label='group0', window_size=10):
    from collections import defaultdict

    total_tasks = len(windows) * len(models)
    done_count = 0
    lock = __import__('threading').Lock()
    results_by_window = defaultdict(list)
    num_entries_by_window = {}
    model_stats = defaultdict(lambda: {"total_elapsed": 0.0, "count": 0})

    def process_one_task(win_start, win_entries, model):
        window_text = format_window(win_entries, win_start)
        result = call_llm_single(window_text, model)
        return {
            'window_start': win_start,
            'model': model,
            'flagged_entries': result.get('flagged_entries', {}),
            'num_entries': len(win_entries),
            'elapsed': result.get('elapsed', 0.0)
        }

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {}
        for win_start, win_entries in windows:
            num_entries_by_window[win_start] = len(win_entries)
            for model in models:
                fut = ex.submit(process_one_task, win_start, win_entries, model)
                futures[fut] = (win_start, model)

        for fut in as_completed(futures):
            result = fut.result()
            ws = result['window_start']
            results_by_window[ws].append(result['flagged_entries'])
            m = result['model']
            model_stats[m]["total_elapsed"] += result['elapsed']
            model_stats[m]["count"] += 1
            with lock:
                done_count += 1
                num_entries = result['num_entries']
                we = ws + num_entries - 1
                print(f"  [{done_count}/{total_tasks}] [{group_label}] "
                      f"window {ws + 1}~{we + 1}", flush=True)

    windows_results = []
    for ws in sorted(results_by_window):
        merged = {}
        for flagged in results_by_window[ws]:
            for k, v in flagged.items():
                if k not in merged or v.get('severity') == 'major':
                    merged[k] = v
        windows_results.append({
            'window_start': ws,
            'group_label': group_label,
            'flagged_entries': merged,
            'num_entries': num_entries_by_window.get(ws, window_size)
        })

    return {"windows_results": windows_results, "model_stats": dict(model_stats)}


# ---------------------------------------------------------------------------
# 覆蓋次數計算
# ---------------------------------------------------------------------------

def line_coverage_counts(total_entries, windows):
    counts = [0] * total_entries
    for start, win_entries in windows:
        for offset in range(len(win_entries)):
            idx = start + offset
            if idx < total_entries:
                counts[idx] += 1
    return counts


# ---------------------------------------------------------------------------
# 彙整標記 & 寫報告
# ---------------------------------------------------------------------------

def aggregate_flags(windows_results: list, window_size=10, stride=5):
    entry_flags = defaultdict(list)
    for win_result in windows_results:
        win_start = win_result['window_start']
        group_label = win_result.get('group_label', 'unknown')
        flagged = win_result.get('flagged_entries', {})
        win_len = win_result.get('num_entries', window_size)
        for rel_idx_str, info in flagged.items():
            try:
                rel_idx = int(rel_idx_str) - 1
            except ValueError:
                continue
            if rel_idx < 0 or rel_idx >= win_len:
                continue
            abs_idx = win_start + rel_idx
            entry_flags[abs_idx].append({**info, '_group': group_label})

    summary = {}
    for idx, flags in entry_flags.items():
        major_count = sum(1 for f in flags if f.get('severity') == 'major')
        total_count = len(flags)
        if total_count >= 2 or major_count >= 1:
            summary[idx] = {'status': 'problem', 'count': total_count, 'flags': flags}
        else:
            summary[idx] = {'status': 'questionable', 'count': total_count, 'flags': flags}
    return summary


def write_group_report(entries, windows_results, title, window_size, stride, output_path):
    flagged_summary = aggregate_flags(windows_results, window_size, stride)
    problem_count = sum(1 for v in flagged_summary.values() if v['status'] == 'problem')
    question_count = sum(1 for v in flagged_summary.values() if v['status'] == 'questionable')

    report_lines = []
    _sep = lambda: report_lines.append('')
    _hline = lambda: report_lines.append('=' * 80)

    _hline()
    report_lines.append(f"{'SRT \u54c1\u8cea\u6aa2\u67e5\u5831\u544a':^80}")
    _hline()
    report_lines.append(f"  \u6a94\u6848: {title}")
    report_lines.append(f"  \u7e3d\u884c\u6578: {len(entries)}")
    report_lines.append(f"  \u6ed1\u7a97: {len(windows_results)} \u500b (window={window_size}, stride={stride})")
    _sep()
    report_lines.append(f"  \u6a19\u8a18\u7d50\u679c: \U0001f534 {problem_count} \u500b\u554f\u984c, \U0001f7e1 {question_count} \u500b\u5b58\u7591")
    _sep()
    _hline()
    report_lines.append(f"{'\u9010\u884c\u6a19\u8a18':^80}")
    _hline()

    for idx, entry in enumerate(entries):
        flag = flagged_summary.get(idx)
        if flag:
            tag = "\U0001f534" if flag['status'] == 'problem' else "\U0001f7e1"
            issues = []
            for f in flag.get('flags', []):
                sev = f.get('severity', 'minor')
                iss = f.get('issue', '')
                if iss:
                    issues.append(f"[{sev}] {iss}")
            report_lines.append(f"  {tag} \u7b2c {idx + 1:>4}\u884c [{entry.start_time}\u2192{entry.end_time}]:")
            report_lines.append(f"      \u539f\u6587: {entry.text[:80]}{'...' if len(entry.text) > 80 else ''}")
            for iss in issues[:2]:
                report_lines.append(f"      \u2192 {iss}")
        else:
            report_lines.append(f"  \u2705 \u7b2c {idx + 1:>4}\u884c")

    _sep()
    report = '\n'.join(report_lines)
    print(f"\n{'=' * 60}")
    print(report)

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n\u5831\u544a\u5df2\u5beb\u5165: {output_path}")
    except Exception as e:
        logger.warning(f"\u5beb\u5165\u5831\u544a\u5931\u6557: {e}")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_model_groups():
    groups = get_nested_config('srt_quality.model_groups', None)
    if groups and isinstance(groups, list) and len(groups) > 0:
        return groups
    models = get_env_or_config('SRT_QUALITY_MODELS', 'srt_quality.models', None)
    if models is None:
        models = get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])
    return [models]


def get_consensus_thresholds():
    th = get_nested_config('srt_quality.consensus_thresholds', [0.2, 0.6])
    if isinstance(th, list) and len(th) >= 2:
        return float(th[0]), float(th[1])
    return 0.2, 0.6


# ---------------------------------------------------------------------------
# 合併報告
# ---------------------------------------------------------------------------

def write_merged_report(entries, all_group_results, group_labels, models_list,
                        title, window_size, stride, windows, output_path,
                        group_timings=None, model_stats_list=None,
                        group_concurrencies=None, total_elapsed=None):
    low_th, high_th = get_consensus_thresholds()

    cov_counts = line_coverage_counts(len(entries), windows)

    report_lines = []
    _sep = lambda: report_lines.append('')
    _hline = lambda: report_lines.append('=' * 80)

    _hline()
    report_lines.append(f"{'SRT \u54c1\u8cea\u6aa2\u67e5\u5831\u544a (\u5171\u8b58\u5408\u4f75)':^80}")
    _hline()
    report_lines.append(f"  \u6a94\u6848: {title}")
    report_lines.append(f"  \u7e3d\u884c\u6578: {len(entries)}")
    report_lines.append(f"  \u7d44\u6578: {len(group_labels)}")
    report_lines.append(f"  \u6ed1\u7a97: window={window_size}, stride={stride}")
    report_lines.append(f"  \u5171\u8b58\u9608\u503c: \U0001f7e1{low_th*100:.0f}% / \U0001f534{high_th*100:.0f}%")
    _sep()

    if group_timings and model_stats_list and group_concurrencies:
        _hline()
        report_lines.append(f"{'\u7d71\u8a08':^80}")
        _hline()
        for g_idx, label in enumerate(group_labels):
            models_raw = models_list[g_idx]
            unique_models = list(dict.fromkeys(models_raw))
            cc = group_concurrencies[g_idx] if g_idx < len(group_concurrencies) else '?'
            elapsed = group_timings[g_idx] if g_idx < len(group_timings) else 0
            report_lines.append(f"  {label} (models: {', '.join(unique_models)} | concurrency={cc}):")
            report_lines.append(f"    group \u82b1\u8cbb: {elapsed:.1f}s")
            stats = model_stats_list[g_idx] if g_idx < len(model_stats_list) else {}
            for m in unique_models:
                ms = stats.get(m, {})
                cnt = ms.get('count', 0)
                avg = ms.get('total_elapsed', 0) / cnt if cnt > 0 else 0
                report_lines.append(f"    {m}: avg {avg:.2f}s, \u5b8c\u6210 {cnt} \u6bb5")
            _sep()
        if total_elapsed is not None:
            report_lines.append(f"  \u7e3d\u8017\u6642: {total_elapsed:.1f}s")
        _sep()

    total_problem = 0
    total_question = 0

    _hline()
    report_lines.append(f"{'\u9010\u884c\u5171\u8b58':^80}")
    _hline()

    for idx, entry in enumerate(entries):
        total_inspections = cov_counts[idx] * len(group_labels)

        # Collect all flags for this line from all groups
        per_group_items = defaultdict(list)
        flagged_count = 0
        for g_idx, grp_results in enumerate(all_group_results):
            for win_res in grp_results:
                win_start = win_res['window_start']
                flagged = win_res.get('flagged_entries', {})
                win_len = win_res.get('num_entries', window_size)
                rel_idx = idx - win_start
                if 0 <= rel_idx < win_len:
                    info = flagged.get(str(rel_idx + 1))
                    if info:
                        per_group_items[g_idx].append(info)
                        flagged_count += 1

        consensus = flagged_count / total_inspections if total_inspections > 0 else 0

        if consensus < low_th:
            tag = "\u2705"
        elif consensus < high_th:
            tag = "\U0001f7e1"
            total_question += 1
        else:
            tag = "\U0001f534"
            total_problem += 1

        report_lines.append(f"  {tag} \u7b2c {idx + 1:>4}\u884c [{entry.start_time}\u2192{entry.end_time}]:")
        report_lines.append(f"      \u539f\u6587: {entry.text[:80]}{'...' if len(entry.text) > 80 else ''}")

        for g_idx in range(len(group_labels)):
            items = per_group_items.get(g_idx, [])
            model_name = models_list[g_idx][0] if models_list[g_idx] else str(g_idx)
            if items:
                for item in items:
                    sev = item.get('severity', 'minor')
                    iss = item.get('issue', '')
                    report_lines.append(f"      group{g_idx} ({model_name}): \u2192 [{sev}] {iss}")
            else:
                report_lines.append(f"      group{g_idx} ({model_name}): \u2192 (\u672a\u6a19\u8a18)")

        pct = consensus * 100
        report_lines.append(f"      \u5171\u8b58\u5ea6: {flagged_count}/{total_inspections} ({pct:.1f}%) \u2192 {tag}")
        _sep()

    _sep()
    report_lines.append(f"  \u7e3d\u8a08: \U0001f534 {total_problem} \u500b\u554f\u984c, \U0001f7e1 {total_question} \u500b\u5b58\u7591")
    _sep()

    report = '\n'.join(report_lines)
    print(f"\n{'=' * 60}")
    print(report)

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n\u5408\u4f75\u5831\u544a\u5df2\u5beb\u5165: {output_path}")
    except Exception as e:
        logger.warning(f"\u5beb\u5165\u5408\u4f75\u5831\u544a\u5931\u6557: {e}")


# ---------------------------------------------------------------------------
# 批次 ID 解析
# ---------------------------------------------------------------------------

def parse_file_id_spec(spec):
    if isinstance(spec, int):
        return [spec]
    s = str(spec).strip()
    if '-' in s:
        parts = s.split('-', 1)
        try:
            start, end = int(parts[0]), int(parts[1])
            return list(range(start, end + 1))
        except ValueError:
            pass
    try:
        return [int(s)]
    except ValueError:
        raise ValueError(f"\u7121\u6cd5\u89e3\u6790 file_id \u683c\u5f0f: {spec}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def process_one_file(file_id, args):
    srt_path, title = load_manifest_entry(file_id)
    print(f"\n{'=' * 60}")
    print(f"\u8f09\u5165 ID {file_id}: {title}")
    entries = parse_srt(srt_path)
    print(f"\u89e3\u6790\u5b8c\u6210\uff1a{len(entries)} \u884c\u5b57\u5e55")

    windows = build_windows(entries, args.window_size, args.stride)
    total_windows = len(windows)
    print(f"\u6ed1\u7a97\uff1a{total_windows} \u500b (window_size={args.window_size}, stride={args.stride})\n")

    model_groups = get_model_groups()
    num_groups = len(model_groups)
    threshold_low, threshold_high = get_consensus_thresholds()

    if isinstance(args.concurrency, list):
        group_concurrencies = args.concurrency
    else:
        group_concurrencies = [args.concurrency] * num_groups

    base_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
    report_dir = os.path.join(base_dir, 'srt_quality_report')
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    all_group_results = [None] * num_groups
    group_labels = [f'group{i}' for i in range(num_groups)]
    group_elapsed = [0.0] * num_groups
    all_model_stats = [None] * num_groups
    file_start_time = time.time()

    with ThreadPoolExecutor(max_workers=num_groups) as ex:
        futures = {}
        group_start_times = {}
        for g_idx, models in enumerate(model_groups):
            grp_cc = group_concurrencies[g_idx] if g_idx < len(group_concurrencies) else group_concurrencies[-1]
            print(f"\n--- {group_labels[g_idx]} \u63d0\u4ea4\u57f7\u884c (models={models}, concurrency={grp_cc}) ---")
            group_start_times[g_idx] = time.time()
            fut = ex.submit(run_one_group, windows, models, grp_cc, group_labels[g_idx], args.window_size)
            futures[fut] = g_idx

        for fut in as_completed(futures):
            g_idx = futures[fut]
            label = group_labels[g_idx]
            grp_result = fut.result()
            group_elapsed[g_idx] = time.time() - group_start_times[g_idx]
            grp_windows = grp_result["windows_results"]
            all_model_stats[g_idx] = grp_result["model_stats"]
            all_group_results[g_idx] = grp_windows
            print(f"\n>>> {label} \u5b8c\u6210 ({group_elapsed[g_idx]:.1f}s) <<<")

            if num_groups > 1:
                group_dir = os.path.join(report_dir, 'group_raw')
                os.makedirs(group_dir, exist_ok=True)
                group_output = os.path.join(group_dir, f"{file_id}_srt_quality_report_{label}_{timestamp}.txt")
            else:
                group_output = os.path.join(report_dir, f"{file_id}_srt_quality_report_{timestamp}.txt")
            write_group_report(entries, grp_windows, title, args.window_size, args.stride, group_output)

    if num_groups > 1 and all(r is not None for r in all_group_results):
        total_elapsed = time.time() - file_start_time
        merged_output = os.path.join(report_dir, f"{file_id}_srt_quality_report_merged_{timestamp}.txt")
        write_merged_report(entries, all_group_results, group_labels, model_groups,
                            title, args.window_size, args.stride, windows, merged_output,
                            group_timings=group_elapsed, model_stats_list=all_model_stats,
                            group_concurrencies=group_concurrencies, total_elapsed=total_elapsed)


def main():
    parser = argparse.ArgumentParser(description='SRT \u5b57\u5e55\u54c1\u8cea\u6aa2\u67e5')
    parser.add_argument('--file-id', type=str, default=None)
    parser.add_argument('--input', default=None)
    parser.add_argument('--output', default=None)
    parser.add_argument('--interactive', action='store_true', help='\u4e92\u52d5\u4fee\u6b63\u6a21\u5f0f')
    parser.add_argument('--window-size', type=int, default=None)
    parser.add_argument('--stride', type=int, default=None)
    parser.add_argument('--concurrency', type=int, default=None,
                        help='\u4e26\u884c LLM \u547c\u53eb\u6578\uff08\u5982\u6709\u591a\u7d44\u5247\u4f7f\u7528 config.json \u7684\u6578\u7d44\uff09')
    args = parser.parse_args()

    if args.concurrency is None:
        raw_cc = get_env_or_config(
            'SRT_QUALITY_CONCURRENCY', 'srt_quality.concurrency', 5)
        if isinstance(raw_cc, list):
            args.concurrency = [int(c) for c in raw_cc]
        else:
            args.concurrency = int(raw_cc)
    else:
        args.concurrency = int(args.concurrency)
    if args.window_size is None:
        args.window_size = get_env_or_config(
            'SRT_QUALITY_WINDOW_SIZE', 'srt_quality.window_size', 10)
    if args.stride is None:
        args.stride = get_env_or_config(
            'SRT_QUALITY_STRIDE', 'srt_quality.stride', 5)

    if args.file_id is not None:
        file_ids = parse_file_id_spec(args.file_id)
        for fid in file_ids:
            process_one_file(fid, args)
    elif args.input:
        # 單一檔案模式（無 manifest）
        print("\u55ae\u4e00\u6a94\u6848\u6a21\u5f0f\u4e0d\u652f\u63f4\u591a group \u5408\u4f75")
        srt_path = args.input
        title = os.path.basename(srt_path)
        entries = parse_srt(srt_path)
        print(f"\u89e3\u6790\u5b8c\u6210\uff1a{len(entries)} \u884c\u5b57\u5e55")
        windows = build_windows(entries, args.window_size, args.stride)
        models = get_model_groups()[0]
        single_cc = args.concurrency[0] if isinstance(args.concurrency, list) else args.concurrency
        grp_results = run_one_group(windows, models, single_cc, 'group0', args.window_size)
        grp_windows = grp_results["windows_results"]
        base_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', './output')
        report_dir = os.path.join(base_dir, 'srt_quality_report')
        os.makedirs(report_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = args.output or os.path.join(report_dir, f"input_srt_quality_report_{timestamp}.txt")
        write_group_report(entries, grp_windows, title, args.window_size, args.stride, output_path)
    else:
        print("\u274c \u8acb\u6307\u5b9a --file-id \u6216 --input")
        sys.exit(1)


if __name__ == '__main__':
    main()
