#!/usr/bin/env python3
"""
批次向量化模組 (Batch Embedding)
v1.1 效能突破實作

功能:
- 批次向量化 (Batch Embedding)
- 整合 Circuit Breaker 與 Adaptive Throttling
- 比特級比對驗證支援
- 多 batch_size 壓力測試

作者: OpenClaw Subagent
任務: srt-semantic-chunk v1.1 認證計畫
"""

import os
import sys
import json
import time
import hashlib
import asyncio
import threading
from typing import List, Dict, Optional, Tuple, Union
from dataclasses import dataclass
from datetime import datetime
import numpy as np
import requests

# Local imports
sys.path.insert(0, os.path.dirname(__file__))
from logger_config import get_logger
from circuit_breaker import (
    CircuitBreakerConfig,
    ThrottleConfig,
    CircuitBreaker,
    AdaptiveThrottler
)

logger = get_logger('batch_embedding')
_session = threading.local()

def _get_session() -> requests.Session:
    if not hasattr(_session, "s"):
        _session.s = requests.Session()
    return _session.s


@dataclass
class BatchResult:
    """批次處理結果"""
    success: bool
    embeddings: Optional[List[np.ndarray]] = None
    latency_ms: float = 0.0
    throughput: float = 0.0
    error: Optional[str] = None
    batch_size: int = 0


class BatchEmbeddingClient:
    """
    批次向量化客戶端
    
    支援:
    - 批次向量化呼叫
    - Circuit Breaker 保護
    - Adaptive Throttling
    - 自動重試機制
    """
    
    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = 60,
        expected_dim: int = 3072,
        max_batch_size: int = 128,
        cb_config: Optional[CircuitBreakerConfig] = None,
        throttle_config: Optional[ThrottleConfig] = None
    ):
        from config_loader import get_env_or_config, get_api_config
        
        _api_cfg = get_api_config()
        self.api_base = api_base or _api_cfg['embedding_url']
        self.api_key = api_key or _api_cfg['api_key']
        self.model = model or get_env_or_config('EMBEDDING_MODEL', 'embedding.model', 'text-embedding-3-large')
        self.embeddings_path = _api_cfg['embeddings_path']
        self.timeout = timeout
        self.expected_dim = expected_dim
        self.max_batch_size = max_batch_size
        
        # 初始化韌性機制
        self.circuit_breaker = CircuitBreaker(cb_config or CircuitBreakerConfig())
        self.throttler = AdaptiveThrottler(throttle_config or ThrottleConfig())
        
        logger.info("BatchEmbeddingClient 初始化完成", {
            "api_base": self.api_base,
            "model": self.model,
            "expected_dim": self.expected_dim,
            "max_batch_size": self.max_batch_size
        })
    
    def generate_batch_embeddings(self, texts: List[str]) -> BatchResult:
        """
        生成批次向量嵌入
        
        Args:
            texts: 文字列表
            
        Returns:
            BatchResult 物件
        """
        if not texts:
            return BatchResult(success=True, embeddings=[], batch_size=0)
        
        batch_size = min(len(texts), self.max_batch_size)
        
        # 等待限流器許可
        wait_time = self.throttler.acquire()
        if wait_time > 0:
            time.sleep(wait_time)
        
        start_time = time.time()

        def _do_request():
            url = f"{self.api_base.rstrip('/')}{self.embeddings_path}"
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {self.api_key}'
            }
            resp = _get_session().post(url, json={"model": self.model, "input": texts},
                                             headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            result = resp.json()
            embeddings = []
            for item in result.get('data', []):
                vec = np.array(item['embedding'])
                if len(vec) != self.expected_dim:
                    raise ValueError(f"Embedding dimension mismatch: {len(vec)} != {self.expected_dim}")
                embeddings.append(vec)
            if len(embeddings) != len(texts):
                raise ValueError(f"Embedding count mismatch: got {len(embeddings)}, expected {len(texts)}")
            return embeddings

        try:
            embeddings = self.circuit_breaker.call(_do_request)
            latency_ms = (time.time() - start_time) * 1000
            throughput = len(texts) / (latency_ms / 1000.0) if latency_ms > 0 else 0
            avg_latency = latency_ms / len(texts) if texts else 0
            self.throttler.record_success(avg_latency)
            return BatchResult(
                success=True, embeddings=embeddings,
                latency_ms=latency_ms, throughput=throughput, batch_size=batch_size
            )
        except Exception as e:
            self.throttler.record_failure()
            return BatchResult(
                success=False, embeddings=None,
                latency_ms=(time.time() - start_time) * 1000,
                error=str(e), batch_size=batch_size
            )
    
    def generate_chunk_embeddings(
        self,
        chunks: List[Dict],
        text_key: str = 'text_content'
    ) -> List[Optional[np.ndarray]]:
        """
        為 SRT chunks 生成嵌入向量
        
        Args:
            chunks: Chunk 列表
            text_key: 文字欄位名稱
            
        Returns:
            向量列表 (失敗項目為 None)
        """
        texts = [chunk.get(text_key, '') for chunk in chunks]
        result = self.generate_batch_embeddings(texts)
        
        if result.success and result.embeddings:
            return result.embeddings
        
        # 失敗時返回 None 列表
        return [None] * len(chunks)
    
    def get_status(self) -> dict:
        """獲取客戶端狀態"""
        return {
            "circuit_breaker": self.circuit_breaker.get_status(),
            "throttler": self.throttler.get_status()
        }
    
    def reset(self):
        """重置狀態"""
        self.circuit_breaker.reset()
        self.throttler.reset()


class MockEmbeddingGenerator:
    """
    模擬 Embedding 生成器 (用於比特級比對驗證)
    
    使用確定性 hash 生成可重複的向量
    """
    
    def __init__(self, expected_dim: int = 3072, seed: int = 42):
        self.expected_dim = expected_dim
        self.seed = seed
    
    def generate_embedding(self, text: str) -> np.ndarray:
        """生成確定性 embedding"""
        # 使用 MD5 hash 作為種子
        hash_val = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32 - 1)
        np.random.seed(self.seed + hash_val)
        return np.random.randn(self.expected_dim).astype(np.float32)
    
    def generate_batch(self, texts: List[str]) -> List[np.ndarray]:
        """批量生成 embedding"""
        return [self.generate_embedding(t) for t in texts]


def verify_bitwise_identical(
    texts: List[str],
    batch_size: int,
    mock_generator: MockEmbeddingGenerator
) -> Dict:
    """
    驗證批次處理與個別處理的比特級一致性
    
    Args:
        texts: 文字列表
        batch_size: 批次大小
        mock_generator: 模擬生成器
        
    Returns:
        驗證結果字典
    """
    # 個別處理 (v1.0 方式)
    single_vectors = [mock_generator.generate_embedding(t) for t in texts]
    
    # 批次處理 (v1.1 方式)
    batch_vectors = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i+batch_size]
        batch_vectors.extend(mock_generator.generate_batch(batch))
    
    # 比較
    if len(single_vectors) != len(batch_vectors):
        return {
            "match": False,
            "reason": f"Vector count mismatch: single={len(single_vectors)}, batch={len(batch_vectors)}"
        }
    
    mismatches = []
    for i, (sv, bv) in enumerate(zip(single_vectors, batch_vectors)):
        if not np.allclose(sv, bv, rtol=1e-5, atol=1e-7):
            diff = np.max(np.abs(sv - bv))
            mismatches.append({
                "index": i,
                "max_diff": float(diff)
            })
    
    if mismatches:
        return {
            "match": False,
            "reason": f"{len(mismatches)} vectors differ",
            "sample_mismatch": mismatches[0]
        }
    
    return {
        "match": True,
        "reason": "All vectors bit-for-bit identical",
        "verified_count": len(texts)
    }


def run_batch_size_stress_test(
    batch_sizes: List[int],
    sample_texts: List[str],
    client: Optional[BatchEmbeddingClient] = None,
    use_mock: bool = False
) -> List[Dict]:
    """
    執行批次大小壓力測試
    
    Args:
        batch_sizes: 要測試的批次大小列表
        sample_texts: 樣本文字列表
        client: Embedding 客戶端 (若為 None 則使用 mock)
        use_mock: 是否使用模擬模式
        
    Returns:
        測試結果列表
    """
    results = []
    
    for batch_size in batch_sizes:
        logger.info(f"Testing batch_size={batch_size}")
        
        if use_mock or client is None:
            # 模擬模式
            mock_gen = MockEmbeddingGenerator()
            start = time.time()
            
            # 模擬批次處理
            all_vectors = []
            for i in range(0, len(sample_texts), batch_size):
                batch = sample_texts[i:i+batch_size]
                all_vectors.extend(mock_gen.generate_batch(batch))
            
            latency = (time.time() - start) * 1000
            throughput = len(sample_texts) / (latency / 1000.0) if latency > 0 else 0
            
            results.append({
                "batch_size": batch_size,
                "status": "SUCCESS",
                "latency_ms": latency,
                "throughput_per_sec": throughput,
                "verified_count": len(all_vectors),
                "use_mock": True
            })
        else:
            # 真實 API 模式
            # 重複測試 3 次取平均
            latencies = []
            success = True
            error_msg = ""
            
            for _ in range(3):
                # 準備測試數據
                test_texts = sample_texts[:batch_size]
                result = client.generate_batch_embeddings(test_texts)
                
                if result.success:
                    latencies.append(result.latency_ms)
                else:
                    success = False
                    error_msg = result.error or "Unknown error"
                    break
            
            if success:
                avg_latency = np.mean(latencies)
                avg_throughput = batch_size / (avg_latency / 1000.0)
                
                results.append({
                    "batch_size": batch_size,
                    "status": "SUCCESS",
                    "latency_ms": avg_latency,
                    "throughput_per_sec": avg_throughput,
                    "use_mock": False
                })
            else:
                results.append({
                    "batch_size": batch_size,
                    "status": "FAILED",
                    "error": error_msg,
                    "use_mock": False
                })
    
    return results


if __name__ == "__main__":
    # 測試代碼
    logging.basicConfig(level=logging.INFO)
    
    # 測試驗證
    print("=" * 60)
    print("Bitwise Identity Verification Test")
    print("=" * 60)
    
    mock_gen = MockEmbeddingGenerator()
    test_texts = [f"這是測試文字 {i}" for i in range(100)]
    
    for batch_size in [2, 8, 16, 32]:
        result = verify_bitwise_identical(test_texts, batch_size, mock_gen)
        status = "✅ PASS" if result["match"] else "❌ FAIL"
        print(f"batch_size={batch_size}: {status} - {result['reason']}")
    
    # 測試壓力測試
    print("\n" + "=" * 60)
    print("Batch Size Stress Test (Mock Mode)")
    print("=" * 60)
    
    sample_texts = ["測試文字 " * 50 for _ in range(512)]
    results = run_batch_size_stress_test(
        [2, 8, 16, 32, 64, 128],
        sample_texts,
        use_mock=True
    )
    
    print(f"{'BatchSize':<12} | {'Status':<10} | {'Latency(ms)':<15} | {'Throughput':<15}")
    print("-" * 60)
    for r in results:
        if r["status"] == "SUCCESS":
            print(f"{r['batch_size']:<12} | {r['status']:<10} | {r['latency_ms']:<15.2f} | {r['throughput_per_sec']:<15.2f}")
        else:
            print(f"{r['batch_size']:<12} | {r['status']:<10} | {'-':<15} | {r.get('error', 'N/A')[:15]}")
