import re
import os
from dataclasses import dataclass
from typing import List
from logger_config import get_logger

logger = get_logger('parse_srt')

@dataclass
class SubtitleEntry:
    start_time: str   # format HH:MM:SS,mmm
    end_time: str
    text: str

def parse_srt(path: str) -> List[SubtitleEntry]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"SRT file not found: {path}")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(path, 'r', encoding='big5') as f:
            content = f.read()
    # split on blank lines (support both \n\n and \r\n\r\n)
    blocks = re.split(r'\r?\n\r?\n+', content.strip())
    entries: List[SubtitleEntry] = []
    for blk in blocks:
        lines = blk.splitlines()
        if len(lines) < 2:
            continue
        # first line may be index, ignore it
        time_line = lines[1] if re.match(r'\d+$', lines[0].strip()) else lines[0]
        m = re.search(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', time_line)
        if not m:
            continue
        start, end = m.group(1), m.group(2)
        text = ' '.join(l.strip() for l in lines[2:] if l.strip())
        entries.append(SubtitleEntry(start, end, text))
    return entries

if __name__ == '__main__':
    import sys, json
    if len(sys.argv) < 2:
        logger.error('Usage: parse_srt.py <srt_path>')
        sys.exit(1)
    f = sys.argv[1]
    ents = parse_srt(f)
    logger.info(json.dumps([e.__dict__ for e in ents], ensure_ascii=False, indent=2))
