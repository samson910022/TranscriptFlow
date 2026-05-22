#!/usr/bin/env python3
"""
統一配置加載模組

從 config.json 讀取預設值，環境變數具有最高優先級。
提供統一的參數存取接口，並包含基本驗證與安全檢查。
"""

import os
import json
import re
import ipaddress
import socket
from pathlib import Path
from typing import Any, Optional, List
from pydantic import BaseModel, Field, field_validator

from dotenv import load_dotenv
load_dotenv()

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = Path(os.getenv('TRANSCRIPTFLOW_CONFIG', PROJECT_ROOT / 'config.json'))
EXAMPLE_CONFIG_PATH = SCRIPT_DIR / 'config.example.json'

_config = None


def get_config() -> dict:
    """
    載入並返回配置字典（單例模式）
    """
    global _config
    if _config is None:
        path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(f"配置檔案不存在：{CONFIG_PATH} 或 {EXAMPLE_CONFIG_PATH}")

        with open(path, 'r', encoding='utf-8') as f:
            _config = json.load(f)
    
    return _config


def get_env_or_config(env_var: str, config_path: str, default: Any = None) -> Any:
    """
    優先讀取環境變數，其次從 config.json 讀取，最後使用預設值
    
    Args:
        env_var: 環境變數名稱
        config_path: config.json 中的路徑（使用點號分隔，如 'chunking.window_size'）
        default: 預設值
    
    Returns:
        對應的值
    """
    # 優先檢查環境變數
    env_val = os.getenv(env_var)
    if env_val is not None:
        # 嘗試自動轉換類型
        return _convert_value(env_val, default)
    
    # 從 config.json 讀取
    config = get_config()
    parts = config_path.split('.')
    val = config
    
    for p in parts:
        if isinstance(val, dict) and p in val:
            val = val[p]
        else:
            return default
    
    return val if val is not None else default


def _convert_value(str_val: str, default: Any) -> Any:
    """
    嘗試將字串轉換為合適的類型
    """
    if default is None:
        return str_val
    
    if isinstance(default, bool):
        return str_val.lower() in ('true', '1', 'yes', 'on')
    elif isinstance(default, int):
        try:
            return int(str_val)
        except ValueError:
            return default
    elif isinstance(default, float):
        try:
            return float(str_val)
        except ValueError:
            return default
    elif isinstance(default, list):
        # 對於列表，嘗試解析 JSON
        try:
            parsed = json.loads(str_val)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return default
    else:
        return str_val


def get_nested_config(path: str, default: Any = None) -> Any:
    """
    直接從 config.json 讀取巢狀值
    
    Args:
        path: config.json 中的路徑（使用點號分隔）
        default: 預設值
    
    Returns:
        對應的值
    """
    config = get_config()
    parts = path.split('.')
    val = config
    
    for p in parts:
        if isinstance(val, dict) and p in val:
            val = val[p]
        else:
            return default
    
    return val if val is not None else default


class ApiConfigSchema(BaseModel):
    base_url: str = Field(default="https://api.openai.com")
    chat_completions_path: str = "/v1/chat/completions"
    embeddings_path: str = "/v1/embeddings"
    models_path: str = "/v1/models"
    api_timeout: int = Field(default=60, ge=1, le=300)

class ChunkingSchema(BaseModel):
    smart_merge_window_size: int = Field(default=5, ge=1, le=20)
    smart_merge_strong_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    smart_merge_weak_pct: float = Field(default=0.05, ge=0.0, le=1.0)
    smart_merge_min_sentences: int = Field(default=8, ge=1)
    smart_merge_noise_drop_len: int = Field(default=2, ge=0)
    smart_merge_noise_weak_len: int = Field(default=3, ge=0)
    min_chunks: int = Field(default=2, ge=1)
    max_chunks: int = Field(default=200, ge=1)

    @field_validator("max_chunks")
    @classmethod
    def max_gte_min(cls, v, info):
        data = info.data
        if "min_chunks" in data and v < data["min_chunks"]:
            raise ValueError(f"max_chunks ({v}) < min_chunks ({data['min_chunks']})")
        return v

class EmbeddingSchema(BaseModel):
    model: str = "text-embedding-3-large"
    expected_dim: int = Field(default=3072, gt=0)
    batch_max_size: int = Field(default=64, ge=1, le=256)
    timeout: int = Field(default=60, ge=1)

class PathsSchema(BaseModel):
    output_dir: str = "./output"
    db_path: str = "./lancedb"
    master_file: str = "./examples/master_file_manifest.example.json"
    data_dir: str = "./examples/srt"
    backup_dir: str = "./output/lance_backup"

class SummarizationSchema(BaseModel):
    models: List[str] = ["gpt-4.1-mini"]
    participant_chunks: int = Field(default=3, ge=1)
    max_retries: int = Field(default=3, ge=0)
    concurrency: int = Field(default=5, ge=1, le=50)
    timeout_sec: int = Field(default=120, ge=1)

class ConfigSchema(BaseModel):
    api: ApiConfigSchema
    chunking: ChunkingSchema = ChunkingSchema()
    embedding: EmbeddingSchema = EmbeddingSchema()
    paths: PathsSchema = PathsSchema()
    summarization: SummarizationSchema = SummarizationSchema()


def validate_config() -> list:
    """
    驗證配置參數的合理性

    Returns:
        錯誤訊息清單（無錯誤時為空清單）
    """
    errors = []
    config = get_config()

    # Pydantic schema validation
    try:
        ConfigSchema(**config)
    except Exception as e:
        errors.append(f"結構驗證失敗: {e}")

    # 驗證 chunking 參數 (legacy)
    window_size = config.get('chunking', {}).get('smart_merge_window_size', 5)
    if not (1 <= window_size <= 20):
        errors.append(f"smart_merge_window_size 必須介於 1~20，當前值：{window_size}")

    max_duration = config.get('chunking', {}).get('max_chunk_duration_sec', 300)
    if max_duration < 30:
        errors.append(f"max_chunk_duration_sec 必須 >= 30，當前值：{max_duration}")

    min_chunks_val = config.get('chunking', {}).get('min_chunks', 2)
    if min_chunks_val < 1:
        errors.append(f"min_chunks 必須 >= 1，當前值：{min_chunks_val}")

    max_chunks_val = config.get('chunking', {}).get('max_chunks', 200)
    if max_chunks_val < min_chunks_val:
        errors.append(f"max_chunks ({max_chunks_val}) 必須 >= min_chunks ({min_chunks_val})")

    # 驗證 embedding 參數
    embed_dim = config.get('embedding', {}).get('expected_dim', 3072)
    if embed_dim <= 0:
        errors.append(f"expected_dim 必須 > 0，當前值：{embed_dim}")

    # 驗證 summarization 參數
    participant_chunks_val = config.get('summarization', {}).get('participant_chunks', 3)
    if participant_chunks_val < 1:
        errors.append(f"participant_chunks 必須 >= 1，當前值：{participant_chunks_val}")

    max_retries_val = config.get('summarization', {}).get('max_retries', 3)
    if max_retries_val < 0:
        errors.append(f"max_retries 必須 >= 0，當前值：{max_retries_val}")

    return errors


def validate_path(path: str, allowed_base: str = None) -> tuple[bool, str]:
    """
    驗證路徑是否合法，防止路徑穿越攻擊
    
    Args:
        path: 要驗證的路徑
        allowed_base: 允許的基础目录（可选）
    
    Returns:
        (是否合法, 訊息)
    """
    if not path:
        return False, "路徑為空"
    
    try:
        real_path = os.path.realpath(path)
        
        # 檢查是否包含 .. 等危險路徑元件
        if '..' in path.split(os.sep):
            # 允許 .. 但需要檢查解析後的實際位置
            if allowed_base:
                real_base = os.path.realpath(allowed_base)
                if not real_path.startswith(real_base + os.sep) and real_path != real_base:
                    return False, f"路徑 {path} 超出允許範圍 {allowed_base}"
        
        return True, "路徑合法"
    except Exception as e:
        return False, f"路徑驗證失敗: {e}"


def sanitize_api_url(url: str) -> tuple[bool, str]:
    """
    驗證 API URL 的安全性

    策略：
    1. 允許 HTTPS 連接（最安全）
    2. 允許 HTTP 連接，但僅限於受信任的內部網路（使用 ipaddress 模組判斷）

    Args:
        url: API URL

    Returns:
        (是否合法, 訊息)
    """
    if not url:
        return False, "URL 為空"

    # 允許 HTTPS，無需檢查
    if url.startswith('https://'):
        return True, "URL 合法 (HTTPS)"

    # 僅處理 HTTP 連接
    if url.startswith('http://'):
        allow_insecure = os.getenv('ALLOW_INSECURE_HTTP', '').lower() in ('1', 'true', 'yes')

        if allow_insecure:
            logger.warning("ALLOW_INSECURE_HTTP 已啟用，允許所有 HTTP 連接（僅限開發測試）")
            return True, "URL 合法 (強制允許 HTTP)"

        try:
            host_part = url.split('http://')[1].split('/')[0].split(':')[0]
            if host_part in ('localhost', '127.0.0.1', '::1'):
                return True, f"URL 合法 (localhost: {host_part})"
            try:
                ip = socket.gethostbyname(host_part)
                if ipaddress.ip_address(ip).is_private:
                    return True, f"URL 合法 (內部網路 IP: {ip})"
            except Exception:
                pass
            return False, f"安全阻止：HTTP 連接至外部網路 ({host_part})。請使用 HTTPS。"
        except Exception as e:
            return False, f"URL 解析失敗: {e}"

    return False, f"不支持的協議: {url.split(':')[0]}"

# 增加一個日誌 helper，避免循環引用
import logging
logger = logging.getLogger(__name__)


def get_api_config() -> dict:
    """
    取得 OpenAI-compatible API 配置（優先環境變數，fallback 到 config.json）
    
    優先級：
    1. OPENAI_BASE_URL / OPENAI_API_KEY
    2. Legacy LITELLM_PROXY_URL / LITELLM_PROXY_KEY
    3. config.json api.base_url / api.api_key
    4. Legacy config.json api.primary.proxy_url
    
    Returns:
        {
            'base_url': str,
            'proxy_url': str,
            'embedding_url': str,
            'api_key': str,
            'api_timeout': int,
            'chat_completions_path': str,
            'embeddings_path': str,
            'models_path': str
        }
    """
    config = get_config()
    api_cfg = config.get('api', {})
    
    # 環境變數優先
    base_url = (
        os.getenv('OPENAI_BASE_URL')
        or os.getenv('LITELLM_PROXY_URL')
        or os.getenv('EMBEDDING_API_BASE')
    )
    embedding_url = os.getenv('EMBEDDING_API_BASE')
    api_key = os.getenv('OPENAI_API_KEY') or os.getenv('LITELLM_PROXY_KEY')
    
    if not base_url:
        base_url = api_cfg.get('base_url', '')

    if not base_url:
        primary = api_cfg.get('primary', {})
        base_url = primary.get('proxy_url', '')

    # OpenAI-compatible endpoints normally share one base URL.
    embedding_url = embedding_url or base_url

    if not api_key:
        api_key = api_cfg.get('api_key', '')
    
    api_timeout = api_cfg.get('api_timeout', 60)
    chat_completions_path = api_cfg.get('chat_completions_path', '/v1/chat/completions')
    embeddings_path = api_cfg.get('embeddings_path', '/v1/embeddings')
    models_path = api_cfg.get('models_path', '/v1/models')
    
    return {
        'base_url': base_url,
        # Backward-compatible alias for older call sites.
        'proxy_url': base_url,
        'embedding_url': embedding_url,
        'api_key': api_key,
        'api_timeout': api_timeout,
        'chat_completions_path': chat_completions_path,
        'embeddings_path': embeddings_path,
        'models_path': models_path
    }


def get_fallback_api_config() -> dict:
    """
    取得備援 API 配置
    """
    config = get_config()
    api_cfg = config.get('api', {})
    fallback = api_cfg.get('fallback', {})
    
    return {
        'proxy_url': fallback.get('proxy_url', ''),
        'embedding_url': fallback.get('embedding_url', ''),
        'api_key': api_cfg.get('api_key', ''),
        'api_timeout': api_cfg.get('api_timeout', 60)
    }


def get_required_env_vars() -> list:
    """
    返回必須設定的環境變數清單（v1.1起 API key 已移至 config.json，環境變數為可選）
    
    Returns:
        環境變數名稱清單
    """
    return []


def check_required_env_vars() -> tuple[bool, list]:
    """
    檢查必要的配置是否已設定（環境變數 或 config.json）
    
    Returns:
        (是否全部設定, 未設定的項目清單)
    """
    missing = []
    
    # 檢查 API key（環境變數 或 config.json）
    api_key = os.getenv('OPENAI_API_KEY') or os.getenv('LITELLM_PROXY_KEY') or get_nested_config('api.api_key')
    if not api_key:
        missing.append('api.api_key (config.json) 或 OPENAI_API_KEY (env)')
    
    return len(missing) == 0, missing


def ensure_secure_permissions(path: str, mode: int = 0o700) -> tuple[bool, str]:
    """
    確保目錄或檔案具有安全的權限
    
    Args:
        path: 路徑
        mode: 權限模式（預設 0o700，僅所有者可訪問）
    
    Returns:
        (是否成功, 訊息)
    """
    try:
        if os.path.exists(path):
            os.chmod(path, mode)
            return True, f"已設置權限 {oct(mode)} 於 {path}"
        else:
            return False, f"路徑不存在：{path}"
    except Exception as e:
        return False, f"設置權限失敗: {e}"
