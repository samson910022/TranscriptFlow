#!/usr/bin/env python3
"""
錯誤統計工具

讀取所有 batch_status_*.json 檔案，統計：
1. 各狀態數量（即時擷取）
2. 成功率計算
3. Top 10 常見錯誤訊息模式
4. 輸出 JSON 格式報告

用法：
    python3 error_stats.py

輸出：
    - JSON 報告: $SRT_OUTPUT_DIR/error_stats_report.json
    - 終端摘要顯示
"""

import os
import sys
import json
import glob
from pathlib import Path
from datetime import datetime
from collections import Counter

# 加入 scripts 目錄到 path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from logger_config import get_logger
from config_loader import get_env_or_config

logger = get_logger('error_stats')


def analyze_batch_status():
    """分析所有 batch_status_*.json 檔案"""
    output_dir = os.getenv('SRT_OUTPUT_DIR')
    if not output_dir:
        from config_loader import get_nested_config
        output_dir = get_nested_config('paths.output_dir', './output')
    
    pattern = os.path.join(output_dir, 'batch_status_*.json')
    files = glob.glob(pattern)
    
    if not files:
        logger.warning(f"未找到任何 batch_status_*.json 檔案 (搜尋路徑: {output_dir})")
        print("❌ 未找到任何批次狀態檔案")
        return
    
    logger.info(f"找到 {len(files)} 個批次狀態檔案")
    print(f"找到 {len(files)} 個批次狀態檔案")
    
    total_files = 0
    status_counts = Counter()
    error_patterns = Counter()
    file_details = []
    
    for bf in files:
        try:
            with open(bf, 'r', encoding='utf-8') as f:
                batch = json.load(f)
            
            # 支援兩種格式：舊版陣列格式 和 新版 {"files": [...]} 格式
            if isinstance(batch, list):
                file_statuses = batch
            elif isinstance(batch, dict) and 'files' in batch:
                file_statuses = batch['files']
            else:
                logger.warning(f"無法識別的格式：{bf}")
                continue
            for file_status in file_statuses:
                total_files += 1
                status = file_status.get('status', 'unknown')
                status_counts[status] += 1
                
                # 記錄錯誤模式
                if status == 'failed_permanent':
                    error_msg = file_status.get('error_log', 'Unknown error')
                    # 標準化錯誤訊息（去除具體路徑、ID 等變數）
                    normalized_msg = _normalize_error_message(error_msg)
                    error_patterns[normalized_msg] += 1
                
                file_details.append({
                    'file_id': file_status.get('file_id', 'unknown'),
                    'status': status,
                    'error_log': file_status.get('error_log', None)
                })
        except Exception as e:
            logger.error(f"讀取 {bf} 失敗: {e}")
    
    # 計算成功率
    done_count = status_counts.get('done', 0)
    success_rate = (done_count / total_files * 100) if total_files > 0 else 0
    
    # 建立報告（過濾零計數項目）
    filtered_status = {k: v for k, v in status_counts.items() if v > 0}
    report = {
        'generated_at': datetime.now().isoformat(),
        'total_files_processed': total_files,
        'batch_files_analyzed': len(files),
        'status_distribution': filtered_status,
        'success_rate': f"{success_rate:.2f}%",
        'top_error_patterns': dict(error_patterns.most_common(10)),
        'summary': filtered_status
    }
    
    # 寫入報告檔案
    report_path = os.path.join(output_dir, 'error_stats_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    # 顯示摘要
    print("\n" + "=" * 60)
    print("錯誤統計報告摘要")
    print("=" * 60)
    print(f"總處理檔案數: {total_files}")
    print(f"批次檔案數: {len(files)}")
    print(f"成功率: {success_rate:.2f}%")
    for st, c in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  - {st}: {c}")
    
    if error_patterns:
        print("\nTop 5 常見錯誤模式:")
        for i, (msg, count) in enumerate(error_patterns.most_common(5), 1):
            print(f"  {i}. ({count} 次) {msg[:80]}..." if len(msg) > 80 else f"  {i}. ({count} 次) {msg}")
    
    print(f"\n✅ 詳細報告已寫入: {report_path}")
    
    return report


def _normalize_error_message(msg: str) -> str:
    """
    標準化錯誤訊息，去除變數內容（如路徑、ID、時間戳記）
    """
    import re
    
    # 移除具體路徑
    msg = re.sub(r'/[\w/.\-]+', '/PATH', msg)
    
    # 移除具體 ID
    msg = re.sub(r'\b\d{1,5}\b', 'ID', msg)
    
    # 移除時間戳記
    msg = re.sub(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}', 'TIMESTAMP', msg)
    
    # 移除具體 IP 地址
    msg = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', 'IP', msg)
    
    # 移除具體錯誤代碼（保留文字說明）
    msg = re.sub(r'\b[0-9A-F]{8,}\b', 'HASH', msg, flags=re.IGNORECASE)
    
    return msg[:150]  # 限制長度


def main():
    """主函數"""
    logger.info("開始分析批次狀態...")
    report = analyze_batch_status()
    
    if report:
        logger.info("錯誤統計分析完成")
        sys.exit(0)
    else:
        logger.warning("未生成報告（可能無數據）")
        sys.exit(0)  # 沒有數據不代表錯誤


if __name__ == '__main__':
    main()
