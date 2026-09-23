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

import datetime
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import tkinter as tk
from tkinter import messagebox

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
    "alpha": 0.92,
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


def detect_clash_node():
    """读取 Clash 当前选中的节点名。
    返回 (节点名, 代理端口)；控制器未开启时节点名返回 None，端口来自本机端口探测。"""
    port = detect_proxy()
    port_num = ""
    if port:
        m = re.search(r":(\d+)$", port)
        if m:
            port_num = m.group(1)
    ctl = _parse_clash_controller()
    if not ctl:
        return None, port_num
    host, cport, secret = ctl
    proxies = _clash_api_json(host, cport, secret, "/proxies")
    if not isinstance(proxies, dict) or not isinstance(proxies.get("proxies"), dict):
        return None, port_num
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
    return cur, port_num


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
    })
    if token:
        req.add_header("Authorization", "Bearer " + token)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


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


def fetch_log_items(cfg, page_size=100, max_pages=50):
    """拉取 /api/log/token 的全部可见调用日志。
    实测该站接口忽略时间/分页参数，直接返回全部可见记录（最近约 4 天 900+ 条），
    因此拉全量后由调用方在本地按时间过滤。"""
    site = normalize_url(cfg.get("site_url", ""))
    key = (cfg.get("api_key") or "").strip()
    if not site or not key:
        return None
    items = []
    for page in range(1, max_pages + 1):
        url = ("{}/api/log/token?page={}&page_size={}".format(site, page, page_size))
        text = _try_get(url, cfg, key, timeout=15)
        obj = json.loads(text)
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, list) or not data:
            break
        items.extend(data)
        if len(data) != page_size:
            break  # 接口忽略分页返回全部，视为已取完
    return items or None


def fmt_tokens(n):
    """token 量格式化：1.2M / 196.4k / 330"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n >= 1e6:
        return "{:.2f}M".format(n / 1e6)
    if n >= 1e3:
        return "{:.1f}k".format(n / 1e3)
    return "{:.0f}".format(n)


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


# ---------------------------------------------------------------- 悬浮窗 UI

class UsageWidget:
    def __init__(self, cfg):
        self.cfg = cfg
        self.bg = "#17181d"
        self.fg = "#eceef1"
        self.dim = "#9aa0ab"
        self.green = "#4fc26a"
        self.red = "#e05d5d"
        self.amber = "#e2b04e"
        self.gray = "#5c6069"

        self.root = tk.Tk()
        self.root.title("AI 中转站用量")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", float(cfg.get("alpha", 0.92)))
        except Exception:
            pass
        self.width = int(cfg.get("width", 330))
        self.range_var = tk.StringVar(value=cfg.get("time_range", "today"))

        self._restore_position()
        self._build_ui()
        self._bind_all_children()
        self.startup_var = tk.BooleanVar(value=self._startup_exists())

        self.root.after(150, self.refresh_now)

    def _range_title(self):
        label = TIME_RANGE_LABELS.get(self.cfg.get("time_range", "today"), "")
        base = self.cfg.get("display_name", "AI 中转站")
        return "{} [{}]".format(base, label) if label else base

    # ---------- 界面 ----------
    def _build_ui(self):
        self.frame = tk.Frame(self.root, bg=self.bg,
                              highlightbackground="#41434d", highlightthickness=1)
        self.frame.pack(fill="both", expand=True)

        hdr = tk.Frame(self.frame, bg=self.bg)
        hdr.pack(fill="x", padx=12, pady=(8, 0))
        self.dot = tk.Label(hdr, text="●", bg=self.bg, fg=self.gray,
                            font=("Microsoft YaHei UI", 8))
        self.dot.pack(side="left")
        self.title_lbl = tk.Label(hdr, text=self._range_title(),
                                  bg=self.bg, fg=self.fg,
                                  font=("Microsoft YaHei UI", 10, "bold"))
        self.title_lbl.pack(side="left", padx=6)
        self.time_lbl = tk.Label(hdr, text="--:--:--", bg=self.bg, fg=self.dim,
                                 font=("Consolas", 9))
        self.time_lbl.pack(side="right")

        self.meta_lbl = tk.Label(self.frame, text="", bg=self.bg, fg="#6b7078",
                                 font=("Microsoft YaHei UI", 8), justify="left", anchor="w")
        self.meta_lbl.pack(fill="x", padx=12, pady=(0, 0))

        self.row_frame = tk.Frame(self.frame, bg=self.bg)
        self.row_frame.pack(fill="x", padx=12, pady=(4, 0))

        self.status_lbl = tk.Label(self.frame, text="", bg=self.bg, fg=self.red,
                                   font=("Microsoft YaHei UI", 9),
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
                    tk.Label(r, text=v, bg=self.bg, fg="#ffffff", anchor="e",
                             font=("Consolas", 11, "bold")).pack(side="right")
                else:
                    tk.Label(r, text=v, bg=self.bg, fg=self.dim, anchor="e",
                             font=("Consolas", 9)).pack(side="right", padx=(0, 6))
        self._resize()

    def _resize(self):
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry("{}x{}+{}+{}".format(self.width, h, self.pos[0], self.pos[1]))

    def _restore_position(self):
        x, y = self.root.winfo_screenwidth() - self.width - 30, 30
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            x, y = int(st.get("x", x)), int(st.get("y", y))
        except Exception:
            pass
        self.pos = (x, y)

    def _save_position(self):
        try:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"x": self.pos[0], "y": self.pos[1]}, f)
        except Exception:
            pass

    # ---------- 交互 ----------
    def _bind_all_children(self):
        def bind_rec(w):
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

    def _on_double(self, e):
        webbrowser.open(normalize_url(self.cfg.get("site_url", "")) +
                        (self.cfg.get("page_path") or "/"))

    def _on_menu(self, e):
        self.startup_var.set(self._startup_exists())
        m = tk.Menu(self.root, tearoff=0, bg="#1e1f26", fg="#e8e8ea",
                    activebackground="#2d2f3a", activeforeground="#ffffff",
                    font=("Microsoft YaHei UI", 9))
        m.add_command(label="立即刷新", command=self.refresh_now)
        m.add_command(label="重新加载配置", command=self._reload_config)
        m.add_command(label="打开后台网站", command=self._open_site)
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
        try:
            m.tk_popup(e.x_root, e.y_root)
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
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = os.path.join(os.path.dirname(sys.executable), "python.exe")
        script = os.path.join(BASE_DIR, "ai_usage_widget.py")
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
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        rows, err = fetch_usage(self.cfg)
        node, port = detect_clash_node()
        self.root.after(0, lambda: self._apply(rows, err, node, port))

    def _apply(self, rows, err, node=None, port=""):
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
        else:
            self.dot.config(fg=self.gray if err and UNCONFIGURED_FLAG in err else self.red)
            hint = ("右键菜单 → 编辑 config.json，填写 site_url 和 api_key 后点“立即刷新”"
                    if err and UNCONFIGURED_FLAG in err else err or "无数据")
            self.status_lbl.config(text="⚠ " + hint)
        interval = max(5, int(self.cfg.get("refresh_seconds", 30))) * 1000
        self.root.after(interval, self.refresh_now)

    def quit(self):
        self._save_position()
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
