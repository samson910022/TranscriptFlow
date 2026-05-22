#!/usr/bin/env python3
"""
smart_merge_diagnostics.py - 診斷 Smart Merge 為何產出 0 chunks

此腳本使用 production 的 smart_merge_3_0() 加上模擬嵌入，
並產生詳細的診斷報告。
"""

import json
import sys
import os
import numpy as np
from typing import List, Dict
from dataclasses import dataclass

# 模擬 parse_srt 模組（避免依賴實際 SRT 檔案）
@dataclass
class SubtitleEntry:
    start_time: str
    end_time: str
    text: str

def parse_srt_mock(content: str) -> List[SubtitleEntry]:
    """模擬解析 SRT 內容"""
    import re
    entries = []
    blocks = re.split(r'\r?\n\r?\n+', content.strip())
    for blk in blocks:
        lines = blk.splitlines()
        if len(lines) < 2:
            continue
        time_line = lines[1] if re.match(r'\d+$', lines[0].strip()) else lines[0]
        m = re.search(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', time_line)
        if not m:
            continue
        start, end = m.group(1), m.group(2)
        text = ' '.join(l.strip() for l in lines[2:] if l.strip())
        entries.append(SubtitleEntry(start, end, text))
    return entries

def mock_generate_embedding(text: str) -> np.ndarray:
    """模擬產生嵌入向量（用於離線診斷）"""
    np.random.seed(hash(text) % (2**32))
    return np.random.randn(3072)

from semantic_chunk import smart_merge_3_0

def smart_merge_3_0_diagnostics(entries, file_id=1):
    """
    呼叫 production 的 smart_merge_3_0() 並回傳診斷資訊。
    """
    total_entries = len(entries)
    diagnostics = {
        "file_id": file_id,
        "total_entries": total_entries,
        "final_chunk_count": 0,
        "chunk_sizes": [],
        "failed_windows": 0,
        "discarded_count": 0,
        "analysis": {},
    }

    if total_entries < 5:
        diagnostics["analysis"]["reason"] = (
            f"總條目數 ({total_entries}) 小於視窗大小，無法執行 Smart Merge"
        )
        return diagnostics

    final_chunks, failed_indices, discarded_chunks = smart_merge_3_0(
        entries=entries,
        file_id=file_id,
        embed_fn=mock_generate_embedding,
    )

    diagnostics["failed_windows"] = len(failed_indices)
    diagnostics["discarded_count"] = len(discarded_chunks)
    diagnostics["final_chunk_count"] = len(final_chunks)
    diagnostics["chunk_sizes"] = [
        c.get("entry_count", 0) for c in final_chunks
    ]
    diagnostics["chunk_previews"] = [
        c.get("text_content", "")[:60] for c in final_chunks
    ]

    if not final_chunks:
        reasons = []
        if failed_indices:
            reasons.append(f"有 {len(failed_indices)} 個嵌入失敗的窗口")
        if discarded_chunks:
            reasons.append(f"所有候選段落被雜訊過濾拋棄（{len(discarded_chunks)} 個）")
        if not reasons:
            reasons.append("Smart Merge 未產出 chunks（請檢查 SRT 內容或參數設定）")
        diagnostics["analysis"]["reasons_zero_chunks"] = reasons
    else:
        sizes = diagnostics["chunk_sizes"]
        diagnostics["analysis"]["chunk_size_stats"] = {
            "min": int(min(sizes)),
            "max": int(max(sizes)),
            "avg": round(float(np.mean(sizes)), 2),
            "median": round(float(np.median(sizes)), 2),
        }

    return diagnostics

def main():
    """主函式：執行診斷並輸出報告"""
    sample_srt_content = """
1
00:00:01,000 --> 00:00:04,000
歡迎來到我們的節目

2
00:00:05,000 --> 00:00:07,000
今天我們要討論代溝的問題

3
00:00:08,500 --> 00:00:12,000
這是一個很重要的話題

4
00:00:13,000 --> 00:00:16,000
讓我們開始吧

5
00:00:17,000 --> 00:00:20,000
首先請教我們的來賓
"""

    print("=" * 80)
    print("Smart Merge 3.0 診斷報告")
    print("=" * 80)
    print()

    entries = parse_srt_mock(sample_srt_content)
    print(f"SRT 解析結果：{len(entries)} 筆條目\n")

    diagnostics = smart_merge_3_0_diagnostics(entries, file_id=1)

    print(f"嵌入失敗窗口：{diagnostics['failed_windows']}")
    print(f"雜訊過濾拋棄：{diagnostics['discarded_count']}")
    print(f"最終 Chunk 數量：{diagnostics['final_chunk_count']}")

    if diagnostics['final_chunk_count'] == 0:
        print("\n原因分析：")
        for r in diagnostics['analysis'].get('reasons_zero_chunks', []):
            print(f"  - {r}")
    elif 'chunk_size_stats' in diagnostics['analysis']:
        s = diagnostics['analysis']['chunk_size_stats']
        print(f"Chunk 大小統計：min={s['min']}, max={s['max']}, avg={s['avg']}")

    output_file = "./output/smart_merge_diagnostics.json"
    os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(diagnostics, f, ensure_ascii=False, indent=2)
    print(f"\n完整診斷數據已寫入：{output_file}")

if __name__ == '__main__':
    main()
