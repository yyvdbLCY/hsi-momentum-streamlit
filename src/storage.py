"""
策略參數儲存模組
用 GitHub Contents API 把參數存到 repo `params/` 目錄
最大 10 個檔案
"""
import os
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

REPO = "yyvdbLCY/hsi-momentum-streamlit"
BRANCH = "main"
PARAMS_DIR = "params"
MAX_FILES = 10


def _get_token() -> str:
    """從 st.secrets 或環境變量拿 GITHUB_PAT"""
    try:
        import streamlit as st
        token = st.secrets.get("GITHUB_PAT", "")
        if token:
            return token
    except Exception:
        pass
    return os.environ.get("GITHUB_PAT", "")


def _api(path: str, method: str = "GET", token: str = "", body: Optional[dict] = None) -> tuple:
    """呼叫 GitHub API, 返回 (status_code, response_dict)"""
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "hsi-momentum-streamlit",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
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


def list_params(token: str = "") -> list:
    """
    列出所有儲存的參數檔
    返回: [{"name": "xxx", "path": "params/xxx.json", "size": 123, ...}]
    """
    if not token:
        token = _get_token()
    if not token:
        return []

    code, data = _api(f"/repos/{REPO}/contents/{PARAMS_DIR}", token=token)
    if code == 404:
        return []  # 目錄還沒建立
    if code != 200:
        return []

    results = []
    for item in data:
        if item.get("type") == "file" and item.get("name", "").endswith(".json"):
            results.append({
                "name": item["name"].replace(".json", ""),
                "path": item["path"],
                "size": item.get("size", 0),
                "sha": item.get("sha", ""),
            })
    return results


def save_params(name: str, params: dict, metrics: dict = None, note: str = "", token: str = "") -> dict:
    """
    儲存參數到 params/{name}.json
    name: 檔案名(不含 .json)
    """
    if not token:
        token = _get_token()
    if not token:
        return {"ok": False, "error": "GITHUB_PAT 未設定"}

    # 清理檔名 (不允許特殊字元)
    clean_name = "".join(c for c in name if c.isalnum() or c in "-_").strip()
    if not clean_name:
        return {"ok": False, "error": "檔名無效"}
    if len(clean_name) > 40:
        return {"ok": False, "error": "檔名過長 (max 40 字元)"}

    # 檢查是否超過 10 個
    existing = list_params(token)
    if clean_name + ".json" not in [r["path"].split("/")[-1] for r in existing]:
        if len(existing) >= MAX_FILES:
            return {"ok": False, "error": f"已達上限 {MAX_FILES} 個檔案, 請先刪除舊的"}

    # 準備內容
    content = {
        "name": clean_name,
        "saved_at": datetime.utcnow().isoformat() + "Z",
        "params": params,
        "metrics": metrics or {},
        "note": note,
    }
    content_b64 = base64.b64encode(json.dumps(content, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")

    # 檢查是否已存在 (要拿 SHA 才能 update)
    path = f"{PARAMS_DIR}/{clean_name}.json"
    code, existing_data = _api(f"/repos/{REPO}/contents/{path}", token=token)
    sha = existing_data.get("sha") if code == 200 else None

    # PUT 創建或更新
    body = {
        "message": f"chore: 儲存策略參數 {clean_name}" + (" (update)" if sha else ""),
        "content": content_b64,
        "branch": BRANCH,
    }
    if sha:
        body["sha"] = sha

    # PUT 創建或更新 (注意: JSON 必須 UTF-8 才能寫中文 message / content, URL 要 quote)
    import urllib.request
    import urllib.parse
    body_json = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path, safe='/')}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "hsi-momentum-streamlit",
    }
    req = urllib.request.Request(url, data=body_json, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            code, resp = resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            err_text = e.read().decode("utf-8")
            err = json.loads(err_text) if err_text else {}
        except Exception:
            err = {"message": str(e)}
        code, resp = e.code, err
    if code in (200, 201):
        return {"ok": True, "name": clean_name, "path": path, "action": "update" if sha else "create"}
    return {"ok": False, "error": resp.get("message", f"HTTP {code}")}


def load_params(name: str, token: str = "") -> Optional[dict]:
    """從 params/{name}.json 載入參數"""
    if not token:
        token = _get_token()
    if not token:
        return None

    path = f"{PARAMS_DIR}/{name}.json"
    code, data = _api(f"/repos/{REPO}/contents/{path}", token=token)
    if code != 200:
        return None
    try:
        raw = base64.b64decode(data.get("content", ""))
        return json.loads(raw)
    except Exception:
        return None


def delete_params(name: str, token: str = "") -> dict:
    """刪除 params/{name}.json"""
    if not token:
        token = _get_token()
    if not token:
        return {"ok": False, "error": "GITHUB_PAT 未設定"}

    path = f"{PARAMS_DIR}/{name}.json"
    code, data = _api(f"/repos/{REPO}/contents/{path}", token=token)
    if code != 200:
        return {"ok": False, "error": f"找不到 {name}"}
    sha = data.get("sha", "")
    if not sha:
        return {"ok": False, "error": "找不到 SHA, 無法刪除"}

    body = {
        "message": f"chore: 刪除策略參數 {name}",
        "sha": sha,
        "branch": BRANCH,
    }
    # DELETE 也要 UTF-8
    import urllib.parse
    body_json = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = f"https://api.github.com/repos/{REPO}/contents/{urllib.parse.quote(path, safe='/')}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "hsi-momentum-streamlit",
    }
    req = urllib.request.Request(url, data=body_json, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8")
            code, resp = resp.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        try:
            err_text = e.read().decode("utf-8")
            err = json.loads(err_text) if err_text else {}
        except Exception:
            err = {"message": str(e)}
        code, resp = e.code, err
    if code == 200:
        return {"ok": True, "name": name}
    return {"ok": False, "error": resp.get("message", f"HTTP {code}")}
