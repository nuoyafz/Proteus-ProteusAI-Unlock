# -*- coding: utf-8 -*-
"""
ProPilot 一键修复 · ProPilotFix   —— 图形界面版（只聊天 · 一条路）

思路很简单：地址 / 密钥 / 模型 三项填好，点一次「一键永久修复」。
工具改 DLL（汉化 + 主窗口标题 + 模型首项 + 地址后缀 /chat/completions）
并原样写注册表，之后 ProPilot 直接请求你自己的上游，零进程零自启。

★ 地址填到 /v1 为止（例如 http://127.0.0.1:8000/v1）。
  工具不会剥 /v1、不会帮你猜、不会改你填的地址。

工具不必放到 ProPilot.dll 同目录：放桌面、微信下载目录、U 盘都行，
启动后自动搜索 DLL（卸载表 → 常见路径 → 全盘限时扫描），找不到可手选。

命令行 / 无人值守：
  ProPilotFix.exe --auto                       自动搜索并修复
  ProPilotFix.exe --auto --no-reg              跳过注册表（沙箱自测）
  ProPilotFix.exe --auto --dir D:/x/Bin        直接指定 DLL 目录
  ProPilotFix.exe --find                       只列出找到的 DLL 目录
  ProPilotFix.exe --probe --cfg X --dir Y      只读接口自检
  ProPilotFix.exe --selftest-ui                界面自检
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

TITLE = "ProPilotFix · ProPilot 一键修复"


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
        self.busy = False
        root.title(TITLE)
        root.geometry("840x660")
        root.minsize(760, 580)
        root.configure(bg=BG)

        self.dir_var = tk.StringVar(value="")
        self.find_var = tk.StringVar(value="正在自动搜索 ProPilot.dll…")
        self.status_var = tk.StringVar(value="就绪 · 建议先完全退出 Proteus 9")
        self.up_var = tk.StringVar(value=P.DEFAULT_CFG["upstream"])
        self.key_var = tk.StringVar(value=P.DEFAULT_CFG["api_key"])
        self.model_var = tk.StringVar(value=P.DEFAULT_CFG["model"])
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
        tk.Label(left, text="只聊天 · 你填什么就是什么 · 零进程零自启",
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
                          anchor="w", width=44)
        self.cb.pack(side="right", padx=(8, 0))
        self.cb_var.trace("w", self._on_pick)

    def _build_cfg(self, parent):
        c = self._card(parent, "② 接口（用别人的中转站：三项都填他自己的）")
        grid = tk.Frame(c, bg=CARD)
        grid.pack(fill="x")
        rows = [
            ("接口地址", self.up_var, 56),
            ("API 密钥", self.key_var, 56),
            ("模型名", self.model_var, 34),
        ]
        for lab, var, w in rows:
            cell = tk.Frame(grid, bg=CARD)
            cell.pack(fill="x", pady=3)
            tk.Label(cell, text=lab, bg=CARD, fg=TEXT, font=FONT,
                     width=8, anchor="w").pack(side="left")
            tk.Entry(cell, textvariable=var, font=FONT, width=w,
                     relief="solid", bd=1, bg="#f8fafc").pack(side="left", ipady=4)

        tk.Label(c, text="地址填到 /v1 为止（例：http://127.0.0.1:8000/v1）。"
                         "本地服务不需要密钥就留空。",
                 bg=CARD, fg=MUTED, font=FONT_SM, anchor="w").pack(fill="x", pady=(7, 0))
        tk.Label(c, textvariable=self.cfg_tip_var, bg=CARD, fg=WARN,
                 font=FONT_SM, anchor="w", wraplength=780,
                 justify="left").pack(fill="x", pady=(3, 0))
        tk.Label(c, textvariable=self.effective_var, bg=CARD, fg=OK,
                 font=FONT_SM, anchor="w", wraplength=780,
                 justify="left").pack(fill="x", pady=(3, 0))
        probe_row = tk.Frame(c, bg=CARD)
        probe_row.pack(fill="x", pady=(6, 0))
        tk.Label(probe_row, text="换自己的接口后，先测一下能不能聊（不发任何修改）",
                 bg=CARD, fg=MUTED, font=FONT_SM).pack(side="left")
        _btn(probe_row, "● 接口自检", self.on_probe).pack(side="right")
        for v in (self.up_var, self.key_var, self.model_var):
            v.trace("w", self._refresh_effective)
        self._refresh_effective()

    def _refresh_effective(self, *a):
        """实时回显「实际生效」的请求地址，专治配置写了却没被读。"""
        try:
            cfg = self.collect_cfg()
            self.effective_var.set(
                "ProPilot 将请求 → %s   |   模型 %s   |   标题 %s" %
                (P.chat_url(cfg), cfg.get("model") or "(未填)", P.WINDOW_TITLE))
        except Exception as e:
            self.effective_var.set("配置未就绪：%r" % (e,))

    def _build_actions(self, parent):
        c = self._card(parent, "③ 一键操作")
        top = tk.Frame(c, bg=CARD)
        top.pack(fill="x")
        self.main_btn = _btn(top, "★  一键永久修复", self.on_fix, primary=True)
        self.main_btn.pack(side="left", ipadx=18)
        tk.Label(top, text="改 DLL + 汉化 + 地址后缀 + 注册表 · 只聊天 · 无需开机自启", bg=CARD,
                 fg=MUTED, font=FONT_SM).pack(side="left", padx=12)

        self.minor = []
        items = (
            ("只读体检", self.on_check),
            ("一键还原", self.on_rollback),
            ("打开所在文件夹", self.on_open),
        )
        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        for text, cmd in items:
            b = _btn(row, text, cmd)
            b.pack(side="left", padx=(0, 7), pady=2)
            self.minor.append(b)

    def _build_log(self, parent):
        c = self._card(parent, "运行日志（自动保存到 ProPilotFix.log）")
        c.pack_propagate(True)
        self.txt = scrolledtext.ScrolledText(
            c, height=10, font=("Consolas", 9), bg="#f8fafc", fg="#334155",
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
        self.status_var.set("已定位 DLL · 填好地址/密钥/模型后点「★ 一键永久修复」")

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
        self.up_var.set(cfg.get("upstream", P.DEFAULT_CFG["upstream"]))
        self.key_var.set(cfg.get("api_key", ""))
        self.model_var.set(cfg.get("model", P.DEFAULT_CFG["model"]))
        self.cfg_tip_var.set(
            "已载入本机配置 ProPilotFix.json（改动后点修复即保存）" if exists else
            "⚠ 当前是示例配置：把地址 / 密钥 / 模型改成你自己的再点修复")
        self._refresh_effective()

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
        self.status_var.set("已定位 DLL · 填好地址/密钥/模型后点「★ 一键永久修复」")

    def collect_cfg(self):
        c = dict(P.DEFAULT_CFG)
        c.update({
            "upstream": self.up_var.get().strip(),
            "api_key": self.key_var.get().strip(),
            "model": self.model_var.get().strip(),
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

    # ---- 按钮 ------------------------------------------------------------
    def on_fix(self):
        cfg = self._ready()
        if cfg is None:
            return

        def job():
            P.cmd_check(cfg)
            rc = P.cmd_fix(cfg, no_reg=False)
            LOG_Q.put(("status",
                       "永久修复完成 · 只聊天 · 零进程零自启 · 重开 Proteus 9 直接聊天"
                       if rc == 0 else "修复未完成，请看日志"))
        self._run("一键永久修复", job)

    def on_check(self):
        cfg = self._ready()
        if cfg is None:
            return
        self._run("只读体检", lambda: (P.cmd_check(cfg),
                                     LOG_Q.put(("status", "体检完成"))))

    def on_probe(self):
        cfg = self._ready(need_dll=False)
        if cfg is None:
            return

        def job():
            rc = P.probe_direct(cfg)
            LOG_Q.put(("status", "接口可用 · 可以一键永久修复" if rc == 0
                       else "接口不通，请看日志里的具体原因"))
        self._run("接口自检", job)

    def on_rollback(self):
        if not messagebox.askokcancel(TITLE, "确定还原 DLL 和注册表到修复前的状态？"):
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
        rc = P.cmd_fix(cfg, no_reg=("--no-reg" in sys.argv))
        print("AUTO RC = %d" % rc, flush=True)
        return rc

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
