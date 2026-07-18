#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Longport 日线行情数据接口（多市场通用：美股 .US / 港股 .HK / A股 .SZ .SH 等）

主要导出：
    fetch_daily_bars(symbol, start, end)  -> pd.DataFrame  # 带本地缓存
    get_api_singleton()                   -> LongportAPI   # 线程安全单例

CLI:
    python longport_api.py AAPL.US 2024-01-01 2024-12-31
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv
from longport.openapi import AdjustType, Config, Period, QuoteContext

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _candle_ts_to_date(ts) -> date:
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return datetime.fromtimestamp(ts).date()


def _index_to_date(idx) -> date:
    if isinstance(idx, datetime):
        return idx.date()
    if isinstance(idx, date):
        return idx
    return pd.Timestamp(idx).date()


class LongportAPI:
    """Longport 日线 API 封装（含重试与单次区间合并拉取）。"""

    def __init__(self, max_retries: int = 3, retry_delay: float = 1.0):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.quote_ctx: Optional[QuoteContext] = None

        init_retries = int(os.getenv('LONGPORT_INIT_RETRIES', '5'))
        init_base_delay = float(os.getenv('LONGPORT_INIT_RETRY_DELAY', '2.0'))
        last_err: Optional[Exception] = None
        for attempt in range(init_retries):
            try:
                self.config = Config.from_env()
                self.quote_ctx = QuoteContext(self.config)
                logger.info('Longport API 初始化成功')
                last_err = None
                break
            except Exception as e:
                last_err = e
                msg = str(e)
                if 'connections limitation' in msg.lower():
                    logger.warning(
                        'API 初始化失败 (%s/%s): 连接数打满 (limit=10)。'
                        '请关闭其它占用 Longport 的进程/脚本后重试: %s',
                        attempt + 1, init_retries, e,
                    )
                else:
                    logger.warning('API 初始化失败 (%s/%s): %s',
                                   attempt + 1, init_retries, e)
                if attempt < init_retries - 1:
                    time.sleep(init_base_delay * (attempt + 1))
        if last_err is not None:
            raise last_err

    def close(self) -> None:
        """释放 QuoteContext，归还 Longport 连接配额。"""
        ctx = self.quote_ctx
        self.quote_ctx = None
        if ctx is None:
            return
        for meth in ('close', 'shutdown', '__del__'):
            fn = getattr(ctx, meth, None)
            if callable(fn) and meth != '__del__':
                try:
                    fn()
                    return
                except Exception:
                    pass

    def _call_with_retry(self, func, *args, **kwargs):
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt < self.max_retries - 1:
                    logger.warning(f'API 调用失败 ({attempt + 1}/{self.max_retries}): {e}'
                                   f'，{self.retry_delay}s 后重试...')
                    time.sleep(self.retry_delay)
                else:
                    raise

    def _fetch_daily_range(self, symbol: str, start_date: date, end_date: date,
                           adjust: AdjustType = AdjustType.ForwardAdjust) -> pd.DataFrame:
        """单次（可多段合并）拉取 [start_date, end_date] 日线。"""
        try:
            candles = self._call_with_retry(
                self.quote_ctx.history_candlesticks_by_date,
                symbol, Period.Day, adjust, start_date, end_date,
            )
            if not candles:
                return pd.DataFrame()

            data = [{
                'date': _candle_ts_to_date(c.timestamp),
                'open': float(c.open), 'high': float(c.high),
                'low': float(c.low), 'close': float(c.close),
                'volume': int(c.volume), 'turnover': float(c.turnover),
            } for c in candles]
            df = pd.DataFrame(data).set_index('date').sort_index()

            # Longport 单次接口约千根上限，区间过长时往前补齐
            max_merges = 20
            for _ in range(max_merges):
                earliest = _index_to_date(df.index.min())
                if earliest <= start_date or len(df) < 900:
                    break
                prev_end = earliest - timedelta(days=1)
                if prev_end < start_date:
                    break
                time.sleep(float(os.getenv('LONGPORT_REQUEST_PAUSE', '0.15')))
                older = self._call_with_retry(
                    self.quote_ctx.history_candlesticks_by_date,
                    symbol, Period.Day, adjust, start_date, prev_end,
                )
                if not older or len(older) < 20:
                    break
                rows = [{
                    'date': _candle_ts_to_date(c.timestamp),
                    'open': float(c.open), 'high': float(c.high),
                    'low': float(c.low), 'close': float(c.close),
                    'volume': int(c.volume), 'turnover': float(c.turnover),
                } for c in older]
                add = pd.DataFrame(rows).set_index('date').sort_index()
                df = pd.concat([add, df]).sort_index()
                df = df[~df.index.duplicated(keep='first')]

            from daily_cache import normalize_df_index
            return normalize_df_index(df)
        except Exception as e:
            logger.error(f'{symbol}: 获取日线数据失败 - {e}')
            return pd.DataFrame()


# -------- 单例 (线程安全) --------

_api_singleton: Optional[LongportAPI] = None
_api_singleton_lock = threading.Lock()


def get_api_singleton() -> LongportAPI:
    """线程安全单例。多线程并发首次访问只会创建一个实例，
    避免触发 Longport 'connections limitation, limit=10' 错误。"""
    global _api_singleton
    if _api_singleton is not None:
        return _api_singleton
    with _api_singleton_lock:
        if _api_singleton is None:
            _api_singleton = LongportAPI()
            import atexit
            atexit.register(close_api_singleton)
    return _api_singleton


def close_api_singleton() -> None:
    """显式关闭单例连接（多脚本连跑时建议在退出前调用）。"""
    global _api_singleton
    with _api_singleton_lock:
        if _api_singleton is not None:
            try:
                _api_singleton.close()
            finally:
                _api_singleton = None
            logger.info('Longport API 连接已释放')


# -------- 对外接口（带本地 CSV 缓存）--------

def fetch_daily_bars(
    symbol: str,
    start_date: date,
    end_date: date,
    adjust: AdjustType = AdjustType.ForwardAdjust,
    log_cache: bool = True,
    progress: Optional[Tuple[int, int]] = None,
) -> pd.DataFrame:
    """拉取日线数据，命中本地缓存时不调用 API。"""
    from daily_cache import merge_daily_cache, normalize_df_index

    if os.getenv('TREND_DISABLE_DAILY_CACHE', '').lower() in ('1', 'true', 'yes'):
        df = get_api_singleton()._fetch_daily_range(symbol, start_date, end_date, adjust)
        return normalize_df_index(df) if df is not None and len(df) else pd.DataFrame()

    def fetch_range(a: date, b: date) -> pd.DataFrame:
        raw = get_api_singleton()._fetch_daily_range(symbol, a, b, adjust)
        return normalize_df_index(raw) if raw is not None and len(raw) else pd.DataFrame()

    df, _ = merge_daily_cache(symbol, start_date, end_date, fetch_range,
                              log_cache=log_cache, progress=progress)
    if df is None or len(df) == 0:
        return pd.DataFrame()
    return normalize_df_index(df)


# -------- CLI --------

def main():
    if not os.getenv('LONGPORT_APP_KEY') or not os.getenv('LONGPORT_ACCESS_TOKEN'):
        print('错误: 未找到 Longport API 凭证，请检查 .env 文件')
        sys.exit(1)
    if len(sys.argv) < 2:
        print('用法: python longport_api.py <symbol> [start_date] [end_date]')
        print('示例: python longport_api.py AAPL.US 2024-01-01 2024-12-31')
        sys.exit(1)

    symbol = sys.argv[1]
    start = (datetime.strptime(sys.argv[2], '%Y-%m-%d').date()
             if len(sys.argv) >= 3 else date.today() - timedelta(days=730))
    end = (datetime.strptime(sys.argv[3], '%Y-%m-%d').date()
           if len(sys.argv) >= 4 else date.today())

    print(f'获取 {symbol} 日线数据: {start} ~ {end}')
    df = fetch_daily_bars(symbol, start, end)
    if df.empty:
        print('未获取到数据'); sys.exit(1)
    print(f'\n共 {len(df)} 条数据。前 5 行：')
    print(df.head())
    print('\n后 5 行：')
    print(df.tail())


if __name__ == '__main__':
    main()
