#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于横截面 rank 策略 + Longport 实时行情的模拟交易程序（独立可运行）。

本脚本**自包含**策略实现，不依赖 backtest.py。运行所需的其它模块：
  - longport_api.py    日线 API + 缓存
  - intraday_api.py    分钟级 K 线 API + RTH 过滤
  - daily_cache.py     日线 CSV 缓存
  - universe.py        股票池（NAS100 ∪ SP500）
以及 requirements.txt 中的 longport-openapi-python / pandas / numpy / python-dotenv。

策略复刻自 backtest.py 的 INTRADAY 模式（与回测同口径）：
  - 池子：NAS100 ∪ S&P 500（universe.get_universe()）
  - 信号：mom_20/60/120 + IBS/Williams%R/rev_5/RSI + EMA9/21 bias，rank 化合成 composite
  - 决策时点：美东 15:50（DECISION_TIME_ET）
  - 入场：top K_LONG 缺位的 → 市价开多
  - 平仓：跌出 top (K * HYSTERESIS_MULT) / 持仓满 MAX_HOLD_DAYS / SPY 200DMA 翻空
  - 止损：max(4%, 2.5×ATR14)，盘中每分钟用实时报价 vs 本地 stop_price 检查

运行：
    python simulate.py                # 守护进程：盘中每分钟检查止损，15:50 决策
    python simulate.py --once         # 立即跑一次决策逻辑（手动触发，调试用）
    python simulate.py --status       # 打印当前 paper 账户持仓与现金、状态文件
    python simulate.py --cancel-orders  # 撤销今日挂单（未终结状态）
    python simulate.py --prune-state    # 本地状态与券商持仓对齐（撤单后清幽灵记录）

前置：
    .env 中填入 Longport **模拟账号**凭证（LONGPORT_APP_KEY / LONGPORT_APP_SECRET /
    LONGPORT_ACCESS_TOKEN）。确保 token 来自模拟交易账户。

状态持久化：simulate_state.json，记录每只本地视角持仓的开仓日 / 入场价 / 止损价 / 持仓天。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from longport_api import fetch_daily_bars, get_api_singleton
from intraday_api import (
    fetch_intraday_bars, filter_rth, to_et,
    PERIOD_MINUTES, parse_decision_time,
)
from universe import get_universe, label as sym_label

load_dotenv()

# ============================================================================
#                              策略参数（与 backtest.py 同步）
# ============================================================================
# Calmar 优化后默认（2026-07）：mom_120 + RSI，K=12，hyst=2.5，止损 4%/2.5×ATR
K_LONG               = 12
HYSTERESIS_MULT      = 2.5
MOM_WEIGHT           = 0.9
BIAS_WEIGHT          = 0.4
USE_RSI_REV          = True
USE_MOM_120          = True
STOP_LOSS_PCT        = 0.04
STOP_LOSS_ATR_MULT   = 2.5
MAX_HOLD_DAYS        = 60
MIN_DOLLAR_VOLUME    = 5e7
REGIME_FILTER        = True
INTRADAY_PERIOD      = "5min"
DECISION_TIME_ET     = "15:50"

STATE_FILE = os.getenv("SIMULATE_STATE_FILE", "simulate_state.json")
LOG_FILE   = os.getenv("SIMULATE_LOG_FILE", "simulate.log")
ET_TZ      = "America/New_York"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("simulate")


# ============================================================================
#                              指标与横截面分数
# ============================================================================

def _ema(s, n): return s.ewm(span=n, adjust=False).mean()

def _atr(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()

def _williams_r(h, l, c, n=14):
    hh = h.rolling(n).max(); ll = l.rolling(n).min()
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
    out["ema9"]   = _ema(c, 9)
    out["ema21"]  = _ema(c, 21)
    out["trend_up"] = (out["ema9"] > out["ema21"]).astype(float)
    out["mom_20"] = c / c.shift(20) - 1
    out["mom_60"] = c / c.shift(60) - 1
    out["mom_120"] = c / c.shift(120) - 1
    out["rev_5"]  = -(c / c.shift(5) - 1)
    rng = (h - l).replace(0, np.nan)
    out["ibs"]    = (c - l) / rng
    out["wr14"]   = _williams_r(h, l, c, 14)
    out["rsi14"]  = _rsi(c, 14)
    out["atr14"]  = _atr(h, l, c, 14)
    out["dollar_vol_20"] = (c * out["volume"]).rolling(20).mean()
    return out


def compute_proxy_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """把每天的 today-row 指标用决策时点截面（pd_close/high/low/volume）覆盖重算。

    与 backtest.compute_proxy_indicators 完全等价。
    """
    out = df.copy()
    if "pd_close" not in out.columns or out["pd_close"].isna().all():
        return out

    pd_c = out["pd_close"]; pd_h = out["pd_high"]
    pd_l = out["pd_low"];   pd_v = out["pd_volume"]
    valid = pd_c.notna()

    out.loc[valid, "mom_20"] = pd_c[valid] / out["close"].shift(20)[valid] - 1
    out.loc[valid, "mom_60"] = pd_c[valid] / out["close"].shift(60)[valid] - 1
    out.loc[valid, "mom_120"] = pd_c[valid] / out["close"].shift(120)[valid] - 1
    out.loc[valid, "rev_5"]  = -(pd_c[valid] / out["close"].shift(5)[valid] - 1)

    rng = (pd_h - pd_l).replace(0, np.nan)
    out.loc[valid, "ibs"] = (pd_c[valid] - pd_l[valid]) / rng[valid]

    hh14 = pd.concat([out["high"].shift(1).rolling(13).max(), pd_h], axis=1).max(axis=1)
    ll14 = pd.concat([out["low"].shift(1).rolling(13).min(), pd_l], axis=1).min(axis=1)
    rng14 = (hh14 - ll14).replace(0, np.nan)
    out.loc[valid, "wr14"] = (-100 * (hh14 - pd_c) / rng14)[valid]

    close_proxy = out["close"].copy()
    close_proxy.loc[valid] = pd_c[valid]
    out.loc[valid, "rsi14"] = _rsi(close_proxy, 14)[valid]

    prev_c = out["close"].shift(1)
    past_tr = pd.concat([
        (out["high"] - out["low"]),
        (out["high"] - prev_c).abs(),
        (out["low"] - prev_c).abs(),
    ], axis=1).max(axis=1).shift(1)
    tr_today = pd.concat([(pd_h - pd_l),
                          (pd_h - prev_c).abs(),
                          (pd_l - prev_c).abs()], axis=1).max(axis=1)
    atr_proxy = (past_tr.rolling(13).sum() + tr_today) / 14
    out.loc[valid, "atr14"] = atr_proxy[valid]

    today_dv = (pd_c * pd_v)
    past_dv  = (out["close"] * out["volume"]).shift(1).rolling(19).sum()
    out.loc[valid, "dollar_vol_20"] = ((past_dv + today_dv) / 20)[valid]

    out["ema9"]  = out["ema9"].shift(1)
    out["ema21"] = out["ema21"].shift(1)
    out["trend_up"] = (out["ema9"] > out["ema21"]).astype(float)
    return out


def _csrank(s):
    valid = s.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=s.index)
    r = valid.rank(method="average", pct=True)
    return (r * 2 - 1).reindex(s.index)


def composite_score(day_panel: pd.DataFrame, mom_w=MOM_WEIGHT,
                    bias_w=BIAS_WEIGHT, use_rsi_rev: bool = USE_RSI_REV,
                    use_mom_120: bool = USE_MOM_120) -> pd.Series:
    s_mom20 = _csrank(day_panel["mom_20"])
    s_mom60 = _csrank(day_panel["mom_60"])
    if use_mom_120 and "mom_120" in day_panel.columns:
        s_mom120 = _csrank(day_panel["mom_120"])
        momentum = (s_mom20 + s_mom60 + s_mom120) / 3
    else:
        momentum = (s_mom20 + s_mom60) / 2

    s_ibs   = -_csrank(day_panel["ibs"])
    s_wr    = -_csrank(-day_panel["wr14"])
    s_rev   = _csrank(day_panel["rev_5"])
    rev_parts = [s_ibs, s_wr, s_rev]
    if use_rsi_rev and "rsi14" in day_panel.columns:
        rev_parts.append(-_csrank(day_panel["rsi14"]))
    reversal = sum(rev_parts) / len(rev_parts)

    bias = day_panel["trend_up"].fillna(0.5) * 2 - 1
    return mom_w * momentum + (1 - mom_w) * reversal + bias_w * bias


# ============================================================================
#                              数据加载（日线 + 分钟）
# ============================================================================

def load_all_daily(symbols: List[str], start: date, end: date) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    max_workers = int(os.getenv("NAS100_FETCH_WORKERS", "8"))
    try:
        get_api_singleton()
    except Exception as e:
        log.warning(f"Longport 初始化失败（若缓存全命中可忽略）: {e}")

    def _fetch(sym):
        try:
            return sym, fetch_daily_bars(sym, start, end, log_cache=False)
        except Exception as e:
            log.warning(f"{sym} 日线拉取失败: {e}")
            return sym, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_fetch, s) for s in symbols]
        for i, f in enumerate(as_completed(futs), 1):
            sym, df = f.result()
            if len(df) > 0:
                out[sym] = df
            if i % 50 == 0:
                log.info(f"  日线已加载 {i}/{len(symbols)}")
    log.info(f"[数据] 日线成功加载 {len(out)}/{len(symbols)}")
    return out


def load_all_intraday(symbols: List[str], start: date, end: date,
                      period_label: str) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    # 可调环境变量 NAS100_INTRADAY_WORKERS；默认 8。若触发 connections limit，改为 4~6。
    max_workers = int(os.getenv("NAS100_INTRADAY_WORKERS", "8"))
    try:
        get_api_singleton()
    except Exception as e:
        log.warning(f"Longport 初始化失败: {e}")

    def _fetch(sym):
        try:
            df = fetch_intraday_bars(sym, start, end, period_label, log_cache=False)
            if len(df) > 0:
                df = filter_rth(df)
            return sym, df
        except Exception as e:
            log.warning(f"{sym} 分钟拉取失败: {e}")
            return sym, pd.DataFrame()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_fetch, s) for s in symbols]
        for i, f in enumerate(as_completed(futs), 1):
            sym, df = f.result()
            if len(df) > 0:
                out[sym] = df
            if i % 50 == 0 or i == len(symbols):
                log.info(f"  分钟已加载 {i}/{len(symbols)}")
    log.info(f"[数据] 分钟成功加载 {len(out)}/{len(symbols)}")
    return out


def summarize_intraday_per_day(intraday_df: pd.DataFrame, period_label: str,
                                decision_time_et: str) -> pd.DataFrame:
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
        "gap_open":  g["open"].first(),
        "pd_open":   g["open"].first(),
        "pd_high":   g["high"].max(),
        "pd_low":    g["low"].min(),
        "pd_close":  g["close"].last(),
        "pd_volume": g["volume"].sum(),
    })
    summary.index.name = None
    return summary


def merge_daily_with_intraday(daily_df, intra_summary):
    if daily_df is None or len(daily_df) == 0:
        return daily_df
    if intra_summary is None or len(intra_summary) == 0:
        for col in ("gap_open", "pd_open", "pd_high", "pd_low", "pd_close", "pd_volume"):
            daily_df[col] = np.nan
        return daily_df
    full_idx = daily_df.index.union(intra_summary.index)
    return daily_df.reindex(full_idx).join(intra_summary, how="left")


def build_today_panel() -> Dict[str, pd.DataFrame]:
    """拉日线（含 warmup 200+ 日）+ 分钟，汇总到决策时点，返回面板。

    分钟范围：先试「仅当日」（快）；若在美东开盘前或非交易日则无 RTH K 线，
    会全部为空——此时自动回溯约 10 个日历日重拉（与盘前 `--once` 兼容）。
    """
    syms = get_universe()
    today = _today_et()
    # 日线 end = 上一个美股工作日（跳过周末）。今天的行由分钟数据合成。
    # 本地缓存覆盖到该日时，~516 只全部秒级命中、无 API 调用。
    daily_end = today - timedelta(days=1)
    while daily_end.weekday() >= 5:  # Sat=5, Sun=6
        daily_end -= timedelta(days=1)
    daily_start = today - timedelta(days=400)
    log.info(f"[数据] 加载 {len(syms)} 只日线 {daily_start} ~ {daily_end} (缓存优先)")
    daily = load_all_daily(syms, daily_start, daily_end)

    def _intraday_nonempty_cnt(intra: Dict[str, pd.DataFrame]) -> int:
        return sum(1 for df in intra.values() if df is not None and len(df) > 0)

    intra_wide_start = today - timedelta(days=10)
    now_et = _et_now()
    rth_open = datetime.strptime("09:30", "%H:%M").time()
    # 周一至周五、且尚未开盘：当日 RTH bar 本来就不存在，跳过「先试仅当日」省一轮 IO
    if now_et.weekday() < 5 and now_et.time() < rth_open:
        log.info(
            f"[数据] 美东开盘前，直接回溯分钟 {INTRADAY_PERIOD} "
            f"{intra_wide_start} ~ {today}"
        )
        intraday = load_all_intraday(
            list(daily.keys()), intra_wide_start, today, INTRADAY_PERIOD,
        )
    else:
        intra_start = today
        log.info(f"[数据] 加载分钟 {INTRADAY_PERIOD} {intra_start} ~ {today} (先试仅当日)")
        intraday = load_all_intraday(
            list(daily.keys()), intra_start, today, INTRADAY_PERIOD,
        )

        if _intraday_nonempty_cnt(intraday) == 0:
            log.warning(
                "当日无可用 RTH 分钟数据（周末或非交易日）；"
                f"回溯拉取分钟 {intra_wide_start} ~ {today}"
            )
            intraday = load_all_intraday(
                list(daily.keys()), intra_wide_start, today, INTRADAY_PERIOD,
            )

    enhanced: Dict[str, pd.DataFrame] = {}
    for sym, dfd in daily.items():
        intra_summary = summarize_intraday_per_day(
            intraday.get(sym, pd.DataFrame()), INTRADAY_PERIOD, DECISION_TIME_ET,
        )
        enhanced[sym] = merge_daily_with_intraday(dfd, intra_summary)
    return {sym: compute_indicators(df) for sym, df in enhanced.items()}


def get_regime() -> bool:
    today = _today_et()
    spy = fetch_daily_bars("SPY.US", today - timedelta(days=400), today,
                            log_cache=False)
    if spy is None or len(spy) < 200:
        log.warning("SPY 数据不足，regime 默认 True")
        return True
    return bool(spy["close"].iloc[-1] > spy["close"].rolling(200).mean().iloc[-1])


# ============================================================================
#                              本地状态持久化
# ============================================================================

@dataclass
class LocalPosition:
    symbol: str
    entry_date: str
    entry_price: float
    stop_price: float
    shares: float
    days_held: int = 0
    last_decision_date: str = ""


def load_state() -> Dict[str, LocalPosition]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        return {k: LocalPosition(**v) for k, v in raw.items()}
    except Exception as e:
        log.warning(f"读取状态失败 {STATE_FILE}: {e}，按空状态启动")
        return {}


def save_state(state: Dict[str, LocalPosition]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: asdict(v) for k, v in state.items()}, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ============================================================================
#                              Longport 交易封装
# ============================================================================

class Broker:
    def __init__(self):
        from longport.openapi import Config as LpConfig, TradeContext
        self.config = LpConfig.from_env()
        self.trade_ctx = TradeContext(self.config)
        self.quote_ctx = get_api_singleton().quote_ctx
        log.info("Longport 交易上下文已建立（请确认 .env 指向模拟账户）")

    def cash_usd(self) -> float:
        try:
            bals = self.trade_ctx.account_balance(currency="USD")
            for b in bals:
                for cinfo in getattr(b, "cash_infos", []) or []:
                    if str(getattr(cinfo, "currency", "")).upper() == "USD":
                        return float(cinfo.available_cash or 0)
                return float(b.net_assets or 0)
        except Exception as e:
            log.warning(f"读取账户余额失败: {e}")
        return 0.0

    def total_equity_usd(self) -> float:
        """USD 净资产（含持仓市值）。用于按 K_LONG 等权计算每仓目标 notional。"""
        try:
            bals = self.trade_ctx.account_balance(currency="USD")
            for b in bals:
                v = float(getattr(b, "net_assets", 0) or 0)
                if v > 0:
                    return v
        except Exception as e:
            log.warning(f"读取账户净资产失败: {e}")
        return 0.0

    def positions(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            resp = self.trade_ctx.stock_positions()
            for ch in resp.channels:
                for p in ch.positions:
                    sym = p.symbol
                    qty = float(p.quantity)
                    if sym.endswith(".US") and qty > 0:
                        out[sym] = out.get(sym, 0.0) + qty
        except Exception as e:
            log.warning(f"读取持仓失败: {e}")
        return out

    def last_price(self, symbol: str) -> Optional[float]:
        try:
            qs = self.quote_ctx.quote([symbol])
            if qs:
                return float(qs[0].last_done)
        except Exception as e:
            log.warning(f"取报价 {symbol} 失败: {e}")
        return None

    def last_prices(self, symbols: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        if not symbols:
            return out
        for i in range(0, len(symbols), 400):
            batch = symbols[i:i+400]
            try:
                for q in self.quote_ctx.quote(batch):
                    try:
                        out[q.symbol] = float(q.last_done)
                    except Exception:
                        pass
            except Exception as e:
                log.warning(f"批量报价失败 ({len(batch)}): {e}")
        return out

    def cancel_open_orders_today(self) -> tuple[int, int]:
        """调用 Longport cancel_order：尽可能撤销当日列表中仍可撤的单。

        对已成交 / 已拒绝 / 已撤回 / 已过期单跳过。
        返回 (成功撤单数, 跳过或失败数)。
        """
        from longport.openapi import OrderStatus

        terminal = (
            OrderStatus.Filled,
            OrderStatus.Canceled,
            OrderStatus.Rejected,
            OrderStatus.Expired,
        )

        try:
            orders = self.trade_ctx.today_orders()
        except Exception as e:
            log.error(f"today_orders 失败: {e}")
            return 0, 0

        ok = 0
        skip_fail = 0
        for o in orders:
            oid = getattr(o, "order_id", None)
            sym = getattr(o, "symbol", "?")
            st = getattr(o, "status", None)
            if st is None or st in terminal:
                skip_fail += 1
                log.info(f"跳过（已终结） {sym} id={oid} status={st}")
                continue
            try:
                self.trade_ctx.cancel_order(oid)
                ok += 1
                log.info(f"已提交撤单 {sym_label(sym)} id={oid} status={st}")
            except Exception as e:
                skip_fail += 1
                log.warning(f"撤单失败 {sym} id={oid}: {e}")
        return ok, skip_fail

    def submit_market(self, symbol: str, side: str, qty: int,
                      remark: str = "") -> Optional[str]:
        """时段感知下单：

        - RTH (09:30–16:00 ET) → 市价单 MO（默认 RTHOnly）
        - 夜盘 (周日–周四 20:00 ET ~ 次日 03:50 ET) → LO + OutsideRTH=Overnight，
          限价为 mkt × 1.05（买）或 × 0.95（卖），确保深度对价成交
        - 其它时段（盘前 04:00–09:30、盘后 16:00–20:00） → LO + OutsideRTH=AnyTime，
          同上限价策略

        注意：Longport 文档明示「MO 仅支持 RTH，会忽略 outside_rth」，因此非 RTH
        必须改用限价单 + outside_rth 才能成交。
        """
        from longport.openapi import OrderType, OrderSide, TimeInForceType, OutsideRTH
        from decimal import ROUND_HALF_UP
        if qty <= 0:
            return None
        side_enum = OrderSide.Buy if side == "buy" else OrderSide.Sell

        now_et = pd.Timestamp.now(tz="UTC").tz_convert(ET_TZ)
        t = now_et.time()
        wd = now_et.weekday()  # 0=Mon
        rth_open  = datetime.strptime("09:30", "%H:%M").time()
        rth_close = datetime.strptime("16:00", "%H:%M").time()
        on_open   = datetime.strptime("20:00", "%H:%M").time()
        on_close  = datetime.strptime("03:50", "%H:%M").time()

        in_rth = wd < 5 and rth_open <= t <= rth_close
        # Overnight: 周日–周四 20:00–次日 03:50；映射到本地 wd：周一凌晨 (wd=0, t<03:50)
        # 或 周日–周四晚 (wd in 6,0..3, t>=20:00)
        in_overnight_late = wd in (6, 0, 1, 2, 3) and t >= on_open
        in_overnight_early = wd in (0, 1, 2, 3, 4) and t < on_close
        in_overnight = in_overnight_late or in_overnight_early

        params: Dict = {
            "symbol": symbol,
            "side": side_enum,
            "submitted_quantity": Decimal(int(qty)),
            "time_in_force": TimeInForceType.Day,
            "remark": remark[:64] if remark else None,
        }

        if in_rth:
            params["order_type"] = OrderType.MO
            mode = "MO/RTH"
        else:
            # 非 RTH：用 LO + outside_rth
            px = self.last_price(symbol)
            if not px or px <= 0:
                log.error(f"  ✗ {side} {symbol}: 非 RTH 时段需要现价计算限价，但报价为空")
                return None
            mult = 1.05 if side_enum == OrderSide.Buy else 0.95
            limit = Decimal(str(px * mult)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
            params["order_type"] = OrderType.LO
            params["submitted_price"] = limit
            if in_overnight:
                params["outside_rth"] = OutsideRTH.Overnight
                mode = f"LO/Overnight @${limit}"
            else:
                params["outside_rth"] = OutsideRTH.AnyTime
                mode = f"LO/AnyTime @${limit}"

        try:
            resp = self.trade_ctx.submit_order(**params)
            oid = getattr(resp, "order_id", None) or str(resp)
            log.info(f"  → {side.upper()} {sym_label(symbol)} x{qty}  {mode}  "
                     f"id={oid}  ({remark})")
            return oid
        except Exception as e:
            log.error(f"  ✗ 下单失败 {side} {symbol} x{qty} ({mode}): {e}")
            return None


# ============================================================================
#                              决策与盘中止损
# ============================================================================

def _today_et() -> date:
    return pd.Timestamp.now(tz="UTC").tz_convert(ET_TZ).date()

def _et_now() -> datetime:
    return pd.Timestamp.now(tz="UTC").tz_convert(ET_TZ).to_pydatetime()


def run_decision(broker: Broker, state: Dict[str, LocalPosition],
                 force: bool = False) -> None:
    today_iso = _today_et().isoformat()
    log.info("=" * 70)
    log.info(f"  决策开始  ET={_et_now().strftime('%Y-%m-%d %H:%M:%S')}  force={force}")
    log.info("=" * 70)

    panel = build_today_panel()
    today_ts = pd.Timestamp(_today_et())

    # 选取决策日：若今日已有分钟数据则用今天；否则用全市场最近一个 pd_close 充足的日期。
    # 这让 --once 在盘前/盘后也能基于上一交易日的截面做一次 dry-run。
    date_counts: Dict[pd.Timestamp, int] = {}
    for df in panel.values():
        if "pd_close" not in df.columns:
            continue
        for d in df.index[df["pd_close"].notna()]:
            date_counts[d] = date_counts.get(d, 0) + 1
    eligible = sorted([d for d, n in date_counts.items()
                       if n >= K_LONG + 5 and d <= today_ts], reverse=True)
    if not eligible:
        log.warning("无任何有效决策日（pd_close 数据不足），退出")
        return
    decision_ts = eligible[0]
    if decision_ts != today_ts:
        log.info(f"今日({today_ts.date()}) 暂无分钟数据，回退到最近交易日 {decision_ts.date()} 做决策")

    today_data: Dict[str, pd.Series] = {}
    for sym, df in panel.items():
        if decision_ts not in df.index:
            continue
        proxy = compute_proxy_indicators(df).loc[decision_ts]
        if pd.isna(proxy.get("pd_close")) or pd.isna(proxy.get("mom_60")) \
           or pd.isna(proxy.get("atr14")):
            continue
        if proxy.get("dollar_vol_20", 0) < MIN_DOLLAR_VOLUME:
            continue
        today_data[sym] = proxy

    if len(today_data) < K_LONG + 5:
        log.warning(f"今日截面仅 {len(today_data)} 只，跳过决策")
        return

    day_panel = pd.DataFrame(today_data).T
    scores = composite_score(day_panel).dropna().sort_values(ascending=False)
    top_k = list(scores.head(K_LONG).index)
    top_band = set(scores.head(int(K_LONG * HYSTERESIS_MULT)).index)
    log.info(f"截面 {len(scores)} 只，top {K_LONG}: {[sym_label(s) for s in top_k]}")

    regime_up = (not REGIME_FILTER) or get_regime()
    log.info(f"SPY > 200DMA = {regime_up}  → "
             f"{'允许开新多' if regime_up else '禁止开新多'}")

    # reconcile：以 broker 实际持仓为真
    broker_pos = broker.positions()
    for sym in list(state.keys()):
        if sym not in broker_pos:
            log.info(f"  [reconcile] 本地有 {sym_label(sym)}，broker 无 → 移除")
            del state[sym]
    for sym, qty in broker_pos.items():
        if sym not in state:
            px = broker.last_price(sym) or 0.0
            state[sym] = LocalPosition(
                symbol=sym, entry_date=today_iso, entry_price=px,
                stop_price=px * (1 - STOP_LOSS_PCT), shares=qty,
                last_decision_date=today_iso,
            )
            log.info(f"  [reconcile] broker 持仓 {sym_label(sym)} x{qty} 登记本地，"
                     f"保守 stop=${state[sym].stop_price:.2f}")

    # days_held 每决策日 +1
    for lp in state.values():
        if lp.last_decision_date != today_iso:
            lp.days_held += 1
            lp.last_decision_date = today_iso

    # 平仓清单
    to_exit: List[tuple] = []
    for sym, lp in state.items():
        if lp.days_held >= MAX_HOLD_DAYS:
            to_exit.append((sym, "max_hold"))
        elif sym not in top_band:
            to_exit.append((sym, "signal_exit"))
        elif not regime_up:
            to_exit.append((sym, "regime_off"))

    held_after_exit = set(state.keys()) - {s for s, _ in to_exit}
    slots = K_LONG - len(held_after_exit)
    to_open: List[str] = []
    if regime_up and slots > 0:
        for sym in top_k:
            if sym in held_after_exit:
                continue
            to_open.append(sym)
            if len(to_open) >= slots:
                break

    log.info(f"决策：平仓 {len(to_exit)}，开仓 {len(to_open)}，"
             f"目标 {len(held_after_exit)+len(to_open)}/{K_LONG}")

    # 平仓
    for sym, reason in to_exit:
        lp = state.get(sym)
        if not lp:
            continue
        qty = int(round(lp.shares))
        if qty <= 0:
            del state[sym]; continue
        if broker.submit_market(sym, "sell", qty, remark=f"exit:{reason}"):
            del state[sym]
    save_state(state)

    # 开仓
    cash = broker.cash_usd()
    equity = broker.total_equity_usd() or cash  # 净资产兜底用现金
    log.info(f"可用现金 ≈ ${cash:,.0f}, 净资产 ≈ ${equity:,.0f}")
    if cash <= 100 or not to_open:
        save_state(state); return

    # 每个仓位的目标 notional = 净资产 / K_LONG（等同回测里的 equity * (1/K)），
    # 再用 (现金 / 剩余空位 × 0.97 buffer) 兜底，避免单笔超过 buying power。
    target_per_pos = equity / max(K_LONG, 1)
    open_slots = max(K_LONG - len(state), 1)
    cash_cap_per_pos = (cash * 0.97) / open_slots
    per_pos_usd = min(target_per_pos, cash_cap_per_pos)
    log.info(
        f"每仓 notional 上限 ≈ ${per_pos_usd:,.0f} "
        f"(target ${target_per_pos:,.0f}, cash_cap ${cash_cap_per_pos:,.0f}, "
        f"open_slots={open_slots})"
    )
    for sym in to_open:
        row = day_panel.loc[sym]
        atr = float(row["atr14"])
        ref_px = float(row["pd_close"])
        live_px = broker.last_price(sym) or ref_px
        if live_px <= 0 or pd.isna(atr):
            continue
        qty = int(per_pos_usd // live_px)
        if qty <= 0:
            log.info(f"  ⨯ {sym_label(sym)} 现价 ${live_px:.2f} 对应 0 股")
            continue
        stop_dist = max(STOP_LOSS_PCT * live_px, STOP_LOSS_ATR_MULT * atr)
        stop_px = live_px - stop_dist
        if broker.submit_market(sym, "buy", qty,
                                 remark=f"enter:rank={list(scores.index).index(sym)+1}"):
            state[sym] = LocalPosition(
                symbol=sym, entry_date=today_iso, entry_price=live_px,
                stop_price=stop_px, shares=qty,
                last_decision_date=today_iso,
            )
            log.info(f"    ↳ stop=${stop_px:.2f}  notional≈${qty*live_px:,.0f}")

    save_state(state)
    log.info("决策结束\n")


def check_stops(broker: Broker, state: Dict[str, LocalPosition]) -> None:
    if not state:
        return
    prices = broker.last_prices(list(state.keys()))
    triggered: List[str] = []
    for sym, lp in state.items():
        px = prices.get(sym)
        if px is None:
            continue
        if px <= lp.stop_price:
            log.warning(f"[STOP] {sym_label(sym)} last=${px:.2f} ≤ stop=${lp.stop_price:.2f}")
            qty = int(round(lp.shares))
            if qty > 0 and broker.submit_market(sym, "sell", qty, remark="stop_loss"):
                triggered.append(sym)
    for sym in triggered:
        state.pop(sym, None)
    if triggered:
        save_state(state)


# ============================================================================
#                              主循环 / CLI
# ============================================================================

def _is_rth(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    t = now_et.time()
    return (datetime.strptime("09:30", "%H:%M").time() <= t
            <= datetime.strptime("16:00", "%H:%M").time())


def _idle_sleep_sec() -> int:
    return int(os.getenv("SIMULATE_IDLE_SLEEP_SEC", "120"))


def _sleep_until_stopped(total_sec: float, stopped: Dict[str, bool]) -> None:
    """分片 sleep，配合 SIGINT/SIGTERM 设置的 stopped['v']，约 1 秒内退出循环。

    长 sleep(120) + 自定义 signal handler 时，部分环境（尤其 SSH）不会立刻打断
    阻塞，看起来 Ctrl+C「无效」；拆成 1s 片即可恢复预期行为。
    """
    if total_sec <= 0:
        return
    deadline = time.monotonic() + total_sec
    while time.monotonic() < deadline:
        if stopped["v"]:
            return
        time.sleep(min(1.0, deadline - time.monotonic()))


def loop() -> None:
    broker = Broker()
    state = load_state()
    log.info(f"启动循环。本地持仓 {len(state)}: {list(state.keys())}")
    log.info(
        "守护模式说明：仅在美东周一至周五 "
        "09:30–16:00 且时钟 ≥ DECISION_TIME_ET 时跑一次决策并下单；"
        "在这之前不会下单。要立即试跑请改用: python simulate.py --once"
    )

    dec_h, dec_m = map(int, DECISION_TIME_ET.split(":"))
    last_decision_date: Optional[date] = None
    stopped = {"v": False}
    def _sig(*_): stopped["v"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    idle_sec = max(60, _idle_sleep_sec())

    while not stopped["v"]:
        try:
            now = _et_now()
            in_rth = _is_rth(now)
            if not in_rth:
                wd = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
                log.info(
                    f"非美股 RTH（美东约 {now.strftime('%Y-%m-%d %H:%M')} "
                    f"周{wd}），{idle_sec}s 后再检查；盘中为每分钟止损 + "
                    f"{DECISION_TIME_ET} 决策。"
                )
                _sleep_until_stopped(float(idle_sec), stopped)
                continue

            check_stops(broker, state)

            today_d = now.date()
            decision_today = now.replace(hour=dec_h, minute=dec_m,
                                          second=0, microsecond=0)
            if now >= decision_today and last_decision_date != today_d:
                log.info(f"到达决策时点，开始拉数据并执行策略…")
                run_decision(broker, state)
                last_decision_date = today_d

            _sleep_until_stopped(60.0, stopped)
        except Exception as e:
            log.exception(f"循环异常，60s 后重试: {e}")
            _sleep_until_stopped(60.0, stopped)
    log.info("收到退出信号，已停止")


def print_status() -> None:
    broker = Broker()
    state = load_state()
    cash = broker.cash_usd()
    pos = broker.positions()
    print(f"\n== Longport 模拟账户状态 ==")
    print(f"可用现金 USD: ${cash:,.2f}")
    print(f"\n券商持仓 ({len(pos)}):")
    for sym, qty in pos.items():
        lp = state.get(sym)
        px = broker.last_price(sym) or 0
        mv = px * qty
        extra = ""
        if lp:
            pnl = (px - lp.entry_price) * qty
            extra = (f"  entry=${lp.entry_price:.2f} stop=${lp.stop_price:.2f} "
                     f"held={lp.days_held}d PnL=${pnl:+,.0f}")
        print(f"  {sym_label(sym):<22} x{qty:<8} @${px:7.2f}  mv=${mv:>10,.0f}{extra}")
    untracked = set(state.keys()) - set(pos.keys())
    if untracked:
        print(f"\n本地状态有但 broker 无（撤单或未成交后常见）: {untracked}")
        print("对齐方式: python simulate.py --prune-state")


def prune_state_to_match_broker() -> int:
    """删除本地状态中 broker 已无持仓的记录（不触发交易）。"""
    broker = Broker()
    state = load_state()
    if not state:
        print("\n本无本地持仓状态，跳过。\n")
        return 0
    pos = set(broker.positions().keys())
    removed: List[str] = []
    for sym in list(state.keys()):
        if sym not in pos:
            state.pop(sym, None)
            removed.append(sym)
    if removed:
        save_state(state)
        log.info(f"已从 {STATE_FILE} 移除 {len(removed)} 条幽灵记录（broker 无仓）")
    else:
        log.info("本地状态与券商持仓一致，未删除条目")
    print(f"\n已对齐：移除 {len(removed)} 条；剩余本地记录 {len(state)} 条。\n")
    return len(removed)


def main():
    ap = argparse.ArgumentParser(description="横截面 rank 策略 + Longport 模拟交易（独立运行）")
    ap.add_argument("--once", action="store_true", help="立即跑一次决策，不进入循环")
    ap.add_argument("--status", action="store_true", help="打印账户状态后退出")
    ap.add_argument(
        "--cancel-orders",
        action="store_true",
        help="撤销当日未终结挂单（市价未触发等）；按 .env 当前密钥对应账户执行",
    )
    ap.add_argument(
        "--prune-state",
        action="store_true",
        help="对齐 simulate_state.json：删掉 broker 侧已无持仓本地仍残留的记录（不下单）",
    )
    args = ap.parse_args()

    log.info(
        f"参数: K_LONG={K_LONG} mom_w={MOM_WEIGHT} bias_w={BIAS_WEIGHT} "
        f"rsi={'Y' if USE_RSI_REV else 'N'} m120={'Y' if USE_MOM_120 else 'N'} "
        f"hyst={HYSTERESIS_MULT}x stop=max({STOP_LOSS_PCT*100:.0f}%,"
        f"{STOP_LOSS_ATR_MULT}×ATR) max_hold={MAX_HOLD_DAYS}d "
        f"regime={REGIME_FILTER} decision={DECISION_TIME_ET} ET"
    )

    if args.cancel_orders:
        broker = Broker()
        n_ok, n_rest = broker.cancel_open_orders_today()
        print(f"\n撤单完成：成功提交 {n_ok} 笔；跳过/失败 {n_rest} 笔。\n")
        return
    if args.prune_state:
        prune_state_to_match_broker()
        return
    if args.status:
        print_status(); return
    if args.once:
        broker = Broker()
        state = load_state()
        run_decision(broker, state, force=True)
        return
    loop()


if __name__ == "__main__":
    main()
