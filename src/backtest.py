"""
HSI Momentum Breakout Backtest Engine
=====================================
1:1 翻譯自 Next.js TypeScript src/lib/backtest.ts (864 行)

Strategy: Donchian channel breakout + ADX filter + ATR-based exits
Entry:  Close > DonchianHigh(N) AND ADX(M) > threshold AND optional trend filter
Exit:   Take-profit at entry + ATR * profitMult
        Stop-loss at entry - ATR * stopMult
        Trailing stop: highest high since entry - ATR * trailMult
        Optional partial profit taking at first TP
"""
from __future__ import annotations
import math
import statistics
from dataclasses import dataclass, field, asdict
from typing import Optional
import json


# ============== Data Classes ==============

@dataclass
class OHLCBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BacktestParams:
    donchianPeriod: int = 5
    atrPeriod: int = 14
    adxPeriod: int = 14
    adxThreshold: float = 18.0
    atrStopMult: float = 2.0
    atrProfitMult: float = 0.6
    atrTrailMult: float = 2.5
    riskPerTrade: float = 0.15
    enableTrailing: bool = False
    useTrendFilter: bool = True
    trendPeriod: int = 50
    partialProfit: bool = True
    partialProfitRatio: float = 0.5
    allowReentry: bool = False
    startingCapital: float = 100000.0


DEFAULT_PARAMS = BacktestParams()  # 20,000 組合優化後最佳 (TRIPLE-PASS 達標: WR 85.7%, AR +10.57%, MDD 12.35%)


@dataclass
class Trade:
    entryDate: str
    entryPrice: float
    exitDate: str
    exitPrice: float
    exitReason: str  # "profit" | "stop" | "trail" | "end"
    shares: int
    pnl: float
    pnlPct: float
    returnPct: float
    holdingDays: int
    atrAtEntry: float


@dataclass
class EquityPoint:
    date: str
    equity: float
    drawdown: float  # fraction 0..1


@dataclass
class BacktestMetrics:
    startEquity: float
    endEquity: float
    totalReturn: float
    annualReturn: float
    maxDrawdown: float
    maxDrawdownDuration: int
    winRate: float
    totalTrades: int
    wins: int
    losses: int
    avgWin: float
    avgLoss: float
    profitFactor: float
    sharpe: float
    sortino: float
    expectancy: float
    avgHoldingDays: float
    longestWinStreak: int
    longestLossStreak: int
    calmar: float
    meetsWinRate: bool
    meetsAnnualReturn: bool
    meetsMaxDrawdown: bool
    overallPass: bool


@dataclass
class IndicatorPoint:
    date: str
    donchianHigh: Optional[float]
    donchianLow: Optional[float]
    atr: Optional[float]
    adx: Optional[float]


@dataclass
class BacktestResult:
    params: BacktestParams
    trades: list
    equity_curve: list
    metrics: BacktestMetrics
    bars: list
    indicator_series: list


# ============== Indicator Calculations ==============

def _true_range(bar: OHLCBar, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return bar.high - bar.low
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def _wilder_smooth(values: list, period: int) -> list:
    """Wilder's smoothing (RMA / SMMA) - 跟普通 EMA 不一樣"""
    out = [float('nan')] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def calculate_atr(bars: list, period: int) -> list:
    tr = [_true_range(bars[i], bars[i - 1].close if i > 0 else None) for i in range(len(bars))]
    return _wilder_smooth(tr, period)


def calculate_donchian(bars: list, period: int) -> tuple:
    n = len(bars)
    high = [None] * n
    low = [None] * n
    for i in range(period, n):
        hh = -math.inf
        ll = math.inf
        # [i-period, i-1] window, exclude current bar
        for j in range(i - period, i):
            if bars[j].high > hh:
                hh = bars[j].high
            if bars[j].low < ll:
                ll = bars[j].low
        high[i] = hh
        low[i] = ll
    return high, low


def calculate_adx(bars: list, period: int) -> list:
    """ADX - Wilder's method"""
    n = len(bars)
    adx = [float('nan')] * n
    if n < period * 2:
        return adx

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n

    for i in range(1, n):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr[i] = _true_range(bars[i], bars[i - 1].close)

    tr_w = _wilder_smooth(tr, period)
    plus_dm_w = _wilder_smooth(plus_dm, period)
    minus_dm_w = _wilder_smooth(minus_dm, period)

    plus_di = [float('nan')] * n
    minus_di = [float('nan')] * n
    dx = [float('nan')] * n

    for i in range(n):
        if not math.isnan(tr_w[i]) and tr_w[i] > 0:
            plus_di[i] = 100.0 * (plus_dm_w[i] / tr_w[i])
            minus_di[i] = 100.0 * (minus_dm_w[i] / tr_w[i])
            s = plus_di[i] + minus_di[i]
            if s > 0:
                dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / s

    # ADX = Wilder smoothing of DX
    dx_valid = [0.0 if math.isnan(v) else v for v in dx]
    first_valid = -1
    for i in range(n):
        if not math.isnan(plus_di[i]):
            first_valid = i
            break
    if first_valid < 0 or first_valid + period > n:
        return adx

    s = sum(dx_valid[first_valid:first_valid + period])
    adx[first_valid + period - 1] = s / period
    for i in range(first_valid + period, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx_valid[i]) / period
    return adx


def calculate_sma(bars: list, period: int) -> list:
    n = len(bars)
    sma = [float('nan')] * n
    if n < period:
        return sma
    s = sum(b.close for b in bars[:period])
    sma[period - 1] = s / period
    for i in range(period, n):
        s += bars[i].close - bars[i - period].close
        sma[i] = s / period
    return sma


# ============== Backtest Engine ==============

def run_backtest(bars: list, params: BacktestParams, bars_per_year: int = 252) -> BacktestResult:
    n = len(bars)
    atr = calculate_atr(bars, params.atrPeriod)
    donchian_high, donchian_low = calculate_donchian(bars, params.donchianPeriod)
    adx = calculate_adx(bars, params.adxPeriod)
    trend_ma = calculate_sma(bars, params.trendPeriod) if params.useTrendFilter else [float('nan')] * n

    indicator_series = []
    for i, b in enumerate(bars):
        indicator_series.append(IndicatorPoint(
            date=b.date,
            donchianHigh=donchian_high[i],
            donchianLow=donchian_low[i],
            atr=atr[i] if not math.isnan(atr[i]) else None,
            adx=adx[i] if not math.isnan(adx[i]) else None,
        ))

    trades = []
    equity_curve = []

    equity = params.startingCapital
    peak_equity = equity
    peak_idx = 0
    max_drawdown = 0.0
    max_drawdown_duration = 0

    position = None  # dict holding position state

    def _new_pos(bar, i, atr_val):
        return {
            'entryDate': bar.date,
            'entryPrice': bar.close,
            'shares': 0,
            'remainingShares': 0,
            'stop': 0.0,
            'target': 0.0,
            'trailStop': None,
            'highestSinceEntry': bar.high,
            'atrAtEntry': atr_val,
            'entryIndex': i,
            'partialFilled': False,
            'partialPnl': 0.0,
            'partialSharesSold': 0,
        }

    for i in range(n):
        bar = bars[i]

        # === Check exits ===
        if position:
            exit_price = None
            exit_reason = None

            use_trailing_now = params.enableTrailing or (params.partialProfit and position['partialFilled'])

            if use_trailing_now and bar.high > position['highestSinceEntry']:
                position['highestSinceEntry'] = bar.high
                new_trail = position['highestSinceEntry'] - atr[i] * params.atrTrailMult
                cur_trail = position['trailStop'] if position['trailStop'] is not None else -math.inf
                if new_trail > cur_trail:
                    position['trailStop'] = new_trail
                    if new_trail > position['stop']:
                        position['stop'] = new_trail

            effective_stop = max(position['stop'], position['trailStop'] or -math.inf) if use_trailing_now else position['stop']

            # Check partial profit take first
            if params.partialProfit and not position['partialFilled'] and bar.high >= position['target']:
                sell_shares = int(position['shares'] * params.partialProfitRatio)
                if 0 < sell_shares < position['remainingShares']:
                    position['partialPnl'] = (position['target'] - position['entryPrice']) * sell_shares
                    position['partialSharesSold'] = sell_shares
                    position['remainingShares'] = position['shares'] - sell_shares
                    position['partialFilled'] = True
                    position['stop'] = position['entryPrice']
                    position['trailStop'] = position['entryPrice']
                else:
                    exit_price = position['target']
                    exit_reason = "profit"

            # After partial handling, check if remaining should exit
            if position and position['partialFilled'] and exit_price is None:
                if bar.low <= effective_stop:
                    exit_price = effective_stop
                    cur_trail = position['trailStop'] or -math.inf
                    exit_reason = "trail" if (use_trailing_now and cur_trail >= position['stop']) else "stop"
            elif position and not position['partialFilled'] and exit_price is None:
                if bar.low <= effective_stop:
                    exit_price = effective_stop
                    cur_trail = position['trailStop'] or -math.inf
                    exit_reason = "trail" if (use_trailing_now and cur_trail >= position['stop']) else "stop"
                elif not params.enableTrailing and not params.partialProfit and bar.high >= position['target']:
                    exit_price = position['target']
                    exit_reason = "profit"

            if exit_price is not None and exit_reason is not None and position:
                remaining_shares = position['remainingShares']
                remaining_pnl = (exit_price - position['entryPrice']) * remaining_shares
                total_pnl = position['partialPnl'] + remaining_pnl
                total_shares = position['shares']
                blended_exit = ((position['partialSharesSold'] * position['target'] + remaining_shares * exit_price) / total_shares) if position['partialFilled'] else exit_price
                pnl_pct = total_pnl / equity if equity > 0 else 0
                return_pct = (blended_exit - position['entryPrice']) / position['entryPrice']

                trades.append(Trade(
                    entryDate=position['entryDate'],
                    entryPrice=position['entryPrice'],
                    exitDate=bar.date,
                    exitPrice=blended_exit,
                    exitReason=exit_reason,
                    shares=total_shares,
                    pnl=total_pnl,
                    pnlPct=pnl_pct,
                    returnPct=return_pct,
                    holdingDays=i - position['entryIndex'],
                    atrAtEntry=position['atrAtEntry'],
                ))
                position = None

        # === Check entry ===
        min_warmup = max(
            params.donchianPeriod,
            params.atrPeriod * 2,
            params.trendPeriod if params.useTrendFilter else 0,
        )
        if position is None and i >= min_warmup:
            dc_high = donchian_high[i]
            adx_val = adx[i]
            atr_val = atr[i]
            prev_close = bars[i - 1].close
            trend_val = trend_ma[i]
            trend_ok = (not params.useTrendFilter) or (not math.isnan(trend_val) and bar.close > trend_val)
            fresh_breakout = prev_close <= dc_high and bar.close > dc_high
            reentry = params.allowReentry and bar.close > dc_high and bar.close > prev_close
            if (trend_ok and dc_high is not None
                    and not math.isnan(atr_val) and atr_val > 0
                    and not math.isnan(adx_val) and adx_val >= params.adxThreshold
                    and (fresh_breakout or reentry)):
                entry_price = bar.close
                stop = entry_price - atr_val * params.atrStopMult
                target = entry_price + atr_val * params.atrProfitMult
                risk_per_share = entry_price - stop
                shares = int((equity * params.riskPerTrade) / risk_per_share)
                if shares > 0:
                    position = _new_pos(bar, i, atr_val)
                    position['shares'] = shares
                    position['remainingShares'] = shares
                    position['stop'] = stop
                    position['target'] = target
                    position['trailStop'] = entry_price - atr_val * params.atrTrailMult

        # Mark-to-market
        mtm_equity = equity
        if position:
            remaining_unreal = (bar.close - position['entryPrice']) * position['remainingShares']
            mtm_equity = equity + position['partialPnl'] + remaining_unreal

        if mtm_equity > peak_equity:
            peak_equity = mtm_equity
            peak_idx = i
        dd = (peak_equity - mtm_equity) / peak_equity if peak_equity > 0 else 0
        if dd > max_drawdown:
            max_drawdown = dd
        dur = i - peak_idx
        if dur > max_drawdown_duration:
            max_drawdown_duration = dur

        equity_curve.append(EquityPoint(date=bar.date, equity=mtm_equity, drawdown=dd))

    # Close any open position at last bar
    if position:
        last_bar = bars[n - 1]
        exit_price = last_bar.close
        remaining_pnl = (exit_price - position['entryPrice']) * position['remainingShares']
        total_pnl = position['partialPnl'] + remaining_pnl
        blended_exit = ((position['partialSharesSold'] * position['target'] + position['remainingShares'] * exit_price) / position['shares']) if position['partialFilled'] else exit_price
        return_pct = (blended_exit - position['entryPrice']) / position['entryPrice']
        trades.append(Trade(
            entryDate=position['entryDate'],
            entryPrice=position['entryPrice'],
            exitDate=last_bar.date,
            exitPrice=blended_exit,
            exitReason="end",
            shares=position['shares'],
            pnl=total_pnl,
            pnlPct=total_pnl / equity if equity > 0 else 0,
            returnPct=return_pct,
            holdingDays=n - 1 - position['entryIndex'],
            atrAtEntry=position['atrAtEntry'],
        ))
        position = None

    # === Clean equity curve (cumulative from closed trades) ===
    sorted_trades = sorted(trades, key=lambda t: t.exitDate)
    clean_equity = []
    cum_equity = params.startingCapital
    cum_peak = params.startingCapital
    cum_peak_idx = 0
    cum_max_dd = 0.0
    cum_max_dd_dur = 0
    trade_idx = 0

    for i in range(len(bars)):
        while trade_idx < len(sorted_trades) and sorted_trades[trade_idx].exitDate == bars[i].date:
            cum_equity += sorted_trades[trade_idx].pnl
            trade_idx += 1
        if cum_equity > cum_peak:
            cum_peak = cum_equity
            cum_peak_idx = i
        dd = (cum_peak - cum_equity) / cum_peak if cum_peak > 0 else 0
        if dd > cum_max_dd:
            cum_max_dd = dd
        dur = i - cum_peak_idx
        if dur > cum_max_dd_dur:
            cum_max_dd_dur = dur
        clean_equity.append(EquityPoint(date=bars[i].date, equity=cum_equity, drawdown=dd))

    # === Metrics ===
    start_equity = params.startingCapital
    end_equity = cum_equity
    total_return = (end_equity - start_equity) / start_equity
    years = len(bars) / bars_per_year
    annual_return = math.pow(end_equity / start_equity, 1 / years) - 1 if years > 0 and end_equity > 0 else 0

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t.pnlPct for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnlPct for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0)

    # Daily returns
    daily_returns = []
    for i in range(1, len(clean_equity)):
        prev = clean_equity[i - 1].equity
        curr = clean_equity[i].equity
        if prev > 0:
            daily_returns.append((curr - prev) / prev)
        else:
            daily_returns.append(0)

    if daily_returns:
        mean_ret = statistics.mean(daily_returns)
        std = statistics.pstdev(daily_returns) if len(daily_returns) > 1 else 0
        sharpe = (mean_ret / std) * math.sqrt(bars_per_year) if std > 0 else 0
        downside = [r for r in daily_returns if r < 0]
        if len(downside) > 1:
            downside_var = sum(r * r for r in downside) / len(downside)
        else:
            downside_var = 0
        downside_std = math.sqrt(downside_var)
        sortino = (mean_ret / downside_std) * math.sqrt(bars_per_year) if downside_std > 0 else 0
    else:
        sharpe = 0
        sortino = 0
        mean_ret = 0

    expectancy = sum(t.pnlPct for t in trades) / len(trades) if trades else 0
    avg_holding_days = sum(t.holdingDays for t in trades) / len(trades) if trades else 0

    # Streaks
    longest_win = 0
    longest_loss = 0
    cur_win = 0
    cur_loss = 0
    for t in trades:
        if t.pnl > 0:
            cur_win += 1
            cur_loss = 0
            longest_win = max(longest_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            longest_loss = max(longest_loss, cur_loss)

    calmar = annual_return / cum_max_dd if cum_max_dd > 0 else 0

    meets_wr = win_rate >= 0.80
    meets_ar = annual_return >= 0.10
    meets_mdd = cum_max_dd <= 0.15
    overall_pass = meets_wr and meets_ar and meets_mdd

    metrics = BacktestMetrics(
        startEquity=start_equity,
        endEquity=end_equity,
        totalReturn=total_return,
        annualReturn=annual_return,
        maxDrawdown=cum_max_dd,
        maxDrawdownDuration=cum_max_dd_dur,
        winRate=win_rate,
        totalTrades=len(trades),
        wins=len(wins),
        losses=len(losses),
        avgWin=avg_win,
        avgLoss=avg_loss,
        profitFactor=profit_factor if profit_factor != math.inf else 999.99,
        sharpe=sharpe,
        sortino=sortino,
        expectancy=expectancy,
        avgHoldingDays=avg_holding_days,
        longestWinStreak=longest_win,
        longestLossStreak=longest_loss,
        calmar=calmar,
        meetsWinRate=meets_wr,
        meetsAnnualReturn=meets_ar,
        meetsMaxDrawdown=meets_mdd,
        overallPass=overall_pass,
    )

    return BacktestResult(
        params=params,
        trades=trades,
        equity_curve=clean_equity,
        metrics=metrics,
        bars=bars,
        indicator_series=indicator_series,
    )


# ============== Scoring & Optimization ==============

def score_metrics(m: BacktestMetrics) -> float:
    """
    1:1 翻譯 TypeScript scoreMetrics()
    用戶首要目標:勝率 >= 80%
    次要目標:年化 >= 10%, MDD <= 15%
    """
    # Hard filter: 拒絕非正期望值 (慢流血)
    if m.expectancy <= 0.001:
        return -1000 + m.winRate * 10
    # 拒絕 PF < 1.0
    if m.profitFactor < 1.0:
        return -500 + m.winRate * 10

    score = 0.0
    # Triple pass mega bonus
    if m.overallPass:
        score += 5000

    # PRIMARY: WR >= 80%
    if m.meetsWinRate:
        score += 1000
        score += m.annualReturn * 8000
        score -= m.maxDrawdown * 50
        if m.meetsAnnualReturn:
            score += 2000
        if m.meetsMaxDrawdown:
            score += 300
    else:
        score += max(0, m.winRate - 0.5) * 200
        score += m.annualReturn * 400
        score -= m.maxDrawdown * 100
        if m.meetsAnnualReturn:
            score += 150
        if m.meetsMaxDrawdown:
            score += 100

    # PF bonus (capped)
    if m.profitFactor > 0:
        score += min(m.profitFactor * 10, 50)

    # Expectancy bonus
    score += m.expectancy * 3000

    # Trade count: < 20 trades 嚴重扣分
    if m.totalTrades < 20:
        score -= (20 - m.totalTrades) * 40

    return score


def optimize_parameters(bars: list, max_combinations: int = 20000, preserve: dict = None, bars_per_year: int = 252) -> dict:
    """1:1 翻譯 optimizeParameters()"""
    preserve = preserve or {}
    donchian_periods = [5, 8, 10, 15, 20, 25]
    adx_thresholds = [12, 15, 18, 20, 22, 25]
    atr_stop_mults = [2.0, 2.5, 3.0, 3.5, 4.0]
    atr_profit_mults = [0.6, 0.8, 1.0, 1.25, 1.5, 1.75]
    risk_per_trades = [0.05, 0.06, 0.08, 0.10, 0.12, 0.15]
    trend_periods = [10, 20, 30, 50]
    use_trend_options = [True, False]
    enable_trailing_options = [False, True]
    partial_profit_options = [False, True]
    allow_reentry_options = [False, True]

    combos = []
    outer_break = False
    for dp in donchian_periods:
        if outer_break: break
        for adx_t in adx_thresholds:
            if outer_break: break
            for stop_m in atr_stop_mults:
                if outer_break: break
                for prof_m in atr_profit_mults:
                    if prof_m >= stop_m:
                        continue
                    for rpt in risk_per_trades:
                        for use_trend in use_trend_options:
                            tp_list = trend_periods if use_trend else [50]
                            for tp in tp_list:
                                for trail in enable_trailing_options:
                                    for pp in partial_profit_options:
                                        for ar in allow_reentry_options:
                                            defaults = asdict(DEFAULT_PARAMS)
                                            defaults.update(preserve)
                                            defaults.update({
                                                'donchianPeriod': dp,
                                                'adxThreshold': adx_t,
                                                'atrStopMult': stop_m,
                                                'atrProfitMult': prof_m,
                                                'riskPerTrade': rpt,
                                                'useTrendFilter': use_trend,
                                                'trendPeriod': tp,
                                                'enableTrailing': trail,
                                                'partialProfit': pp,
                                                'allowReentry': ar,
                                            })
                                            p = BacktestParams(**defaults)
                                            combos.append(p)
                                            if len(combos) >= max_combinations:
                                                outer_break = True
                                                break
                                        if outer_break: break
                                    if outer_break: break
                                if outer_break: break
                            if outer_break: break
                        if outer_break: break
                    if outer_break: break
                if outer_break: break
            if outer_break: break
        if outer_break: break

    all_results = []
    for params in combos:
        result = run_backtest(bars, params, bars_per_year)
        score = score_metrics(result.metrics)
        all_results.append({'params': params, 'metrics': result.metrics, 'score': score})

    all_results.sort(key=lambda x: x['score'], reverse=True)
    best = all_results[0]
    return {
        'bestParams': best['params'],
        'bestMetrics': best['metrics'],
        'bestScore': best['score'],
        'allResults': all_results[:20],  # top 20
    }


# ============== Data Loading ==============

def load_bars(filepath: str) -> list:
    """Load HSI bars from JSON file"""
    with open(filepath, 'r') as f:
        data = json.load(f)
    return [OHLCBar(
        date=b['date'],
        open=b['open'],
        high=b['high'],
        low=b['low'],
        close=b['close'],
        volume=b.get('volume', 0),
    ) for b in data]
