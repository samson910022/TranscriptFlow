"""
srt_quality_check.py — SRT 字幕品質檢查（滑窗評分）

用法:
    python3 scripts/srt_quality_check.py --file-id 3
    python3 scripts/srt_quality_check.py --input sample.srt
    python3 scripts/srt_quality_check.py --file-id 3 --interactive
    python3 scripts/srt_quality_check.py --file-id 3 --output /tmp/report.txt
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

logger = get_logger('srt_quality_check')

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = 3
TIMEOUT_SEC = 120


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


def call_llm_for_scoring(window_text: str, start_idx: int) -> dict:
    prompt = f"""你是一個字幕品質審查員。以下是某影片字幕的一小段（{start_idx + 1} 行），每行前面有編號和時間戳。

請針對此段落的整體品質評分（0-25 each，總分 100）：

1. 連貫性: 語句之間是否流暢銜接、主題是否一致
2. 邏輯合理性: 對話邏輯是否合理、有無矛盾或不通順處
3. 語句品質: 有無錯字、語法錯誤、轉錄錯誤
4. 時間合理性: 時間戳長度與對白量是否合理

另外，請標記有問題的具體行號（從 1 開始），以及每個問題的嚴重程度（major/minor）。

字幕段落：
{window_text}

回覆 JSON 格式：
{{
  "scores": {{
    "連貫性": {{"score": N, "reason": "..."}},
    "邏輯合理性": {{"score": N, "reason": "..."}},
    "語句品質": {{"score": N, "reason": "..."}},
    "時間合理性": {{"score": N, "reason": "..."}}
  }},
  "total_score": N,
  "flagged_entries": {{
    "3": {{"severity": "major", "issue": "語意不完整，疑似轉錄斷句錯誤"}},
    "7": {{"severity": "minor", "issue": "時間戳與前一句差距過大"}}
  }}
}}"""

    models = get_env_or_config('SRT_QUALITY_MODELS', 'srt_quality.models', None)
    if models is None:
        models = get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"}

    for attempt in range(1, MAX_RETRIES + 1):
        model = models[(attempt - 1) % len(models)]
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一個專業的字幕品質審查員。請嚴格但公正地評估字幕品質。"},
                {"role": "user", "content": prompt},
            ],
            "timeout": TIMEOUT_SEC,
        }
        try:
            import requests
            resp = requests.post(f"{API_BASE_URL.rstrip('/')}{CHAT_ENDPOINT}",
                                 headers=headers, json=payload, timeout=TIMEOUT_SEC + 10)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError("null content")
            content = content.strip()
            m = re.search(r'\{.*\}', content, re.DOTALL)
            data = json.loads(m.group()) if m else json.loads(content)
            # 驗證回傳格式
            flagged = data.get('flagged_entries', {})
            for key in flagged:
                int(key)  # 非數字 key → raise ValueError
            for dim in ['連貫性', '邏輯合理性', '語句品質', '時間合理性']:
                if 'score' not in data.get('scores', {}).get(dim, {}):
                    raise ValueError(f"missing score for {dim}")
            return data
        except Exception as e:
            logger.warning(f"LLM call attempt {attempt} failed: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    return {"scores": {}, "total_score": 0, "flagged_entries": {}}


def present_interactive(srt_path: str, flagged_entries: dict, suggestions: dict):
    """Interactive mode: show each flagged entry and ask user what to do."""
    import readline
    entries = parse_srt(srt_path)
    print("\n=== 互動修正模式 ===\n")
    for idx, entry in enumerate(entries):
        flag = flagged_entries.get(str(idx), {})
        if not flag:
            continue
        entry_num = idx + 1
        severity = flag.get('severity', 'minor')
        issue = flag.get('issue', '')
        tag = "🔴" if severity == "major" else "🟡"
        print(f"{tag} 第 {entry_num} 行 [{entry.start_time} → {entry.end_time}]")
        print(f"   原文: {entry.text}")
        print(f"   問題: {issue}")
        if suggestions and str(idx) in suggestions:
            print(f"   建議: {suggestions[str(idx)]}")
        while True:
            action = input("   [r]替換 / [e]編輯 / [k]保留 / [q]離開: ").strip().lower()
            if action == 'r':
                print(f"   已替換為: {suggestions.get(str(idx), entry.text)}")
                break
            elif action == 'e':
                new_text = input("   請輸入新內容: ").strip()
                if new_text:
                    print(f"   已更新")
                break
            elif action == 'k':
                break
            elif action == 'q':
                return
    print("\n互動完成。")


def build_windows(entries, window_size=10, stride=5):
    windows = []
    for i in range(0, len(entries), stride):
        win = entries[i:i + window_size]
        if len(win) < window_size // 2:
            break
        windows.append((i, win))
    return windows


def format_window(entries, start_idx):
    lines = []
    for offset, e in enumerate(entries):
        lines.append(f"[{start_idx + offset + 1}] {e.start_time} → {e.end_time}: {e.text}")
    return '\n'.join(lines)


def aggregate_flags(windows_results: list, window_size=10, stride=5):
    entry_flags = defaultdict(list)
    for win_result in windows_results:
        win_start = win_result['window_start']
        flagged = win_result.get('flagged_entries', {})
        for rel_idx_str, info in flagged.items():
            try:
                rel_idx = int(rel_idx_str) - 1
            except ValueError:
                continue
            abs_idx = win_start + rel_idx
            entry_flags[abs_idx].append(info)

    summary = {}
    for idx, flags in entry_flags.items():
        major_count = sum(1 for f in flags if f.get('severity') == 'major')
        minor_count = sum(1 for f in flags if f.get('severity') != 'major')
        total_count = len(flags)
        if total_count >= 2 or major_count >= 1:
            summary[idx] = {'status': 'problem', 'count': total_count, 'flags': flags}
        else:
            summary[idx] = {'status': 'questionable', 'count': total_count, 'flags': flags}
    return summary


def main():
    parser = argparse.ArgumentParser(description='SRT 字幕品質檢查')
    parser.add_argument('--file-id', type=int, default=None)
    parser.add_argument('--input', default=None)
    parser.add_argument('--output', default=None)
    parser.add_argument('--interactive', action='store_true', help='互動修正模式')
    parser.add_argument('--window-size', type=int, default=10)
    parser.add_argument('--stride', type=int, default=5)
    parser.add_argument('--concurrency', type=int, default=None,
                        help='並行 LLM 呼叫數（預設從 config.json 讀取）')
    args = parser.parse_args()
    if args.concurrency is None:
        args.concurrency = get_env_or_config(
            'SRT_QUALITY_CONCURRENCY', 'srt_quality.concurrency', 5)

    # 載入 SRT
    if args.file_id is not None:
        srt_path, title = load_manifest_entry(args.file_id)
    elif args.input:
        srt_path = args.input
        title = os.path.basename(srt_path)
    else:
        print("❌ 請指定 --file-id 或 --input")
        sys.exit(1)

    print(f"載入: {title}")
    entries = parse_srt(srt_path)
    print(f"解析完成：{len(entries)} 行字幕")

    # 建立滑窗
    windows = build_windows(entries, args.window_size, args.stride)
    print(f"滑窗：{len(windows)} 個 (window_size={args.window_size}, stride={args.stride})\n")

    # 並行送 LLM 評分
    windows_results = []
    total_windows = len(windows)
    done_count = 0
    lock = __import__('threading').Lock()

    def process_one_window(win_start, win_entries):
        window_text = format_window(win_entries, win_start)
        result = call_llm_for_scoring(window_text, win_start)
        result['window_start'] = win_start
        return result

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(process_one_window, ws, we): (ws, we) for ws, we in windows}
        for fut in as_completed(futures):
            result = fut.result()
            windows_results.append(result)
            with lock:
                done_count += 1
                total = result.get('total_score', 0)
                flagged = len(result.get('flagged_entries', {}))
                ws = result.get('window_start', 0)
                we = ws + args.window_size - 1
                print(f"  [{done_count}/{total_windows}] window {ws + 1}~{we + 1}: "
                      f"總分 {total}/100, 標記 {flagged} 行", flush=True)

    # 彙整跨窗標記
    flagged_summary = aggregate_flags(windows_results, args.window_size, args.stride)
    problem_count = sum(1 for v in flagged_summary.values() if v['status'] == 'problem')
    question_count = sum(1 for v in flagged_summary.values() if v['status'] == 'questionable')

    # 輸出報告
    report_lines = []
    _sep = lambda: report_lines.append('')
    _hline = lambda: report_lines.append('=' * 80)

    _hline()
    report_lines.append(f"{'SRT 品質檢查報告':^80}")
    _hline()
    report_lines.append(f"  檔案: {title}")
    report_lines.append(f"  總行數: {len(entries)}")
    report_lines.append(f"  滑窗: {len(windows)} 個 (window={args.window_size}, stride={args.stride})")
    _sep()

    # 整體分數
    if windows_results:
        avg_total = sum(r.get('total_score', 0) for r in windows_results) / len(windows_results)
        avg_scores = {}
        for dim in ['連貫性', '邏輯合理性', '語句品質', '時間合理性']:
            vals = [r.get('scores', {}).get(dim, {}).get('score', 0) for r in windows_results if r.get('scores')]
            avg_scores[dim] = sum(vals) / len(vals) if vals else 0
        report_lines.append(f"  整體平均分數: {avg_total:.1f}/100")
        for dim, sc in avg_scores.items():
            report_lines.append(f"    {dim}: {sc:.1f}/25")
        _sep()

    report_lines.append(f"  標記結果: 🔴 {problem_count} 個問題, 🟡 {question_count} 個存疑")
    _sep()

    # 逐行標記
    _hline()
    report_lines.append(f"{'逐行標記':^80}")
    _hline()
    for idx, entry in enumerate(entries):
        flag = flagged_summary.get(idx)
        if flag:
            tag = "🔴" if flag['status'] == 'problem' else "🟡"
            issues = []
            for f in flag.get('flags', []):
                sev = f.get('severity', 'minor')
                iss = f.get('issue', '')
                if iss:
                    issues.append(f"[{sev}] {iss}")
            report_lines.append(f"  {tag} 第 {idx + 1:>4}行 [{entry.start_time}→{entry.end_time}]:")
            report_lines.append(f"      原文: {entry.text[:80]}{'...' if len(entry.text) > 80 else ''}")
            for iss in issues[:2]:
                report_lines.append(f"      → {iss}")
        else:
            report_lines.append(f"  ✅ 第 {idx + 1:>4}行")

    _sep()

    report = '\n'.join(report_lines)
    print(f"\n{'=' * 60}")
    print(report)

    # 輸出到檔案
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n報告已寫入: {args.output}")

    # 互動模式
    if args.interactive:
        suggestions = {}
        for wr in windows_results:
            for rel_idx_str, info in wr.get('flagged_entries', {}).items():
                abs_idx = wr['window_start'] + int(rel_idx_str) - 1
                if info.get('suggestion') and str(abs_idx) not in suggestions:
                    suggestions[str(abs_idx)] = info['suggestion']
        present_interactive(srt_path, {str(k): v['flags'][0] for k, v in flagged_summary.items()}, suggestions)


if __name__ == '__main__':
    main()
