import os
import json
import sys
from typing import List, Dict, Union, Optional
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from datetime import datetime
import tempfile

# Local imports
from parse_srt import parse_srt, SubtitleEntry
from logger_config import get_logger
from config_loader import get_env_or_config, validate_config, check_required_env_vars, get_api_config
from batch_embedding import BatchEmbeddingClient

logger = get_logger('semantic_chunk')

# L2: 延遲初始化 — 從 config_loader 取值的變數延後到函數內初始化
# 以下變數在 semantic_chunk() 首次呼叫時才設定
_batch_embedding_client = None

# ── Chunking parameters (from config) ─────────────────────────────────────────────
SMART_MERGE_WINDOW_SIZE = get_env_or_config('SMART_MERGE_WINDOW_SIZE', 'chunking.smart_merge_window_size', 5)
SMART_MERGE_STRONG_PCT = get_env_or_config('SMART_MERGE_STRONG_PCT', 'chunking.smart_merge_strong_pct', 0.02)
SMART_MERGE_WEAK_PCT = get_env_or_config('SMART_MERGE_WEAK_PCT', 'chunking.smart_merge_weak_pct', 0.05)
SMART_MERGE_MIN_SENTENCES = get_env_or_config('SMART_MERGE_MIN_SENTENCES', 'chunking.smart_merge_min_sentences', 8)
SMART_MERGE_NOISE_DROP_LEN = get_env_or_config('SMART_MERGE_NOISE_DROP_LEN', 'chunking.smart_merge_noise_drop_len', 2)
SMART_MERGE_NOISE_WEAK_LEN = get_env_or_config('SMART_MERGE_NOISE_WEAK_LEN', 'chunking.smart_merge_noise_weak_len', 3)
MIN_CHUNKS = get_env_or_config('MIN_CHUNKS', 'chunking.min_chunks', 2)
MAX_CHUNKS = get_env_or_config('MAX_CHUNKS', 'chunking.max_chunks', 200)

# ── Embedding validation thresholds ───────────────────────────────────────────
MIN_VALID_VECTORS = 2
VALID_VECTORS_FOR_SIMILARITY = 5

# ── Progress tracking parameters ──────────────────────────────────────────────
PROGRESS_INTERVAL = 50
PROGRESS_DIR = os.path.join(os.getenv('SRT_OUTPUT_DIR', './output'), '.progress')

def _parse_timestamp(ts: str) -> float:
    """Convert SRT timestamp 'HH:MM:SS,mmm' → seconds (float)."""
    ts = ts.replace(',', '.')
    h, m, s = ts.split(':')
    return float(h) * 3600 + float(m) * 60 + float(s)



def _load_progress(file_id: int):
    """Load progress from file if exists."""
    os.makedirs(PROGRESS_DIR, exist_ok=True)
    progress_file = os.path.join(PROGRESS_DIR, f'embedding_progress_{file_id}.json')
    if os.path.exists(progress_file):
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logger.info(f"📍 載入進度：已處理 {data.get('completed_count', 0)} / {data.get('total_count', 'N/A')} 筆 (最後更新：{data.get('last_update', 'N/A')})")
            return data
        except Exception as e:
            logger.warning(f"進度檔案讀取失敗：{e}，將重新開始")
            return None
    return None

def _save_progress(file_id: int, completed_count: int, total_count: int, valid_count: int, failed_count: int):
    """Save progress to file."""
    progress_file = os.path.join(PROGRESS_DIR, f'embedding_progress_{file_id}.json')
    try:
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump({
                'file_id': file_id,
                'completed_count': completed_count,
                'total_count': total_count,
                'valid_count': valid_count,
                'failed_count': failed_count,
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'processing'
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"進度儲存失敗：{e}")

def _mark_progress_complete(file_id: int, total_count: int, valid_count: int, failed_count: int):
    """Mark progress as complete."""
    progress_file = os.path.join(PROGRESS_DIR, f'embedding_progress_{file_id}.json')
    try:
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump({
                'file_id': file_id,
                'completed_count': total_count,
                'total_count': total_count,
                'valid_count': valid_count,
                'failed_count': failed_count,
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'complete'
            }, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ 嵌入向量生成完成！有效 {valid_count} 筆，失敗 {failed_count} 筆")
    except Exception as e:
        logger.error(f"完成標記儲存失敗：{e}")

def _ensure_client():
    """L2: 延遲初始化 BatchEmbeddingClient，避免模組頂層副作用"""
    global _batch_embedding_client
    if _batch_embedding_client is None:
        _api_cfg = get_api_config()
        _batch_embedding_client = BatchEmbeddingClient(
            api_base=_api_cfg['embedding_url'],
            api_key=_api_cfg['api_key'],
            model=get_env_or_config('EMBEDDING_MODEL', 'embedding.model', 'text-embedding-3-large'),
            timeout=get_env_or_config('EMBEDDING_TIMEOUT', 'embedding.timeout', 60),
            expected_dim=get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072),
        )


def _get_expected_dim() -> int:
    return get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072)


def generate_embedding(text: str, expected_dim: Optional[int] = None) -> Optional[np.ndarray]:
    """Query the OpenAI-compatible embedding endpoint for a single vector."""
    # H1+H3: 使用統一的 BatchEmbeddingClient
    _ensure_client()
    if expected_dim is None:
        expected_dim = _get_expected_dim()
    try:
        embeddings = _batch_embedding_client.generate_batch_embeddings([text])
        if embeddings.success and embeddings.embeddings and len(embeddings.embeddings) == 1:
            emb = embeddings.embeddings[0]
            if len(emb) != expected_dim:
                logger.error(f"Embedding dimension mismatch: got {len(emb)}, expected {expected_dim}. Check config.json api.base_url and embedding.expected_dim")
                return None
            return emb
        else:
            logger.error(f"Batch embedding failed: {embeddings.error or 'unknown error'}")
            return None
    except Exception as e:
        logger.error(f"Embedding request failed for text snippet '{text[:30]}...': {e}")
        return None

def _embed_windows(windows: List[str], embed_fn=None) -> tuple[List, List[int]]:
    """向量化所有窗口，回傳 (vectors, failed_indices)。"""
    if embed_fn:
        vectors = []
        failed = []
        for i, w in enumerate(windows):
            try:
                vec = embed_fn(w)
                if vec is not None:
                    vectors.append(vec)
                else:
                    vectors.append(None)
                    failed.append(i)
            except Exception:
                vectors.append(None)
                failed.append(i)
        return vectors, failed
    _ensure_client()
    batch_size = get_env_or_config('BATCH_EMBEDDING_MAX_SIZE', 'embedding.batch_max_size', 32)
    vectors = [None] * len(windows)
    failed_windows = []

    def _process_batch(indices, current_batch_size):
        texts = [windows[i] for i in indices]
        try:
            res = _batch_embedding_client.generate_batch_embeddings(texts)
            if res.success and res.embeddings:
                for i, vec in zip(indices, res.embeddings):
                    vectors[i] = vec
                return True
            raise ValueError(res.error or "Unknown error")
        except Exception as e:
            if current_batch_size <= 1:
                failed_windows.extend(indices)
                return False
            half = len(indices) // 2
            logger.warning(f"Batch {len(indices)} failed ({e}), splitting into {half}+{len(indices)-half}")
            return _process_batch(indices[:half], current_batch_size // 2) and \
                   _process_batch(indices[half:], current_batch_size // 2)

    all_indices = list(range(len(windows)))
    for i in range(0, len(all_indices), batch_size):
        _process_batch(all_indices[i:i + batch_size], batch_size)
    return vectors, failed_windows


def _compute_similarities(vectors: List, window_size: int) -> tuple[List[float], List[int]]:
    """計算相鄰非重疊視窗的 cosine similarity，回傳 (sims, sim_to_break_idx)。"""
    sims, sim_to_break_idx = [], []
    for i in range(len(vectors) - window_size):
        v1, v2 = vectors[i], vectors[i + window_size]
        if v1 is not None and v2 is not None:
            sim = cosine_similarity([v1], [v2])[0][0]
        else:
            sim = 1.0
        sims.append(sim)
        sim_to_break_idx.append(i + window_size)
    return sims, sim_to_break_idx


def _resolve_breakpoints(sims: List[float], sorted_idx: List[int],
                          sim_to_break_idx: List[int], total_entries: int,
                          high_pct: float, low_pct: float,
                          min_sentences: int) -> tuple[List[int], float]:
    """從相似度決定最終斷點位置，回傳 (active_breaks, low_pct_strength_threshold)。"""
    n_sims = len(sims)
    strengths = [1.0 - s for s in sims]
    low_cut = max(1, int(n_sims * low_pct))
    high_cut = max(1, int(n_sims * high_pct))
    threshold = strengths[sorted_idx[min(low_cut, n_sims - 1)]]

    absolute = set(sorted_idx[:low_cut])
    pending = sorted_idx[low_cut:high_cut]

    active_breaks = [0, total_entries] + [sim_to_break_idx[i] for i in absolute]
    active_breaks.sort()

    for sim_idx in reversed(pending):
        bp_line = sim_to_break_idx[sim_idx]
        left = right = 0
        for b in active_breaks:
            if b <= bp_line:
                left = b
            if b > bp_line:
                right = b
                break
        if bp_line - left >= min_sentences and right - bp_line >= min_sentences:
            active_breaks.append(bp_line)
            active_breaks.sort()
    return active_breaks, threshold


def _build_bp_meta(sims: List[float], strengths: List[float],
                    sorted_idx: List[int], sim_to_break_idx: List[int]) -> dict:
    """建立斷點強度查詢表 {entry_idx -> {strength_pct, cosine, strength}}。"""
    meta = {}
    for rank, sim_idx in enumerate(sorted_idx):
        bp_line = sim_to_break_idx[sim_idx]
        meta[bp_line] = {
            'strength_pct': round((rank + 1) / len(sims) * 100, 1),
            'cosine': round(sims[sim_idx], 4),
            'strength': round(1.0 - sims[sim_idx], 4),
        }
    return meta


def _bp_info(entry_idx: int, total_entries: int, bp_meta: dict) -> dict:
    if entry_idx == 0:
        return {'boundary_side': 'left', 'strength_pct': None, 'cosine': None, 'label': '檔案起點'}
    if entry_idx == total_entries:
        return {'boundary_side': 'right', 'strength_pct': None, 'cosine': None, 'label': '檔案終點'}
    info = bp_meta.get(entry_idx)
    if info:
        return {**info, 'boundary_side': 'auto', 'label': f"{info['strength_pct']}%"}
    return {'boundary_side': 'auto', 'strength_pct': None, 'cosine': None, 'label': '-'}


def _build_segments(entries: List[SubtitleEntry], active_breaks: List[int],
                     bp_meta: dict, file_id: int, total_entries: int,
                     noise_drop_len: int, noise_weak_len: int,
                     low_pct_threshold: float) -> tuple[List[Dict], List[Dict]]:
    """從斷點列表產出最終段落與被排除段落。"""
    final, discarded = [], []
    for i in range(len(active_breaks) - 1):
        start_idx = active_breaks[i]
        end_idx = active_breaks[i + 1] - 1
        chunk_len = end_idx - start_idx + 1
        seg = entries[start_idx: end_idx + 1]

        chunk = {
            'chunk_id': f"{file_id}_{start_idx}",
            'start_time': seg[0].start_time,
            'end_time': seg[-1].end_time,
            'entry_count': chunk_len,
            'text_content': ' '.join(e.text for e in seg),
            'boundary_type': 'semantic',
            'left_boundary': _bp_info(start_idx, total_entries, bp_meta),
            'right_boundary': _bp_info(active_breaks[i + 1], total_entries, bp_meta),
        }

        if chunk_len <= noise_drop_len:
            chunk['dropped'] = True
            chunk['drop_reason'] = f'noise_too_short (<= {noise_drop_len} lines)'
            discarded.append(chunk)
        elif chunk_len == noise_weak_len:
            ls = bp_meta.get(start_idx, {}).get('strength', 0.0)
            rs = bp_meta.get(active_breaks[i + 1], {}).get('strength', 0.0)
            if ls >= low_pct_threshold and rs >= low_pct_threshold:
                chunk['dropped'] = True
                chunk['drop_reason'] = 'noise_weak_links (both ends below strong threshold)'
                discarded.append(chunk)
            else:
                final.append(chunk)
        else:
            final.append(chunk)
    return final, discarded


def smart_merge_3_0(entries: List[SubtitleEntry], file_id: int,
                    window_size: int = SMART_MERGE_WINDOW_SIZE,
                    min_sentences: int = SMART_MERGE_MIN_SENTENCES,
                    high_pct: float = SMART_MERGE_WEAK_PCT,
                    low_pct: float = SMART_MERGE_STRONG_PCT,
                    noise_drop_len: int = SMART_MERGE_NOISE_DROP_LEN,
                    noise_weak_len: int = SMART_MERGE_NOISE_WEAK_LEN,
                    embed_fn=None) -> tuple[List[Dict], List[int], List[Dict]]:
    total_entries = len(entries)
    if total_entries < window_size:
        return [{'start_idx': 0, 'end_idx': total_entries - 1,
                 'start_time': entries[0].start_time, 'end_time': entries[-1].end_time,
                 'entry_count': total_entries,
                 'text_content': ' '.join(e.text for e in entries),
                 'boundary_type': 'single_chunk',
                 'left_boundary': {'boundary_side': 'left', 'strength_pct': None, 'cosine': None, 'label': '檔案起點'},
                 'right_boundary': {'boundary_side': 'right', 'strength_pct': None, 'cosine': None, 'label': '檔案終點'},
                 'chunk_id': f"{file_id}_0"}], [], []

    windows = [' '.join(e.text for e in entries[i:i + window_size])
               for i in range(total_entries - window_size + 1)]

    vectors, failed_windows = _embed_windows(windows, embed_fn=embed_fn)

    valid_count = sum(1 for v in vectors if v is not None)
    if valid_count < MIN_VALID_VECTORS:
        return [], failed_windows, []
    if valid_count < VALID_VECTORS_FOR_SIMILARITY:
        return [], failed_windows, []
    logger.info(f"Embedding: {valid_count}/{len(windows)} vectors")

    sims, sim_to_break_idx = _compute_similarities(vectors, window_size)
    if not sims:
        return [{'start_idx': 0, 'end_idx': total_entries - 1,
                 'start_time': entries[0].start_time, 'end_time': entries[-1].end_time,
                 'entry_count': total_entries,
                 'text_content': ' '.join(e.text for e in entries),
                 'boundary_type': 'single_chunk',
                 'left_boundary': {'boundary_side': 'left', 'strength_pct': None, 'cosine': None, 'label': '檔案起點'},
                 'right_boundary': {'boundary_side': 'right', 'strength_pct': None, 'cosine': None, 'label': '檔案終點'},
                 'chunk_id': f"{file_id}_0"}], failed_windows, []

    n_sims = len(sims)
    strengths = [1.0 - s for s in sims]
    sorted_idx = sorted(range(n_sims), key=lambda x: strengths[x], reverse=True)

    active_breaks, low_pct_threshold = _resolve_breakpoints(
        sims, sorted_idx, sim_to_break_idx, total_entries, high_pct, low_pct, min_sentences)

    bp_meta = _build_bp_meta(sims, strengths, sorted_idx, sim_to_break_idx)
    final_chunks, discarded_chunks = _build_segments(
        entries, active_breaks, bp_meta, file_id, total_entries,
        noise_drop_len, noise_weak_len, low_pct_threshold)

    return final_chunks, failed_windows, discarded_chunks

def semantic_chunk(entries: List[SubtitleEntry], file_id: int, threshold: Union[float, str] = "auto") -> List[Dict]:
    """Create semantic chunks using Smart Merge 3.0 algorithm. NO FALLBACK."""
    if not entries:
        logger.info("輸入條目為空，返回空列表")
        return []
    total_entries = len(entries)
    logger.info(f"開始執行 Smart Merge 3.0：共 {total_entries} 筆")

    # 執行 Smart Merge 3.0（chunking 速度很快，不做 progress resume）
    # 若需 resume，請於 LLM-heavy summarization 階段實作 content-addressed checkpoint
    final_chunks, failed_indices, discarded_chunks = smart_merge_3_0(
        entries=entries,
        file_id=file_id,
        window_size=SMART_MERGE_WINDOW_SIZE,
        min_sentences=SMART_MERGE_MIN_SENTENCES,
        high_pct=SMART_MERGE_WEAK_PCT,
        low_pct=SMART_MERGE_STRONG_PCT,
        noise_drop_len=SMART_MERGE_NOISE_DROP_LEN,
        noise_weak_len=SMART_MERGE_NOISE_WEAK_LEN
    )

    if failed_indices:
        failed_path = os.path.join(PROGRESS_DIR, f'failed_chunks_{file_id}.json')
        try:
            with open(failed_path, 'w', encoding='utf-8') as f:
                json.dump(failed_indices, f, ensure_ascii=False, indent=2)
            logger.warning(f"⚠️ 發現 {len(failed_indices)} 個嵌入失敗的窗口")
        except Exception as e:
            logger.warning(f"寫入失敗 chunk 記錄時出錯: {e}")
        raise RuntimeError(f"Smart Merge 失敗：共有 {len(failed_indices)} 個窗口向量化失敗。已停止流程要求人工檢查。")

    if not final_chunks:
        raise RuntimeError("Smart Merge 失敗：產出 0 個 chunks。請檢查 SRT 內容或調整相似度閾值。")

    if discarded_chunks:
        logger.info(f"Smart Merge excluded {len(discarded_chunks)} noise chunks")
    logger.info(f"✅ Smart Merge 成功：產出 {len(final_chunks)} 個語意段落")
    return final_chunks
