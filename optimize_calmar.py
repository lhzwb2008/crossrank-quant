#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""以 Calmar 为主目标的参数 / 因子搜索（纯本地缓存，无 API）。

评分：
  Phase1（DAILY 长周期筛选）：最大化 daily_calmar
  Phase2（双段验证）：最大化 min(daily_calmar, intra_calmar)
    —— 避免只在 2024-2026 lucky regime 上过拟合

用法：
  python optimize_calmar.py                  # 默认 48 组随机 + baseline
  python optimize_calmar.py --n-trials 80
  python optimize_calmar.py --top-k 12       # Phase2 验证前 K 名
  python optimize_calmar.py --workers 6
"""
from __future__ import annotations

import argparse
import itertools
import os
import pickle
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

import backtest as bt
from run_cached_backtest import load_daily_cache, load_intraday_cache
from universe import get_universe

# 全局：worker 进程通过 initializer 注入
_G: Dict[str, Any] = {}


def _parse_end(s: str) -> date:
    if s == "today":
        return date.today()
    return date.fromisoformat(s)


def load_context(daily_start: date, intra_start: date, end: date,
                 build_intra: bool = True) -> dict:
    """一次加载：日线 panel +（可选）分钟 panel + regime。"""
    syms = get_universe()
    print(f"[数据] 读取日线缓存 {len(syms)} 只 | {daily_start} ~ {end}", flush=True)
    data: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(syms, 1):
        df = load_daily_cache(sym, daily_start, end, warmup_days=260)
        if len(df) > 0:
            data[sym] = df
        if i % 100 == 0:
            print(f"  日线 {i}/{len(syms)}", flush=True)
    print(f"[数据] 日线有效 {len(data)}/{len(syms)}", flush=True)

    print("[计算] 构造 DAILY panel...", flush=True)
    t0 = time.time()
    daily_panel = bt.build_daily_panel(data)
    print(f"  DAILY panel 完成 ({time.time()-t0:.1f}s)", flush=True)

    intra_panel = None
    if build_intra:
        print(f"[数据] 读取分钟缓存 ({bt.INTRADAY_PERIOD})...", flush=True)
        intraday: Dict[str, pd.DataFrame] = {}
        keys = list(data.keys())
        for i, sym in enumerate(keys, 1):
            df = load_intraday_cache(sym, intra_start, end, bt.INTRADAY_PERIOD)
            if len(df) > 0:
                intraday[sym] = df
            if i % 100 == 0:
                print(f"  分钟 {i}/{len(keys)}", flush=True)
        print(f"[数据] 分钟有效 {len(intraday)}/{len(keys)}", flush=True)
        print("[计算] 构造 INTRADAY panel...", flush=True)
        t1 = time.time()
        # 与 backtest.main 一致：enhanced → build_panel；
        # compute_proxy_indicators 在 run_backtest 内部按日应用
        enhanced = bt.build_intraday_enhanced_panel(
            data, intraday, bt.INTRADAY_PERIOD, bt.DECISION_TIME_ET,
        )
        intra_panel = bt.build_panel(enhanced)
        print(f"  INTRADAY panel 完成 ({time.time()-t1:.1f}s)", flush=True)

    spy = load_daily_cache("SPY.US", daily_start - timedelta(days=400), end, warmup_days=0)
    if len(spy) == 0:
        regime = pd.Series(True, index=pd.bdate_range(daily_start, end))
    else:
        regime = spy["close"] > spy["close"].rolling(200).mean()

    return {
        "daily_panel": daily_panel,
        "intra_panel": intra_panel,
        "regime": regime,
        "daily_start": daily_start,
        "intra_start": intra_start,
        "end": end,
    }


def _init_worker(ctx_path: str):
    global _G
    with open(ctx_path, "rb") as f:
        _G = pickle.load(f)


def _cfg_from_params(params: dict, start: date, end: date, mode: str) -> bt.Config:
    return bt.Config(
        start=start,
        end=end,
        mode=mode,
        verbose_trades=False,
        print_daily_positions=False,
        k_long=int(params["k_long"]),
        mom_weight=float(params["mom_weight"]),
        bias_weight=float(params["bias_weight"]),
        risk_weight=float(params["risk_weight"]),
        use_rsi_rev=bool(params["use_rsi_rev"]),
        use_mom_120=bool(params["use_mom_120"]),
        hysteresis_mult=float(params["hysteresis_mult"]),
        stop_loss_pct=float(params["stop_loss_pct"]),
        stop_loss_atr_mult=float(params["stop_loss_atr_mult"]),
        max_hold_days=int(params["max_hold_days"]),
        vol_target_annual=float(params["vol_target_annual"]),
    )


def _run_one(panel, regime, params: dict, start: date, end: date, mode: str) -> dict:
    cfg = _cfg_from_params(params, start, end, mode)
    result = bt.run_backtest(panel, cfg, regime_series=regime)
    m = bt.metrics_raw(result, cfg)
    return m


def eval_trial(args: Tuple[str, dict, bool, Optional[dict]]) -> dict:
    """worker：评估一组参数。
    phase2=True 时只跑 INTRADAY，并用 daily_metrics 拼出双段 score。
    """
    trial_id, params, phase2, daily_metrics = args
    regime = _G["regime"]
    end = _G["end"]

    t0 = time.time()
    if daily_metrics is None:
        d = _run_one(
            _G["daily_panel"], regime, params,
            _G["daily_start"], end, "daily",
        )
    else:
        d = daily_metrics

    out = {
        "id": trial_id,
        "params": params,
        "d_calmar": d["calmar"],
        "d_cagr": d["cagr"],
        "d_mdd": d["max_dd"],
        "d_sharpe": d["sharpe"],
        "d_trades": d["n_trades"],
        "elapsed": time.time() - t0,
    }

    if phase2 and _G.get("intra_panel") is not None:
        t1 = time.time()
        i = _run_one(
            _G["intra_panel"], regime, params,
            _G["intra_start"], end, "intraday",
        )
        out.update({
            "i_calmar": i["calmar"],
            "i_cagr": i["cagr"],
            "i_mdd": i["max_dd"],
            "i_sharpe": i["sharpe"],
            "i_trades": i["n_trades"],
            "score": min(d["calmar"], i["calmar"]),
            "elapsed": out["elapsed"] + (time.time() - t1),
        })
    else:
        out["score"] = d["calmar"]
    return out


def baseline_params() -> dict:
    return {
        "k_long": bt.K_LONG,
        "mom_weight": bt.MOM_WEIGHT,
        "bias_weight": bt.BIAS_WEIGHT,
        "risk_weight": bt.RISK_WEIGHT,
        "use_rsi_rev": bt.USE_RSI_REV,
        "use_mom_120": bt.USE_MOM_120,
        "hysteresis_mult": bt.HYSTERESIS_MULT,
        "stop_loss_pct": bt.STOP_LOSS_PCT,
        "stop_loss_atr_mult": bt.STOP_LOSS_ATR_MULT,
        "max_hold_days": bt.MAX_HOLD_DAYS,
        "vol_target_annual": bt.VOL_TARGET_ANNUAL,
    }


def sample_params(rng: random.Random) -> dict:
    return {
        "k_long": rng.choice([6, 8, 10, 12, 15]),
        "mom_weight": rng.choice([0.5, 0.6, 0.7, 0.8, 0.9]),
        "bias_weight": rng.choice([0.0, 0.1, 0.2, 0.3, 0.4]),
        "risk_weight": rng.choice([0.0, 0.1, 0.2, 0.3, 0.4, 0.5]),
        "use_rsi_rev": rng.choice([False, True]),
        "use_mom_120": rng.choice([False, True]),
        "hysteresis_mult": rng.choice([2.5, 3.0, 4.0, 5.0, 6.0]),
        "stop_loss_pct": rng.choice([0.04, 0.05, 0.06, 0.08, 0.10]),
        "stop_loss_atr_mult": rng.choice([1.0, 1.5, 2.0, 2.5, 3.0]),
        "max_hold_days": rng.choice([40, 60, 80, 100, 120]),
        "vol_target_annual": rng.choice([0.0, 0.15, 0.20, 0.25]),
    }


def focused_grid() -> List[dict]:
    """围绕 Calmar 改善方向的小网格（低波动倾斜 × 持仓/止损）。"""
    base = baseline_params()
    grids = []
    for risk_w, k, hold, atr, rsi, mom120 in itertools.product(
        [0.0, 0.2, 0.3, 0.4],
        [8, 10, 12],
        [60, 80, 100],
        [1.5, 2.0, 2.5],
        [False, True],
        [False, True],
    ):
        p = dict(base)
        p.update({
            "risk_weight": risk_w,
            "k_long": k,
            "max_hold_days": hold,
            "stop_loss_atr_mult": atr,
            "use_rsi_rev": rsi,
            "use_mom_120": mom120,
        })
        grids.append(p)
    # 控制规模：随机抽 36 组 + 若干有代表性的固定组
    rng = random.Random(42)
    if len(grids) > 36:
        grids = rng.sample(grids, 36)
    # 保证包含「纯新因子」对照
    for risk_w in (0.2, 0.3, 0.4):
        p = dict(base)
        p["risk_weight"] = risk_w
        grids.append(p)
    p = dict(base)
    p.update({"risk_weight": 0.3, "use_rsi_rev": True})
    grids.append(p)
    p = dict(base)
    p.update({"risk_weight": 0.3, "use_mom_120": True})
    grids.append(p)
    p = dict(base)
    p.update({"risk_weight": 0.3, "use_rsi_rev": True, "use_mom_120": True})
    grids.append(p)
    # 去重
    seen = set()
    uniq = []
    for p in grids:
        key = tuple(sorted(p.items()))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _fmt_row(r: dict, rank: int) -> str:
    p = r["params"]
    factors = (
        f"risk={p['risk_weight']:.1f} "
        f"rsi={'Y' if p['use_rsi_rev'] else 'N'} "
        f"m120={'Y' if p['use_mom_120'] else 'N'}"
    )
    line = (
        f"{rank:>3d}  score={r['score']:.3f}  "
        f"D:Cal={r['d_calmar']:.2f} CAGR={r['d_cagr']*100:5.1f}% "
        f"MDD={r['d_mdd']*100:6.1f}% Sh={r['d_sharpe']:.2f}  "
    )
    if "i_calmar" in r:
        line += (
            f"I:Cal={r['i_calmar']:.2f} CAGR={r['i_cagr']*100:5.1f}% "
            f"MDD={r['i_mdd']*100:6.1f}% Sh={r['i_sharpe']:.2f}  "
        )
    line += (
        f"| k={p['k_long']:>2d} mom={p['mom_weight']:.1f} bias={p['bias_weight']:.1f} "
        f"hyst={p['hysteresis_mult']:.1f} sl={p['stop_loss_pct']*100:.0f}%/"
        f"{p['stop_loss_atr_mult']:.1f}x hold={p['max_hold_days']} "
        f"volT={p['vol_target_annual']:.2f} {factors}"
    )
    return line


def run_phase(trials: List[Tuple[str, dict, bool]], workers: int,
              ctx_path: str, label: str) -> List[dict]:
    print(f"\n===== {label}: {len(trials)} 组 | workers={workers} =====", flush=True)
    results: List[dict] = []
    t0 = time.time()
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_init_worker,
        initargs=(ctx_path,),
    ) as ex:
        futs = {ex.submit(eval_trial, t): t[0] for t in trials}
        done = 0
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                results.append(r)
                print(
                    f"  [{done}/{len(trials)}] {r['id']}: "
                    f"score={r['score']:.3f} D.Calmar={r['d_calmar']:.2f} "
                    f"({r['elapsed']:.0f}s)",
                    flush=True,
                )
            except Exception as e:
                print(f"  [{done}/{len(trials)}] {futs[fut]} FAILED: {e}", flush=True)
    print(f"  阶段完成 {time.time()-t0:.0f}s", flush=True)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def main():
    ap = argparse.ArgumentParser(description="Calmar 导向参数搜索")
    ap.add_argument("--n-trials", type=int, default=48, help="随机搜索组数")
    ap.add_argument("--top-k", type=int, default=10, help="Phase2 INTRADAY 验证前 K")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--daily-start", default=bt.DAILY_START)
    ap.add_argument("--intra-start", default=bt.INTRA_START)
    ap.add_argument("--end", default="2026-05-11", help="回测终点（默认对齐 README）")
    ap.add_argument("--skip-intra", action="store_true", help="跳过 Phase2")
    ap.add_argument("--out", default="optimize_calmar_results.csv")
    args = ap.parse_args()

    end = _parse_end(args.end)
    daily_start = date.fromisoformat(args.daily_start)
    intra_start = date.fromisoformat(args.intra_start)

    ctx = load_context(daily_start, intra_start, end, build_intra=not args.skip_intra)
    cache_dir = Path("data_cache")
    cache_dir.mkdir(exist_ok=True)
    ctx_path = str(cache_dir / "_optimize_ctx.pkl")
    print(f"[缓存] 写入 panel pickle → {ctx_path}", flush=True)
    with open(ctx_path, "wb") as f:
        pickle.dump(ctx, f, protocol=pickle.HIGHEST_PROTOCOL)

    rng = random.Random(args.seed)
    params_list: List[dict] = [baseline_params()]
    params_list.extend(focused_grid())
    while len(params_list) < 1 + len(focused_grid()) + args.n_trials:
        params_list.append(sample_params(rng))

    # 去重
    seen = set()
    uniq_params = []
    for p in params_list:
        key = tuple(sorted(p.items()))
        if key not in seen:
            seen.add(key)
            uniq_params.append(p)

    print(f"[搜索] 共 {len(uniq_params)} 组唯一参数（含 baseline + 聚焦网格 + 随机）", flush=True)

    phase1_trials = [(f"p1_{i:03d}", p, False, None) for i, p in enumerate(uniq_params)]
    # baseline 标记
    phase1_trials[0] = ("baseline", uniq_params[0], False, None)

    results1 = run_phase(phase1_trials, args.workers, ctx_path, "Phase1 DAILY Calmar")

    print("\n----- Phase1 Top 15 (按 DAILY Calmar) -----", flush=True)
    for i, r in enumerate(results1[:15], 1):
        print(_fmt_row(r, i), flush=True)

    results2: List[dict] = []
    if not args.skip_intra and ctx.get("intra_panel") is not None:
        top = results1[: args.top_k]
        # 始终带上 baseline
        ids = {r["id"] for r in top}
        if "baseline" not in ids:
            base = next(r for r in results1 if r["id"] == "baseline")
            top = [base] + top
        phase2_trials = []
        for r in top:
            daily_m = {
                "calmar": r["d_calmar"],
                "cagr": r["d_cagr"],
                "max_dd": r["d_mdd"],
                "sharpe": r["d_sharpe"],
                "n_trades": r["d_trades"],
            }
            phase2_trials.append((r["id"] + "_v2", r["params"], True, daily_m))
        results2 = run_phase(phase2_trials, args.workers, ctx_path, "Phase2 INTRADAY + min(Calmar)")

        print("\n----- Phase2 Top (按 min(D,I) Calmar) -----", flush=True)
        for i, r in enumerate(results2, 1):
            print(_fmt_row(r, i), flush=True)

        best = results2[0]
        print("\n===== 最佳参数（Calmar 双段）=====", flush=True)
    else:
        best = results1[0]
        print("\n===== 最佳参数（仅 DAILY Calmar）=====", flush=True)

    print(_fmt_row(best, 1), flush=True)
    print("\n参数 dict:", flush=True)
    for k, v in best["params"].items():
        print(f"  {k}: {v}", flush=True)

    # 写 CSV
    rows = results2 if results2 else results1
    flat = []
    for r in rows:
        row = {**{f"p_{k}": v for k, v in r["params"].items()},
               **{k: v for k, v in r.items() if k != "params"}}
        flat.append(row)
    pd.DataFrame(flat).to_csv(args.out, index=False)
    print(f"\n[输出] {args.out}", flush=True)

    # 清理大 pickle（可选保留便于续跑）
    # os.remove(ctx_path)


if __name__ == "__main__":
    main()
