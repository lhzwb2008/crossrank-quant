#!/usr/bin/env python3
"""用本地缓存复现 backtest.py 主流程（DAILY + INTRADAY），参数对齐 simulate.py。"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd

import backtest as bt
from run_cached_backtest import load_daily_cache, load_intraday_cache
from universe import get_universe


def main():
    # 与 backtest.py / simulate.py 默认一致
    end = date.today()
    daily_start = date.fromisoformat(bt.DAILY_START)
    intra_start = date.fromisoformat(bt.INTRA_START)

    daily_cfg = bt.Config(start=daily_start, end=end, mode="daily", verbose_trades=False)
    intra_cfg = bt.Config(start=intra_start, end=end, mode="intraday", verbose_trades=False)

    print("=" * 60)
    print("  基准回测（纯缓存 | 对齐 simulate / backtest 默认参数）")
    print("=" * 60)
    print(f"  DAILY   {daily_cfg.start} ~ {daily_cfg.end}")
    print(f"  INTRA   {intra_cfg.start} ~ {intra_cfg.end}")
    print(f"  K_LONG={daily_cfg.k_long} mom={daily_cfg.mom_weight} bias={daily_cfg.bias_weight}")
    print(f"  risk_w={daily_cfg.risk_weight} rsi={daily_cfg.use_rsi_rev} mom120={daily_cfg.use_mom_120}")
    print(f"  hyst={daily_cfg.hysteresis_mult}x stop=max({daily_cfg.stop_loss_pct*100:.0f}%,"
          f"{daily_cfg.stop_loss_atr_mult}×ATR) hold={daily_cfg.max_hold_days}d")
    print(f"  regime={daily_cfg.regime_filter} vol_target={daily_cfg.vol_target_annual}")
    print("=" * 60)

    syms = get_universe()
    t0 = time.time()
    print(f"\n[缓存] 日线 {len(syms)}...", flush=True)
    data = {}
    for i, sym in enumerate(syms, 1):
        df = load_daily_cache(sym, daily_start, end, warmup_days=260)
        if len(df):
            data[sym] = df
        if i % 100 == 0:
            print(f"  {i}/{len(syms)}", flush=True)
    print(f"[缓存] 日线有效 {len(data)}/{len(syms)} ({time.time()-t0:.1f}s)", flush=True)

    spy = load_daily_cache("SPY.US", daily_start - timedelta(days=400), end, warmup_days=0)
    qqq = load_daily_cache("QQQ.US", daily_start, end, warmup_days=0)
    if len(spy) == 0:
        raise SystemExit("SPY 缓存缺失，无法算 regime")

    # 有效终点：以 SPY 缓存为准（regime 依赖）；成分股若更新更多也不超越 SPY
    data_end = spy.index.max().date()
    print(f"[缓存] 有效终点 = {data_end} (受 SPY/regime 缓存限制)", flush=True)

    daily_cfg.end = data_end
    intra_cfg.end = data_end

    print("\n[计算] DAILY panel...", flush=True)
    t1 = time.time()
    daily_panel = bt.build_daily_panel(data)
    print(f"  done ({time.time()-t1:.1f}s)", flush=True)

    print(f"\n[缓存] 分钟 {bt.INTRADAY_PERIOD}...", flush=True)
    t2 = time.time()
    intraday = {}
    keys = list(data.keys())
    for i, sym in enumerate(keys, 1):
        df = load_intraday_cache(sym, intra_start, data_end, bt.INTRADAY_PERIOD)
        if len(df):
            intraday[sym] = df
        if i % 100 == 0:
            print(f"  {i}/{len(keys)}", flush=True)
    print(f"[缓存] 分钟有效 {len(intraday)}/{len(keys)} ({time.time()-t2:.1f}s)", flush=True)

    print("\n[计算] INTRADAY panel...", flush=True)
    t3 = time.time()
    enhanced = bt.build_intraday_enhanced_panel(
        data, intraday, bt.INTRADAY_PERIOD, bt.DECISION_TIME_ET,
    )
    intra_panel = bt.build_panel(enhanced)
    print(f"  done ({time.time()-t3:.1f}s)", flush=True)

    regime = spy["close"] > spy["close"].rolling(200).mean()
    qqq_d = bt._slice_qqq(qqq, daily_cfg.start, daily_cfg.end) if len(qqq) else None
    qqq_i = bt._slice_qqq(qqq, intra_cfg.start, intra_cfg.end) if len(qqq) else None

    print("\n========== 回测 DAILY ==========", flush=True)
    t4 = time.time()
    daily_result = bt.run_backtest(daily_panel, daily_cfg, regime_series=regime)
    daily_summary = bt.summarize(daily_result, daily_cfg, qqq_d)
    print(f"  ({time.time()-t4:.1f}s)", flush=True)
    bt.print_summary(daily_summary, title="DAILY 基准（对齐 simulate 参数）")

    print("\n========== 回测 INTRADAY ==========", flush=True)
    t5 = time.time()
    intra_result = bt.run_backtest(intra_panel, intra_cfg, regime_series=regime)
    intra_summary = bt.summarize(intra_result, intra_cfg, qqq_i)
    print(f"  ({time.time()-t5:.1f}s)", flush=True)
    bt.print_summary(intra_summary, title="INTRADAY 基准（对齐 simulate / 实盘）")

    bt.print_compare(daily_summary, intra_summary)
    bt.print_yearly_comparison(daily_result, intra_result)

    dm = bt.metrics_raw(daily_result, daily_cfg)
    im = bt.metrics_raw(intra_result, intra_cfg)
    print("\n===== 优化基准锚点（原始指标）=====")
    print(f"  DAILY    Calmar={dm['calmar']:.3f}  CAGR={dm['cagr']*100:.2f}%  "
          f"MDD={dm['max_dd']*100:.2f}%  Sharpe={dm['sharpe']:.2f}")
    print(f"  INTRADAY Calmar={im['calmar']:.3f}  CAGR={im['cagr']*100:.2f}%  "
          f"MDD={im['max_dd']*100:.2f}%  Sharpe={im['sharpe']:.2f}")
    print(f"  score=min(D,I) Calmar = {min(dm['calmar'], im['calmar']):.3f}")
    print("===================================")


if __name__ == "__main__":
    main()
