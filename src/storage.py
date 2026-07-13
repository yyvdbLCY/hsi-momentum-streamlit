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


def test_token_permissions(token: str = "") -> dict:
    """
    測試 GITHUB_PAT 權限
    返回:
      - ok: bool
      - scopes: list of granted scopes
      - can_read_contents: bool
      - can_write_contents: bool
      - can_dispatch: bool
      - error: error message if any
      - help: 修復建議
    """
    if not token:
        token = _get_token()
    if not token:
        return {
            "ok": False,
            "error": "GITHUB_PAT 未設定",
            "help": "請在 Streamlit Cloud → App settings → Secrets 加:\n\nGITHUB_PAT = \"ghp_...\"",
        }

    result = {
        "ok": True,
        "scopes": [],
        "can_read_contents": False,
        "can_write_contents": False,
        "can_dispatch": False,
        "token_prefix": token[:7] + "..." if len(token) > 7 else "(太短)",
    }

    # 1. 看 token scope
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}", "User-Agent": "test"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            # 從 header 看 scope
            for h, v in resp.getheaders():
                if h.lower() == "x-oauth-scopes":
                    result["scopes"] = v.split(", ") if v else []
            # 拿 user 資料
            user = json.loads(resp.read().decode("utf-8"))
            result["login"] = user.get("login", "?")
            result["user_type"] = user.get("type", "?")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"Token 無效: HTTP {e.code} {e.reason}"}

    # 2. 測讀 Contents API
    code_read, _ = _api(f"/repos/{REPO}/contents/{PARAMS_DIR}", token=token)
    result["can_read_contents"] = (code_read == 200)

    # 3. 測寫 Contents API (創建測試文件, 然后刪)
    test_path = f"{PARAMS_DIR}/__test_perm.txt"
    test_content_b64 = base64.b64encode(b"test").decode("utf-8")
    code_write, resp_write = _api(
        f"/repos/{REPO}/contents/{test_path}",
        method="PUT",
        token=token,
        body={"message": "test permission", "content": test_content_b64, "branch": BRANCH},
    )
    result["can_write_contents"] = (code_write in (200, 201))
    if not result["can_write_contents"]:
        result["write_error"] = resp_write.get("message", "unknown")

    # 清理測試文件
    if code_write in (200, 201):
        sha = resp_write.get("content", {}).get("sha", "")
        if sha:
            _api(
                f"/repos/{REPO}/contents/{test_path}",
                method="DELETE",
                token=token,
                body={"message": "cleanup test", "sha": sha, "branch": BRANCH},
            )

    # 4. 測 dispatch workflow
    code_dispatch, _ = _api(
        f"/repos/{REPO}/dispatches",
        method="POST",
        token=token,
        body={"event_type": "perm-test", "client_payload": {}},
    )
    result["can_dispatch"] = (code_dispatch == 204)

    # 5. 修復建議
    if not result["can_write_contents"]:
        if "classic" in result.get("user_type", "").lower() or not any("workflow" in s for s in result["scopes"]):
            result["help"] = (
                "⚠️ Token 權限不足。建議:\n\n"
                "1. 去 https://github.com/settings/tokens?type=beta\n"
                "2. Generate **Fine-grained personal access token**\n"
                "3. Repository: 只選 yyvdbLCY/hsi-momentum-streamlit\n"
                "4. Permissions:\n"
                "   - Contents: **Read and write** ✅\n"
                "   - Actions: Read and write ✅\n"
                "5. 複製新 token 設到 Streamlit Cloud Secrets"
            )
        else:
            result["help"] = f"寫入失敗: {result.get('write_error', 'unknown')}"

    return result


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


def list_params(token: str = "", interval: str = "") -> list:
    """
    列出所有儲存的參數檔
    interval: "1h" / "daily" / "" (不限, 返回全部)
    返回: [{"name": "xxx", "path": "params/xxx.json", "size": 123, "interval": "1h" | "daily" | ""}]
    根據 interval 參數過濾: 只返回該 K 線類型或不限類型的檔案
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
            # 拿 file content 讀 interval 標籤
            file_interval = ""
            try:
                content_resp = _api(f"/repos/{REPO}/contents/{item['path']}", token=token)
                if content_resp[0] == 200 and "content" in content_resp[1]:
                    raw = base64.b64decode(content_resp[1]["content"])
                    meta = json.loads(raw)
                    file_interval = meta.get("interval", "")
            except Exception:
                pass

            # 過濾: file_interval 為空 (不限) 或跟查詢的 interval 匹配
            if interval and file_interval and file_interval != interval:
                continue

            results.append({
                "name": item["name"].replace(".json", ""),
                "path": item["path"],
                "size": item.get("size", 0),
                "sha": item.get("sha", ""),
                "interval": file_interval,
            })
    return results


def save_params(name: str, params: dict, metrics: dict = None, note: str = "", token: str = "", interval: str = "") -> dict:
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
        "interval": interval,  # "1h" | "daily" | ""
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


def trigger_telegram_workflow(message: str, chat_id: str = "", token: str = "") -> dict:
    """
    透過 GitHub repository_dispatch 觸發 telegram-notify workflow
    因為 sandbox 被牆無法直連 api.telegram.org
    """
    if not token:
        token = _get_token()
    if not token:
        return {"ok": False, "error": "GITHUB_PAT 未設定"}

    body = {
        "event_type": "telegram-notify",
        "client_payload": {
            "message": message,
            "chat_id": chat_id,
        }
    }
    code, resp = _api(
        f"/repos/{REPO}/dispatches",
        method="POST",
        token=token,
        body=body,
    )
    if code == 204:
        return {"ok": True, "msg": "Workflow 觸發成功, Telegram 推送在 5-10 秒後抵達"}
    return {"ok": False, "error": f"HTTP {code}: {resp.get('message', 'unknown')}"}
