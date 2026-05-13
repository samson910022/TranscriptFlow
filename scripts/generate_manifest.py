"""
generate_manifest.py — 掃描 data_dir 自動產生 master_file_manifest.json

用法:
    python3 scripts/generate_manifest.py
    python3 scripts/generate_manifest.py --data-dir /path/to/srt/files
    python3 scripts/generate_manifest.py --output /path/to/manifest.json
    python3 scripts/generate_manifest.py --dry-run

預設 data_dir 與 output 路徑從 config.json 的 paths.data_dir / paths.master_file 讀取。
"""

import argparse
import json
import os
import sys
import glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import get_nested_config


def scan_data_dir(data_dir: str) -> list:
    if not os.path.isdir(data_dir):
        print(f"❌ data_dir 不存在: {data_dir}")
        sys.exit(1)

    srt_files = sorted(glob.glob(os.path.join(data_dir, '*.srt')))
    entries = []
    skipped = 0

    for srt_path in srt_files:
        if srt_path.endswith('.srt.bak'):
            continue
        basename = srt_path[:-4]
        mp3_path = basename + '.mp3'
        filename_srt = os.path.basename(srt_path)
        filename_mp3 = os.path.basename(mp3_path)

        if not os.path.exists(mp3_path):
            print(f"  ⚠️  跳過（無對應 mp3）: {filename_srt}")
            skipped += 1
            continue

        entries.append({
            "filename_srt": filename_srt,
            "filename_mp3": filename_mp3,
            "path_srt": srt_path,
            "path_mp3": mp3_path,
        })

    return entries, skipped


def main():
    parser = argparse.ArgumentParser(description='產生 master_file_manifest.json')
    parser.add_argument('--data-dir', default=None,
                        help='SRT/MP3 所在目錄（預設: config.json paths.data_dir）')
    parser.add_argument('--output', default=None,
                        help='輸出 manifest 路徑（預設: config.json paths.master_file）')
    parser.add_argument('--dry-run', action='store_true',
                        help='僅列印掃描結果，不寫入檔案')
    args = parser.parse_args()

    data_dir = args.data_dir or get_nested_config('paths.data_dir', '')
    if not data_dir:
        print("❌ 請指定 --data-dir 或在 config.json 中設定 paths.data_dir")
        sys.exit(1)

    output_path = args.output or get_nested_config('paths.master_file', '')
    if not output_path and not args.dry_run:
        print("❌ 請指定 --output 或在 config.json 中設定 paths.master_file")
        sys.exit(1)

    print(f"掃描目錄: {data_dir}")
    entries, skipped = scan_data_dir(data_dir)

    if not entries:
        print("❌ 未找到任何有效的 SRT+MP3 配對")
        sys.exit(1)

    entries_with_id = [
        {"id": idx, **e} for idx, e in enumerate(entries)
    ]

    manifest = {
        "total_count": len(entries_with_id),
        "files": entries_with_id,
    }

    print(f"\n掃描完成: {len(entries_with_id)} 個檔案" +
          (f"（{skipped} 個因無 mp3 跳過）" if skipped else ""))

    if args.dry_run:
        print("\n--- Dry Run 輸出預覽（前 5 筆）---")
        print(json.dumps({"total_count": manifest["total_count"], "files": manifest["files"][:5]},
                         indent=2, ensure_ascii=False))
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"✅ manifest 已寫入: {output_path}")


if __name__ == '__main__':
    main()
