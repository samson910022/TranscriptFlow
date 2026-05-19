"""
evaluate_chunks.py — B+ 方案：LLM 評估 Chunk 品質（邊界 + 雜訊處理）

用法:
    python3 scripts/evaluate_chunks.py --reports /path/to/output/0_20260513_123228/*.json
    python3 scripts/evaluate_chunks.py --reports /path/to/dir
    python3 scripts/evaluate_chunks.py --reports /path/to/dir --sample 5
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import get_api_config, get_env_or_config
from logger_config import get_logger
from llm_client import call_llm as _call_llm

logger = get_logger('evaluate_chunks')

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = 3
TIMEOUT_SEC = 120


def collect_reports(paths: list) -> dict:
    """Collect JSON report files, group by config label."""
    json_files = []
    for p in paths:
        if os.path.isfile(p) and p.endswith('.json'):
            json_files.append(p)
        elif os.path.isdir(p):
            json_files.extend(sorted(glob.glob(os.path.join(p, '*.json'))))
        elif os.path.isfile(p):
            pass  # skip non-json

    groups = defaultdict(list)
    for jf in sorted(json_files):
        with open(jf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if 'segments' not in data:
            continue
        label = None
        # Try to infer label from file path
        basename = os.path.basename(jf)
        fname = os.path.splitext(basename)[0]
        fname = fname.replace('_test_report', '').replace(f"_{data.get('file_id', '')}", '').lstrip('_')
        # Find matching label by scanning suite-like parent filenames
        parts = basename.replace('.json', '').split('_')
        if len(parts) >= 4:
            label = '_'.join(parts[2:])  # heuristic: after "ID_test_report_timestamp"
        if not label or label == fname.split('_')[-1]:
            label = 'unknown'
        groups[label].append(data)
    return dict(groups)


def call_llm_evaluate(chunk: dict) -> dict:
    is_dropped = chunk.get('dropped', False)
    lb = chunk.get('left_boundary', {})
    rb = chunk.get('right_boundary', {})

    status_tag = "❌ 已排除" if is_dropped else "✅ 已納入"
    boundary_info = (
        f"  左邊界: {lb.get('label', '-')} (cosine {lb.get('cosine', '-')})\n"
        f"  右邊界: {rb.get('label', '-')} (cosine {rb.get('cosine', '-')})"
    )

    prompt = f"""你是一個語意分段品質評估員。以下是 Smart Merge 3.0 演算法產出的一個段落：

狀態: {status_tag}
時間: {chunk.get('start_time', '')} → {chunk.get('end_time', '')}
行數: {chunk.get('entry_count', '')}
邊界類型: {chunk.get('boundary_type', '')}
{boundary_info}

段落內容:
{chunk.get('text_content', '')}

評分標準（總分 30）：

1. 內部連貫性 (0-10): 段落內的主題是否一致、語句是否連貫
2. 斷點正確性 (0-10): 此段落的起點和終點是否為合理的斷點位置
3. 雜訊處理 (0-10): 該丟的／不該丟的判斷是否合理

請以 JSON 格式回覆：
{{
  "boundary_coherence": {{"score": N, "reason": "..."}},
  "boundary_correctness": {{"score": N, "reason": "..."}},
  "noise_handling": {{"score": N, "reason": "..."}},
  "total_score": N,
  "note": "..."
}}"""

    models = get_env_or_config('EVALUATION_CHUNK_MODELS', 'evaluation.chunk_models', None)
    if models is None:
        models = get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])

    try:
        data = _call_llm(prompt=prompt, models=models,
                         system_prompt="你是專業的語意分段品質評估員。請嚴格但公正地評分。")
        data['chunk_id'] = chunk.get('chunk_id', '')
        data['dropped'] = is_dropped
        return data
    except Exception as e:
        logger.warning(f"Evaluate chunk failed: {e}")
        return {"chunk_id": chunk.get('chunk_id', ''), "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description='B+ 方案：LLM 評估 Chunk 品質')
    parser.add_argument('--reports', nargs='+', required=True,
                        help='JSON report 路徑或目錄')
    parser.add_argument('--output', default=None, help='輸出目錄')
    parser.add_argument('--sample', type=int, default=0,
                        help='每個 config 取樣 N 個 chunks 評分（0=全部）')
    parser.add_argument('--dir', type=str, default=None, help='支援舊版 --dir 參數')
    args = parser.parse_args()

    groups = collect_reports(args.reports)
    if not groups:
        print("❌ 未找到任何有效的 JSON report")
        sys.exit(1)

    print(f"找到 {len(groups)} 個 config 群組:")
    for label, reports in groups.items():
        print(f"  {label}: {len(reports)} 個 report 檔案")

    output_dir = args.output
    if not output_dir:
        base_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', '.')
        output_dir = os.path.join(base_dir, 'chunk_evaluation')
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    all_results = []
    for label, reports in groups.items():
        print(f"\n{'=' * 60}")
        print(f"  評估: {label}")
        print(f"{'=' * 60}")

        # Collect all segments from all reports for this label
        all_segs = []
        for r in reports:
            all_segs.extend(r.get('segments', []))
        print(f"  共 {len(all_segs)} 個 segments")

        if args.sample > 0 and len(all_segs) > args.sample:
            import random
            random.seed(42)
            all_segs = random.sample(all_segs, args.sample)
            print(f"  取樣 {args.sample} 個")

        label_scores = {"boundary_coherence": [], "boundary_correctness": [], "noise_handling": [], "total": []}
        label_chunks = []
        for idx, seg in enumerate(all_segs):
            print(f"  [{idx + 1}/{len(all_segs)}] {seg.get('chunk_id', '?')}...", end=' ', flush=True)
            result = call_llm_evaluate(seg)
            label_chunks.append(result)
            if 'error' not in result:
                for key in ['boundary_coherence', 'boundary_correctness', 'noise_handling']:
                    s = result.get(key, {}).get('score', 0)
                    label_scores[key].append(s)
                label_scores['total'].append(result.get('total_score', 0))
                print(f"總分 {result.get('total_score', 0)}")
            else:
                print(f"❌ {result['error'][:60]}")

        all_results.append({
            'label': label,
            'chunks': label_chunks,
            'scores': {
                k: (sum(v) / len(v) if v else 0) for k, v in label_scores.items()
            },
            'chunk_count': len(label_chunks),
        })
        s = all_results[-1]['scores']
        print(f"  → {label} 平均: "
              f"連貫性 {s.get('boundary_coherence', 0):.1f} | "
              f"斷點 {s.get('boundary_correctness', 0):.1f} | "
              f"雜訊 {s.get('noise_handling', 0):.1f} | "
              f"總分 {s.get('total', 0):.1f}")

    # 產出比對報告
    report_lines = []
    _sep = lambda: report_lines.append('')
    _hline = lambda: report_lines.append('=' * 80)

    _hline()
    report_lines.append(f"{'B+ Chunk 品質評估報告':^80}")
    _hline()
    report_lines.append(f"  執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    _sep()

    for res in all_results:
        _hline()
        report_lines.append(f"  Config: {res['label']:^74}")
        _hline()
        s = res['scores']
        report_lines.append(f"  內部連貫性 (0-10):        {s.get('boundary_coherence', 0):>5.1f}")
        report_lines.append(f"  斷點正確性 (0-10):        {s.get('boundary_correctness', 0):>5.1f}")
        report_lines.append(f"  雜訊處理   (0-10):        {s.get('noise_handling', 0):>5.1f}")
        report_lines.append(f"  ─────────────────────────────")
        report_lines.append(f"  總分       (0-30):        {s.get('total', 0):>5.1f}")
        report_lines.append(f"  評估 chunk 數:             {res['chunk_count']}")
        _sep()

        # Best / worst chunks
        scored = [c for c in res['chunks'] if 'error' not in c and 'total_score' in c]
        if scored:
            best = max(scored, key=lambda x: x.get('total_score', 0))
            worst = min(scored, key=lambda x: x.get('total_score', 0))
            report_lines.append(f"  最佳 chunk: {best.get('chunk_id')} ({best.get('total_score', 0)}分)")
            report_lines.append(f"    {best.get('note', '')[:100]}")
            report_lines.append(f"  最差 chunk: {worst.get('chunk_id')} ({worst.get('total_score', 0)}分)")
            report_lines.append(f"    {worst.get('note', '')[:100]}")
        _sep()

    # 跨 config 比較
    if len(all_results) >= 2:
        _hline()
        report_lines.append(f"{'跨 Config 比較':^80}")
        _hline()
        header = "  {:<20}".format("指標")
        for res in all_results:
            header += f"  {res['label']:<20}"
        report_lines.append(header)
        report_lines.append("  " + "-" * len(header))
        for key, name in [('boundary_coherence', '連貫性'), ('boundary_correctness', '斷點'),
                          ('noise_handling', '雜訊'), ('total', '總分')]:
            row = f"  {name:<20}"
            for res in all_results:
                s = res['scores'].get(key, 0)
                row += f"  {s:<20.1f}"
            report_lines.append(row)
        _sep()

        # 推薦
        best_label = max(all_results, key=lambda x: x['scores'].get('total', 0))
        report_lines.append(f"  🏆 最佳參數: {best_label['label']} (總分 {best_label['scores'].get('total', 0):.1f})")
        _sep()

    report = '\n'.join(report_lines)
    print(f"\n{report}")

    output_path = os.path.join(output_dir, f"chunk_evaluation_{timestamp}.txt")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n評估報告已寫入: {output_path}")


if __name__ == '__main__':
    main()
