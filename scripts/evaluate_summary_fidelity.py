"""
evaluate_summary_fidelity.py — LLM 評估摘要忠實度

評估每個 chunk 的 LLM 摘要是否忠於原文，不混入幻覺或遺漏關鍵資訊。

用法:
    python3 scripts/evaluate_summary_fidelity.py --reports /path/to/output/0_20260513_123228/*.json
    python3 scripts/evaluate_summary_fidelity.py --reports /path/to/dir --sample 5
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

logger = get_logger('evaluate_summary')

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = 3
TIMEOUT_SEC = 120


def collect_reports(paths: list) -> list:
    json_files = []
    for p in paths:
        if os.path.isfile(p) and p.endswith('.json'):
            json_files.append(p)
        elif os.path.isdir(p):
            json_files.extend(sorted(glob.glob(os.path.join(p, '*.json'))))
    segments = []
    for jf in sorted(json_files):
        with open(jf, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for seg in data.get('segments', []):
            if not seg.get('dropped') and seg.get('summary') and seg.get('text_content'):
                seg['_report_label'] = os.path.basename(os.path.dirname(jf))
                segments.append(seg)
    return segments


def call_llm_evaluate(text: str, summary: str, model_used: str) -> dict:
    prompt = f"""你是一個摘要品質審查員。請評估以下 LLM 生成的摘要是否忠實反映原文。

原始內容:
{text}

摘要（由 {model_used} 生成）:
{summary}

評分標準（0-25 each，總分 100）：

1. 事實正確性 (0-25): 摘要中的資訊是否都能在原文中找到依據？有無幻覺（hallucination）內容？
2. 完整性 (0-25): 摘要是否涵蓋原文的關鍵論點？有無重要資訊被遺漏？
3. 中立性 (0-25): 摘要是否客觀？有無添加原文沒有的解釋、立場或評價？
4. 整體品質 (0-25): 綜合以上三項的整體判斷

請以 JSON 格式回覆：
{{
  "factual_accuracy": {{"score": N, "reason": "..."}},
  "completeness": {{"score": N, "reason": "..."}},
  "neutrality": {{"score": N, "reason": "..."}},
  "overall_quality": {{"score": N, "reason": "..."}},
  "total_score": N,
  "hallucinated_content": ["列出幻覺內容的簡短描述（若有）"],
  "missing_key_points": ["列出遺漏的關鍵點（若有）"],
  "note": "..."
}}"""

    models = get_env_or_config('EVALUATION_FIDELITY_MODELS', 'evaluation.fidelity_models', None)
    if models is None:
        models = get_env_or_config('SUMMARIZATION_MODELS', 'summarization.models', ["gpt-4.1-mini"])

    try:
        return _call_llm(prompt=prompt, models=models,
                         system_prompt="你是專業的摘要品質審查員。請嚴格檢驗摘要的事實正確性。")
    except Exception as e:
        logger.warning(f"Evaluate fidelity failed: {e}")
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description='LLM 摘要忠實度評估')
    parser.add_argument('--reports', nargs='+', required=True,
                        help='JSON report 路徑或目錄')
    parser.add_argument('--output', default=None, help='輸出目錄')
    parser.add_argument('--sample', type=int, default=0,
                        help='取樣 N 個 chunks 評分（0=全部）')
    args = parser.parse_args()

    segments = collect_reports(args.reports)
    if not segments:
        print("❌ 未找到任何含摘要的 segments")
        sys.exit(1)

    if args.sample > 0 and len(segments) > args.sample:
        import random
        random.seed(42)
        segments = random.sample(segments, args.sample)

    # Group by model_used
    by_model = defaultdict(list)
    for seg in segments:
        m = seg.get('model_used', 'unknown')
        by_model[m].append(seg)

    print(f"評估 {len(segments)} 個 chunks，{len(by_model)} 種模型")
    for model, segs in sorted(by_model.items()):
        print(f"  {model}: {len(segs)} chunks")

    output_dir = args.output
    if not output_dir:
        base_dir = get_env_or_config('SRT_OUTPUT_DIR', 'paths.output_dir', '.')
        output_dir = os.path.join(base_dir, 'summary_fidelity')
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    model_results = {}
    for model, segs in sorted(by_model.items()):
        print(f"\n--- 評估模型: {model} ({len(segs)} chunks) ---")
        model_scores = {"factual_accuracy": [], "completeness": [], "neutrality": [],
                        "overall_quality": [], "total": []}
        all_hallucinations = []
        all_missing = []

        for idx, seg in enumerate(segs):
            print(f"  [{idx + 1}/{len(segs)}] {seg.get('chunk_id', '?')}...", end=' ', flush=True)
            result = call_llm_evaluate(seg['text_content'], seg['summary'], model)
            if 'error' not in result:
                for key in ['factual_accuracy', 'completeness', 'neutrality', 'overall_quality']:
                    score = result.get(key, {}).get('score', 0)
                    model_scores[key].append(score)
                model_scores['total'].append(result.get('total_score', 0))
                all_hallucinations.extend(result.get('hallucinated_content', []))
                all_missing.extend(result.get('missing_key_points', []))
                print(f"{result.get('total_score', 0)}分")
            else:
                print(f"❌")

        avg = {k: (sum(v) / len(v) if v else 0) for k, v in model_scores.items()}
        model_results[model] = {
            'count': len(segs),
            'avg_scores': avg,
            'hallucinations': all_hallucinations,
            'missing': all_missing,
        }
        print(f"  → {model} 平均: "
              f"事實 {avg.get('factual_accuracy', 0):.1f} | "
              f"完整 {avg.get('completeness', 0):.1f} | "
              f"中立 {avg.get('neutrality', 0):.1f} | "
              f"整體 {avg.get('overall_quality', 0):.1f} | "
              f"總分 {avg.get('total', 0):.1f}")

    # 產出報告
    report_lines = []
    _sep = lambda: report_lines.append('')
    _hline = lambda: report_lines.append('=' * 80)

    _hline()
    report_lines.append(f"{'摘要忠實度評估報告':^80}")
    _hline()
    report_lines.append(f"  執行時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report_lines.append(f"  評估 chunks: {len(segments)}")
    _sep()

    for model, res in sorted(model_results.items()):
        _hline()
        report_lines.append(f"  Model: {model:^74}")
        _hline()
        s = res['avg_scores']
        report_lines.append(f"  事實正確性 (0-25):        {s.get('factual_accuracy', 0):>5.1f}")
        report_lines.append(f"  完整性     (0-25):        {s.get('completeness', 0):>5.1f}")
        report_lines.append(f"  中立性     (0-25):        {s.get('neutrality', 0):>5.1f}")
        report_lines.append(f"  整體品質   (0-25):        {s.get('overall_quality', 0):>5.1f}")
        report_lines.append(f"  ─────────────────────────────")
        report_lines.append(f"  總分       (0-100):       {s.get('total', 0):>5.1f}")
        report_lines.append(f"  評估 chunk 數:             {res['count']}")
        _sep()

        if res['hallucinations']:
            report_lines.append("  偵測到的幻覺內容（前 10）:")
            for h in res['hallucinations'][:10]:
                report_lines.append(f"    ⚠️  {h}")
            _sep()
        if res['missing']:
            report_lines.append("  遺漏的關鍵點（前 10）:")
            for m in res['missing'][:10]:
                report_lines.append(f"    📌 {m}")
            _sep()

    # 跨 model 比較
    if len(model_results) >= 2:
        _hline()
        report_lines.append(f"{'跨 Model 比較':^80}")
        _hline()
        header = "  {:<25}".format("指標")
        for model in sorted(model_results):
            header += f"  {model:<25}"
        report_lines.append(header)
        report_lines.append("  " + "-" * len(header))
        for key, name in [('factual_accuracy', '事實正確性'), ('completeness', '完整性'),
                          ('neutrality', '中立性'), ('overall_quality', '整體品質'), ('total', '總分')]:
            row = f"  {name:<25}"
            for model in sorted(model_results):
                s = model_results[model]['avg_scores'].get(key, 0)
                row += f"  {s:<25.1f}"
            report_lines.append(row)
        _sep()

        best = max(model_results, key=lambda m: model_results[m]['avg_scores'].get('total', 0))
        report_lines.append(f"  🏆 最佳摘要模型: {best}")
        _sep()

    report = '\n'.join(report_lines)
    print(f"\n{report}")

    output_path = os.path.join(output_dir, f"summary_fidelity_{timestamp}.txt")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"\n評估報告已寫入: {output_path}")


if __name__ == '__main__':
    main()
