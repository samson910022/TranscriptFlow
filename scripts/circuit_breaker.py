#!/usr/bin/env python3
"""
Circuit Breaker 與 Adaptive Throttling 實作模組
v1.1 韌性強化機制

作者: OpenClaw Subagent
任務: srt-semantic-chunk v1.1 認證計畫
"""

import time
import json
import threading
from enum import Enum
from typing import Optional, List, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque
import logging

logger = logging.getLogger('circuit_breaker')


class CircuitBreakerOpenError(Exception):
    """Circuit breaker 斷開時拋出的例外"""
    def __init__(self, message="Circuit breaker is OPEN", cause=None):
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause


class CircuitState(Enum):
    """電路斷開器狀態"""
    CLOSED = "closed"      # 正常運作
    OPEN = "open"          # 斷開，拒絕請求
    HALF_OPEN = "half_open"  # 半開，測試是否恢復


@dataclass
class CircuitBreakerConfig:
    """電路斷開器配置"""
    failure_threshold: int = 5           # 失敗次數閾值，超過則開啟電路
    success_threshold: int = 3           # 成功次數閾值，半開時超過則關閉電路
    timeout: float = 30.0                # 電路開啟的超時時間 (秒)
    half_open_max_calls: int = 3         # 半開狀態下最大允許的測試呼叫數
    fallback_enabled: bool = True        # 是否啟用 fallback
    record_errors: bool = True           # 是否記錄錯誤歷史


@dataclass
class ThrottleConfig:
    """自適應限流配置"""
    initial_rate: float = 10.0           # 初始請求速率 (requests/sec)
    min_rate: float = 1.0                # 最小速率
    max_rate: float = 50.0               # 最大速率
    rate_increase_factor: float = 1.2    # 速率增加倍數
    rate_decrease_factor: float = 0.5    # 速率減少倍數
    error_rate_threshold: float = 0.3    # 錯誤率閾值 (30%)
    latency_threshold_ms: float = 1000.0 # 延遲閾值 (毫秒)
    window_size: int = 100               # 統計窗口大小


@dataclass
class Metrics:
    """監控指標"""
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    retried_calls: int = 0
    rejected_calls: int = 0  # 因電路斷開被拒絕
    total_latency_ms: float = 0.0
    error_history: deque = field(default_factory=lambda: deque(maxlen=100))
    latency_history: deque = field(default_factory=lambda: deque(maxlen=100))
    
    @property
    def success_rate(self) -> float:
        if self.total_calls == 0:
            return 1.0
        return self.successful_calls / self.total_calls
    
    @property
    def error_rate(self) -> float:
        return 1.0 - self.success_rate
    
    @property
    def avg_latency_ms(self) -> float:
        if not self.latency_history:
            return 0.0
        return sum(self.latency_history) / len(self.latency_history)


class CircuitBreaker:
    """
    電路斷開器 (Circuit Breaker) 實作
    
    狀態轉換:
    - CLOSED → OPEN: 當失敗次數 >= failure_threshold
    - OPEN → HALF_OPEN: 當 timeout 到期
    - HALF_OPEN → CLOSED: 當成功次數 >= success_threshold
    - HALF_OPEN → OPEN: 當任何失敗發生
    """
    
    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.half_open_calls = 0
        self._lock = threading.Lock()
        self.metrics = Metrics()
        
        logger.info("Circuit Breaker 已初始化", {
            "failure_threshold": self.config.failure_threshold,
            "timeout": self.config.timeout,
            "fallback_enabled": self.config.fallback_enabled
        })
    
    def __call__(self, func: Callable) -> Callable:
        """裝飾器模式"""
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        return wrapper
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """執行帶有電路斷開器保護的呼叫"""
        with self._lock:
            self._check_state_transition()
            self.metrics.total_calls += 1
            
            if self.state == CircuitState.OPEN:
                self.metrics.rejected_calls += 1
                logger.warning(f"⚡ 電路斷開，拒絕請求 (半開測試呼叫數: {self.half_open_calls})")
                if self.config.fallback_enabled:
                    return self._invoke_fallback(func, *args, **kwargs)
                raise Exception("Circuit Breaker is OPEN")
            
            if self.state == CircuitState.HALF_OPEN:
                if self.half_open_calls >= self.config.half_open_max_calls:
                    logger.warning("半開狀態已達最大測試呼叫數，繼續拒絕")
                    self.metrics.rejected_calls += 1
                    if self.config.fallback_enabled:
                        return self._invoke_fallback(func, *args, **kwargs)
                    raise Exception("Circuit Breaker is OPEN (half-open limit)")
                self.half_open_calls += 1
        
        # 實際執行
        start_time = time.time()
        try:
            result = func(*args, **kwargs)
            latency_ms = (time.time() - start_time) * 1000
            
            with self._lock:
                self._on_success()
                self.metrics.successful_calls += 1
                self.metrics.latency_history.append(latency_ms)
                self.metrics.total_latency_ms += latency_ms
            
            return result
            
        except Exception as e:
            with self._lock:
                self._on_failure()
                self.metrics.failed_calls += 1
                self.metrics.error_history.append({
                    "timestamp": datetime.now().isoformat(),
                    "error": str(e)
                })
            raise
    
    def _check_state_transition(self):
        """檢查狀態轉換"""
        if self.state == CircuitState.OPEN:
            if self.last_failure_time and \
               (time.time() - self.last_failure_time) >= self.config.timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                self.success_count = 0
                logger.info("🔄 電路轉為 HALF_OPEN，開始測試恢復")
    
    def _on_success(self):
        """成功處理"""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.config.success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                self.half_open_calls = 0
                logger.info("✅ 電路已關閉，系統恢復正常")
    
    def _on_failure(self):
        """失敗處理"""
        self.last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            # 半開狀態下任何失敗都立即切回 OPEN
            self.state = CircuitState.OPEN
            self.half_open_calls = 0
            self.success_count = 0
            logger.error(f"❌ 半開狀態失敗，電路切回 OPEN (總失敗次數: {self.failure_count})")
        else:
            self.failure_count += 1
            if self.failure_count >= self.config.failure_threshold:
                self.state = CircuitState.OPEN
                logger.error(f"🚨 達到失敗閾值 ({self.failure_count})，電路斷開!")
    
    def _invoke_fallback(self, func: Callable, *args, **kwargs) -> Any:
        """觸發 fallback 機制 — 拋出例外，不回傳假資料"""
        logger.info("Circuit breaker OPEN，觸發 fallback")
        self.metrics.retried_calls += 1
        raise CircuitBreakerOpenError("Circuit breaker is OPEN")
    
    def get_status(self) -> dict:
        """獲取當前狀態"""
        with self._lock:
            return {
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "half_open_calls": self.half_open_calls,
                "last_failure_time": self.last_failure_time,
                "metrics": {
                    "total_calls": self.metrics.total_calls,
                    "successful_calls": self.metrics.successful_calls,
                    "failed_calls": self.metrics.failed_calls,
                    "rejected_calls": self.metrics.rejected_calls,
                    "success_rate": self.metrics.success_rate,
                    "error_rate": self.metrics.error_rate,
                    "avg_latency_ms": self.metrics.avg_latency_ms
                }
            }
    
    def reset(self):
        """重置電路斷開器"""
        with self._lock:
            self.state = CircuitState.CLOSED
            self.failure_count = 0
            self.success_count = 0
            self.half_open_calls = 0
            self.last_failure_time = None
            logger.info("🔄 Circuit Breaker 已重置")


class AdaptiveThrottler:
    """
    自適應限流器 (Adaptive Throttling)
    
    根據系統負載自動調整請求速率:
    - 高成功率 + 低延遲 → 增加速率
    - 高錯誤率 + 高延遲 → 減少速率
    """
    
    def __init__(self, config: Optional[ThrottleConfig] = None):
        self.config = config or ThrottleConfig()
        self.current_rate = self.config.initial_rate
        self._lock = threading.Lock()
        self.last_request_time = 0.0
        self.metrics = Metrics()
        
        logger.info("Adaptive Throttler 已初始化", {
            "initial_rate": self.current_rate,
            "min_rate": self.config.min_rate,
            "max_rate": self.config.max_rate
        })
    
    def acquire(self) -> float:
        """
        獲取許可，返回需要等待的時間 (秒)
        """
        with self._lock:
            now = time.time()
            elapsed_since_last = now - self.last_request_time
            
            # 計算最小間隔時間
            min_interval = 1.0 / self.current_rate
            
            if elapsed_since_last < min_interval:
                wait_time = min_interval - elapsed_since_last
                return wait_time
            
            return 0.0
    
    def record_success(self, latency_ms: float):
        """記錄成功請求並調整速率"""
        with self._lock:
            self.metrics.successful_calls += 1
            self.metrics.latency_history.append(latency_ms)
            self.last_request_time = time.time()
            
            # 調整速率邏輯
            if len(self.metrics.latency_history) >= 10:
                avg_latency = self.metrics.avg_latency_ms
                error_rate = self.metrics.error_rate
                
                if error_rate < self.config.error_rate_threshold and \
                   avg_latency < self.config.latency_threshold_ms:
                    # 系統健康，增加速率
                    new_rate = min(self.current_rate * self.config.rate_increase_factor, 
                                  self.config.max_rate)
                    if new_rate > self.current_rate:
                        self.current_rate = new_rate
                        logger.debug(f"⬆️ 增加速率: {self.current_rate:.2f} req/s (latency: {avg_latency:.0f}ms)")
                else:
                    # 系統負載過高，降低速率
                    new_rate = max(self.current_rate * self.config.rate_decrease_factor,
                                 self.config.min_rate)
                    if new_rate < self.current_rate:
                        self.current_rate = new_rate
                        logger.warning(f"⬇️ 降低速率: {self.current_rate:.2f} req/s (error_rate: {error_rate:.2%}, latency: {avg_latency:.0f}ms)")
    
    def record_failure(self):
        """記錄失敗請求"""
        with self._lock:
            self.metrics.failed_calls += 1
            self.last_request_time = time.time()
            
            # 失敗時立即降低速率
            new_rate = max(self.current_rate * self.config.rate_decrease_factor,
                         self.config.min_rate)
            if new_rate < self.current_rate:
                self.current_rate = new_rate
                logger.warning(f"⬇️ 失敗，降低速率: {self.current_rate:.2f} req/s")
    
    def get_status(self) -> dict:
        """獲取當前狀態"""
        with self._lock:
            return {
                "current_rate": self.current_rate,
                "min_rate": self.config.min_rate,
                "max_rate": self.config.max_rate,
                "last_request_time": self.last_request_time,
                "metrics": {
                    "successful_calls": self.metrics.successful_calls,
                    "failed_calls": self.metrics.failed_calls,
                    "avg_latency_ms": self.metrics.avg_latency_ms
                }
            }
    
    def reset(self):
        """重置限流器"""
        with self._lock:
            self.current_rate = self.config.initial_rate
            self.last_request_time = 0.0
            logger.info("🔄 Adaptive Throttler 已重置")


if __name__ == "__main__":
    # 測試代碼
    logging.basicConfig(level=logging.INFO)
    
    # 測試 Circuit Breaker
    cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, timeout=5))
    
    def failing_func():
        raise Exception("Simulated failure")
    
    def success_func():
        return "OK"
    
    print("Testing Circuit Breaker...")
    for i in range(5):
        try:
            cb.call(failing_func)()
        except Exception as e:
            print(f"  Call {i+1}: Failed - {e}")
    
    print(f"Status: {cb.get_status()}")
    
    # 測試 Adaptive Throttler
    print("\nTesting Adaptive Throttler...")
    throttler = AdaptiveThrottler()
    for i in range(10):
        wait = throttler.acquire()
        if wait > 0:
            time.sleep(wait)
        print(f"  Request {i+1}: rate={throttler.current_rate:.2f}")
        throttler.record_success(100.0)  # 假設延遲 100ms
    
    print(f"Status: {throttler.get_status()}")
