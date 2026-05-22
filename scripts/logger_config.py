import logging
import os
import sys
import re

# 將日誌放置於 SRT_OUTPUT_DIR
PROJECT_ROOT = os.getenv('SRT_PROJECT_ROOT', os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../')))
OUTPUT_DIR = os.getenv('SRT_OUTPUT_DIR', './output')
LOG_FILE = os.path.join(OUTPUT_DIR, 'srt_pipeline.log')


class SensitiveDataFilter(logging.Filter):
    """
    敏感資訊過濾器
    自動過濾 API Key 等敏感資訊，防止洩露到日誌中
    """
    def __init__(self):
        super().__init__()
        self.sensitive_patterns = []
        self._update_patterns()
    
    def _update_patterns(self):
        """更新敏感資訊過濾列表"""
        # 優先從 config.json 讀取 API Key
        api_key = None
        try:
            import json
            cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.json')
            if not os.path.exists(cfg_path):
                cfg_path = os.path.join(os.path.dirname(__file__), 'config.json')
            with open(cfg_path, 'r') as f:
                cfg = json.load(f)
                api_key = cfg.get('api', {}).get('api_key', '')
        except Exception:
            pass
        # Fallback 到環境變數
        if not api_key:
            api_key = os.getenv('OPENAI_API_KEY') or os.getenv('LITELLM_PROXY_KEY', '')
        if api_key:
            esc = re.escape(api_key)
            if not any(p.pattern == esc for p in self.sensitive_patterns):
                self.sensitive_patterns.append(re.compile(esc, re.IGNORECASE))
        
        # 其他可能的敏感資訊（可擴展）
    
    def filter(self, record):
        """過濾敏感資訊"""
        # 更新過濾列表（確保最新；僅保留最近 5 個 pattern，避免舊 key 累積）
        self._update_patterns()
        if len(self.sensitive_patterns) > 5:
            self.sensitive_patterns = self.sensitive_patterns[-5:]
        
        # 過濾訊息
        if record.msg:
            record.msg = str(record.msg)
            for pattern in self.sensitive_patterns:
                record.msg = pattern.sub('***REDACTED***', record.msg)
        
        # 過濾額外參數
        if record.args:
            if isinstance(record.args, dict):
                for key, value in list(record.args.items()):
                    if isinstance(value, str):
                        for pattern in self.sensitive_patterns:
                            record.args[key] = pattern.sub('***REDACTED***', value)
            elif isinstance(record.args, (list, tuple)):
                new_args = []
                for arg in record.args:
                    if isinstance(arg, str):
                        for pattern in self.sensitive_patterns:
                            arg = pattern.sub('***REDACTED***', arg)
                        new_args.append(arg)
                    else:
                        new_args.append(arg)
                record.args = tuple(new_args)
        
        # 過濾 exc_info / exc_text（traceback 可能包含 key 片段）
        if record.exc_info and record.exc_info[1]:
            try:
                msg = str(record.exc_info[1])
                for pattern in self.sensitive_patterns:
                    msg = pattern.sub('***REDACTED***', msg)
                record.exc_info = (record.exc_info[0], type(record.exc_info[1])(msg), record.exc_info[2])
            except Exception:
                pass
        if record.exc_text:
            for pattern in self.sensitive_patterns:
                record.exc_text = pattern.sub('***REDACTED***', record.exc_text)
        
        return True


def get_logger(name):
    # 確保日誌目錄存在並設置安全權限
    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        # 設置目錄權限為 0o700（僅所有者可訪問）
        try:
            os.chmod(log_dir, 0o700)
        except OSError:
            pass  # 權限設置失敗不影響功能
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    
    # 添加敏感資訊過濾器
    sensitive_filter = SensitiveDataFilter()
    
    # 防止重複添加 handler
    if not logger.handlers:
        # File handler: 記錄所有細節到檔案 (DEBUG 以上)
        file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_handler.addFilter(sensitive_filter)
        logger.addHandler(file_handler)
        
        # 設置日誌檔案權限為 0o600（僅所有者可讀寫）
        try:
            if os.path.exists(LOG_FILE):
                os.chmod(LOG_FILE, 0o600)
        except OSError:
            pass
        
        # Console handler: 記錄重要訊息到終端 (INFO 以上)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        console_handler.addFilter(sensitive_filter)
        logger.addHandler(console_handler)
    
    return logger
