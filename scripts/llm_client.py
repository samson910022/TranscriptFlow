"""
llm_client.py — 統一 LLM API 呼叫客戶端

提供共用的 call_llm() 與 get_models()，避免 5 個檔案重複實作相同的模式。
"""

import json
import re
import time
import threading
from typing import List, Optional, Dict
import requests

from config_loader import get_api_config, get_env_or_config
from logger_config import get_logger
from batch_embedding import BatchEmbeddingClient

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = get_env_or_config('MAX_RETRIES', 'summarization.max_retries', 3)
TIMEOUT_SEC = get_env_or_config('TIMEOUT_SEC', 'summarization.timeout_sec', 120)

_session = threading.local()
_logger = get_logger('llm_client')
_embedding_client = None
_embedding_lock = threading.Lock()


def _get_session() -> requests.Session:
    if not hasattr(_session, "s"):
        _session.s = requests.Session()
    return _session.s


def get_models(config_key: str = 'summarization.models',
               env_var: str = 'SUMMARIZATION_MODELS',
               default: list = None) -> List[str]:
    if default is None:
        default = ["gpt-4.1-mini"]
    return get_env_or_config(env_var, config_key, default)


def get_embedding_client():
    global _embedding_client
    if _embedding_client is None:
        with _embedding_lock:
            if _embedding_client is None:
                _api_cfg = get_api_config()
                _embedding_client = BatchEmbeddingClient(
                    api_base=_api_cfg['embedding_url'],
                    api_key=_api_cfg['api_key'],
                    model=get_env_or_config('EMBEDDING_MODEL', 'embedding.model', 'text-embedding-3-large'),
                    timeout=get_env_or_config('EMBEDDING_TIMEOUT', 'embedding.timeout', 60),
                    expected_dim=get_env_or_config('EMBEDDING_EXPECTED_DIM', 'embedding.expected_dim', 3072),
                )
    return _embedding_client


def call_llm(prompt: str, model: str = None, system_prompt: str = None,
             models: List[str] = None, response_json: bool = True) -> dict:
    """
    統一的 LLM API 呼叫。

    Args:
        prompt: 使用者提示文字
        model: 指定模型（若無，從 models 輪循）
        system_prompt: 系統提示（可選）
        models: 模型清單（用於輪循 fallback，需和 model 擇一提供）
        response_json: 是否預期回傳 JSON（預設 True）

    Returns:
        解析後的 dict（response_json=True 時），否則回傳原始字串
    """
    if not model and not models:
        models = get_models()
    if not model and models:
        model = models[0]

    models_for_retry = models or [model]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        current_model = models_for_retry[(attempt - 1) % len(models_for_retry)]
        payload = {
            "model": current_model,
            "messages": messages,
            "timeout": TIMEOUT_SEC,
        }
        try:
            resp = _get_session().post(
                f"{API_BASE_URL.rstrip('/')}{CHAT_ENDPOINT}",
                headers=headers, json=payload, timeout=TIMEOUT_SEC + 10
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError("LLM returned null content")
            content = content.strip()
            if response_json:
                m = re.search(r'\{.*\}', content, re.DOTALL)
                return json.loads(m.group()) if m else json.loads(content)
            return {"text": content, "model": current_model}
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} attempts: {last_error}")


def extract_participants(chunks: List[Dict]) -> List[str]:
    """
    僅使用影片開頭的幾個 chunk 來提取參與者。
    因為節目來賓介紹通常集中在開場，這樣可以減少 token 消耗並降低幻覺。
    """
    PARTICIPANT_CHUNKS = get_env_or_config('PARTICIPANT_CHUNKS', 'summarization.participant_chunks', 3)
    eligible = chunks[:PARTICIPANT_CHUNKS]
    if not eligible:
        _logger.warning("No chunks available for participant extraction")
        return []

    opening_text = '\n'.join(c['text_content'] for c in eligible)
    total_chars = len(opening_text)
    _logger.info(f"Extracting participants from first {len(eligible)} chunks ({total_chars} chars)...")

    prompt = (
        "以下是一個影片開場字幕。請從對話中找出實際有在節目中發言的人（主持人、來賓）。"
        "判斷依據：該人物有使用第一人稱發言、被主持人介紹為來賓、或參與對話輪替。"
        "不要列出被討論但沒有實際發言的人物（例如書的作者、歷史人物、名人、專家等）。"
        "只輸出實際參與對話的真實人名或常用暱稱。"
        "用 JSON 格式回傳：\n"
        '{"participants": ["名字 1", "名字 2", ...]}'
        f"\n\n開場字幕：\n{opening_text}"
    )

    models = get_models()
    _logger.info(f"Using participant extraction models: {models[:3]}... (total {len(models)} models)")

    for attempt in range(1, MAX_RETRIES + 1):
        model = models[(attempt - 1) % len(models)]
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一個影片分析助理，專門識別節目中的講者。"},
                {"role": "user", "content": prompt},
            ],
            "timeout": 120,
        }
        try:
            resp = _get_session().post(
                f"{API_BASE_URL.rstrip('/')}{CHAT_ENDPOINT}",
                headers=headers, json=payload, timeout=150
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if content is None:
                raise ValueError("LLM returned null content")
            content = content.strip()
            m = re.search(r'\{[^{}]*\}', content, re.DOTALL)
            data = json.loads(m.group()) if m else json.loads(content)
            participants = data.get("participants", [])
            _logger.info(f"Participants extracted ({model}): {participants}")
            return participants
        except Exception as exc:
            _logger.warning(f"Participants extraction attempt {attempt} with {model}: {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

    _logger.warning(f"All {MAX_RETRIES} participants extraction attempts failed")
    return []
