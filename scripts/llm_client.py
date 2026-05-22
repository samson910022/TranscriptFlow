"""
llm_client.py — 統一 LLM API 呼叫客戶端

提供共用的 call_llm() 與 get_models()，避免 5 個檔案重複實作相同的模式。
"""

import json
import re
import time
import threading
from typing import List, Optional
import requests

from config_loader import get_api_config, get_env_or_config

_api_cfg = get_api_config()
API_BASE_URL = _api_cfg['base_url']
CHAT_ENDPOINT = _api_cfg['chat_completions_path']
API_KEY = _api_cfg['api_key']
MAX_RETRIES = get_env_or_config('MAX_RETRIES', 'summarization.max_retries', 3)
TIMEOUT_SEC = get_env_or_config('TIMEOUT_SEC', 'summarization.timeout_sec', 120)

_session = threading.local()

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
