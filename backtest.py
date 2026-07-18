#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股横截面动量 / 反转选股回测（默认池子 = NAS100 ∪ S&P 500，~516 只）

策略简介：
- 池子：NAS100 ∪ S&P 500 静态成分（[`universe.py`](universe.py)），每日按 20D 平均成交额过滤 ≥ $50M
- 信号：动量 (mom_20/60/120) + 反转 (IBS / Williams%R / rev_5 / RSI) + EMA bias，rank 化聚合
- 入场：横截面 composite 分数 top K_LONG 做多
- 出场：止损（max(4%, 2.5×ATR)）/ 持仓 MAX_HOLD_DAYS 到期 / 信号退出 hysteresis 带（2.5×K）
- 大盘 regime 过滤：仅 SPY > 200DMA 时开新多头
- 波动率目标仓位：按近 20D 组合波动反向缩放仓位（高波动减仓、低波动加仓）
- 交易成本：Longport 美股平台费 $0.005/股 (min $1) + SEC 0.0000278% + TAF $0.000166/股
- 滑点：默认 5 bps/侧

详细介绍见 README.md。所有可调参数集中在文件顶部的 CONFIG 区块。
运行：  python backtest.py
"""

from __future__ import annotations

# ============================================================================
#                              用户可调参数
# ============================================================================

# ---- 回测区间与本金 ----
# 默认运行模式：同时跑两份回测对照
#   1) DAILY 模式（用纯日 K，T 收盘信号 → T+1 开盘成交）
#       覆盖跨牛熊的长周期，论证策略稳健性，但日内止损精度受限
#   2) INTRADAY 模式（用 5min K + DECISION_TIME_ET 决策点）
#       仅回溯 ~2 年（Longport 分钟数据上限），但与实盘成交逻辑完全一致
DAILY_START   = "2020-01-01"        # 长周期日 K 回测起点
INTRA_START   = "2024-05-08"        # 分钟级回测起点（受 Longport 分钟数据上限制约）
BACKTEST_END  = "today"             # 共同终点："today" 或 "YYYY-MM-DD"
STARTING_CAPITAL = 100000           # 起始本金（美元），仅用于显示与 P&L 美元金额

# ---- 持仓结构 ----
K_LONG  = 12                        # 最多同时持有的多头数（Calmar 优化后）
K_SHORT = 0                         # 最多同时持有的空头数（0 = 纯多头，默认）
LONG_WEIGHT_FRAC = 1.0              # 多头占 gross 的比例；空头占 (1 - 此值)
                                    #   1.0  = 纯多头（默认；建议同时 K_SHORT=0）
                                    #   2/3  ≈ 多空 2:1
                                    #   0.5  = 多空 1:1
GROSS_LEVERAGE = 1.0                # 总毛敞口（long + |short|）

# ---- 信号聚合 ----
MOM_WEIGHT  = 0.9                   # 动量信号权重；反转 = 1 - 此值
BIAS_WEIGHT = 0.4                   # EMA9/21 趋势 bias 系数
RISK_WEIGHT = 0.0                   # 低波动倾斜权重；>0 时偏好低 vol 标的（利于 Calmar）
USE_RSI_REV = True                  # True 时把 RSI(14) 并入反转块（偏好超卖）
USE_MOM_120 = True                  # True 时动量块加入 mom_120

# ---- Hysteresis 与持仓维持 ----
HYSTERESIS_MULT = 2.5               # 持仓需 score 退出 top/bottom (mult * K) 才平仓
                                    # 越大换手越低；2 几乎不滞后，4 月换手降到合理水平

# ---- 风控 ----
STOP_LOSS_PCT      = 0.04           # 个股止损百分比下限
STOP_LOSS_ATR_MULT = 2.5            # 个股止损 ATR 倍数；实际止损 = max(pct, ATR mult)
MAX_HOLD_DAYS      = 60             # 最大持仓天数
MIN_DOLLAR_VOLUME  = 5e7            # 流动性过滤：20D 平均成交额下限（美元）

# ---- 大盘 regime 过滤 ----
REGIME_FILTER = True                # True 时启用 SPY 200DMA：上方仅开多，下方仅开空（关闭时全开）

# ---- 波动率目标仓位 ----
VOL_TARGET_ANNUAL = 0.25            # 目标年化波动；0 = 关闭。开仓时按 (target / realized_vol) 缩放
VOL_TARGET_LOOKBACK = 20            # 计算已实现组合波动的滚动窗口
VOL_SCALE_MIN = 0.3                 # 缩放下限（高波动期最多压到原 30%）
VOL_SCALE_MAX = 2.0                 # 缩放上限（低波动期最多放大到 200%）

# ---- 交易成本与滑点（Longport 美股口径） ----
ENABLE_COSTS         = True
PLATFORM_FEE_PER_SHARE = 0.005      # 平台费 $0.005/股，每单最低 $1（双边收取）
PLATFORM_FEE_MIN     = 1.0
SEC_FEE_RATE         = 0.0000278    # SEC 费率（仅卖出方收取）
TAF_PER_SHARE        = 0.000166     # 交易活动费（仅卖出方收取）
TAF_MAX_PER_ORDER    = 8.3
SLIPPAGE_BPS         = 5.0          # 单侧滑点（基点；买入抬价 / 卖出压价）

# ---- 日内决策（方案：分钟 K 模拟实盘 15:50 决策） ----
INTRADAY_PERIOD  = "5min"           # 1min / 5min / 15min / 30min / 60min
DECISION_TIME_ET = "15:50"          # 美东时间 HH:MM；决策与成交时点（NAS100 收盘前 10 分钟）
                                    # 实盘流程：每日该时点跑一次脚本 → 立即提交订单
                                    # 注意：分钟数据 Longport 仅回溯 ~2 年

# ---- 输出 ----
VERBOSE_TRADES = False              # True 时打印每一笔交易的开/平仓（默认关闭，盈/亏 Top 仍会展示）
PRINT_DAILY_POSITIONS = False       # True 时每日打印当前持仓快照（很啰嗦，调试用）

# ============================================================================
#                       以下为实现，一般不需要修改
# ============================================================================

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from longport_api import fetch_daily_bars, get_api_singleton
from intraday_api import (
    fetch_intraday_bars, filter_rth, to_et,
    PERIOD_MINUTES, parse_decision_time,
)
from universe import get_universe, label as sym_label


@dataclass
class Config:
    start: date
    end: date
    starting_capital: float = STARTING_CAPITAL
    k_long: int = K_LONG
    k_short: int = K_SHORT
    long_weight_frac: float = LONG_WEIGHT_FRAC
    gross_leverage: float = GROSS_LEVERAGE
    hysteresis_mult: float = HYSTERESIS_MULT
    stop_loss_pct: float = STOP_LOSS_PCT
    stop_loss_atr_mult: float = STOP_LOSS_ATR_MULT
    max_hold_days: int = MAX_HOLD_DAYS
    min_dollar_volume: float = MIN_DOLLAR_VOLUME
    mom_weight: float = MOM_WEIGHT
    bias_weight: float = BIAS_WEIGHT
    risk_weight: float = RISK_WEIGHT
    use_rsi_rev: bool = USE_RSI_REV
    use_mom_120: bool = USE_MOM_120
    regime_filter: bool = REGIME_FILTER
    vol_target_annual: float = VOL_TARGET_ANNUAL
    vol_target_lookback: int = VOL_TARGET_LOOKBACK
    vol_scale_min: float = VOL_SCALE_MIN
    vol_scale_max: float = VOL_SCALE_MAX
    enable_costs: bool = ENABLE_COSTS
    platform_fee_per_share: float = PLATFORM_FEE_PER_SHARE
    platform_fee_min: float = PLATFORM_FEE_MIN
    sec_fee_rate: float = SEC_FEE_RATE
    taf_per_share: float = TAF_PER_SHARE
    taf_max_per_order: float = TAF_MAX_PER_ORDER
    slippage_bps: float = SLIPPAGE_BPS
    intraday_period: str = INTRADAY_PERIOD
    decision_time_et: str = DECISION_TIME_ET
    verbose_trades: bool = VERBOSE_TRADES
    print_daily_positions: bool = PRINT_DAILY_POSITIONS
    mode: str = "intraday"          # "intraday" or "daily"，仅用于日志显示


# ---------------- 数据加载 ----------------

def load_all_data(symbols: List[str], start: date, end: date,
                  warmup_days: int = 120) -> Dict[str, pd.DataFrame]:
    fetch_start = start - timedelta(days=warmup_days * 2)
    out: Dict[str, pd.DataFrame] = {}
    max_workers = int(os.getenv("NAS100_FETCH_WORKERS", "8"))

    # 预热单例：避免 8 个 worker 并发首次访问时各自创建连接（触发 limit=10）
    # 仅当存在缓存未命中需调 API 时才会真正实例化；若全部命中缓存，单例不会创建
    # 这里通过让主线程先尝试一次（带 try）来完成初始化
    try:
        get_api_singleton()
    except Exception as e:
        print(f"[警告] Longport 初始化失败（若数据已全部缓存可忽略）: {e}")

    def _fetch(sym: str):
        try:
            df = fetch_daily_bars(sym, fetch_start, end, log_cache=False)
            return sym, df
        except Exception as e:
            print(f"[警告] {sym} 拉取失败: {e}")
            return sym, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_fetch, s) for s in symbols]
        for i, f in enumerate(as_completed(futs), 1):
            sym, df = f.result()
            if len(df) > 0:
                out[sym] = df
            if i % 20 == 0:
                print(f"  已加载 {i}/{len(symbols)}")
    print(f"[数据] 成功加载 {len(out)}/{len(symbols)} 个标的")
    return out


def load_all_intraday(symbols: List[str], start: date, end: date,
                      period_label: str) -> Dict[str, pd.DataFrame]:
    """并发拉取分钟级 RTH 数据。Longport 限速下保守用 4 worker。"""
    out: Dict[str, pd.DataFrame] = {}
    max_workers = int(os.getenv("NAS100_INTRADAY_WORKERS", "4"))

    try:
        get_api_singleton()
    except Exception as e:
        print(f"[警告] Longport 初始化失败（若分钟数据已全部缓存可忽略）: {e}")

    def _fetch(sym: str):
        try:
            df = fetch_intraday_bars(sym, start, end, period_label, log_cache=False)
            if len(df) > 0:
                df = filter_rth(df)
            return sym, df
        except Exception as e:
            print(f"[警告] {sym} 分钟数据拉取失败: {e}")
            return sym, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_fetch, s) for s in symbols]
        for i, f in enumerate(as_completed(futs), 1):
            sym, df = f.result()
            if len(df) > 0:
                out[sym] = df
            if i % 10 == 0 or i == len(symbols):
                print(f"  已加载分钟数据 {i}/{len(symbols)}")
    print(f"[数据-分钟] 成功加载 {len(out)}/{len(symbols)} 个标的")
    return out


def summarize_intraday_per_day(intraday_df: pd.DataFrame, period_label: str,
                                decision_time_et: str) -> pd.DataFrame:
    """按 ET 交易日聚合分钟 K 至决策时点。

    输出列：
      gap_open      : 当日 RTH 第一根 bar 的 open（用于检测 gap-down/up 触发的止损成交价）
      pd_open       : 当日 RTH 开盘价（≈ gap_open）
      pd_high       : RTH 开盘 → 决策 bar 期间最高
      pd_low        : RTH 开盘 → 决策 bar 期间最低
      pd_close      : 决策 bar 收盘价（≈ 决策时点价格）
      pd_volume     : RTH 开盘 → 决策 bar 期间累计成交量
    索引 = pd.Timestamp（normalize 到日，与 daily_cache 索引一致）
    """
    if intraday_df is None or len(intraday_df) == 0:
        return pd.DataFrame()
    period_min = PERIOD_MINUTES[period_label]
    dec_h, dec_m = parse_decision_time(decision_time_et)
    cutoff_start_sec = dec_h * 3600 + dec_m * 60 - period_min * 60

    et = to_et(intraday_df.index)
    df = intraday_df.copy()
    df["__et_date"] = et.normalize().tz_localize(None)
    df["__et_secs"] = et.hour * 3600 + et.minute * 60 + et.second
    df = df[df["__et_secs"] <= cutoff_start_sec]
    if df.empty:
        return pd.DataFrame()

    g = df.groupby("__et_date", sort=True)
    summary = pd.DataFrame({
        "gap_open": g["open"].first(),
        "pd_open":  g["open"].first(),
        "pd_high":  g["high"].max(),
        "pd_low":   g["low"].min(),
        "pd_close": g["close"].last(),
        "pd_volume": g["volume"].sum(),
    })
    summary.index.name = None
    return summary


def merge_daily_with_intraday(daily_df: pd.DataFrame,
                              intra_summary: pd.DataFrame) -> pd.DataFrame:
    """将分钟聚合结果按日期左连接到日线，对应日缺失则该天无法决策。"""
    if daily_df is None or len(daily_df) == 0:
        return daily_df
    if intra_summary is None or len(intra_summary) == 0:
        # 没分钟数据 → 占位（这些日子无法决策）
        for col in ("gap_open", "pd_open", "pd_high", "pd_low", "pd_close", "pd_volume"):
            daily_df[col] = np.nan
        return daily_df
    return daily_df.join(intra_summary, how="left")


# ---------------- 指标 ----------------

def _ema(s, n): return s.ewm(span=n, adjust=False).mean()

def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def _williams_r(h, l, c, n=14):
    hh = h.rolling(n).max()
    ll = l.rolling(n).min()
    return -100 * (hh - c) / (hh - ll).replace(0, np.nan)


def _rsi(c, n=14):
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    h, l, c = out["high"], out["low"], out["close"]
    out["ema9"] = _ema(c, 9)
    out["ema21"] = _ema(c, 21)
    out["trend_up"] = (out["ema9"] > out["ema21"]).astype(float)
    out["mom_20"] = c / c.shift(20) - 1
    out["mom_60"] = c / c.shift(60) - 1
    out["mom_120"] = c / c.shift(120) - 1
    out["rev_5"] = -(c / c.shift(5) - 1)
    rng = (h - l).replace(0, np.nan)
    out["ibs"] = (c - l) / rng
    out["wr14"] = _williams_r(h, l, c, 14)
    out["rsi14"] = _rsi(c, 14)
    out["vol_20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
    out["atr14"] = _atr(h, l, c, 14)
    out["dollar_vol_20"] = (c * out["volume"]).rolling(20).mean()
    return out


def build_panel(data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    return {sym: compute_indicators(df) for sym, df in data.items()}


def compute_proxy_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """使用决策时点（pd_close / pd_high / pd_low / pd_volume）重算 today 的指标。

    历史天的指标已经由 compute_indicators 用 full-day OHLC 算好；
    本函数把每一天的 today-row 用「决策时点截面」**覆盖**重算一遍：
      - mom_20 / mom_60 / rev_5：用 pd_close 替代 close
      - ibs / wr14：用 pd_close + pd_high + pd_low
      - atr14：用 (pd_high, pd_low, pd_close) 的 TR 与 (前 13 日真实 TR) 平均
      - dollar_vol_20：用 pd_close * pd_volume 替代当日值（其余 19 日不变）
      - ema9 / ema21 / trend_up：bias 取前一日值（避免 today close 介入 bias）
    """
    out = df.copy()
    if "pd_close" not in out.columns or out["pd_close"].isna().all():
        # 无分钟数据 → 退化为日线指标
        return out

    pd_c = out["pd_close"]
    pd_h = out["pd_high"]
    pd_l = out["pd_low"]
    pd_v = out["pd_volume"]
    valid = pd_c.notna()

    # 动量 / 反转：替换今日分子
    out.loc[valid, "mom_20"] = pd_c[valid] / out["close"].shift(20)[valid] - 1
    out.loc[valid, "mom_60"] = pd_c[valid] / out["close"].shift(60)[valid] - 1
    out.loc[valid, "mom_120"] = pd_c[valid] / out["close"].shift(120)[valid] - 1
    out.loc[valid, "rev_5"]  = -(pd_c[valid] / out["close"].shift(5)[valid] - 1)

    # IBS / Williams%R / RSI 用日内截面
    rng = (pd_h - pd_l).replace(0, np.nan)
    out.loc[valid, "ibs"] = (pd_c[valid] - pd_l[valid]) / rng[valid]
    hh14 = pd.concat([out["high"].shift(1).rolling(13).max(), pd_h], axis=1).max(axis=1)
    ll14 = pd.concat([out["low"].shift(1).rolling(13).min(), pd_l], axis=1).min(axis=1)
    rng14 = (hh14 - ll14).replace(0, np.nan)
    out.loc[valid, "wr14"] = (-100 * (hh14 - pd_c) / rng14)[valid]

    # RSI：用 pd_close 替换今日收益后滚动（近似，与日线口径一致）
    close_proxy = out["close"].copy()
    close_proxy.loc[valid] = pd_c[valid]
    out.loc[valid, "rsi14"] = _rsi(close_proxy, 14)[valid]
    out.loc[valid, "vol_20"] = (
        close_proxy.pct_change().rolling(20).std() * np.sqrt(252)
    )[valid]

    # ATR：用决策时点的 TR 替代当日
    prev_c = out["close"].shift(1)
    tr_today = pd.concat([(pd_h - pd_l),
                          (pd_h - prev_c).abs(),
                          (pd_l - prev_c).abs()], axis=1).max(axis=1)
    # 简化：take 13-day mean of past TR + today's intraday TR
    past_tr = pd.concat([
        (out["high"] - out["low"]),
        (out["high"] - prev_c).abs(),
        (out["low"] - prev_c).abs(),
    ], axis=1).max(axis=1).shift(1)
    atr_proxy = (past_tr.rolling(13).sum() + tr_today) / 14
    out.loc[valid, "atr14"] = atr_proxy[valid]

    # dollar_vol_20：替换今日的 close*volume
    today_dv = (pd_c * pd_v)
    past_dv = (out["close"] * out["volume"]).shift(1).rolling(19).sum()
    out.loc[valid, "dollar_vol_20"] = ((past_dv + today_dv) / 20)[valid]

    # ema/trend_up：用前一日（已收盘的）值，避免分钟级偏差
    out["ema9"] = out["ema9"].shift(1)
    out["ema21"] = out["ema21"].shift(1)
    out["trend_up"] = (out["ema9"] > out["ema21"]).astype(float)

    return out


def build_intraday_enhanced_panel(daily_data: Dict[str, pd.DataFrame],
                                   intraday: Dict[str, pd.DataFrame],
                                   period_label: str,
                                   decision_time_et: str) -> Dict[str, pd.DataFrame]:
    """构造 INTRADAY 模式的 panel：日 K + 分钟决策时点截面。"""
    enhanced: Dict[str, pd.DataFrame] = {}
    for sym, dfd in daily_data.items():
        intra_df = intraday.get(sym, pd.DataFrame())
        intra_summary = summarize_intraday_per_day(
            intra_df, period_label, decision_time_et,
        )
        enhanced[sym] = merge_daily_with_intraday(dfd, intra_summary)
    return enhanced


def build_daily_panel(daily_data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """构造 DAILY 模式的 panel（**参考基准**：今日收盘信号 + 今日收盘成交）。

    DAILY 模式仅作跨牛熊压力测试的参考，不可实盘（实盘里你拿到 close 时已经收盘）。
    设计上故意允许 look-ahead，使其与 INTRADAY 的对比仅剩 2 个差异：
      1. 决策时点：DAILY=16:00 真收盘 vs INTRADAY=15:50（10 分钟差）
      2. 止损精度：DAILY 用日 K 全天 high/low vs INTRADAY 用 5min K（细节级别差）

    伪决策时点截面：
      - pd_close      := 当日 close（=信号基准价 + 执行价）
      - gap_open      := 当日 open（用于 Phase A 跳空止损检测）
      - pd_high/pd_low := 当日 high/low（全天止损扫描）
      - 指标不 shift：直接用 compute_indicators 的当日值
    """
    out: Dict[str, pd.DataFrame] = {}
    for sym, dfd in daily_data.items():
        df = compute_indicators(dfd).copy()
        df["gap_open"]  = df["open"]
        df["pd_open"]   = df["open"]
        df["pd_close"]  = df["close"]
        df["pd_high"]   = df["high"]
        df["pd_low"]    = df["low"]
        df["pd_volume"] = df["volume"]
        out[sym] = df
    return out


def _csrank(s):
    valid = s.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=s.index)
    r = valid.rank(method="average", pct=True)
    return (r * 2 - 1).reindex(s.index)


def composite_score(day_panel: pd.DataFrame, mom_w=0.7, bias_w=0.2,
                    risk_w: float = 0.0, use_rsi_rev: bool = False,
                    use_mom_120: bool = False) -> pd.Series:
    s_mom20 = _csrank(day_panel["mom_20"])
    s_mom60 = _csrank(day_panel["mom_60"])
    if use_mom_120 and "mom_120" in day_panel.columns:
        s_mom120 = _csrank(day_panel["mom_120"])
        momentum_block = (s_mom20 + s_mom60 + s_mom120) / 3
    else:
        momentum_block = (s_mom20 + s_mom60) / 2

    s_ibs = -_csrank(day_panel["ibs"])
    s_wr = -_csrank(-day_panel["wr14"])
    s_rev = _csrank(day_panel["rev_5"])
    rev_parts = [s_ibs, s_wr, s_rev]
    if use_rsi_rev and "rsi14" in day_panel.columns:
        # 低 RSI（超卖）→ 高分
        rev_parts.append(-_csrank(day_panel["rsi14"]))
    reversal_block = sum(rev_parts) / len(rev_parts)

    bias = day_panel["trend_up"].fillna(0.5) * 2 - 1
    score = mom_w * momentum_block + (1 - mom_w) * reversal_block + bias_w * bias
    if risk_w and "vol_20" in day_panel.columns:
        # 低波动 → 高分（利于压回撤 / 抬 Calmar）
        score = score + risk_w * (-_csrank(day_panel["vol_20"]))
    return score


# ---------------- 回测引擎 ----------------

@dataclass
class Position:
    side: int
    entry_date: pd.Timestamp
    entry_price: float
    weight: float
    shares: float          # 用于美元 P&L 计算
    stop_price: float
    open_cost: float = 0.0 # 开仓时支付的滑点+平台费(+SEC/TAF 若开空)
    days_held: int = 0
    last_mark: float = 0.0  # 上次 MTM 参考价（首次为开仓 open；之后为前一日 close）


@dataclass
class BacktestResult:
    equity: pd.Series
    daily_returns: pd.Series
    trades: List[dict]
    long_count: pd.Series
    short_count: pd.Series


def _fmt_usd(x): return f"${x:,.0f}"


def _open_cost(cfg: 'Config', shares: float, price: float, side: int) -> float:
    """开仓总成本（美元）：滑点 + 平台费 (+ side=-1 即开空时还有 SEC/TAF)。"""
    if not cfg.enable_costs:
        return 0.0
    notional = shares * price
    slip = notional * cfg.slippage_bps / 10000.0
    plat = max(shares * cfg.platform_fee_per_share, cfg.platform_fee_min)
    cost = slip + plat
    if side == -1:  # 开空 = 卖出，触发 SEC/TAF
        cost += notional * cfg.sec_fee_rate
        cost += min(shares * cfg.taf_per_share, cfg.taf_max_per_order)
    return cost


def _close_cost(cfg: 'Config', shares: float, price: float, side: int) -> float:
    """平仓总成本（美元）：滑点 + 平台费 (+ side=+1 即平多卖出时还有 SEC/TAF)。"""
    if not cfg.enable_costs:
        return 0.0
    notional = shares * price
    slip = notional * cfg.slippage_bps / 10000.0
    plat = max(shares * cfg.platform_fee_per_share, cfg.platform_fee_min)
    cost = slip + plat
    if side == 1:  # 平多 = 卖出
        cost += notional * cfg.sec_fee_rate
        cost += min(shares * cfg.taf_per_share, cfg.taf_max_per_order)
    return cost


def run_backtest(panel: Dict[str, pd.DataFrame], cfg: Config,
                 regime_series: Optional[pd.Series] = None) -> BacktestResult:
    """
    执行约定（分钟级 + DECISION_TIME_ET 单一决策点）：
      Phase A: 09:30 → decision_time，扫描存量持仓的日内止损（gap_open / pd_low / pd_high）
      Phase B: decision_time，用「日线历史 + 当日截至决策时点」的 panel 生成信号
               立即在 decision_close 执行：信号反转/max_hold/regime 平仓 + 缺仓位开仓
      Phase C: decision_time → 16:00，对剩余持仓 MTM 到当日真收盘 (close)
    实盘对应：每日 decision_time_et 跑一次脚本，按生成的订单立即下单。
    """
    all_dates = sorted({d for df in panel.values() for d in df.index})
    all_dates = [d for d in all_dates
                 if pd.Timestamp(cfg.start) <= d <= pd.Timestamp(cfg.end)]
    if not all_dates:
        raise RuntimeError("无可用交易日")

    symbols = list(panel.keys())
    positions: Dict[str, Position] = {}
    equity = float(cfg.starting_capital)
    equity_curve, daily_rets = [], []
    long_counts, short_counts = [], []
    trades: List[dict] = []

    long_w = cfg.gross_leverage * cfg.long_weight_frac
    short_w = cfg.gross_leverage * (1 - cfg.long_weight_frac)
    long_per_pos_base = long_w / cfg.k_long if cfg.k_long > 0 else 0
    short_per_pos_base = short_w / cfg.k_short if cfg.k_short > 0 else 0

    v = cfg.verbose_trades

    # 预计算每股的「决策时点 panel」
    #   intraday: 用 pd_close/pd_high/pd_low/pd_volume 重算今日指标
    #   daily:   panel 在 build_daily_panel 里已经把指标 shift(1)，直接复用即可
    if cfg.mode == "daily":
        proxy_panel: Dict[str, pd.DataFrame] = panel
    else:
        proxy_panel = {sym: compute_proxy_indicators(df) for sym, df in panel.items()}

    def _log_open(side, sym, date_, px, stop_px, weight, equity_now, long_n, short_n):
        if not v:
            return
        s = "LONG " if side == 1 else "SHORT"
        notional = equity_now * weight
        print(f"  [{date_.strftime('%Y-%m-%d')}] OPEN  {s} {sym_label(sym):<22} "
              f"@ ${px:7.2f}  stop=${stop_px:7.2f}  "
              f"size={_fmt_usd(notional)} ({weight*100:.1f}%)  "
              f"holdings: L{long_n} S{short_n}")

    def _log_close(side, sym, entry_d, exit_d, entry_px, exit_px,
                   shares, days_held, reason):
        if not v:
            return
        pnl_pct = (exit_px / entry_px - 1) * side
        pnl_usd = (exit_px - entry_px) * shares * side
        s = "LONG " if side == 1 else "SHORT"
        print(f"  [{exit_d.strftime('%Y-%m-%d')}] CLOSE {s} {sym_label(sym):<22} "
              f"@ ${exit_px:7.2f}  entry=${entry_px:7.2f}  "
              f"pnl={pnl_pct*100:+6.2f}% / {_fmt_usd(pnl_usd):>10s}  "
              f"held={days_held}d  ({reason})")

    def _realize_close(sym: str, exit_px: float, reason: str,
                       exit_date: pd.Timestamp) -> float:
        """实现一次平仓：扣除现金端 close_cost，记录 trade，返回今日 MTM 贡献(ratio)。"""
        nonlocal equity
        pos = positions.pop(sym)
        close_c = _close_cost(cfg, pos.shares, exit_px, pos.side)
        equity -= close_c
        gross_pnl = (exit_px - pos.entry_price) * pos.shares * pos.side
        pnl_usd = gross_pnl - pos.open_cost - close_c
        pnl_pct = pnl_usd / (pos.entry_price * pos.shares) if pos.shares > 0 else 0
        trades.append({
            "symbol": sym, "side": pos.side,
            "entry_date": pos.entry_date, "exit_date": exit_date,
            "entry_price": pos.entry_price, "exit_price": exit_px,
            "shares": pos.shares, "days_held": pos.days_held,
            "reason": reason, "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "costs": pos.open_cost + close_c,
        })
        _log_close(pos.side, sym, pos.entry_date, exit_date, pos.entry_price,
                   exit_px, pos.shares, pos.days_held, reason)
        if pos.last_mark > 0:
            return (exit_px / pos.last_mark - 1) * pos.side * pos.weight
        return 0.0

    for i, today in enumerate(all_dates):
        day_pnl = 0.0

        def _row(sym):
            df = panel[sym]
            return df.loc[today] if today in df.index else None

        def _proxy_row(sym):
            df = proxy_panel[sym]
            return df.loc[today] if today in df.index else None

        # ============ Phase A: 09:30 → 决策时点，扫描存量持仓日内止损 ============
        for sym in list(positions.keys()):
            pos = positions[sym]
            row = _row(sym)
            if row is None:
                continue
            gap_open = row.get("gap_open", np.nan)
            pd_low = row.get("pd_low", np.nan)
            pd_high = row.get("pd_high", np.nan)
            if pd.isna(gap_open):
                # 无分钟数据回退：跳过日内止损（让 Phase B 信号处理）
                continue

            stop_hit = False
            exit_px = np.nan
            if pos.side == 1:
                if gap_open <= pos.stop_price:
                    # 跳空开盘已穿止损 → 按开盘价成交（更差）
                    stop_hit, exit_px = True, gap_open
                elif not pd.isna(pd_low) and pd_low <= pos.stop_price:
                    stop_hit, exit_px = True, pos.stop_price
            else:  # short
                if gap_open >= pos.stop_price:
                    stop_hit, exit_px = True, gap_open
                elif not pd.isna(pd_high) and pd_high >= pos.stop_price:
                    stop_hit, exit_px = True, pos.stop_price

            if stop_hit:
                day_pnl += _realize_close(sym, float(exit_px), "stop_loss", today)

        # ============ Phase B: 决策时点 ============
        # B.1 构造横截面 panel（用 proxy 指标）
        today_data = {}
        for sym in symbols:
            prow = _proxy_row(sym)
            if prow is None:
                continue
            if pd.isna(prow.get("pd_close")) or pd.isna(prow.get("mom_60")) \
               or pd.isna(prow.get("atr14")):
                continue
            if prow.get("dollar_vol_20", 0) < cfg.min_dollar_volume:
                continue
            today_data[sym] = prow

        has_panel = len(today_data) >= cfg.k_long + cfg.k_short
        if has_panel:
            day_panel = pd.DataFrame(today_data).T

            scores = composite_score(
                day_panel,
                mom_w=cfg.mom_weight,
                bias_w=cfg.bias_weight,
                risk_w=cfg.risk_weight,
                use_rsi_rev=cfg.use_rsi_rev,
                use_mom_120=cfg.use_mom_120,
            ).dropna().sort_values(ascending=False)
            top_k = set(scores.head(cfg.k_long).index)
            bot_k = set(scores.tail(cfg.k_short).index) if cfg.k_short > 0 else set()
            top_2k = set(scores.head(int(cfg.k_long * cfg.hysteresis_mult)).index)
            bot_2k = (set(scores.tail(int(cfg.k_short * cfg.hysteresis_mult)).index)
                      if cfg.k_short > 0 else set())

            allow_long = allow_short = True
            if cfg.regime_filter and regime_series is not None and today in regime_series.index:
                up = bool(regime_series.loc[today])
                allow_long, allow_short = up, not up

            # 波动率目标缩放
            vol_scale = 1.0
            if cfg.vol_target_annual > 0 and len(daily_rets) >= cfg.vol_target_lookback:
                recent = np.asarray(daily_rets[-cfg.vol_target_lookback:])
                sd = recent.std()
                if sd > 1e-6:
                    rv = sd * np.sqrt(252)
                    vol_scale = float(np.clip(cfg.vol_target_annual / rv,
                                               cfg.vol_scale_min, cfg.vol_scale_max))
            long_per_pos = long_per_pos_base * vol_scale
            short_per_pos = short_per_pos_base * vol_scale

            # B.2 平仓：max_hold / 信号反转 / regime 翻转 → 在 pd_close 立即成交
            for sym in list(positions.keys()):
                pos = positions[sym]
                prow = _proxy_row(sym)
                if prow is None or pd.isna(prow.get("pd_close")):
                    continue
                reason = None
                if pos.days_held >= cfg.max_hold_days:
                    reason = "max_hold"
                elif pos.side == 1 and (sym not in top_2k or not allow_long):
                    reason = "signal_exit"
                elif pos.side == -1 and (sym not in bot_2k or not allow_short):
                    reason = "signal_exit"
                if reason is None:
                    continue
                exit_px = float(prow["pd_close"])
                day_pnl += _realize_close(sym, exit_px, reason, today)

            # B.3 开仓：缺槽位的 top_k / bot_k 立即在 pd_close 开仓
            cur_long_n = sum(1 for p in positions.values() if p.side == 1)
            cur_short_n = sum(1 for p in positions.values() if p.side == -1)
            held = set(positions.keys())

            if allow_long and cur_long_n < cfg.k_long:
                for sym in scores.index:
                    if cur_long_n >= cfg.k_long:
                        break
                    if sym not in top_k or sym in held:
                        continue
                    prow = day_panel.loc[sym]
                    px = float(prow["pd_close"]); atr = float(prow["atr14"])
                    if pd.isna(px) or pd.isna(atr) or px <= 0:
                        continue
                    stop_dist = max(cfg.stop_loss_pct * px, cfg.stop_loss_atr_mult * atr)
                    notional = equity * long_per_pos
                    shares = notional / px
                    stop_px = px - stop_dist
                    open_c = _open_cost(cfg, shares, px, side=1)
                    equity -= open_c
                    positions[sym] = Position(
                        side=1, entry_date=today, entry_price=px,
                        weight=long_per_pos, shares=shares, stop_price=stop_px,
                        open_cost=open_c, last_mark=px,
                    )
                    cur_long_n += 1
                    _log_open(1, sym, today, px, stop_px, long_per_pos,
                              equity, cur_long_n, cur_short_n)

            if allow_short and cur_short_n < cfg.k_short:
                for sym in scores.index[::-1]:
                    if cur_short_n >= cfg.k_short:
                        break
                    if sym not in bot_k or sym in held:
                        continue
                    prow = day_panel.loc[sym]
                    px = float(prow["pd_close"]); atr = float(prow["atr14"])
                    if pd.isna(px) or pd.isna(atr) or px <= 0:
                        continue
                    stop_dist = max(cfg.stop_loss_pct * px, cfg.stop_loss_atr_mult * atr)
                    notional = equity * short_per_pos
                    shares = notional / px
                    stop_px = px + stop_dist
                    open_c = _open_cost(cfg, shares, px, side=-1)
                    equity -= open_c
                    positions[sym] = Position(
                        side=-1, entry_date=today, entry_price=px,
                        weight=short_per_pos, shares=shares, stop_price=stop_px,
                        open_cost=open_c, last_mark=px,
                    )
                    cur_short_n += 1
                    _log_open(-1, sym, today, px, stop_px, short_per_pos,
                              equity, cur_long_n, cur_short_n)

        # ============ Phase C: 决策时点 → 16:00，剩余持仓 MTM 到真收盘 ============
        for sym, pos in positions.items():
            row = _row(sym)
            if row is None:
                continue
            today_close = row.get("close", np.nan)
            if pd.isna(today_close) or pos.last_mark <= 0:
                continue
            day_pnl += (today_close / pos.last_mark - 1) * pos.side * pos.weight
            pos.last_mark = float(today_close)
            pos.days_held += 1

        equity *= (1 + day_pnl)
        daily_rets.append(day_pnl)
        equity_curve.append(equity)

        cur_long_n = sum(1 for p in positions.values() if p.side == 1)
        cur_short_n = sum(1 for p in positions.values() if p.side == -1)
        long_counts.append(cur_long_n)
        short_counts.append(cur_short_n)

        if cfg.print_daily_positions and (cur_long_n + cur_short_n) > 0:
            longs = [s for s, p in positions.items() if p.side == 1]
            shorts = [s for s, p in positions.items() if p.side == -1]
            print(f"  [{today.strftime('%Y-%m-%d')}] equity={_fmt_usd(equity)}  "
                  f"L({len(longs)}): {','.join(longs)}  "
                  f"S({len(shorts)}): {','.join(shorts)}")

    idx = pd.DatetimeIndex(all_dates)
    return BacktestResult(
        equity=pd.Series(equity_curve, index=idx, name="equity"),
        daily_returns=pd.Series(daily_rets, index=idx, name="ret"),
        trades=trades,
        long_count=pd.Series(long_counts, index=idx),
        short_count=pd.Series(short_counts, index=idx),
    )


# ---------------- 评估与打印 ----------------

def metrics_raw(result: BacktestResult, cfg: Config) -> dict:
    """返回原始浮点指标，供参数搜索使用。"""
    rets = result.daily_returns.dropna()
    n = len(rets)
    eq = result.equity
    cum_ret = float(eq.iloc[-1] / cfg.starting_capital - 1) if n > 0 else 0.0
    years = n / 252
    cagr = float((1 + cum_ret) ** (1 / years) - 1) if years > 0 else 0.0
    vol = float(rets.std() * np.sqrt(252)) if n and rets.std() > 0 else 0.0
    sharpe = float(rets.mean() * 252 / (rets.std() * np.sqrt(252))) if n and rets.std() > 0 else 0.0
    dd = eq / eq.cummax() - 1
    max_dd = float(dd.min()) if n else 0.0
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0
    return {
        "cum_ret": cum_ret,
        "cagr": cagr,
        "vol": vol,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "calmar": calmar,
        "n_trades": len(result.trades),
        "years": years,
    }


def summarize(result: BacktestResult, cfg: Config,
              benchmark: Optional[pd.Series] = None) -> dict:
    m = metrics_raw(result, cfg)
    cum_ret, cagr, vol, sharpe, max_dd, calmar = (
        m["cum_ret"], m["cagr"], m["vol"], m["sharpe"], m["max_dd"], m["calmar"],
    )
    n = len(result.daily_returns.dropna())
    eq = result.equity
    years = m["years"]

    trades = result.trades
    wins = [t for t in trades if t["pnl_pct"] > 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_hold = np.mean([t["days_held"] for t in trades]) if trades else 0
    long_trades = [t for t in trades if t["side"] == 1]
    short_trades = [t for t in trades if t["side"] == -1]
    long_pnl = sum(t["pnl_usd"] for t in long_trades)
    short_pnl = sum(t["pnl_usd"] for t in short_trades)
    total_costs = sum(t.get("costs", 0) for t in trades)

    out = {
        "区间": f"{result.equity.index[0].strftime('%Y-%m-%d')} ~ "
                 f"{result.equity.index[-1].strftime('%Y-%m-%d')} ({years:.2f}年)",
        "起始本金": _fmt_usd(cfg.starting_capital),
        "终值": _fmt_usd(eq.iloc[-1]) if n else "-",
        "累计收益": f"{cum_ret*100:+.2f}%  ({_fmt_usd(eq.iloc[-1] - cfg.starting_capital)})" if n else "-",
        "年化收益(CAGR)": f"{cagr*100:.2f}%",
        "年化波动": f"{vol*100:.2f}%",
        "Sharpe": f"{sharpe:.2f}",
        "最大回撤": f"{max_dd*100:.2f}%",
        "Calmar": f"{calmar:.2f}",
        "总交易笔数": str(len(trades)),
        "  其中多头": f"{len(long_trades)}笔, 累计 {_fmt_usd(long_pnl)}",
        "  其中空头": f"{len(short_trades)}笔, 累计 {_fmt_usd(short_pnl)}",
        "总交易成本": f"{_fmt_usd(total_costs)} ({total_costs/cfg.starting_capital*100:.2f}%)",
        "胜率": f"{win_rate*100:.1f}%",
        "平均持仓天数": f"{avg_hold:.1f}",
        "平均多头持仓数": f"{result.long_count.mean():.1f}",
        "平均空头持仓数": f"{result.short_count.mean():.1f}",
    }
    if benchmark is not None and len(benchmark) > 1:
        bret = benchmark.pct_change().dropna()
        bcum = (1 + bret).prod()
        byears = len(bret) / 252
        bcagr = bcum ** (1 / byears) - 1 if byears > 0 else 0
        bvol = bret.std() * np.sqrt(252)
        bsharpe = bret.mean() * 252 / bvol if bvol > 0 else 0
        bdd = (benchmark / benchmark.cummax() - 1).min()
        out["基准(QQQ)累计"] = f"{(bcum-1)*100:+.2f}%"
        out["基准(QQQ)CAGR"] = f"{bcagr*100:.2f}%"
        out["基准(QQQ)Sharpe"] = f"{bsharpe:.2f}"
        out["基准(QQQ)MDD"] = f"{bdd*100:.2f}%"
    return out


def print_summary(summary: dict, title: str = "回测汇总"):
    print("\n" + "=" * 60)
    print(f"                        {title}")
    print("=" * 60)
    for k, v in summary.items():
        print(f"  {k:<20s} {v}")
    print("=" * 60)


def _yearly_stats(result: BacktestResult) -> Dict[int, dict]:
    """按自然年聚合：收益、最大回撤、Sharpe、平仓笔数。"""
    eq = result.equity
    rets = result.daily_returns.dropna()
    out: Dict[int, dict] = {}
    for y, r in rets.groupby(rets.index.year):
        eq_y = eq.loc[eq.index.year == y]
        dd = (eq_y / eq_y.cummax() - 1).min() if len(eq_y) else 0.0
        sd = r.std()
        out[int(y)] = {
            "ret":    float((1 + r).prod() - 1) * 100,
            "mdd":    float(dd) * 100,
            "sharpe": float(r.mean() * 252 / (sd * np.sqrt(252))) if sd > 1e-12 else 0.0,
            "trades": sum(1 for t in result.trades
                          if pd.Timestamp(t["exit_date"]).year == y),
        }
    return out


def print_yearly_comparison(daily_result: BacktestResult,
                             intra_result: BacktestResult) -> None:
    """逐年打印 DAILY vs INTRADAY 对比。短周期无数据年份留空。"""
    d = _yearly_stats(daily_result)
    i = _yearly_stats(intra_result)
    years = sorted(d.keys() | i.keys())

    print("\n" + "=" * 84)
    print("  逐年对比（DAILY 跨牛熊 / INTRADAY 实盘对齐；短周期未覆盖的年份留空）")
    print("=" * 84)
    print(f"  {'年份':<6}{'收益%':>20}{'最大回撤%':>22}{'Sharpe':>18}{'平仓笔数':>16}")
    print(f"  {'':<6}{'Dly / Int':>20}{'Dly / Int':>22}{'Dly / Int':>18}{'Dly / Int':>16}")
    print("-" * 84)
    for y in years:
        a = d.get(y); b = i.get(y)
        def f(x, key, fmt):
            return f"{x[key]:{fmt}}" if x else "  --  "
        print(f"  {y:<6}"
              f"{f(a,'ret','+8.2f')} / {f(b,'ret','+8.2f')}"
              f"  {f(a,'mdd','+8.2f')} / {f(b,'mdd','+8.2f')}"
              f"  {f(a,'sharpe','+6.2f')} / {f(b,'sharpe','+6.2f')}"
              f"  {f(a,'trades','>5')} / {f(b,'trades','>5')}")
    print("=" * 84)


def print_compare(daily_summary: dict, intra_summary: dict):
    """并排对比两份回测的核心指标。"""
    keys = ["区间", "起始本金", "终值", "累计收益", "年化收益(CAGR)",
            "年化波动", "Sharpe", "最大回撤", "Calmar",
            "总交易笔数", "总交易成本", "胜率", "平均持仓天数",
            "基准(QQQ)累计", "基准(QQQ)CAGR", "基准(QQQ)Sharpe", "基准(QQQ)MDD"]
    print("\n" + "=" * 96)
    print(f"  {'指标':<18s}  {'DAILY (跨牛熊)':<32s}  {'INTRADAY (实盘对齐)':<32s}")
    print("=" * 96)
    for k in keys:
        v_d = daily_summary.get(k, "-")
        v_i = intra_summary.get(k, "-")
        print(f"  {k:<18s}  {str(v_d):<32s}  {str(v_i):<32s}")
    print("=" * 96)


def print_top_trades(trades: List[dict], n: int = 10):
    if not trades:
        return
    sorted_t = sorted(trades, key=lambda t: t["pnl_usd"], reverse=True)
    print("\n----- 盈利 Top 10 -----")
    for t in sorted_t[:n]:
        s = "LONG " if t["side"] == 1 else "SHORT"
        print(f"  {sym_label(t['symbol']):<22} {s} {t['entry_date'].strftime('%Y-%m-%d')} → "
              f"{t['exit_date'].strftime('%Y-%m-%d')}  "
              f"${t['entry_price']:7.2f} → ${t['exit_price']:7.2f}  "
              f"{t['pnl_pct']*100:+6.2f}% / {_fmt_usd(t['pnl_usd']):>10s}  "
              f"({t['days_held']}d, {t['reason']})")
    print("\n----- 亏损 Top 10 -----")
    for t in sorted_t[-n:][::-1]:
        s = "LONG " if t["side"] == 1 else "SHORT"
        print(f"  {sym_label(t['symbol']):<22} {s} {t['entry_date'].strftime('%Y-%m-%d')} → "
              f"{t['exit_date'].strftime('%Y-%m-%d')}  "
              f"${t['entry_price']:7.2f} → ${t['exit_price']:7.2f}  "
              f"{t['pnl_pct']*100:+6.2f}% / {_fmt_usd(t['pnl_usd']):>10s}  "
              f"({t['days_held']}d, {t['reason']})")


# ---------------- 入口 ----------------

def _slice_qqq(qqq_df: pd.DataFrame, start: date, end: date) -> Optional[pd.Series]:
    if qqq_df is None or len(qqq_df) == 0:
        return None
    s = qqq_df.loc[(qqq_df.index >= pd.Timestamp(start)) &
                    (qqq_df.index <= pd.Timestamp(end)), "close"]
    return s if len(s) else None


def _make_cfg(start: date, end: date, mode: str) -> Config:
    return Config(start=start, end=end, mode=mode)


def main():
    end_date = date.today() if BACKTEST_END == "today" else date.fromisoformat(BACKTEST_END)
    daily_start = date.fromisoformat(DAILY_START)
    intra_start = date.fromisoformat(INTRA_START)

    daily_cfg = _make_cfg(daily_start, end_date, "daily")
    intra_cfg = _make_cfg(intra_start, end_date, "intraday")

    print("=" * 60)
    print(f"  策略配置（两段对照）")
    print("=" * 60)
    print(f"  DAILY   区间   {daily_cfg.start} ~ {daily_cfg.end}  "
          f"(参考基准: 当日 close 信号 + close 成交，跨牛熊压力测试，不可实盘)")
    print(f"  INTRA   区间   {intra_cfg.start} ~ {intra_cfg.end}  "
          f"(分钟级 {intra_cfg.intraday_period} + {intra_cfg.decision_time_et} ET 决策，与实盘一致)")
    print(f"  起始本金       {_fmt_usd(daily_cfg.starting_capital)}")
    print(f"  最大持仓       多 {daily_cfg.k_long} / 空 {daily_cfg.k_short}")
    print(f"  动量/反转      {daily_cfg.mom_weight:.2f} / {1-daily_cfg.mom_weight:.2f}")
    print(f"  Hysteresis     {daily_cfg.hysteresis_mult}x K")
    print(f"  止损           max({daily_cfg.stop_loss_pct*100:.0f}%, "
          f"{daily_cfg.stop_loss_atr_mult}×ATR14)")
    print(f"  最大持仓天     {daily_cfg.max_hold_days}")
    print(f"  Regime过滤     {'开启' if daily_cfg.regime_filter else '关闭'}")
    print(f"  逐笔打印       {'开启' if daily_cfg.verbose_trades else '关闭（默认）'}")

    syms = get_universe()

    # ---------- 数据加载（按更长的 daily 区间一次性加载，intraday 只覆盖近 2 年） ----------
    print(f"\n[数据] 加载 {len(syms)} 只成分股日线 ({daily_cfg.start} ~ {daily_cfg.end})...")
    data = load_all_data(syms, daily_cfg.start, daily_cfg.end)

    print(f"\n[数据] 加载分钟级数据 ({intra_cfg.intraday_period}, 区间 "
          f"{intra_cfg.start} ~ {intra_cfg.end}, 决策时点 {intra_cfg.decision_time_et} ET)...")
    intraday = load_all_intraday(list(data.keys()), intra_cfg.start, intra_cfg.end,
                                  intra_cfg.intraday_period)
    missing_intra = sum(1 for s in data.keys() if len(intraday.get(s, pd.DataFrame())) == 0)
    if missing_intra:
        print(f"[警告] {missing_intra} 只标的无分钟数据（将无法在 INTRADAY 模式决策）")

    # ---------- 构造两份 panel ----------
    print(f"\n[数据] 构造 DAILY panel（信号 + 成交价均 = 当日 close，参考基准）...")
    daily_panel = build_daily_panel(data)

    print(f"[数据] 构造 INTRADAY panel（聚合分钟数据到决策时点截面）...")
    intra_enhanced = build_intraday_enhanced_panel(
        data, intraday, intra_cfg.intraday_period, intra_cfg.decision_time_et,
    )
    intra_panel = build_panel(intra_enhanced)

    # ---------- regime + benchmark ----------
    spy_df = fetch_daily_bars("SPY.US",
                               daily_cfg.start - timedelta(days=400),
                               daily_cfg.end, log_cache=False)
    spy_close = spy_df["close"]
    regime_series = (spy_close > spy_close.rolling(200).mean())
    up_days = int(regime_series.sum())
    print(f"[regime] SPY 200DMA：{up_days}/{len(regime_series)} 日上行")

    qqq_df = fetch_daily_bars("QQQ.US",
                               daily_cfg.start - timedelta(days=10),
                               daily_cfg.end, log_cache=False)

    # ---------- 跑两份回测 ----------
    print(f"\n========== 回测 1/2: DAILY 模式（{daily_cfg.start} ~ {daily_cfg.end}） ==========")
    daily_result = run_backtest(daily_panel, daily_cfg, regime_series=regime_series)
    daily_summary = summarize(daily_result, daily_cfg,
                                _slice_qqq(qqq_df, daily_cfg.start, daily_cfg.end))
    print_summary(daily_summary, title="DAILY 模式（跨牛熊参考）")
    print_top_trades(daily_result.trades)

    print(f"\n========== 回测 2/2: INTRADAY 模式（{intra_cfg.start} ~ {intra_cfg.end}） ==========")
    intra_result = run_backtest(intra_panel, intra_cfg, regime_series=regime_series)
    intra_summary = summarize(intra_result, intra_cfg,
                                _slice_qqq(qqq_df, intra_cfg.start, intra_cfg.end))
    print_summary(intra_summary, title="INTRADAY 模式（实盘对齐）")
    print_top_trades(intra_result.trades)

    # ---------- 并排对比 + 自然年分项 ----------
    print_compare(daily_summary, intra_summary)
    print_yearly_comparison(daily_result, intra_result)


if __name__ == "__main__":
    main()
