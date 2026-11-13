# -*- coding: utf-8 -*-
"""
ProPilot 9.2 一键修复 · 方哥修复版~   —— 图形界面版

默认「纯直连」：改 DLL + 注册表后，ProPilot 直接请求你自己的上游，
零常驻进程、零开机自启，重开 Proteus 9 就能聊（只聊天）。
工具不必放到 ProPilot.dll 同目录：放桌面、微信下载目录、U 盘都行，
启动后自动搜索 DLL（卸载表 → 常见路径 → 全盘限时扫描），找不到可手选。

双击：图形界面（推荐）
命令行 / 无人值守：
  方哥修复版.exe --auto                       自动搜索并修复（纯直连）
  方哥修复版.exe --auto --no-reg              跳过注册表（沙箱自测）
  方哥修复版.exe --auto --dir D:/x/Bin        直接指定 DLL 目录
  方哥修复版.exe --find                       只列出找到的 DLL 目录
  方哥修复版.exe --probe --cfg X --dir Y      只读接口自检
  方哥修复版.exe --selftest-ui                界面自检
"""
import ctypes
import json
import os
import queue
import string
import sys
import threading
import time
import traceback

TITLE = "ProPilot 一键修复 · 方哥修复版~"


# --------------------------------------------------------------------------
#  windowed 打包时 sys.stdout / stderr 是 None，先兜住，避免任何 print 炸掉
# --------------------------------------------------------------------------
class _Null(object):
    def write(self, *a):
        pass

    def flush(self):
        pass

    def reconfigure(self, **kw):
        pass


class _Tee(object):
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")

    def write(self, s):
        try:
            self.f.write(s)
            self.f.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass

    def reconfigure(self, **kw):
        pass


_AUTO = any(a in sys.argv for a in ("--auto", "--find", "--probe", "--selftest-ui"))

if _AUTO:
    _here = os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    _tee = _Tee(os.path.join(_here, "ProPilotFix_auto.log"))
    sys.stdout = _tee
    sys.stderr = _tee
else:
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()

import ProPilotFix as P  # noqa: E402  核心逻辑（同目录 / 打进同一个 exe）

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, scrolledtext  # noqa: E402

LOG_Q = queue.Queue()
_orig_log = P.log


def _gui_log(msg=""):
    _orig_log(msg)
    LOG_Q.put(("log", str(msg)))


if not _AUTO:
    P.log = _gui_log


def _arg(name):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
    return ""


# ==========================================================================
#  自动定位 ProPilot.dll
#  顺序：工具目录 -> 注册表卸载项 -> 常见安装位置 -> 全盘限时扫描
# ==========================================================================
_SKIP = {
    "windows", "programdata", "$recycle.bin", "system volume information",
    "appdata", "node_modules", ".git", "__pycache__", "xwechat_files",
    "wechat files", "tencent", "microsoft", "installer", "temp", "tmp",
    "recovery", "perflogs", "msocache", "intel", "nvidia", "amd",
    "packages", "venv", ".venv", "site-packages", "cache", "crashdumps",
    "windowsapps", "winsxs", "servicing", "logs",
}


def _add_dir(d, hits, seen):
    try:
        d = os.path.abspath(d)
    except Exception:
        return
    k = d.lower()
    if k in seen:
        return
    seen.add(k)
    if os.path.isfile(os.path.join(d, "ProPilot.dll")):
        hits.append(d)


def _reg_install_dirs():
    """从卸载项里找 Labcenter / Proteus 的安装位置，比扫盘快得多也准得多。"""
    out = []
    w = P.winreg
    if not w:
        return out
    hives = (w.HKEY_LOCAL_MACHINE, w.HKEY_CURRENT_USER)
    paths = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for hive in hives:
        for path in paths:
            try:
                k = w.OpenKey(hive, path)
            except OSError:
                continue
            i = 0
            while True:
                try:
                    sub = w.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    with w.OpenKey(k, sub) as sk:
                        name = str(w.QueryValueEx(sk, "DisplayName")[0])
                        loc = ""
                        try:
                            loc = str(w.QueryValueEx(sk, "InstallLocation")[0])
                        except OSError:
                            pass
                        try:
                            ico = str(w.QueryValueEx(sk, "DisplayIcon")[0])
                        except OSError:
                            ico = ""
                    if "proteus" in name.lower() or "labcenter" in name.lower():
                        for cand in (loc, ico):
                            if not cand:
                                continue
                            p = cand.split(",")[0].strip().strip('"')
                            if os.path.isfile(p):
                                p = os.path.dirname(p)
                            if os.path.isdir(p):
                                out.append(p)
                except OSError:
                    pass
            try:
                w.CloseKey(k)
            except Exception:
                pass
    return out


def _common_dirs():
    out = []
    for letter in string.ascii_uppercase:
        dr = letter + ":\\"
        if not os.path.isdir(dr):
            continue
        for rel in (r"Program Files\Labcenter Electronics",
                    r"Program Files (x86)\Labcenter Electronics",
                    r"Program Files\Proteus 9 Professional",
                    "Protens9", "Proteus 9 Professional", "Proteus",
                    "Labcenter Electronics"):
            out.append(os.path.join(dr, rel))
    return out


def find_dll_dirs(timeout=20, progress=None):
    hits, seen = [], set()
    deadline = time.time() + timeout

    def say(s):
        if progress:
            progress(s)

    _add_dir(P.base_dir(), hits, seen)

    for b in _reg_install_dirs() + _common_dirs():
        for sub in ("", "Bin", "BIN", "bin", "Bin64"):
            _add_dir(os.path.join(b, sub) if sub else b, hits, seen)
        try:
            for n in os.listdir(b):
                p = os.path.join(b, n)
                if os.path.isdir(p):
                    _add_dir(os.path.join(p, "Bin"), hits, seen)
                    _add_dir(p, hits, seen)
        except Exception:
            pass
    say("注册表 + 常见安装位置：找到 %d 个" % len(hits))
    if hits:
        return hits

    # 注册表没装信息才扫盘；限时 + 剪枝，避免卡死
    for letter in string.ascii_uppercase:
        root = letter + ":\\"
        if not os.path.isdir(root):
            continue
        say("正在扫描 %s 盘…" % letter)
        try:
            for cur, dirs, files in os.walk(root, topdown=True):
                if time.time() > deadline:
                    say("扫描到时间上限，已找到 %d 个" % len(hits))
                    return hits
                depth = cur[len(root):].count(os.sep)
                dirs[:] = [x for x in dirs
                           if x.lower() not in _SKIP
                           and not x.startswith("$") and depth < 7]
                if "ProPilot.dll" in files:
                    _add_dir(cur, hits, seen)
                    if len(hits) >= 6:
                        return hits
        except Exception:
            pass
    return hits


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ==========================================================================
#  界面
# ==========================================================================
BG = "#eef2f8"
CARD = "#ffffff"
TEXT = "#1f2937"
MUTED = "#64748b"
ACCENT = "#1f6feb"
ACCENT_D = "#1557c0"
OK = "#15803d"
WARN = "#b45309"
ERR = "#c62828"
FONT = ("Microsoft YaHei UI", 10)
FONT_SM = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_BIG = ("Microsoft YaHei UI", 15, "bold")


def _btn(parent, text, cmd, primary=False, width=None):
    kw = {}
    if width:
        kw["width"] = width
    return tk.Button(
        parent, text=text, command=cmd, font=FONT_B if primary else FONT,
        bg=ACCENT if primary else "#e6edf9",
        fg="#ffffff" if primary else TEXT,
        activebackground=ACCENT_D if primary else "#d6e2f5",
        activeforeground="#ffffff" if primary else TEXT,
        relief="flat", bd=0, padx=14, pady=8 if primary else 5,
        cursor="hand2", **kw)


class App(object):
    def __init__(self, root):
        self.root = root
        self.bridge_started = False
        self.busy = False
        root.title(TITLE)
        root.geometry("880x740")
        root.minsize(780, 640)
        root.configure(bg=BG)

        self.dir_var = tk.StringVar(value="")
        self.find_var = tk.StringVar(value="正在自动搜索 ProPilot.dll…")
        self.status_var = tk.StringVar(value="就绪 · 建议先完全退出 Proteus 9")
        self.up_var = tk.StringVar(value=P.DEFAULT_CFG["upstream"])
        self.key_var = tk.StringVar(value=P.DEFAULT_CFG["api_key"])
        self.model_var = tk.StringVar(value=P.DEFAULT_CFG["model"])
        self.port_var = tk.StringVar(value=str(P.DEFAULT_CFG["bridge_port"]))
        self.pid_var = tk.StringVar(value=str(P.DEFAULT_CFG["provider_id"]))
        self.extra_var = tk.StringVar(value=str(P.DEFAULT_CFG["extra_models"]))
        self.force_var = tk.BooleanVar(value=bool(P.DEFAULT_CFG["force_model"]))
        self.trust_var = tk.BooleanVar(value=bool(P.DEFAULT_CFG["trust_client_key"]))
        self.effective_var = tk.StringVar(value="")
        self.cfg_tip_var = tk.StringVar(value="")

        self._build_header()
        body = tk.Frame(root, bg=BG, padx=14, pady=10)
        body.pack(fill="both", expand=True)

        self._build_find(body)
        self._build_cfg(body)
        self._build_actions(body)
        self._build_log(body)

        foot = tk.Frame(root, bg="#dbe4f0", height=28)
        foot.pack(fill="x", side="bottom")
        tk.Label(foot, textvariable=self.status_var, bg="#dbe4f0", fg=TEXT,
                 font=FONT_SM, anchor="w", padx=12).pack(fill="x", pady=5)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._pump)
        threading.Thread(target=self._auto_find, daemon=True).start()

    # ---- 版面 ------------------------------------------------------------
    def _build_header(self):
        h = tk.Frame(self.root, bg=ACCENT, height=78)
        h.pack(fill="x")
        h.pack_propagate(False)
        left = tk.Frame(h, bg=ACCENT)
        left.pack(side="left", padx=18, pady=10)
        tk.Label(left, text="ProPilot 一键修复", bg=ACCENT, fg="#ffffff",
                 font=FONT_BIG, anchor="w").pack(anchor="w")
        tk.Label(left, text="方哥修复版~   ·   放在任意文件夹都能自动找到 DLL",
                 bg=ACCENT, fg="#d6e6ff", font=FONT_SM, anchor="w").pack(anchor="w", pady=(2, 0))
        right = tk.Frame(h, bg=ACCENT)
        right.pack(side="right", padx=16)
        if _is_admin():
            tk.Label(right, text="● 管理员权限", bg=ACCENT, fg="#d1fae5",
                     font=FONT_B).pack(anchor="e")
        else:
            tk.Label(right, text="● 非管理员（写 DLL 可能失败）", bg=ACCENT,
                     fg="#ffe4e6", font=FONT_B).pack(anchor="e")
            tk.Button(right, text="点此以管理员重启", command=self.elevate,
                      bg="#ffffff", fg=ACCENT, font=FONT_SM, relief="flat",
                      padx=10, pady=3, cursor="hand2").pack(anchor="e", pady=(4, 0))

    def _card(self, parent, text):
        f = tk.LabelFrame(parent, text=" " + text + " ", bg=CARD, fg=ACCENT,
                          font=FONT_B, padx=12, pady=9, bd=1,
                          relief="solid", labelanchor="nw")
        f.pack(fill="x", pady=(0, 9))
        return f

    def _build_find(self, parent):
        c = self._card(parent, "① 找到 ProPilot.dll（自动搜索，也可手动选）")
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text="目录", bg=CARD, fg=TEXT, font=FONT, width=6,
                 anchor="w").pack(side="left")
        tk.Entry(row, textvariable=self.dir_var, font=FONT, relief="solid",
                 bd=1, bg="#f8fafc").pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))
        _btn(row, "浏览…", self.browse).pack(side="left", padx=(0, 6))
        _btn(row, "重新搜索", lambda: threading.Thread(
            target=self._auto_find, daemon=True).start()).pack(side="left")

        row2 = tk.Frame(c, bg=CARD)
        row2.pack(fill="x", pady=(8, 0))
        tk.Label(row2, textvariable=self.find_var, bg=CARD, fg=MUTED,
                 font=FONT_SM, anchor="w").pack(side="left")
        self.cb = None
        self.cb_var = tk.StringVar()
        self.cb = tk.OptionMenu(row2, self.cb_var, "")
        self.cb.configure(font=FONT_SM, bg="#f8fafc", relief="solid", bd=1,
                          anchor="w", width=46)
        self.cb.pack(side="right", padx=(8, 0))
        self.cb_var.trace("w", self._on_pick)

    def _build_cfg(self, parent):
        c = self._card(parent, "② 接口配置（用别人的中转站：URL / 密钥 / 模型 三项都要改成他自己的）")
        grid = tk.Frame(c, bg=CARD)
        grid.pack(fill="x")
        items = [
            ("上游地址", self.up_var, 52),
            ("API 密钥", self.key_var, 52),
            ("模型名", self.model_var, 30),
            ("代理端口", self.port_var, 10),
        ]
        self.cfg_entries = []
        for i, (lab, var, w) in enumerate(items):
            r, col = divmod(i, 2)
            cell = tk.Frame(grid, bg=CARD)
            cell.grid(row=r, column=col, sticky="w", padx=(0, 18), pady=3)
            tk.Label(cell, text=lab, bg=CARD, fg=TEXT, font=FONT,
                     width=9, anchor="w").pack(side="left")
            e = tk.Entry(cell, textvariable=var, font=FONT, width=w,
                         relief="solid", bd=1, bg="#f8fafc")
            e.pack(side="left", ipady=4)
            self.cfg_entries.append(e)

        opt = tk.Frame(c, bg=CARD)
        opt.pack(fill="x", pady=(7, 0))
        tk.Checkbutton(opt, text="强制纠正模型", variable=self.force_var, bg=CARD,
                       fg=TEXT, font=FONT_SM, activebackground=CARD,
                       selectcolor="#ffffff").pack(side="left")
        tk.Checkbutton(opt, text="透传客户端密钥(一般别勾)", variable=self.trust_var,
                       bg=CARD, fg=TEXT, font=FONT_SM, activebackground=CARD,
                       selectcolor="#ffffff").pack(side="left", padx=(14, 0))
        tk.Label(opt, text="额外模型数", bg=CARD, fg=TEXT,
                 font=FONT_SM).pack(side="left", padx=(14, 0))
        tk.Entry(opt, textvariable=self.extra_var, font=FONT_SM, width=5,
                 relief="solid", bd=1, bg="#f8fafc").pack(side="left", padx=4)
        tk.Label(opt, text="服务商槽", bg=CARD, fg=TEXT,
                 font=FONT_SM).pack(side="left", padx=(14, 0))
        tk.Entry(opt, textvariable=self.pid_var, font=FONT_SM, width=4,
                 relief="solid", bd=1, bg="#f8fafc").pack(side="left", padx=4)

        tk.Label(c, textvariable=self.cfg_tip_var, bg=CARD, fg=WARN,
                 font=FONT_SM, anchor="w", wraplength=800,
                 justify="left").pack(fill="x", pady=(8, 0))
        tk.Label(c, textvariable=self.effective_var, bg=CARD, fg=OK,
                 font=FONT_SM, anchor="w", wraplength=800,
                 justify="left").pack(fill="x", pady=(3, 0))
        probe_row = tk.Frame(c, bg=CARD)
        probe_row.pack(fill="x", pady=(6, 0))
        tk.Label(probe_row,
                 text="换成别人的中转站 / 云 API 后，先测一下能不能聊（不发任何修改）",
                 bg=CARD, fg=MUTED, font=FONT_SM).pack(side="left")
        _btn(probe_row, "● 接口自检", self.on_probe).pack(side="right")
        for v in (self.up_var, self.key_var, self.model_var, self.port_var,
                  self.pid_var, self.extra_var,
                  self.force_var, self.trust_var):
            v.trace("w", self._refresh_effective)
        self._refresh_effective()

    def _refresh_effective(self, *a):
        """实时回显「实际生效」的转发目标，专治配置写了却没被读。"""
        try:
            cfg = self.collect_cfg(quiet=True)
            u = P.init_bridge(cfg)
            base = P.direct_base_url(cfg)
            self.effective_var.set(
                "直连 → %s/v1/chat/completions  |  模型 %s  |  密钥来源 %s  |  标题 %s" %
                (base, u["model"],
                 "透传客户端" if u["trust_client"] else "本工具配置",
                 P.WINDOW_TITLE))
        except Exception as e:
            self.effective_var.set("配置未就绪：%r" % (e,))

    def _build_actions(self, parent):
        c = self._card(parent, "③ 一键操作")
        top = tk.Frame(c, bg=CARD)
        top.pack(fill="x")
        self.main_btn = _btn(top, "★  一键永久修复", self.on_fix, primary=True)
        self.main_btn.pack(side="left", ipadx=18)
        tk.Label(top, text="改 DLL + 汉化 + 注册表 · 纯直连零插件 · 无需开机自启", bg=CARD,
                 fg=MUTED, font=FONT_SM).pack(side="left", padx=12)

        self.minor = []
        items = (
            ("兼容代理模式(手动)", self.on_compat),
            ("只读体检", self.on_check),
            ("配置自检", self.on_cfgcheck),
            ("端到端自测", self.on_test),
            ("一键还原", self.on_rollback),
            ("打开所在文件夹", self.on_open),
        )
        for i, (text, cmd) in enumerate(items):
            if i % 3 == 0:
                row = tk.Frame(c, bg=CARD)
                row.pack(fill="x", pady=(10 if i == 0 else 2, 0))
            b = _btn(row, text, cmd)
            b.pack(side="left", padx=(0, 7), pady=2)
            self.minor.append(b)

    def _build_log(self, parent):
        c = self._card(parent, "运行日志（自动保存到 ProPilotFix.log）")
        c.pack_propagate(True)
        self.txt = scrolledtext.ScrolledText(
            c, height=11, font=("Consolas", 9), bg="#f8fafc", fg="#334155",
            relief="solid", bd=1, wrap="word")
        self.txt.pack(fill="both", expand=True)
        self.txt.insert("end", "准备就绪。正在自动搜索 ProPilot.dll …\n")
        self.txt.configure(state="normal")

    # ---- 事件 ------------------------------------------------------------
    def _pump(self):
        try:
            while True:
                kind, val = LOG_Q.get_nowait()
                if kind == "log":
                    self.txt.insert("end", str(val) + "\n")
                    self.txt.see("end")
                elif kind == "status":
                    self.status_var.set(str(val))
                elif kind == "progress":
                    self.find_var.set(str(val))
                elif kind == "found":
                    self._on_found(val)
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _auto_find(self):
        LOG_Q.put(("progress", "正在自动搜索 ProPilot.dll…"))
        try:
            dirs = find_dll_dirs(progress=lambda s: LOG_Q.put(("progress", s)))
        except Exception as e:
            LOG_Q.put(("progress", "搜索出错：%r" % (e,)))
            dirs = []
        LOG_Q.put(("found", dirs))

    def _on_found(self, dirs):
        if not dirs:
            self.find_var.set("没找到 ProPilot.dll —— 点「浏览…」手动选它的文件夹")
            self.status_var.set("等待手动选择 DLL 目录")
            return
        self.find_var.set("找到 %d 个，已自动选中；多个可在右侧切换" % len(dirs))
        menu = self.cb["menu"]
        menu.delete(0, "end")
        for d in dirs:
            menu.add_command(label=d,
                             command=lambda v=d: (self.cb_var.set(v), self.dir_var.set(v),
                                                  self._load_cfg(v)))
        self.cb_var.set(dirs[0])
        self.dir_var.set(dirs[0])
        self._load_cfg(dirs[0])
        self.status_var.set("已定位 DLL · 填好上游/密钥/模型后点「★ 一键永久修复」")

    def _on_pick(self, *a):
        d = self.cb_var.get()
        if d and os.path.isdir(d):
            self.dir_var.set(d)
            self._load_cfg(d)

    def _load_cfg(self, d):
        cfg = dict(P.DEFAULT_CFG)
        p = os.path.join(d, "ProPilotFix.json")
        exists = os.path.isfile(p)
        if exists:
            try:
                with open(p, encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except Exception:
                pass
        self.up_var.set(cfg["upstream"])
        self.key_var.set(cfg["api_key"])
        self.model_var.set(cfg["model"])
        self.port_var.set(str(cfg["bridge_port"]))
        self.pid_var.set(str(cfg.get("provider_id", 4)))
        self.extra_var.set(str(cfg.get("extra_models", 0)))
        self.force_var.set(bool(cfg.get("force_model", True)))
        self.trust_var.set(bool(cfg.get("trust_client_key", False)))
        self.cfg_tip_var.set(
            "已载入本机配置 ProPilotFix.json（改动后点修复即保存）" if exists else
            "⚠ 当前是示例配置：把上游 / 密钥 / 模型改成你自己的再点修复；"
            "本地服务不需要密钥可留空")

    def browse(self):
        d = filedialog.askdirectory(title="选择 ProPilot.dll 所在的文件夹（一般是 Bin）")
        if not d:
            return
        if not os.path.isfile(os.path.join(d, "ProPilot.dll")):
            messagebox.showwarning(TITLE, "这个文件夹里没有 ProPilot.dll，请重新选。\n\n"
                                          "一般是 ...\\Proteus 9 Professional\\BIN\\")
            return
        self.dir_var.set(d)
        self.cb_var.set(d)
        self._load_cfg(d)
        self.find_var.set("已手动指定")
        self.status_var.set("已定位 DLL · 填好上游/密钥/模型后点「★ 一键永久修复」")

    def collect_cfg(self, quiet=False):
        def _int(var, dflt):
            try:
                return int(str(var.get()).strip())
            except Exception:
                return dflt
        port = _int(self.port_var, P.DEFAULT_CFG["bridge_port"])
        if not quiet and str(port) != str(self.port_var.get()).strip():
            self.port_var.set(str(port))
        c = dict(P.DEFAULT_CFG)
        c.update({
            "upstream": self.up_var.get().strip(),
            "api_key": self.key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "bridge_port": port,
            "provider_id": _int(self.pid_var, 4),
            "force_model": bool(self.force_var.get()),
            "extra_models": _int(self.extra_var, 0),
            "trust_client_key": bool(self.trust_var.get()),
        })
        return c

    def _ready(self, need_dll=True):
        d = self.dir_var.get().strip()
        if need_dll and (not d or not os.path.isfile(os.path.join(d, "ProPilot.dll"))):
            messagebox.showwarning(TITLE, "先选好 ProPilot.dll 所在的文件夹。\n\n"
                                          "点「重新搜索」或「浏览…」。")
            return None
        cfg = self.collect_cfg()
        P.set_work_dir(d if d else P.base_dir())
        try:
            P.save_cfg(cfg)
        except Exception as e:
            LOG_Q.put(("log", "[!] 配置写入失败（不影响修复）：%r" % (e,)))
        return cfg

    def _set_busy(self, v):
        self.busy = v
        state = "disabled" if v else "normal"
        try:
            self.main_btn.configure(state=state)
            for b in self.minor:
                b.configure(state=state)
        except Exception:
            pass

    def _run(self, label, fn):
        if self.busy:
            return
        self._set_busy(True)
        self.status_var.set(label + "…")

        def work():
            try:
                fn()
            except Exception as e:
                LOG_Q.put(("log", "[X] 异常：%r" % (e,)))
                LOG_Q.put(("log", traceback.format_exc()))
                LOG_Q.put(("status", "出错了，请看日志"))
            finally:
                LOG_Q.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def _start_bridge(self, cfg):
        if self.bridge_started:
            LOG_Q.put(("log", "[i] 代理已在运行，不重复启动"))
            return
        self.bridge_started = True

        def run():
            try:
                P.serve(cfg)
            except Exception as e:
                LOG_Q.put(("log", "[X] 代理异常：%r" % (e,)))
            finally:
                self.bridge_started = False
                LOG_Q.put(("log", "[i] 代理已停止"))
                LOG_Q.put(("status", "代理已停止"))
        threading.Thread(target=run, daemon=True).start()
        LOG_Q.put(("status", "代理运行中 · 127.0.0.1:%s · 关闭本窗口即停止" %
                   cfg["bridge_port"]))

    # ---- 按钮 ------------------------------------------------------------
    def on_fix(self):
        cfg = self._ready()
        if cfg is None:
            return

        def job():
            P.cmd_check(cfg)
            rc = P.cmd_fix(cfg, no_reg=False, direct=True)
            LOG_Q.put(("status",
                       "永久修复完成 · 纯直连 · 零进程零自启 · 重开 Proteus 9 直接聊天"
                       if rc == 0 else "修复未完成，请看日志"))
        self._run("一键永久修复", job)

    def on_compat(self):
        cfg = self._ready()
        if cfg is None:
            return
        if not messagebox.askokcancel(
                TITLE,
                "切到「兼容代理」模式？\n\n"
                "· 注册表接口地址改回 127.0.0.1:<代理端口>，由本工具代理转发\n"
                "· 只在你老上游协议不兼容、直连聊不通时才需要\n"
                "· 代理是独立后台进程，关掉本窗口也不停，但【不会开机自启】\n"
                "· 重开机后要用，得再点一次这个按钮\n\n"
                "想回到默认零插件状态，点一次「一键永久修复」即可。"):
            return

        def job():
            P.cmd_check(cfg)
            rc = P.cmd_fix(cfg, no_reg=False, direct=False)
            if rc == 0:
                _, msg = P.spawn_bridge()
                LOG_Q.put(("log", "[8] " + msg))
                LOG_Q.put(("status", "兼容代理已启动 · 手动模式 · 重开 Proteus 9"))
            else:
                LOG_Q.put(("status", "代理模式切换未完成，请看日志"))
        self._run("兼容代理", job)

    def on_check(self):
        cfg = self._ready()
        if cfg is None:
            return
        self._run("只读体检", lambda: (P.cmd_check(cfg),
                                     LOG_Q.put(("status", "体检完成"))))

    def on_cfgcheck(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return

        def job():
            P.log("配置自检（只读，不改任何东西）")
            P.cfg_report(cfg)
            errs = P.validate_cfg(cfg)
            for e in errs:
                P.log("[X] " + e)
            P.log("[i] 上面就是本次修复实际会用的值，和界面填的不一致 = 没读到")
            LOG_Q.put(("status", "配置自检完成" if not errs else "配置有错，请看日志"))
        self._run("配置自检", job)

    def on_probe(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return

        def job():
            rc = P.probe_direct(cfg)
            LOG_Q.put(("status", "接口可用 · 可以一键永久修复" if rc == 0
                       else "接口不通，请看日志里的具体原因"))
        self._run("接口自检", job)

    def on_fixreg(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return

        def job():
            P.reg_backup()
            P.reg_fix(cfg)
            LOG_Q.put(("log", "只修注册表完成"))
            LOG_Q.put(("status", "注册表已修复"))
        self._run("只修注册表", job)

    def on_test(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return
        self._run("端到端自测", lambda: (P.selftest(cfg),
                                       LOG_Q.put(("status", "自测结束"))))

    def on_serve(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return

        def job():
            P.init_bridge(cfg)
            if P.bridge_alive(cfg["bridge_port"]):
                LOG_Q.put(("log", "[i] 端口 %s 上已有代理在运行，不重复启动" %
                           cfg["bridge_port"]))
            else:
                _, msg = P.spawn_bridge()
                LOG_Q.put(("log", "[i] " + msg))
            LOG_Q.put(("status", "代理已启动（手动，不开机自启）"))
        self._run("启动代理", job)

    def on_rollback(self):
        if not messagebox.askokcancel(TITLE, "确定还原 DLL 和注册表到修复前的状态？\n\n"
                                             "会同时停止本工具启动的后台代理。"):
            return
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return
        self._run("一键还原", lambda: (P.cmd_rollback(False),
                                     LOG_Q.put(("status", "已还原"))))

    def on_open(self):
        d = self.dir_var.get().strip() or P.base_dir()
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror(TITLE, "打不开目录：%r" % (e,))

    def elevate(self):
        try:
            args = " ".join('"%s"' % a for a in sys.argv[1:])
            if getattr(sys, "frozen", False):
                target, params = sys.executable, args
            else:
                target = sys.executable
                params = '"%s" %s' % (os.path.abspath(__file__), args)
            ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
            self.root.destroy()
        except Exception as e:
            messagebox.showerror(TITLE, "提权失败：%r" % (e,))

    def on_close(self):
        if self.bridge_started and not messagebox.askokcancel(
                TITLE, "代理正在运行，关闭窗口会停止代理。确定退出？"):
            return
        self.root.destroy()


# ==========================================================================
#  入口
# ==========================================================================
def main():
    if "--probe" in sys.argv:
        d = _arg("--dir")
        if d:
            P.set_work_dir(d)
        cfgpath = _arg("--cfg")
        if cfgpath and os.path.isfile(cfgpath):
            with open(cfgpath, encoding="utf-8") as f:
                cfg = dict(P.DEFAULT_CFG)
                cfg.update(json.load(f))
            cfg.pop("window_title", None)
        else:
            cfg = P.load_cfg(interactive=False)
        rc = P.probe_direct(cfg)
        print("PROBE RC = %d" % rc, flush=True)
        return rc

    if "--selftest-ui" in sys.argv:
        # 验证打包后 tcl/tk 数据完整、界面能构建（不显示窗口、不留进程）
        r = tk.Tk()
        r.withdraw()
        App(r)
        r.update_idletasks()
        print("UI_BUILD_OK  title=%s  widgets=%d" %
              (TITLE, len(r.winfo_children())), flush=True)
        r.destroy()
        return 0

    if "--find" in sys.argv:
        dirs = find_dll_dirs(timeout=25,
                             progress=lambda s: print("[find] " + s, flush=True))
        print("FIND_RESULT=" + json.dumps(dirs, ensure_ascii=False), flush=True)
        return 0 if dirs else 2

    if "--auto" in sys.argv:
        d = _arg("--dir")
        if d:
            dirs = [d]
            print("AUTO 指定目录 = " + d, flush=True)
        else:
            dirs = find_dll_dirs(progress=lambda s: print("[auto] " + s, flush=True))
        if not dirs:
            print("AUTO FAIL: 未找到 ProPilot.dll", flush=True)
            return 2
        P.set_work_dir(dirs[0])
        print("AUTO DIR = " + P.HERE, flush=True)
        cfgpath = _arg("--cfg")
        if cfgpath and os.path.isfile(cfgpath):
            with open(cfgpath, encoding="utf-8") as f:
                cfg = dict(P.DEFAULT_CFG)
                cfg.update(json.load(f))
            cfg.pop("window_title", None)
            P.save_cfg(cfg)
            print("AUTO CFG = " + cfgpath, flush=True)
        else:
            cfg = P.load_cfg(interactive=False)
        rc = P.cmd_fix(cfg, no_reg=("--no-reg" in sys.argv), direct=True)
        print("AUTO RC = %d" % rc, flush=True)
        return rc

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
