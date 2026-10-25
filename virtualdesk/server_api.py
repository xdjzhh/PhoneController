# -*- coding: utf-8 -*-
"""server.py HTTP 接口的轻量客户端（仅标准库 urllib，供 toolbar 服务调用）。

toolbar 是独立常驻进程，可能在 server 启动之前就已运行（登录窗阶段），
因此所有接口都必须「连不上就返回失败」，绝不抛异常打断 UI。
server 地址来自 virtual_desktop_config.json 的 services 清单中
key=="open_autoglm" 条目的 host/port（config 唯一真相源，与
virtual_desktop.py 的 _get_open_autoglm_spec 同源），缺省 127.0.0.1:8080。
"""

import json
import os
import urllib.request
import urllib.error

# config 随服务包落在 virtualdesk/ 内（与 virtual_desktop.py 的 _CONFIG_FILE
# 锚定同一目录）。
_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "virtual_desktop_config.json")

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8080


def base_url(timeout: float = 0.4) -> str:
    """从 config services 清单的 open_autoglm 条目解析 server 基址；
    任何异常回退默认地址。"""
    host, port = _DEFAULT_HOST, _DEFAULT_PORT
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # services 清单里的 open_autoglm 条目优先；旧版顶层条目兼容回落
        oag = cfg.get("open_autoglm") or {}
        for spec in (cfg.get("services") or []):
            if isinstance(spec, dict) and spec.get("key") == "open_autoglm":
                oag = spec
                break
        host = str(oag.get("host") or host)
        port = int(oag.get("port") or port)
    except Exception:
        pass
    return f"http://{host}:{port}"


def _post(path: str, payload: dict, timeout: float = 1.5) -> tuple[bool, str]:
    url = f"{base_url()}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        if data.get("success", True):
            return True, raw
        return False, data.get("error", raw)
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", errors="replace"))
            return False, data.get("error", f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"server 未就绪（{e!r}）"


def is_up(timeout: float = 0.4) -> bool:
    """server 是否已起来（GET /status，连不上即 False）。"""
    url = f"{base_url()}/status"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


def set_edit_mode(editing: bool, app_package=None) -> tuple[bool, str]:
    """切换编辑模式（卡片红绿灯 + notif/thinking 面板联动，由 server 端编排）。"""
    payload = {"editing": bool(editing)}
    if app_package:
        payload["app_package"] = app_package
    return _post("/display/edit_mode", payload)
