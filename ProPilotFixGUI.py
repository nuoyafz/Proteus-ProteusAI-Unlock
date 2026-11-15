# -*- coding: utf-8 -*-
"""
ProPilotFix —— 修好 ProPilot 9「自定义服务商」连不上 / 405 的原版 bug

这个工具不需要你填任何地址/密钥/模型。
它只做一件事：改 ProPilot.dll 里的服务商实现，让「自定义」正常拼出
    <你填的根地址> + /v1/chat/completions
之后你在 ProPilot「大模型服务 → 自定义」里填自己的地址/密钥/模型即可。

原版 bug（逆向定位）：Custom 服务商类的 URL 构造只把 baseUrl 原样交给
QNetworkRequest::setUrl()，从不追加路径 -> POST http://host/ -> 405。

顺带：界面汉化、主窗口标题、模型下拉框首项跟随你已保存的模型。

命令行：
  ProPilotFix.exe --auto                 自动搜索 DLL 并修复
  ProPilotFix.exe --auto --no-reg        跳过注册表（自测）
  ProPilotFix.exe --auto --dir D:/x/Bin  指定 DLL 目录
  ProPilotFix.exe --find                 只列出找到的 DLL 目录
  ProPilotFix.exe --selftest-ui          界面自检
"""
import ctypes
import os
import queue
import string
import sys
import threading
import time
import traceback

TITLE = "ProPilotFix · ProPilot 自定义服务商修复"


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


_AUTO = any(a in sys.argv for a in ("--auto", "--find", "--selftest-ui"))

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
        self.dir_var = tk.StringVar(value="")
        root.title(TITLE + "    v3")
        root.geometry("760x600")
        root.minsize(680, 520)
        root.configure(bg=BG)

        self._build_head()
        self._build_body()
        self._build_log()
        root.after(120, self._pump)
        threading.Thread(target=self._auto_find, daemon=True).start()

    # ---- 组件 ------------------------------------------------------------
    def _card(self, parent, title=None):
        outer = tk.Frame(parent, bg=CARD, highlightbackground="#dbe4f0",
                         highlightthickness=1)
        inner = tk.Frame(outer, bg=CARD, padx=14, pady=12)
        inner.pack(fill="both", expand=True)
        if title:
            tk.Label(inner, text=title, bg=CARD, fg=MUTED, font=FONT_SM,
                     anchor="w").pack(fill="x", pady=(0, 8))
        return inner

    def _build_head(self):
        head = tk.Frame(self.root, bg=BG, padx=16, pady=12)
        head.pack(fill="x")
        tk.Label(head, text="ProPilot 自定义服务商 · 一键修复",
                 bg=BG, fg=TEXT, font=FONT_BIG, anchor="w").pack(fill="x")
        tk.Label(head, text="修的是原版 bug：ProPilot 把地址原样发出去，不拼 /v1/chat/completions → 405。"
                            "\n修好之后，地址/密钥/模型全部在 ProPilot 里自己填，本工具不需要你填任何东西。",
                 bg=BG, fg=MUTED, font=FONT_SM, anchor="w", justify="left").pack(fill="x", pady=(4, 0))

    def _build_body(self):
        wrap = tk.Frame(self.root, bg=BG, padx=16)
        wrap.pack(fill="x")
        c = self._card(wrap, "① 定位 ProPilot.dll")
        c.master.master.pack(fill="x", pady=(0, 10))

        row = tk.Frame(c, bg=CARD)
        row.pack(fill="x")
        self.path_lbl = tk.Label(row, text="正在自动搜索…", bg=CARD, fg=TEXT,
                                 font=FONT_SM, anchor="w", justify="left")
        self.path_lbl.pack(side="left", fill="x", expand=True)
        _btn(row, "重新搜索", self.search).pack(side="right", padx=(6, 0))
        _btn(row, "浏览…", self.browse).pack(side="right")

        c2 = self._card(wrap, "② 一键修复")
        c2.master.master.pack(fill="x", pady=(0, 10))
        b = _btn(c2, "★  一键修复 ProPilot", self.on_fix, primary=True)
        b.pack(fill="x", pady=(0, 8))
        self.main_btn = b
        self.status_var = tk.StringVar(value="准备就绪")
        tk.Label(c2, textvariable=self.status_var, bg=CARD, fg=OK, font=FONT_SM,
                 anchor="w").pack(fill="x")

        r2 = tk.Frame(c2, bg=CARD)
        r2.pack(fill="x", pady=(8, 0))
        self.minor = []
        for t, fn in (("只读体检", self.on_check), ("一键还原", self.on_rollback),
                      ("打开目录", self.on_open)):
            bb = _btn(r2, t, fn)
            bb.pack(side="left", padx=(0, 7))
            self.minor.append(bb)

        note = ("用法：修复完打开 Proteus 9 → ProPilot 设置 → 大模型服务 → 服务商选「自定义」\n"
                "　　　接口根地址填到端口为止，例如 http://127.0.0.1:18900 或 https://api.openai.com\n"
                "　　　（不要带 /v1、不要带 /chat/completions）→ 填密钥和模型 → OK → 直接发消息")
        tk.Label(c2, text=note, bg="#f6f9ff", fg=MUTED, font=FONT_SM,
                 anchor="w", justify="left", padx=8, pady=6).pack(fill="x", pady=(8, 0))

    def _build_log(self):
        wrap = tk.Frame(self.root, bg=BG, padx=16, pady=10)
        wrap.pack(fill="both", expand=True)
        c = self._card(wrap, "运行日志（同时写入 ProPilotFix.log）")
        c.master.master.pack(fill="both", expand=True)
        self.txt = scrolledtext.ScrolledText(
            c, height=9, font=("Consolas", 9), bg="#f8fafc", fg="#334155",
            relief="solid", bd=1, wrap="word")
        self.txt.pack(fill="both", expand=True)
        self.txt.insert("end", "准备就绪。正在自动搜索 ProPilot.dll …\n")

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
                elif kind == "found":
                    self._on_found(val)
                elif kind == "done":
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _auto_find(self):
        try:
            dirs = find_dll_dirs(progress=lambda s: LOG_Q.put(("log", "[搜索] " + s)))
        except Exception as e:
            LOG_Q.put(("log", "[X] 搜索出错：%r" % (e,)))
            dirs = []
        LOG_Q.put(("found", dirs))

    def search(self):
        if self.busy:
            return
        self.path_lbl.configure(text="正在自动搜索…")
        threading.Thread(target=self._auto_find, daemon=True).start()

    def _on_found(self, dirs):
        self.dirs = dirs
        if not dirs:
            self.path_lbl.configure(text="没找到 ProPilot.dll —— 点「浏览…」手动选它的文件夹")
            self.status_var.set("等待手动选择 DLL 目录")
            return
        d = dirs[0]
        self.dir_var.set(d)
        self.path_lbl.configure(text="ProPilot.dll：%s" % os.path.join(d, "ProPilot.dll"))
        self.status_var.set("已定位 DLL · 点「★ 一键修复 ProPilot」")

    def browse(self):
        d = filedialog.askdirectory(title="选择 ProPilot.dll 所在的文件夹（一般是 Bin）")
        if not d:
            return
        if not os.path.isfile(os.path.join(d, "ProPilot.dll")):
            messagebox.showwarning(TITLE, "这个文件夹里没有 ProPilot.dll，请重新选。\n\n"
                                          "一般是 ...\\Proteus 9 Professional\\BIN\\")
            return
        self.dir_var.set(d)
        self.path_lbl.configure(text="ProPilot.dll：%s" % os.path.join(d, "ProPilot.dll"))
        self.status_var.set("已手动指定 DLL 目录")

    def _ready(self):
        d = self.dir_var.get().strip()
        if not d or not os.path.isfile(os.path.join(d, "ProPilot.dll")):
            messagebox.showwarning(TITLE, "先定位 ProPilot.dll。\n\n"
                                          "点「重新搜索」或「浏览…」。")
            return None
        P.set_work_dir(d)
        return d

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
        if self._ready() is None:
            return

        def job():
            P.cmd_check({})
            rc = P.cmd_fix({}, no_reg=False)
            LOG_Q.put(("status",
                       "修复完成 · 去 ProPilot 里填你自己的地址/密钥/模型 就能聊"
                       if rc == 0 else "修复未完成，请看日志"))
        self._run("一键修复", job)

    def on_check(self):
        if self._ready() is None:
            return
        self._run("只读体检", lambda: (P.cmd_check({}),
                                     LOG_Q.put(("status", "体检完成"))))

    def on_rollback(self):
        if self._ready() is None:
            return
        if not messagebox.askokcancel(TITLE, "确定把 DLL 还原到修复前的状态？"):
            return
        self._run("一键还原", lambda: (P.cmd_rollback(False),
                                     LOG_Q.put(("status", "已还原"))))

    def on_open(self):
        d = self.dir_var.get().strip() or P.base_dir()
        try:
            os.startfile(d)
        except Exception as e:
            messagebox.showerror(TITLE, "打不开目录：%r" % (e,))


def main():
    if "--find" in sys.argv:
        ds = find_dll_dirs(timeout=25, progress=lambda s: print("[find] " + s, flush=True))
        for d in ds:
            print("DLL_DIR=" + d, flush=True)
        print("FOUND=%d" % len(ds), flush=True)
        return 0

    if "--selftest-ui" in sys.argv:
        root = tk.Tk()
        try:
            App(root)
            root.update_idletasks()
            print("UI_BUILD_OK", flush=True)
            return 0
        except Exception as e:
            print("UI_BUILD_FAIL %r" % (e,), flush=True)
            return 1
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    if "--auto" in sys.argv:
        d = _arg("--dir").strip()
        if not d:
            ds = find_dll_dirs(timeout=30, progress=lambda s: print("[auto] " + s, flush=True))
            d = ds[0] if ds else ""
        if not d:
            print("AUTO FAIL 找不到 ProPilot.dll", flush=True)
            return 2
        P.set_work_dir(d)
        print("AUTO DIR=" + d, flush=True)
        rc = P.cmd_fix({}, no_reg=("--no-reg" in sys.argv))
        print("AUTO_RC=%d" % rc, flush=True)
        return rc

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
