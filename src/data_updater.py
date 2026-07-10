"""
數據更新模組
- 從 AkShare 拉 HSI 日 K (5 年)
- 從 yfinance 拉 HSI 1h K
- 用 GitHub Contents API 推送到 repo data/{file}.json
"""
import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional

REPO = "yyvdbLCY/hsi-momentum-streamlit"
BRANCH = "main"


def _get_token() -> str:
    try:
        import streamlit as st
        token = st.secrets.get("GITHUB_PAT", "")
        if token:
            return token
    except Exception:
        pass
    return os.environ.get("GITHUB_PAT", "")


def _api(path: str, method: str = "GET", token: str = "", body: Optional[dict] = None) -> tuple:
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hsi-momentum-streamlit",
    }
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            err_text = e.read().decode("utf-8")
            err = json.loads(err_text) if err_text else {}
        except Exception:
            err = {"message": str(e)}
        return e.code, err
    except Exception as e:
        return 0, {"message": str(e)}


def fetch_hsi_daily_akshare() -> list:
    """從 AkShare 拉 HSI 5 年日 K (推薦)"""
    try:
        import akshare as ak
    except ImportError:
        return []
    df = ak.stock_hk_index_daily_sina(symbol="HSI")
    bars = []
    for _, row in df.iterrows():
        bars.append({
            "date": str(row['date']),
            "open": float(row['open']),
            "high": float(row['high']),
            "low": float(row['low']),
            "close": float(row['close']),
            "volume": float(row['volume']) if 'volume' in row and not pd_isnan(row['volume']) else 0,
        })
    return bars


def pd_isnan(v):
    """檢查 nan, 不依賴 pandas"""
    try:
        return v != v
    except Exception:
        return False


def fetch_hsi_1h_yfinance(max_retries: int = 3) -> list:
    """從 yfinance 拉 HSI 1h K (60 天歷史限制) - 加重試避免 rate limit"""
    try:
        import yfinance as yf
    except ImportError:
        return []
    import time as _time

    all_bars = []
    last_error = None
    for attempt in range(max_retries):
        try:
            df = yf.download("^HSI", period="60d", interval="1h", progress=False)
            if df is not None and len(df) > 0:
                for idx, row in df.iterrows():
                    all_bars.append({
                        "date": idx.strftime("%Y-%m-%d %H:%M:%S"),
                        "open": float(row['Open'].values[0]) if hasattr(row['Open'], 'values') else float(row['Open']),
                        "high": float(row['High'].values[0]) if hasattr(row['High'], 'values') else float(row['High']),
                        "low": float(row['Low'].values[0]) if hasattr(row['Low'], 'values') else float(row['Low']),
                        "close": float(row['Close'].values[0]) if hasattr(row['Close'], 'values') else float(row['Close']),
                        "volume": float(row['Volume'].values[0]) if hasattr(row['Volume'], 'values') else float(row['Volume']),
                    })
                return all_bars
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                _time.sleep(20)  # 每次重試等 20 秒
    return []  # 重試完都失敗, 返回空


def push_to_repo(filename: str, data: list, commit_msg: str = "", token: str = "") -> dict:
    """把 data 推到 repo data/{filename}"""
    if not token:
        token = _get_token()
    if not token:
        return {"ok": False, "error": "GITHUB_PAT 未設定"}

    path = f"data/{filename}"
    content_b64 = base64.b64encode(json.dumps(data, ensure_ascii=False).encode("utf-8")).decode("utf-8")

    # 拿現有 SHA
    code, existing = _api(f"/repos/{REPO}/contents/{path}", token=token)
    sha = existing.get("sha") if code == 200 else None

    body = {
        "message": commit_msg or f"chore: 更新 {filename} ({len(data)} 筆)",
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    code, resp = _api(f"/repos/{REPO}/contents/{path}", method="PUT", token=token, body=body)
    if code in (200, 201):
        return {
            "ok": True,
            "path": path,
            "size": len(content_b64),
            "bars": len(data),
            "action": "update" if sha else "create",
        }
    return {"ok": False, "error": f"HTTP {code}: {resp.get('message', 'unknown')}"}


def update_hsi_daily(token: str = "") -> dict:
    """完整流程: AkShare 拉日 K → 推 GitHub → 返回結果"""
    bars = fetch_hsi_daily_akshare()
    if not bars:
        return {"ok": False, "error": "AkShare 拉數據失敗"}
    last_date = bars[-1]['date']
    msg = f"chore: 更新 HSI 日 K ({len(bars)} 筆, 最後更新: {last_date})"
    r = push_to_repo("hsi.json", bars, msg, token)
    r['last_date'] = last_date
    return r


def update_hsi_1h(token: str = "") -> dict:
    """完整流程: yfinance 拉 1h K → 推 GitHub → 返回結果"""
    bars = fetch_hsi_1h_yfinance()
    if not bars:
        return {"ok": False, "error": "yfinance 拉 1h 數據失敗 (rate limit?)"}
    last_date = bars[-1]['date']
    msg = f"chore: 更新 HSI 1h K ({len(bars)} 筆, 最後更新: {last_date})"
    r = push_to_repo("hsi_1h.json", bars, msg, token)
    r['last_date'] = last_date
    return r
