#!/usr/bin/env python3
"""纯本地缓存回测（不调用 Longport API）。"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pandas as pd

import backtest as bt
from daily_cache import daily_cache_path, normalize_df_index
from intraday_api import intraday_cache_path, filter_rth
from universe import get_universe, label as sym_label


def _slice_df(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    t0 = pd.Timestamp(start)
    t1 = pd.Timestamp(end)
    mask = (df.index >= t0) & (df.index <= t1)
    return df.loc[mask].copy()


def load_daily_cache(symbol: str, start: date, end: date,
                     warmup_days: int = 240) -> pd.DataFrame:
    path = daily_cache_path(symbol)
    if not os.path.exists(path) or os.path.getsize(path) < 10:
        return pd.DataFrame()
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df = normalize_df_index(df)
    fetch_start = start - timedelta(days=warmup_days)
    t0 = pd.Timestamp(fetch_start)
    t1 = pd.Timestamp(end)
    return df.loc[(df.index >= t0) & (df.index <= t1)].copy()


def load_intraday_cache(symbol: str, start: date, end: date,
                        period: str = "5min") -> pd.DataFrame:
    path = intraday_cache_path(symbol, period)
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    df = df.sort_index()
    t0 = pd.Timestamp(start)
    t1 = pd.Timestamp(end) + pd.Timedelta(days=1)
    out = df.loc[(df.index >= t0) & (df.index < t1)].copy()
    return filter_rth(out) if len(out) else out


def main():
    end_date = date.today()
    start_date = end_date - timedelta(days=182)
    mode = os.getenv("BT_MODE", "intraday")  # intraday | daily

    cfg = bt.Config(
        start=start_date,
        end=end_date,
        mode=mode,
        verbose_trades=True,
    )

    print("=" * 70, flush=True)
    print(f"  纯缓存回测（无 API）| 模式: {mode.upper()}", flush=True)
    print(f"  区间: {start_date} ~ {end_date}", flush=True)
    print(f"  决策时点: {cfg.decision_time_et} ET | 本金: ${cfg.starting_capital:,}", flush=True)
    print("=" * 70, flush=True)

    syms = get_universe()
    print(f"\n[缓存] 读取 {len(syms)} 只日线...", flush=True)
    data: dict = {}
    for i, sym in enumerate(syms, 1):
        df = load_daily_cache(sym, start_date, end_date)
        if len(df) > 0:
            data[sym] = df
        if i % 100 == 0:
            print(f"  日线 {i}/{len(syms)}", flush=True)
    print(f"[缓存] 日线有效 {len(data)}/{len(syms)}", flush=True)

    if mode == "intraday":
        print(f"\n[缓存] 读取分钟线 ({cfg.intraday_period})...", flush=True)
        intraday: dict = {}
        keys = list(data.keys())
        for i, sym in enumerate(keys, 1):
            df = load_intraday_cache(sym, start_date, end_date, cfg.intraday_period)
            if len(df) > 0:
                intraday[sym] = df
            if i % 100 == 0:
                print(f"  分钟 {i}/{len(keys)}", flush=True)
        print(f"[缓存] 分钟有效 {len(intraday)}/{len(keys)}", flush=True)

        print("\n[计算] 构造 INTRADAY panel...", flush=True)
        enhanced = bt.build_intraday_enhanced_panel(
            data, intraday, cfg.intraday_period, cfg.decision_time_et,
        )
        panel = bt.build_panel(enhanced)
    else:
        print("\n[计算] 构造 DAILY panel...", flush=True)
        panel = bt.build_daily_panel(data)

    spy_df = load_daily_cache("SPY.US", start_date - timedelta(days=400), end_date, warmup_days=0)
    if len(spy_df) == 0:
        print("[警告] SPY 缓存缺失，regime 默认全开", flush=True)
        regime_series = pd.Series(True, index=pd.bdate_range(start_date, end_date))
    else:
        regime_series = spy_df["close"] > spy_df["close"].rolling(200).mean()

    qqq_df = load_daily_cache("QQQ.US", start_date, end_date, warmup_days=0)
    qqq_bench = None
    if len(qqq_df) > 0:
        qqq_bench = qqq_df.loc[
            (qqq_df.index >= pd.Timestamp(start_date)) &
            (qqq_df.index <= pd.Timestamp(end_date)), "close"
        ]

    print(f"\n========== 回测 ({mode.upper()}) ==========", flush=True)
    print("\n----- 逐笔 OPEN/CLOSE -----\n", flush=True)
    result = bt.run_backtest(panel, cfg, regime_series=regime_series)
    summary = bt.summarize(result, cfg, qqq_bench)
    bt.print_summary(summary, title=f"{mode.upper()} 近6个月（纯缓存）")

    trades = result.trades
    print(f"\n----- 全部平仓交易 ({len(trades)} 笔) -----\n", flush=True)
    hdr = (f"  {'#':<4} {'标的':<24} {'方向':<6} {'开仓日':<12} {'平仓日':<12} "
           f"{'入场价':>9} {'出场价':>9} {'盈亏%':>8} {'盈亏$':>12} {'天数':>5} {'原因'}")
    print(hdr, flush=True)
    print("  " + "-" * 120, flush=True)
    for i, t in enumerate(trades, 1):
        side = "LONG" if t["side"] == 1 else "SHORT"
        print(
            f"  {i:<4} {sym_label(t['symbol']):<24} {side:<6} "
            f"{t['entry_date'].strftime('%Y-%m-%d'):<12} {t['exit_date'].strftime('%Y-%m-%d'):<12} "
            f"${t['entry_price']:8.2f} ${t['exit_price']:8.2f} "
            f"{t['pnl_pct']*100:+7.2f}% ${t['pnl_usd']:+10,.0f} "
            f"{t['days_held']:>5d} {t['reason']}",
            flush=True,
        )
    bt.print_top_trades(trades)


if __name__ == "__main__":
    main()
