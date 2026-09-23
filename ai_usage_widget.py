# -*- coding: utf-8 -*-
"""
AI 中转站用量桌面悬浮窗
=====================================================
定时拉取中转站 /api/log/token 调用日志，按模型汇总消耗金额，实时显示在桌面悬浮窗。

用法：
    1. 编辑同目录 config.json，填写 site_url（中转站面板网址）和 api_key（你的 API-Key）
    2. 双击 start_widget.vbs 启动（无黑窗口）
    3. 左键拖动窗口，右键菜单（立即刷新/时间筛选/打开后台/编辑配置/开机自启/退出），双击标题打开后台

时间筛选（config.json 的 time_range 字段，或右键菜单切换）：
    today(今天) / yesterday(昨天) / week(本周) / month(本月) / all(全部)

Clash 跟随：
    - 所有请求走 Clash 本地代理端口（默认自动探测 7897 等），Clash 里切换节点即自动生效；
    - 每次刷新会自动重新探测代理端口（端口变了也能跟上）；
    - 若在 Clash Verge 设置里启用了外部控制器，悬浮窗会显示当前节点名（如「节点 香港-01」）。

命令行可选：python ai_usage_widget.py <配置文件路径>   （用于测试，默认读取 config.json）
"""

import ctypes
import datetime
import gzip
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox

# 锁定时窗口鼠标穿透（Windows）
GWL_EXSTYLE = -20
GWLP_WNDPROC = -4
WS_EX_TRANSPARENT = 0x00000020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_shell32 = ctypes.windll.shell32
# 关键：64 位下这些函数返回/接收 64 位句柄，不声明类型会被截断导致托盘图标创建失败
_user32.CreateWindowExW.restype = ctypes.c_void_p
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_user32.LoadImageW.restype = ctypes.c_void_p
_user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
_shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.c_void_p]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_user32.CallWindowProcW.restype = ctypes.c_long
_user32.CallWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]
_user32.DefWindowProcW.restype = ctypes.c_long
_user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                   ctypes.c_void_p]
_user32.GetCursorPos.argtypes = [ctypes.c_void_p]
_user32.GetParent.restype = ctypes.c_void_p
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))   # exe 模式：config 放 exe 同目录
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "widget_state.json")
SINGLE_INSTANCE_PORT = 47831

DEFAULT_CONFIG = {
    "site_url": "https://你的中转站域名",
    "api_key": "你的API-Key",
    "refresh_seconds": 300,
    "mode": "auto",                    # auto / token_usage / self / page
    "display_name": "AI 中转站",
    "quota_per_unit": 500000.0,        # 额度换算：quota ÷ 该值 ≈ 金额（多数 one-api/new-api 站默认 50w）
    "currency": "¥",                   # 金额符号：¥ 或 $
    "show_converted": True,            # True 显示换算后金额，False 显示原始额度数值
    "proxy": "auto",                   # "" 直连；"auto" 自动探测 Clash 端口；或 "http://127.0.0.1:7897"
    "time_range": "today",             # 时间筛选：today / yesterday / week / month / all
    "page_path": "/",                  # 右键“打开后台”使用的路径
    "width": 330,
    "alpha": 0.1,            # 整窗不透明度 0~1（越小越透）；背景与文字共用一套透明度
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

MODE_API_PATHS = {
    "token_usage": "/api/usage/token",
    "self": "/api/user/self",
}

UNCONFIGURED_FLAG = "尚未配置"

# Clash/mihomo 常见代理端口（proxy: "auto" 时探测）
PROXY_PORTS = (7897, 7890, 7891, 7892, 8889)
_proxy_cache = {"t": 0.0, "p": ""}


def detect_proxy():
    """探测本机常见 Clash/mihomo 代理端口，返回 http 代理地址；未找到返回空串"""
    for port in PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            s.connect(("127.0.0.1", port))
            return "http://127.0.0.1:%d" % port
        except OSError:
            pass
        finally:
            s.close()
    return ""


def resolve_proxy(cfg, max_age=60):
    """解析 proxy 配置："" 直连；"auto" 自动探测（带缓存）；显式地址直接用"""
    p = (cfg.get("proxy") or "").strip()
    if not p:
        return ""
    if p.lower() != "auto":
        return p if p.startswith(("http://", "https://")) else "http://" + p
    if time.time() - _proxy_cache["t"] < max_age:
        return _proxy_cache["p"]
    _proxy_cache["t"] = time.time()
    _proxy_cache["p"] = detect_proxy()
    return _proxy_cache["p"]


# Clash Verge 运行时配置目录（用于读取外部控制器与当前节点）
CLASH_CFG_DIRS = [
    os.path.join(os.environ.get("APPDATA", ""), "io.github.clash-verge-rev.clash-verge-rev"),
    os.path.join(os.environ.get("APPDATA", ""), "clash-verge"),
    os.path.join(os.environ.get("APPDATA", ""), "io.github.clash-verge-rev.clash-verge"),
]


def _parse_clash_controller():
    """从 Clash Verge 运行时配置读取 (控制器地址, 端口, secret)；未启用控制器返回 None"""
    for d in CLASH_CFG_DIRS:
        for name in ("clash-verge.yaml", "config.yaml"):
            p = os.path.join(d, name)
            if not os.path.exists(p):
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    txt = f.read()
            except Exception:
                continue
            m = re.search(r"^\s*external-controller\s*:\s*['\"]?([^'\"\s#]+)", txt, re.M)
            if not m or not m.group(1).strip():
                continue
            addr = m.group(1).strip()
            host, _, port = addr.rpartition(":")
            if not port.isdigit():
                host, port = "127.0.0.1", 9097
            ms = re.search(r"^\s*secret\s*:\s*['\"]?([^'\"\s#]+)", txt, re.M)
            return (host or "127.0.0.1"), int(port), (ms.group(1).strip() if ms else None)
    return None


def _clash_api_json(host, port, secret, path, timeout=2.5):
    """调用 Clash 控制器 API，先试 Bearer secret 再试无认证"""
    for token in (secret, None):
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(
            "http://{}:{}{}".format(host, port, path), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and token:
                continue
            return None
        except Exception:
            return None
    return None


_node_cache = {"t": 0.0, "v": (None, "")}


def detect_clash_node(max_age=60):
    """读取 Clash 当前选中的节点名（带 60 秒缓存，避免每次刷新都探测）。
    返回 (节点名, 代理端口)；控制器未开启时节点名返回 None，端口来自本机端口探测。"""
    if time.time() - _node_cache["t"] < max_age:
        return _node_cache["v"]
    port = detect_proxy()
    port_num = ""
    if port:
        m = re.search(r":(\d+)$", port)
        if m:
            port_num = m.group(1)
    ctl = _parse_clash_controller()
    if not ctl:
        _node_cache["t"] = time.time()
        _node_cache["v"] = (None, port_num)
        return _node_cache["v"]
    host, cport, secret = ctl
    proxies = _clash_api_json(host, cport, secret, "/proxies")
    if not isinstance(proxies, dict) or not isinstance(proxies.get("proxies"), dict):
        _node_cache["t"] = time.time()
        _node_cache["v"] = (None, port_num)
        return _node_cache["v"]
    allp = proxies["proxies"]
    cur = "GLOBAL"
    for _ in range(8):
        g = allp.get(cur)
        if not isinstance(g, dict):
            break
        now = g.get("now")
        if not now or now == cur:
            break
        cur = now
    _node_cache["t"] = time.time()
    _node_cache["v"] = (cur, port_num)
    return _node_cache["v"]


# ---------------------------------------------------------------- 配置

def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for k, v in user_cfg.items():
                    cfg[k] = v
        except Exception as e:
            print("[config] 读取失败（将使用默认配置）:", e)
    else:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return cfg


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return url
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url.rstrip("/")


# ---------------------------------------------------------------- 抓取与解析

def http_get(url, token=None, timeout=12, proxy=""):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/html, */*",
        "Accept-Encoding": "gzip",   # 该站日志接口一次返回全部记录（~1.6MB），gzip 可压缩 96%
    })
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        data = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")


def _try_get(url, cfg, token=None, timeout=12):
    """按配置顺序尝试 代理→直连；全部失败时清缓存重新探测代理端口再试一次
    （Clash 端口/节点可能刚切换，重试能自动跟到新路径）"""
    proxy = resolve_proxy(cfg, max_age=20)
    order = [proxy, ""] if proxy else [""]
    last = None
    for p in order:
        try:
            return http_get(url, token, timeout, p)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise  # 限流：重试只会更糟
            last = e
        except Exception as e:
            last = e
    # 清缓存重新探测（端口可能已变），换到新路径再试一次
    try:
        _proxy_cache["t"] = 0.0
        p2 = resolve_proxy(cfg, max_age=20)
        if p2 and p2 != proxy:
            return http_get(url, token, timeout, p2)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise
        last = e
    except Exception as e:
        last = e
    raise last if last else RuntimeError("request failed")


def fmt_quota(raw, cfg):
    """把额度数值转成显示文本"""
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if cfg.get("show_converted", True):
        unit = float(cfg.get("quota_per_unit", 500000.0))
        return "{}{:,.2f}".format(cfg.get("currency", "¥"), num / unit)
    return "{:,.0f}".format(num)


def _unwrap(data):
    obj = data.get("data") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        obj = data if isinstance(data, dict) else None
    return obj


def parse_token_usage(text, cfg):
    """new-api /api/usage/token：total_available(剩余) total_used(已用) total_granted(总量)"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    obj = _unwrap(data)
    if not isinstance(obj, dict):
        return None
    rows = []
    if obj.get("unlimited_quota"):
        rows.append(("剩余额度", "∞ 无限额度"))
    elif "total_available_usd" in obj:
        rows.append(("剩余额度", "${:,.2f}".format(float(obj["total_available_usd"]))))
    elif "total_available" in obj:
        rows.append(("剩余额度", fmt_quota(obj["total_available"], cfg)))
    if "total_used_usd" in obj:
        rows.append(("已用额度", "${:,.2f}".format(float(obj["total_used_usd"]))))
    elif "total_used" in obj:
        rows.append(("已用额度", fmt_quota(obj["total_used"], cfg)))
    if obj.get("total_granted") is not None and not obj.get("unlimited_quota"):
        rows.append(("总额度", fmt_quota(obj["total_granted"], cfg)))
    if obj.get("expires_at") is not None:
        try:
            ts = float(obj["expires_at"])
            if ts <= 0:
                rows.append(("到期时间", "长期有效"))
            else:
                if ts > 1e12:
                    ts /= 1000.0
                rows.append(("到期时间", time.strftime("%Y-%m-%d", time.localtime(ts))))
        except Exception:
            pass
    return rows or None


def parse_self(text, cfg):
    """one-api/new-api /api/user/self：quota(剩余) used_quota(已用) request_count(请求次数)"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    obj = _unwrap(data)
    if not isinstance(obj, dict):
        return None
    rows = []
    if "quota" in obj:
        rows.append(("剩余额度", fmt_quota(obj["quota"], cfg)))
    if "used_quota" in obj:
        rows.append(("已用额度", fmt_quota(obj["used_quota"], cfg)))
    if "request_count" in obj:
        try:
            rows.append(("请求次数", "{:,}".format(int(obj["request_count"]))))
        except Exception:
            pass
    return rows or None


def parse_page(text, cfg):
    """兜底：从后台 HTML 里抓关键词附近的数字"""
    rows = []
    seen = set()
    keywords = ["剩余额度", "剩余余额", "账户余额", "可用额度", "已用额度",
                "余额", "已用", "quota", "balance"]
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text):
            seg = text[m.end(): m.end() + 80]
            seg = re.split(r"[\n\r<\"'{}]", seg)[0]
            nm = re.search(r"\d[\d,]*\.?\d*", seg)
            if not nm:
                continue
            val = nm.group(0).replace(",", "")
            if val in seen:
                break
            seen.add(val)
            cur = "¥" if re.search(r"[¥￥]", seg[:12]) else ("$" if "$" in seg[:12] else cfg.get("currency", ""))
            label = "剩余额度" if kw in ("quota", "balance") else kw
            rows.append((label, cur + val))
            break
        if len(rows) >= 4:
            break
    return rows[:4] or None


TIME_RANGE_LABELS = {
    "today": "今天",
    "yesterday": "昨天",
    "week": "本周",
    "month": "本月",
    "all": "全部",
}


def _range_ts(cfg):
    """根据 time_range 计算 (开始时间戳, 结束时间戳)，按东八区"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    tr = (cfg.get("time_range") or "today").strip()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if tr == "yesterday":
        start, end = today0 - datetime.timedelta(days=1), today0
    elif tr == "week":
        start, end = today0 - datetime.timedelta(days=now.weekday()), now
    elif tr == "month":
        start, end = today0.replace(day=1), now
    elif tr == "all":
        start, end = datetime.datetime(2020, 1, 1, tzinfo=tz), now
    else:  # today（默认）
        start, end = today0, now
    return int(start.timestamp()), int(end.timestamp())


def fetch_log_items(cfg, page_size=1000, max_pages=20):
    """拉取 /api/log/token 的全部可见调用日志。
    实测该站接口忽略时间参数、按 page_size 分页返回；page_size=1000 一次即可覆盖当前量级（900+ 条），
    接口若忽略 page_size 也会一次返回全部。拉全量后由调用方在本地按时间过滤。"""
    site = normalize_url(cfg.get("site_url", ""))
    key = (cfg.get("api_key") or "").strip()
    if not site or not key:
        return None
    items = []
    for page in range(1, max_pages + 1):
        url = ("{}/api/log/token?page={}&page_size={}".format(site, page, page_size))
        text = _try_get(url, cfg, key, timeout=20)
        obj = json.loads(text)
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) != page_size:
            break  # 已取完（不足一页或接口忽略分页返回全部）
    return items or None


def fmt_tokens(n):
    """token 量格式化：以万为单位（120万 / 19.6万 / 0.2万），不足一千显示原值"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return "{:.0f}".format(n)
    w = n / 10000.0
    if w >= 100:
        return "{:.0f}万".format(w)
    return "{:.1f}万".format(w)


def fetch_usage(cfg):
    """按时间范围拉取调用日志并按模型汇总，返回 (rows, error)
    rows 每项 = (模型名, 消耗金额, token总量)"""
    site = normalize_url(cfg.get("site_url", ""))
    key = (cfg.get("api_key") or "").strip()

    if not site or "你的" in site or not key or "你的" in key:
        return None, UNCONFIGURED_FLAG + "：请编辑 config.json，填写 site_url 和 api_key"

    try:
        start_ts, end_ts = _range_ts(cfg)
        items = fetch_log_items(cfg)
    except Exception as e:
        return None, "获取用量日志失败: {}".format(e)

    if items:
        # 接口不做时间过滤，这里在本地按筛选范围过滤
        items = [it for it in items
                 if start_ts <= int(it.get("created_at") or 0) <= end_ts]

    if not items:
        return [("提示", "该时段暂无调用")], None

    agg = {}  # 模型名 -> [quota, prompt_tokens, completion_tokens]
    for it in items:
        name = it.get("model_name") or "未知模型"
        e = agg.setdefault(name, [0.0, 0, 0])
        e[0] += float(it.get("quota") or 0)
        e[1] += int(it.get("prompt_tokens") or 0)
        e[2] += int(it.get("completion_tokens") or 0)

    unit = float(cfg.get("quota_per_unit", 500000.0))
    rows = []
    for name, (quota, pt, ct) in sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True):
        amt = quota / unit
        txt = "${:,.2f}".format(amt) if amt >= 1 else "${:,.4f}".format(amt)
        rows.append((name, txt, fmt_tokens(pt + ct)))
    return rows, None


# ---------------------------------------------------------------- 系统托盘（任务栏快捷显示）

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4
WM_APP_TRAY = 0x8000
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
_tray_icon_path = os.path.join(tempfile.gettempdir(), "ai_usage_tray.ico")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_uint,
                             ctypes.c_void_p, ctypes.c_void_p)


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeout", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _make_tray_icon():
    """生成托盘图标（蓝底白色 $ 符号）"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (32, 32), (30, 111, 235, 255))
        d = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
        except Exception:
            font = None
        d.text((16, 17), "$", fill=(255, 255, 255, 255), font=font, anchor="mm")
        img.save(_tray_icon_path, "ICO", sizes=[(16, 16), (32, 32)])
        return True
    except Exception:
        return False


class TrayIcon:
    """系统托盘常驻图标：悬停显示最新数据，左键呼出/收起悬浮窗，右键菜单，双击开后台
    宿主直接用 tkinter 主窗口（自建消息窗口在本机 CreateWindowExW 失败，故改为子类化主窗）"""

    def __init__(self, owner):
        self.owner = owner
        self.hwnd = None
        self.nid = None
        self._proc = None      # 新窗口过程（保持引用防 GC）
        self._old_proc = 0     # 原窗口过程（还原用）
        self._create()

    def _create(self):
        try:
            _make_tray_icon()
            if not os.path.exists(_tray_icon_path):
                return
            self.hwnd = self.owner._hwnd()
            if not self.hwnd:
                return
            self._proc = WNDPROC(self._wndproc)
            self._old_proc = _user32.SetWindowLongPtrW(
                self.hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, ctypes.c_void_p))
            if not self._old_proc:
                return
            icon = _user32.LoadImageW(None, _tray_icon_path, IMAGE_ICON,
                                      16, 16, LR_LOADFROMFILE)
            if not icon:
                return
            self.nid = NOTIFYICONDATAW()
            self.nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            self.nid.hWnd = self.hwnd
            self.nid.uID = 1
            self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self.nid.uCallbackMessage = WM_APP_TRAY
            self.nid.hIcon = icon
            self.nid.szTip = "AI 中转站用量"
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid)):
                self.nid = None
        except Exception:
            self.hwnd = None
            self.nid = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            if lparam == WM_LBUTTONUP:
                self.owner.root.after(0, self.owner._tray_click)
            elif lparam == WM_LBUTTONDBLCLK:
                self.owner.root.after(0, self.owner._open_site)
            elif lparam == WM_RBUTTONUP:
                self.owner.root.after(0, self.owner._tray_menu)
            return 0
        if self._old_proc:
            return _user32.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def update_tip(self, text):
        """更新悬停提示（最新用量数据）"""
        if self.nid is None:
            return
        try:
            self.nid.uFlags = NIF_TIP
            self.nid.szTip = text[:127]
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))
        except Exception:
            pass

    def destroy(self):
        if self.nid is not None:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            except Exception:
                pass
            self.nid = None
        if self._old_proc and self.hwnd:
            try:
                _user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_proc)
            except Exception:
                pass
            self._old_proc = 0


# ---------------------------------------------------------------- 悬浮窗 UI

class UsageWidget:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bg = "#17181d"
        self.fg = "#1e6feb"
        self.dim = "#1e6feb"
        self.green = "#4fc26a"
        self.red = "#e05d5d"
        self.amber = "#e2b04e"
        self.gray = "#5c6069"

        self.root = tk.Tk()
        self.root.title("AI 中转站用量")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # 整窗透明度（背景与文字共用一套，Windows 透明键会导致透明区点击穿透、无法拖动，故不用）
        try:
            self.root.attributes("-alpha", float(cfg.get("alpha", 0.1)))
        except Exception:
            pass
        self.width = int(cfg.get("width", 330))
        self.range_var = tk.StringVar(value=cfg.get("time_range", "today"))

        self._locked = False
        self._lock_popup = None
        self._hidden = False
        self._restore_position()
        self._build_ui()
        self.tray = TrayIcon(self)
        self._bind_all_children()
        self.startup_var = tk.BooleanVar(value=self._startup_exists())

        if self._locked:
            self._set_locked(True)

        self.root.after(150, self.refresh_now)

    def _range_title(self):
        label = TIME_RANGE_LABELS.get(self.cfg.get("time_range", "today"), "")
        base = self.cfg.get("display_name", "AI 中转站")
        return "{} [{}]".format(base, label) if label else base

    # ---------- 界面 ----------
    def _build_ui(self):
        self.frame = tk.Frame(self.root, bg=self.bg,
                              highlightbackground="#62697a", highlightthickness=2)
        self.frame.pack(fill="both", expand=True)

        hdr = tk.Frame(self.frame, bg=self.bg)
        hdr.pack(fill="x", padx=12, pady=(8, 0))
        self.dot = tk.Label(hdr, text="●", bg=self.bg, fg=self.gray,
                            font=("Microsoft YaHei UI", 9))
        self.dot.pack(side="left")
        self.title_lbl = tk.Label(hdr, text=self._range_title(),
                                  bg=self.bg, fg=self.fg,
                                  font=("Microsoft YaHei UI", 11))
        self.title_lbl.pack(side="left", padx=6)
        self.time_lbl = tk.Label(hdr, text="--:--:--", bg=self.bg, fg=self.dim,
                                 font=("Consolas", 10))
        self.lock_btn = tk.Label(hdr, text="🔓", bg=self.bg, fg=self.dim,
                                 font=("Segoe UI Emoji", 10), cursor="hand2")
        self.lock_btn.pack(side="right", padx=(0, 2))
        self.lock_btn.bind("<Button-1>", lambda e: self._toggle_lock())
        self.time_lbl.pack(side="right", padx=(0, 8))

        self.meta_lbl = tk.Label(self.frame, text="", bg=self.bg, fg="#1e6feb",
                                 font=("Microsoft YaHei UI", 9), justify="left", anchor="w")
        self.meta_lbl.pack(fill="x", padx=12, pady=(0, 0))

        self.row_frame = tk.Frame(self.frame, bg=self.bg)
        self.row_frame.pack(fill="x", padx=12, pady=(4, 0))

        self.status_lbl = tk.Label(self.frame, text="", bg=self.bg, fg=self.red,
                                   font=("Microsoft YaHei UI", 10),
                                   justify="left", anchor="w",
                                   wraplength=self.width - 24)
        self.status_lbl.pack(fill="x", padx=12, pady=(2, 6))

        self._set_rows([("提示", "正在初始化…")])

    def _set_rows(self, rows):
        for w in self.row_frame.winfo_children():
            w.destroy()
        for row in rows:
            label = row[0]
            values = row[1:]  # 1 或 2 个值：金额 / 金额+token
            r = tk.Frame(self.row_frame, bg=self.bg)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=self.bg, fg=self.dim, anchor="w",
                     font=("Microsoft YaHei UI", 10)).pack(side="left")
            # 右侧值从右往左排：最后一个是主值（白色粗体），前面的附值（token）灰色小字
            for i, v in enumerate(reversed(values)):
                if i == 0:
                    tk.Label(r, text=v, bg=self.bg, fg="#1e6feb", anchor="e",
                             font=("Consolas", 12)).pack(side="right")
                else:
                    tk.Label(r, text=v, bg=self.bg, fg=self.dim, anchor="e",
                             font=("Consolas", 10)).pack(side="right", padx=(0, 6))
        self._resize()

    def _resize(self):
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry("{}x{}+{}+{}".format(self.width, h, self.pos[0], self.pos[1]))
        self._sync_lock_popup()

    def _restore_position(self):
        x, y = self.root.winfo_screenwidth() - self.width - 30, 30
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            x, y = int(st.get("x", x)), int(st.get("y", y))
            self._locked = bool(st.get("locked", False))
        except Exception:
            pass
        self.pos = (x, y)

    def _save_position(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": self.pos[0], "y": self.pos[1],
                           "locked": bool(getattr(self, "_locked", False))}, f)
        except Exception:
            pass

    # ---------- 交互 ----------
    def _bind_all_children(self):
        def bind_rec(w):
            if w is self.lock_btn:
                return  # 锁按钮只响应自己的点击（切换锁定），不参与拖动/菜单
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Double-Button-1>", self._on_double)
            w.bind("<Button-3>", self._on_menu)
            for c in w.winfo_children():
                bind_rec(c)
        bind_rec(self.root)

    def _on_press(self, e):
        self._dx, self._dy = e.x_root - self.pos[0], e.y_root - self.pos[1]

    def _on_drag(self, e):
        x, y = e.x_root - self._dx, e.y_root - self._dy
        self.pos = (x, y)
        self.root.geometry("+{}+{}".format(x, y))
        self._sync_lock_popup()

    # ---------- 锁定（右上角锁：锁定时整窗鼠标穿透，只能点锁解锁） ----------
    def _hwnd(self):
        w = self.root.winfo_id()
        p = _user32.GetParent(w)
        return p if p else w

    def _set_click_through(self, on):
        try:
            hwnd = self._hwnd()
            ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                ex |= WS_EX_TRANSPARENT
            else:
                ex &= ~WS_EX_TRANSPARENT
            _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
            _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER |
                                 SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass

    def _toggle_lock(self):
        self._set_locked(not self._locked)

    def _set_locked(self, locked):
        self._locked = locked
        self._set_click_through(locked)
        if locked:
            self.lock_btn.config(text="🔒")
            self._ensure_lock_popup()
        else:
            self.lock_btn.config(text="🔓")
            if self._lock_popup is not None:
                try:
                    self._lock_popup.destroy()
                except Exception:
                    pass
                self._lock_popup = None
        self._save_position()

    def _ensure_lock_popup(self):
        """锁定后主窗穿透，需要一个小而可点的独立窗口承载锁图标，点击它解锁"""
        if self._lock_popup is not None:
            return
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=self.bg, highlightbackground="#62697a", highlightthickness=1)
        lbl = tk.Label(p, text="🔒", bg=self.bg, fg="#ffffff",
                       font=("Segoe UI Emoji", 10), cursor="hand2")
        lbl.pack(padx=2, pady=1)
        lbl.bind("<Button-1>", lambda e: self._toggle_lock())
        self._lock_popup = p
        self._sync_lock_popup()

    def _sync_lock_popup(self):
        if self._lock_popup is None:
            return
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            x = self.pos[0] + self.width - 30
            y = self.pos[1] + 6
            # 兜底：锁块永远留在屏幕可见范围内，即使主窗被拖出屏幕也能点锁解锁
            x = max(2, min(x, sw - 34))
            y = max(2, min(y, sh - 34))
            self._lock_popup.geometry("+{}+{}".format(x, y))
        except Exception:
            pass

    def _on_double(self, e):
        webbrowser.open(normalize_url(self.cfg.get("site_url", "")) +
                        (self.cfg.get("page_path") or "/"))

    def _on_menu(self, e):
        m = self._build_menu()
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _build_menu(self):
        self.startup_var.set(self._startup_exists())
        m = tk.Menu(self.root, tearoff=0, bg="#1e1f26", fg="#e8e8ea",
                    activebackground="#2d2f3a", activeforeground="#ffffff",
                    font=("Microsoft YaHei UI", 9))
        m.add_command(label="立即刷新", command=self.refresh_now)
        m.add_command(label="重新加载配置", command=self._reload_config)
        m.add_command(label="打开后台网站", command=self._open_site)
        m.add_separator()
        # 锁定/解锁兜底：即使锁块被拖出屏幕，托盘菜单也能解锁
        m.add_command(label="解锁窗口" if self._locked else "锁定窗口",
                      command=self._toggle_lock)
        m.add_separator()
        ranges = tk.Menu(m, tearoff=0, bg="#1e1f26", fg="#e8e8ea",
                         activebackground="#2d2f3a", activeforeground="#ffffff",
                         font=("Microsoft YaHei UI", 9))
        self.range_var.set(self.cfg.get("time_range", "today"))
        for key, label in TIME_RANGE_LABELS.items():
            ranges.add_radiobutton(label=label, value=key, variable=self.range_var,
                                   command=lambda k=key: self._set_range(k))
        m.add_cascade(label="时间筛选", menu=ranges)
        m.add_separator()
        m.add_command(label="编辑 config.json", command=self._edit_config)
        m.add_command(label="打开所在文件夹", command=self._open_folder)
        m.add_separator()
        m.add_checkbutton(label="开机自启", variable=self.startup_var,
                          command=self._toggle_startup)
        m.add_separator()
        m.add_command(label="退出", command=self.quit)
        return m

    # ---------- 托盘（任务栏）交互 ----------
    def _tray_click(self):
        """左键单击：呼出/收起悬浮窗"""
        self._hidden = not self._hidden
        if self._hidden:
            self.root.withdraw()
            if self._lock_popup is not None:
                try:
                    self._lock_popup.withdraw()
                except Exception:
                    pass
        else:
            try:
                self.root.deiconify()
            except Exception:
                pass
            if self._lock_popup is not None:
                try:
                    self._lock_popup.deiconify()
                    self._sync_lock_popup()
                except Exception:
                    pass

    def _tray_menu(self):
        pt = POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        m = self._build_menu()
        try:
            m.tk_popup(pt.x, pt.y)
        finally:
            m.grab_release()

    def _open_site(self):
        webbrowser.open(normalize_url(self.cfg.get("site_url", "")) +
                        (self.cfg.get("page_path") or "/"))

    def _reload_config(self):
        self.cfg = load_config()
        self.range_var.set(self.cfg.get("time_range", "today"))
        self.title_lbl.config(text=self._range_title())
        self.dot.config(fg=self.gray)
        self.time_lbl.config(text="--:--:--")
        self.meta_lbl.config(text="")
        self.status_lbl.config(text="")
        self._set_rows([("提示", "已重新加载配置")])
        self.refresh_now()

    def _set_range(self, key):
        """切换时间筛选并写回 config.json"""
        self.cfg["time_range"] = key
        self.title_lbl.config(text=self._range_title())
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                conf = json.load(f)
            conf["time_range"] = key
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(conf, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        self.refresh_now()

    def _edit_config(self):
        try:
            os.startfile(CONFIG_PATH)
        except Exception as e:
            messagebox.showerror("编辑配置", "无法打开 config.json：" + str(e))

    def _open_folder(self):
        try:
            os.startfile(BASE_DIR)
        except Exception:
            pass

    # ---------- 开机自启（生成/删除“启动”文件夹里的快捷方式） ----------
    def _startup_dir(self):
        return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                            "Start Menu", "Programs", "Startup")

    def _startup_path(self):
        return os.path.join(self._startup_dir(), "AI中转站用量悬浮窗.lnk")

    def _startup_exists(self):
        return os.path.exists(self._startup_path())

    def _toggle_startup(self):
        import subprocess
        lnk = self._startup_path()
        if self._startup_exists():
            try:
                os.remove(lnk)
                self.startup_var.set(False)
                return
            except Exception as e:
                messagebox.showerror("开机自启", "关闭失败：" + str(e))
                return
        pyw = sys.executable
        if not getattr(sys, "frozen", False):
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = os.path.join(os.path.dirname(sys.executable), "python.exe")
        script = os.path.join(BASE_DIR, "ai_usage_widget.py")
        if getattr(sys, "frozen", False):
            script = sys.executable
        ps = ("$sh = New-Object -ComObject WScript.Shell;"
              "$lnk = $sh.CreateShortcut('{0}');"
              "$lnk.TargetPath = '{1}';"
              "$lnk.Arguments = '\"{2}\"';"
              "$lnk.WindowStyle = 7;"
              "$lnk.Save()").format(lnk.replace("'", "''"), pyw.replace("'", "''"),
                                    script.replace("'", "''"))
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           check=True, capture_output=True)
            self.startup_var.set(True)
        except Exception as e:
            messagebox.showerror("开机自启", "设置失败：" + str(e))

    # ---------- 刷新 ----------
    def refresh_now(self):
        self.dot.config(fg=self.amber)
        self._fetching = True
        self._fetch_seq = getattr(self, "_fetch_seq", 0) + 1
        self._my_seq = self._fetch_seq
        threading.Thread(target=self._worker, daemon=True).start()
        # 超时守卫：45 秒还没完成就标红提示，避免一直黄
        self.root.after(45000, lambda: self._watchdog(self._my_seq))

    def _watchdog(self, seq):
        if getattr(self, "_fetching", False) and seq == getattr(self, "_fetch_seq", 0):
            self._fetching = False
            self.dot.config(fg=self.red)
            self.status_lbl.config(text="⚠ 刷新超时（节点可能慢或不通），等待下次自动刷新")

    def _worker(self):
        rows, err = fetch_usage(self.cfg)
        node, port = detect_clash_node()
        self.root.after(0, lambda: self._apply(rows, err, node, port))

    def _apply(self, rows, err, node=None, port=""):
        self._fetching = False
        self.time_lbl.config(text=time.strftime("%H:%M:%S"))
        meta = ""
        if port:
            meta = "代理端口 " + port
        if node:
            meta = ("节点 " + node + (" · " + meta if meta else ""))
        elif not port and not node:
            meta = "未检测到 Clash 代理"
        if meta:
            self.meta_lbl.config(text=meta)
        else:
            self.meta_lbl.config(text="")
        if rows:
            self.dot.config(fg=self.green)
            self.status_lbl.config(text="")
            self._set_rows(rows)
            self._update_tray(rows)
        else:
            self.dot.config(fg=self.gray if err and UNCONFIGURED_FLAG in err else self.red)
            hint = ("右键菜单 → 编辑 config.json，填写 site_url 和 api_key 后点“立即刷新”"
                    if err and UNCONFIGURED_FLAG in err else err or "无数据")
            self.status_lbl.config(text="⚠ " + hint)
        interval = max(5, int(self.cfg.get("refresh_seconds", 30))) * 1000
        self.root.after(interval, self.refresh_now)

    def _update_tray(self, rows):
        """把最新汇总写到托盘悬停提示（任务栏快捷查看）"""
        try:
            title = self._range_title()
            lines = [title, "更新时间 " + time.strftime("%H:%M:%S")]
            for r in rows[:5]:
                lines.append("  ".join(str(x) for x in r))
            self.tray.update_tip("\n".join(lines))
        except Exception:
            pass

    def quit(self):
        self._save_position()
        try:
            self.tray.destroy()
        except Exception:
            pass
        self.root.destroy()


# ---------------------------------------------------------------- 入口

def _acquire_singleton():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return None


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else CONFIG_PATH
    cfg = load_config(cfg_path)
    if not _acquire_singleton():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo("AI 中转站用量", "悬浮窗已经在运行了，看一下桌面角落：）")
        r.destroy()
        return
    w = UsageWidget(cfg)
    w.root.mainloop()


if __name__ == "__main__":
    main()
