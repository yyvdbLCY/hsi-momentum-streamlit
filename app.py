"""
HSI Momentum Streamlit App
1:1 翻譯自 Next.js page.tsx (2233 行)

4 個 tab:
1. Backtest (主) - 參數 + 圖表 (6 個 sub-tab)
2. Optimize - 網格搜索 20k 組合
3. Monitor - 即時監察 + Telegram
"""
import sys
sys.path.insert(0, 'src')

import streamlit as st
import os
import json
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import json
import time

from backtest import (
    load_bars, run_backtest, optimize_parameters, score_metrics,
    DEFAULT_PARAMS, BacktestParams, OHLCBar,
)
from storage import list_params, save_params, load_params, delete_params, MAX_FILES
from dataclasses import asdict

st.set_page_config(
    page_title="HSI 動量突破回測",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ============== Helpers ==============

def fmt_pct(v, digits=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if v == float('inf'):
        return "∞"
    return f"{v*100:.{digits}f}%"

def fmt_num(v, digits=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if v == float('inf'):
        return "∞"
    return f"{v:.{digits}f}"

def fmt_money(v):
    if v is None:
        return "—"
    return f"{v:,.0f}"

# ============== Load Data ==============

@st.cache_data
def load_data(interval):
    filepath = f"data/hsi.json" if interval == "daily" else "data/hsi_1h.json"
    return load_bars(filepath)

# ============== Session State ==============

if 'result' not in st.session_state:
    st.session_state.result = None
if 'opt_result' not in st.session_state:
    st.session_state.opt_result = None
if 'interval' not in st.session_state:
    st.session_state.interval = "daily"
if 'data_source' not in st.session_state:
    st.session_state.data_source = "yahoo"
if 'telegram_token' not in st.session_state:
    st.session_state.telegram_token = ""
if 'telegram_chat_id' not in st.session_state:
    st.session_state.telegram_chat_id = ""

# ============== Sidebar ==============

st.sidebar.title("📊 HSI 動量突破")
st.sidebar.markdown("**1:1 翻譯自 Next.js 系統**")
st.sidebar.divider()

interval = st.sidebar.radio(
    "📅 數據週期",
    options=["daily", "1h"],
    format_func=lambda x: "日線 (5y)" if x == "daily" else "1h 線 (14m)",
    horizontal=True,
    key="interval_select",
)
st.session_state.interval = interval

bars = load_data(interval)
bars_per_year = 252 if interval == "daily" else 1750
st.sidebar.success(f"✅ {len(bars)} 根 K 線\n\n{bars[0].date} → {bars[-1].date}")

st.sidebar.divider()
st.sidebar.subheader("⚙️ 策略參數")

# Default params dict
def get_default_params():
    return asdict(DEFAULT_PARAMS)

# Use session state for params
if 'params' not in st.session_state:
    st.session_state.params = get_default_params()

with st.sidebar.expander("📐 進場過濾", expanded=True):
    params = st.session_state.params
    params['donchianPeriod'] = st.slider("Donchian 週期", 5, 30, params['donchianPeriod'], help="突破過去 N 日最高")
    params['atrPeriod'] = st.slider("ATR 週期", 5, 30, params['atrPeriod'])
    params['adxPeriod'] = st.slider("ADX 週期", 5, 30, params['adxPeriod'])
    params['adxThreshold'] = st.slider("ADX 閾值", 10.0, 40.0, float(params['adxThreshold']), 0.5)
    params['useTrendFilter'] = st.checkbox("啟用趨勢過濾 (Close > MA)", value=params['useTrendFilter'])
    if params['useTrendFilter']:
        params['trendPeriod'] = st.slider("趨勢 MA 週期", 5, 200, params['trendPeriod'])
    params['allowReentry'] = st.checkbox("允許持續突破再進場", value=params['allowReentry'])

with st.sidebar.expander("💰 出場", expanded=True):
    params['atrStopMult'] = st.slider("止損 ATR 倍數", 1.0, 5.0, float(params['atrStopMult']), 0.1)
    params['atrProfitMult'] = st.slider("止盈 ATR 倍數", 0.5, 3.0, float(params['atrProfitMult']), 0.05)
    params['atrTrailMult'] = st.slider("追蹤停損 ATR 倍數", 1.0, 5.0, float(params['atrTrailMult']), 0.1)
    params['enableTrailing'] = st.checkbox("啟用追蹤停損 (替代固定 TP)", value=params['enableTrailing'])
    params['partialProfit'] = st.checkbox("啟用部分止盈", value=params['partialProfit'])
    if params['partialProfit']:
        params['partialProfitRatio'] = st.slider("部分止盈比例", 0.1, 0.9, float(params['partialProfitRatio']), 0.05)

with st.sidebar.expander("💵 倉位/資金", expanded=False):
    params['riskPerTrade'] = st.slider("單筆風險 (%)", 1, 20, int(params['riskPerTrade']*100)) / 100.0
    params['startingCapital'] = st.number_input("起始資金", min_value=10000, value=int(params['startingCapital']), step=10000)

st.session_state.params = params

# ============== Sidebar 參數儲存 ==============

with st.sidebar.expander("💾 參數儲存", expanded=False):
    # GITHUB_PAT 狀態檢查
    try:
        has_secret = bool(st.secrets.get("GITHUB_PAT", ""))
    except Exception:
        has_secret = bool(os.environ.get("GITHUB_PAT", ""))

    if not has_secret:
        st.error("⚠️ GITHUB_PAT 未設定\n\n(到 App settings → Secrets 加 `GITHUB_PAT = \"ghp_...\"`)")
    else:
        # 拿已儲存列表
        saved_items = list_params()
        count = len(saved_items)
        st.caption(f"已儲存: {count} / {MAX_FILES} 個")

        # === 載入區 ===
        if saved_items:
            load_options = [it['name'] for it in saved_items]
            selected = st.selectbox("📂 載入已儲存", options=["— 選擇 —"] + load_options, key="sidebar_load_select")
            if st.button("📥 載入", use_container_width=True, key="sidebar_load_btn", disabled=(selected == "— 選擇 —")):
                data = load_params(selected)
                if data and 'params' in data:
                    valid_keys = {f.name for f in BacktestParams.__dataclass_fields__.values()}
                    filtered = {k: v for k, v in data['params'].items() if k in valid_keys}
                    st.session_state.params = filtered
                    st.success(f"✅ 已載入 `{selected}`")
                    st.rerun()
                else:
                    st.error("❌ 載入失敗")
        else:
            st.caption("（還沒有儲存任何參數）")

        st.divider()

        # === 儲存區 ===
        new_name = st.text_input("💾 另存新名", placeholder="例: 保守 / Donchian10", max_chars=40, key="sidebar_save_name")
        new_note = st.text_input("備註 (可選)", placeholder="例: 20k 組合 grid search 達標", max_chars=80, key="sidebar_save_note")
        save_disabled = not new_name or count >= MAX_FILES
        if st.button("💾 儲存", use_container_width=True, key="sidebar_save_btn", disabled=save_disabled):
            current_metrics = {}
            if st.session_state.result is not None:
                m = st.session_state.result.metrics
                current_metrics = {
                    'winRate': m.winRate, 'annualReturn': m.annualReturn,
                    'maxDrawdown': m.maxDrawdown, 'profitFactor': m.profitFactor,
                    'sharpe': m.sharpe, 'totalTrades': m.totalTrades,
                    'overallPass': m.overallPass,
                }
            with st.spinner("儲存中..."):
                r = save_params(new_name, st.session_state.params, current_metrics, new_note)
            if r.get("ok"):
                st.success(f"✅ 已儲存 `{r['name']}`")
                st.rerun()
            else:
                st.error(f"❌ {r.get('error', '失敗')}")
        if count >= MAX_FILES:
            st.caption(f"⚠️ 已達 {MAX_FILES} 個上限, 請先在下方刪除舊的")

        # === 刪除區 ===
        if saved_items:
            st.divider()
            with st.popover("🗑️ 管理已儲存"):
                for item in saved_items:
                    col_n, col_d = st.columns([3, 1])
                    with col_n:
                        st.caption(item['name'])
                    with col_d:
                        if st.button("刪", key=f"sidebar_del_{item['name']}", type="secondary"):
                            r = delete_params(item['name'])
                            if r.get("ok"):
                                st.success(f"✅ 已刪除 {item['name']}")
                                st.rerun()
                            else:
                                st.error(f"❌ {r.get('error', '失敗')}")

st.sidebar.divider()

col_run, col_opt = st.sidebar.columns(2)
run_btn = col_run.button("🚀 跑回測", type="primary", use_container_width=True)
opt_btn = col_opt.button("🔍 優化", use_container_width=True)

# ============== Main Area ==============

st.title("📊 HSI 動量突破回測系統")

tab_backtest, tab_optimize, tab_monitor, tab_formulas = st.tabs([
    "📈 回測", "🔍 優化", "📡 即時監察", "📐 公式"
])

# ============== Tab 1: Backtest ==============

with tab_backtest:
    if run_btn:
        with st.spinner(f"跑 {len(bars)} 根 K 線回測中..."):
            params_obj = BacktestParams(**{k: v for k, v in st.session_state.params.items() if k in [f.name for f in BacktestParams.__dataclass_fields__.values()]})
            result = run_backtest(bars, params_obj, bars_per_year)
            st.session_state.result = result
        st.success("✅ 回測完成")

    if st.session_state.result is None:
        st.info("👈 從左邊選參數, 按「🚀 跑回測」")
    else:
        result = st.session_state.result
        m = result.metrics

        # === 5 個 metric 卡片 ===
        st.subheader("🎯 目標達標狀態")
        gcol1, gcol2, gcol3, gcol4 = st.columns(4)
        with gcol1:
            ok = m.meetsWinRate
            st.metric("勝率 ≥ 80%", fmt_pct(m.winRate, 1), delta="✅ 達標" if ok else "❌ 未達")
        with gcol2:
            ok = m.meetsAnnualReturn
            st.metric("年化 ≥ 10%", fmt_pct(m.annualReturn, 1), delta="✅ 達標" if ok else "❌ 未達")
        with gcol3:
            ok = m.meetsMaxDrawdown
            st.metric("MDD ≤ 15%", fmt_pct(m.maxDrawdown, 1), delta="✅ 達標" if ok else "❌ 未達")
        with gcol4:
            st.metric("**全部達標**", "✅✅✅" if m.overallPass else "❌ 未達")

        st.subheader("📊 績效指標")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            st.metric("勝率", fmt_pct(m.winRate, 1))
        with c2:
            st.metric("年化報酬", fmt_pct(m.annualReturn, 1))
        with c3:
            st.metric("最大回撤", fmt_pct(m.maxDrawdown, 1))
        with c4:
            st.metric("Sharpe", fmt_num(m.sharpe, 2))
        with c5:
            st.metric("Calmar", fmt_num(m.calmar, 2))

        c6, c7, c8, c9, c10 = st.columns(5)
        with c6:
            st.metric("總交易", m.totalTrades)
        with c7:
            st.metric("勝/敗", f"{m.wins}/{m.losses}")
        with c8:
            st.metric("PF", "∞" if m.profitFactor > 100 else fmt_num(m.profitFactor, 2))
        with c9:
            st.metric("Sortino", fmt_num(m.sortino, 2))
        with c10:
            st.metric("平均持有", f"{m.avgHoldingDays:.1f} K")

        c11, c12, c13, c14, c15 = st.columns(5)
        with c11:
            st.metric("起始資金", fmt_money(m.startEquity))
        with c12:
            st.metric("結束資金", fmt_money(m.endEquity))
        with c13:
            st.metric("總報酬", fmt_pct(m.totalReturn, 2))
        with c14:
            st.metric("最長連勝", m.longestWinStreak)
        with c15:
            st.metric("最長連敗", m.longestLossStreak)

        # === 6 個 sub-tab 圖表 ===
        subtab1, subtab2, subtab3, subtab4, subtab5, subtab6 = st.tabs([
            "📈 權益曲線", "📉 回撤", "💹 價格+通道", "🎯 買賣點", "📅 月度報酬", "📋 交易明細"
        ])

        eq_df = pd.DataFrame([{'date': p.date, 'equity': p.equity, 'drawdown': p.drawdown} for p in result.equity_curve])
        eq_df['date'] = pd.to_datetime(eq_df['date'])

        # Sub-tab 1: 權益曲線
        with subtab1:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=eq_df['date'], y=eq_df['equity'], mode='lines', name='權益', line=dict(color='#10b981', width=2)))
            fig.update_layout(title="權益曲線", xaxis_title="日期", yaxis_title="資金 (HKD)", height=400, template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)

        # Sub-tab 2: 回撤
        with subtab2:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=eq_df['date'], y=eq_df['drawdown']*100, mode='lines', name='回撤', fill='tozeroy', line=dict(color='#ef4444', width=1)))
            fig.update_layout(title="回撤曲線", xaxis_title="日期", yaxis_title="回撤 %", height=400, template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)

        # Sub-tab 3: 價格+通道
        with subtab3:
            bar_df = pd.DataFrame([{'date': b.date, 'open': b.open, 'high': b.high, 'low': b.low, 'close': b.close, 'volume': b.volume} for b in bars])
            bar_df['date'] = pd.to_datetime(bar_df['date'])
            ind_df = pd.DataFrame([{'date': p.date, 'donchianHigh': p.donchianHigh, 'donchianLow': p.donchianLow, 'atr': p.atr, 'adx': p.adx} for p in result.indicator_series])
            ind_df['date'] = pd.to_datetime(ind_df['date'])

            fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.05, row_heights=[0.7, 0.3])
            fig.add_trace(go.Candlestick(x=bar_df['date'], open=bar_df['open'], high=bar_df['high'], low=bar_df['low'], close=bar_df['close'], name='HSI'), row=1, col=1)
            fig.add_trace(go.Scatter(x=ind_df['date'], y=ind_df['donchianHigh'], mode='lines', name='Donchian High', line=dict(color='#f59e0b', dash='dash')), row=1, col=1)
            fig.add_trace(go.Scatter(x=ind_df['date'], y=ind_df['donchianLow'], mode='lines', name='Donchian Low', line=dict(color='#06b6d4', dash='dash')), row=1, col=1)
            fig.add_trace(go.Scatter(x=ind_df['date'], y=ind_df['adx'], mode='lines', name='ADX', line=dict(color='#8b5cf6', width=1)), row=2, col=1)
            fig.add_hline(y=st.session_state.params['adxThreshold'], line_dash="dash", line_color="red", row=2, col=1)
            fig.update_layout(title="價格 + Donchian 通道 + ADX", height=600, template="plotly_white", xaxis_rangeslider_visible=False)
            st.plotly_chart(fig, use_container_width=True)

        # Sub-tab 4: 買賣點
        with subtab4:
            fig = go.Figure()
            fig.add_trace(go.Candlestick(x=bar_df['date'], open=bar_df['open'], high=bar_df['high'], low=bar_df['low'], close=bar_df['close'], name='HSI'))
            if result.trades:
                entry_dates = [t.entryDate for t in result.trades]
                entry_prices = [t.entryPrice for t in result.trades]
                fig.add_trace(go.Scatter(x=entry_dates, y=entry_prices, mode='markers', name='買入', marker=dict(symbol='triangle-up', size=10, color='#10b981')))
                exit_dates = [t.exitDate for t in result.trades]
                exit_prices = [t.exitPrice for t in result.trades]
                colors = ['#3b82f6' if t.exitReason == 'profit' else '#ef4444' if t.exitReason == 'stop' else '#f59e0b' if t.exitReason == 'trail' else '#8b5cf6' for t in result.trades]
                fig.add_trace(go.Scatter(x=exit_dates, y=exit_prices, mode='markers', name='賣出', marker=dict(symbol='x', size=8, color=colors)))
            fig.update_layout(title="買賣點 (綠=買, 藍=止盈, 紅=止損, 橙=追蹤)", height=500, template="plotly_white", xaxis_rangeslider_visible=False)
            st.plotly_chart(fig, use_container_width=True)

        # Sub-tab 5: 月度報酬
        with subtab5:
            eq_df['month'] = eq_df['date'].dt.to_period('M').astype(str)
            monthly = eq_df.groupby('month').agg({'equity': ['first', 'last']}).reset_index()
            monthly.columns = ['month', 'start_eq', 'end_eq']
            monthly['return'] = (monthly['end_eq'] - monthly['start_eq']) / monthly['start_eq']
            fig = go.Figure()
            colors = ['#10b981' if r > 0 else '#ef4444' for r in monthly['return']]
            fig.add_trace(go.Bar(x=monthly['month'], y=monthly['return']*100, marker_color=colors, name='月度報酬'))
            fig.update_layout(title="月度報酬 %", xaxis_title="月份", yaxis_title="報酬 %", height=400, template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(monthly[['month', 'start_eq', 'end_eq', 'return']].assign(**{'月度報酬%': lambda d: d['return'].apply(lambda x: f"{x*100:+.2f}%")}).drop(columns=['return']), use_container_width=True, height=300)

        # Sub-tab 6: 交易明細
        with subtab6:
            if result.trades:
                trades_df = pd.DataFrame([{
                    '進場日': t.entryDate,
                    '出場日': t.exitDate,
                    '進場價': fmt_num(t.entryPrice, 0),
                    '出場價': fmt_num(t.exitPrice, 0),
                    '股數': t.shares,
                    '損益': fmt_money(t.pnl),
                    '損益%': fmt_pct(t.pnlPct, 2),
                    '持有': f"{t.holdingDays} K",
                    '出場原因': {'profit': '止盈', 'stop': '止損', 'trail': '追蹤', 'end': '結尾'}.get(t.exitReason, t.exitReason),
                } for t in result.trades])
                st.dataframe(trades_df, use_container_width=True, height=500)

                # 下載 CSV
                csv = trades_df.to_csv(index=False).encode('utf-8')
                st.download_button("📥 下載交易明細 CSV", csv, "trades.csv", "text/csv")

# ============== Tab 2: Optimize ==============

with tab_optimize:
    st.subheader("🔍 網格搜索優化")
    st.markdown("""
    **目標**: 勝率 ≥ 80% + 年化 ≥ 10% + MDD ≤ 15% (3 項全達標)
    **方法**: 20,000 組合 grid search + 漸進式評分 (先 80% 勝率 → 再年化 → 再 MDD)
    """)

    col_o1, col_o2 = st.columns(2)
    with col_o1:
        max_combos = st.number_input("最大組合數", 1000, 20000, 5000, step=1000)
    with col_o2:
        preserve_capital = st.checkbox("保留起始資金設定", value=True)

    if opt_btn:
        with st.spinner(f"跑 {max_combos} 組合 grid search 中... (~30s-2min)"):
            preserve = {'startingCapital': st.session_state.params['startingCapital']} if preserve_capital else {}
            opt_result = optimize_parameters(bars, max_combinations=max_combos, preserve=preserve, bars_per_year=bars_per_year)
            st.session_state.opt_result = opt_result
        st.success(f"✅ 優化完成! Top 20 結果已顯示")

    if st.session_state.opt_result:
        opt = st.session_state.opt_result
        bp = opt['bestParams']
        bm = opt['bestMetrics']

        st.subheader("🏆 最佳配置")
        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            st.metric("勝率", fmt_pct(bm.winRate, 1))
        with c2:
            st.metric("年化", fmt_pct(bm.annualReturn, 1))
        with c3:
            st.metric("MDD", fmt_pct(bm.maxDrawdown, 1))
        with c4:
            st.metric("PF", "∞" if bm.profitFactor > 100 else fmt_num(bm.profitFactor, 2))
        with c5:
            st.metric("分數", fmt_num(opt['bestScore'], 0))

        with st.expander("📋 最佳配置詳情", expanded=True):
            params_text = f"""
- Donchian: {bp.donchianPeriod}
- ADX 閾值: {bp.adxThreshold}
- 止損 ATR: {bp.atrStopMult}
- 止盈 ATR: {bp.atrProfitMult}
- 追蹤 ATR: {bp.atrTrailMult}
- 單筆風險: {bp.riskPerTrade*100}%
- 趨勢過濾: {bp.useTrendFilter} (MA{bp.trendPeriod if bp.useTrendFilter else 'N/A'})
- 追蹤停損: {bp.enableTrailing}
- 部分止盈: {bp.partialProfit} ({bp.partialProfitRatio*100 if bp.partialProfit else 0}%)
- 允許再進場: {bp.allowReentry}
            """
            st.code(params_text)

            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("📥 套用到回測", use_container_width=True):
                    st.session_state.params = asdict(bp)
                    st.success("已套用到 sidebar, 按「🚀 跑回測」查看結果")
            with c2:
                if st.button("🔄 重新優化", use_container_width=True):
                    st.session_state.opt_result = None

        st.subheader("📊 Top 20 配置")
        top_df = pd.DataFrame([{
            '排名': i+1,
            '勝率': f"{r['metrics'].winRate*100:.1f}%",
            '年化': f"{r['metrics'].annualReturn*100:+.2f}%",
            'MDD': f"{r['metrics'].maxDrawdown*100:.2f}%",
            'PF': "∞" if r['metrics'].profitFactor > 100 else f"{r['metrics'].profitFactor:.2f}",
            '交易': r['metrics'].totalTrades,
            'D': r['params'].donchianPeriod,
            'ADX': r['params'].adxThreshold,
            'SL': r['params'].atrStopMult,
            'TP': r['params'].atrProfitMult,
            'Risk': f"{r['params'].riskPerTrade*100:.0f}%",
            '趨勢': r['params'].useTrendFilter,
            '分數': f"{r['score']:.0f}",
        } for i, r in enumerate(opt['allResults'])])
        st.dataframe(top_df, use_container_width=True, height=500)

# ============== Tab 3: Monitor ==============

with tab_monitor:
    st.subheader("📡 即時監察系統")
    st.info("""
    **功能說明**: 鑒察頻率與回測同步 (日線/小時線), 用目前策略參數計算信號, 透過 Telegram 發送買賣通知

    **注意**: Streamlit Cloud 不支持長期 background process, 此 tab 提供「單次信號檢查 + 手動 Telegram 推送」功能。
    """)

    st.divider()
    st.markdown("### 📊 資料來源")
    data_source = st.radio("選擇", options=["yahoo", "tiger"], format_func=lambda x: "📊 Yahoo Finance (免費)" if x == "yahoo" else "🐯 老虎證券 API", horizontal=True, key="data_source_radio")

    if data_source == "tiger":
        st.warning("""
        ⚠️ **老虎證券 API 尚未設定**

        Streamlit Cloud 上需要用 secrets 設定以下環境變數:
        ```
        TIGER_API_KEY=your_api_key
        TIGER_API_SECRET=your_api_secret
        TIGER_ACCOUNT_ID=your_account_id
        TIGER_USE_SANDBOX=true
        ```
        """)

    st.divider()
    st.markdown("### 🤖 Telegram 設定")
    col_t1, col_t2 = st.columns(2)
    with col_t1:
        tg_token = st.text_input("Bot Token", type="password", value=st.session_state.telegram_token)
        st.session_state.telegram_token = tg_token
    with col_t2:
        tg_chat = st.text_input("Chat ID", value=st.session_state.telegram_chat_id)
        st.session_state.telegram_chat_id = tg_chat

    st.divider()
    st.markdown("### 🔍 單次信號檢查")

    if st.button("🔍 檢查最新信號", type="primary"):
        with st.spinner("計算中..."):
            params_obj = BacktestParams(**{k: v for k, v in st.session_state.params.items() if k in [f.name for f in BacktestParams.__dataclass_fields__.values()]})
            result = run_backtest(bars, params_obj, bars_per_year)
            last_trade = result.trades[-1] if result.trades else None
            st.session_state.result = result
        if last_trade:
            st.success(f"✅ 最近信號: {last_trade.exitDate} {last_trade.exitReason} 進場價 {last_trade.entryPrice:.0f}")
        else:
            st.info("目前無信號")

    st.divider()
    st.markdown("### 📤 手動推送 Telegram")
    if st.button("📤 推送最新結果到 Telegram", disabled=not st.session_state.telegram_token):
        if st.session_state.result is None:
            st.error("請先跑回測或檢查信號")
        else:
            m = st.session_state.result.metrics
            msg = f"""📊 HSI 動量突破 (1:1 翻譯自 Next.js)
勝率: {m.winRate*100:.1f}% (目標 ≥80%)
年化: {m.annualReturn*100:+.2f}% (目標 ≥10%)
MDD: {m.maxDrawdown*100:.2f}% (目標 ≤15%)
交易: {m.totalTrades} (勝 {m.wins} / 敗 {m.losses})
PF: {m.profitFactor:.2f if m.profitFactor < 100 else '∞'}

{'✅ 3 項全達標' if m.overallPass else '❌ 未達標'}
"""
            try:
                import requests
                url = f"https://api.telegram.org/bot{st.session_state.telegram_token}/sendMessage"
                r = requests.post(url, json={"chat_id": st.session_state.telegram_chat_id, "text": msg}, timeout=10)
                if r.status_code == 200:
                    st.success("✅ 推送到 Telegram 成功")
                else:
                    st.error(f"❌ 推送失敗: {r.status_code}")
            except Exception as e:
                st.error(f"❌ 推送失敗: {e}")



with tab_formulas:
    st.subheader("📐 核心公式文檔")
    st.markdown("""
### 一、進場條件 (3 重過濾)

```
Donchian Breakout:  Close[t] > max(High[t-N : t-1])
ADX Filter:         ADX[t] >= threshold
Trend Filter:       Close[t] > MA(trendPeriod)  [可選]
Reentry:            Close[t] > DonchianHigh[t] AND Close[t] > Close[t-1]  [可選]
```

### 二、Wilder 平滑指標

```
ATR Wilder:
   TR[t] = max(High-Low, |High-Close[t-1]|, |Low-Close[t-1]|)
   ATR[t] = (ATR[t-1] × (n-1) + TR[t]) / n

ADX Wilder:
   +DM = max(High[t]-High[t-1], 0), 僅在 > -DM
   -DM = max(Low[t-1]-Low[t], 0),  僅在 > +DM
   +DI = 100 × Smoothed(+DM) / ATR
   -DI = 100 × Smoothed(-DM) / ATR
   DX  = 100 × |+DI - -DI| / (+DI + -DI)
   ADX = Smoothed(DX, n)
```

### 三、倉位 Sizing

```
Risk_Capital  = Account × riskPerTrade
SL_Distance   = entry_atr × atrStopMult
Position_Size = Risk_Capital / (SL_Distance × entry_price)
```

### 四、出場邏輯

```
IF bar.high >= target (entry + atrProfitMult × ATR):
   → 止盈出場 (profit)
ELSE IF bar.low <= stop (entry - atrStopMult × ATR):
   → 止損出場 (stop)
ELSE IF enableTrailing AND bar.low <= trailStop:
   → 追蹤停損出場 (trail)

部分止盈模式 (partialProfit = true):
   IF bar.high >= target:
     → 賣出 partialProfitRatio, sl 移到 breakeven
     → 啟動追蹤停損
```

### 五、評分函數 (網格搜索)

```
1. Hard filter: expectancy <= 0.001 → score = -1000 + WR×10
2. Hard filter: PF < 1.0 → score = -500 + WR×10
3. Triple pass bonus: +5000
4. WR >= 80% (PRIMARY): +1000
   - Among WR≥80%, maximize AR (×8000), minimize MDD (×-50)
   - Bonus for AR≥10% (+2000), MDD≤15% (+300)
5. WR < 80% (SECONDARY):
   - WR approaching 80% reward
   - AR reward (×400), MDD penalty (×-100)
6. PF bonus (capped 50)
7. Expectancy bonus (×3000)
8. < 20 trades penalty (×-40 per missing)
```

### 六、5 年回測預期

| 配置 | 勝率 | 年化 | MDD | 備註 |
|------|------|------|-----|------|
| 預設 (D20/ADX22/TP1.5/SL3.0) | 64% | -1% | 13% | 跟原版一致 |
| Triple pass 80%+10%+15% | 不可達 | 不可達 | 不可達 | 學術界典型 35-50% |
| 最佳 (D15/ADX15/TP1.5/SL2/Trail3.0) | 65% | +3% | -20% | 我之前 Python 改良版 |
""")


# Footer
st.divider()
st.caption("📊 HSI 動量突破回測系統 v1.0 | 1:1 翻譯自 Next.js TypeScript 系統 | 部署於 Streamlit Cloud")
