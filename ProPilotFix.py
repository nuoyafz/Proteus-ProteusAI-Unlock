# -*- coding: utf-8 -*-
"""
ProPilotFix —— ProPilot 9.2 自定义接口 / 汉化 一键永久修复工具
纯标准库、零第三方依赖、可分发；本版本不含任何私人地址 / 密钥 / 模型。

解决什么：Proteus 9 自带的 ProPilot AI 助手用「自定义接口」连不上自己的
OpenAI 兼容服务、界面全英文、模型被写死 gpt-4o。本工具一次性改写
ProPilot.dll 里的字符串常量 + 写注册表，之后 ProPilot 直接请求你自己的上游
（纯直连），零常驻进程、零开机自启，重开 Proteus 就能聊。详见 README.md。

修的是什么（REA/Ghidra 逆向 + 真机日志实证）：
  1) 汉化 64 条界面 / 报错文案 + 主窗口标题。source_en.ts 里 ProPilot 条目
     是空译文，Qt 查不到译文会回退源串，故原地改 .rdata 里的 UTF-8 常量
     即可，不动代码。
  2) 模型下拉框：FUN_18007ce60 写死 5 个 gpt 模型，点 OK 会把首项
     gpt-4o 写回注册表，覆盖用户保存的模型。改首条 LEA 的 disp32 指向
     .rdata 空闲块里写入的目标模型（可反复更换，不会卡在旧模型）。
  3) 注册表：Custom(4) 写入上游「根地址」/ 模型 / 密钥，清理占位符脏值。
     ProPilot 自己拼 /v1/chat/completions 与 /v1/models。
  4) 兼容代理（手动兜底，默认不启用）：ProPilot 三条 URL 规则互相矛盾
       聊天      baseUrl + /v1/chat/completions
       模型表    baseUrl + /v1/models
       测试连接  baseUrl 原样 POST（POST /v1 -> 404）
     代理把所有写法归一到上游正确路由；强制把请求里硬编码的 gpt-4o
     纠正成配置模型；模型表返回 data[].id + models[].name 双格式。

命令（命令行版；日常推荐 GUI：python ProPilotFixGUI.py）：
  python ProPilotFix.py fix        一键修复（纯直连，默认）
  python ProPilotFix.py probe      接口自检：只读探测自己的上游能不能聊
  python ProPilotFix.py check      只读体检（DLL / 汉化 / 模型 / 注册表）
  python ProPilotFix.py cfgcheck   打印实际生效配置
  python ProPilotFix.py test       端到端自测（同进程起代理，13 条路径）
  python ProPilotFix.py serve      手动启动兼容代理（不自启）
  python ProPilotFix.py fixreg     只修注册表（Proteus 退出写回后重跑）
  python ProPilotFix.py rollback   一键还原 DLL + 注册表
  --no-reg                         任意命令后附加：跳过所有注册表读写
  --dir X                          指定 ProPilot.dll 所在目录

备份：ProPilot.dll.orig（只建一次，永不覆盖）+ ProPilotFix_registry.json
日志：ProPilotFix.log
配置：ProPilotFix.json（含你的 API 密钥，切勿提交到 Git）
"""
import base64
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import winreg
except ImportError:
    winreg = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOL = "ProPilotFix 1.0"

# 主窗口标题：写死在程序里，界面、配置文件、命令行都改不了
WINDOW_TITLE = "ProPilotFix"

# ==========================================================================
#  固定条件区  —— 换环境只改这里，或改同目录的 ProPilotFix.json
# ==========================================================================
DEFAULT_CFG = {
    # 下面 3 项只是「示例值」，不代表任何人的配置，也不是推荐的服务商。
    # ★ 必须改成你自己的：改界面上那 4 项，或改 DLL 目录里的 ProPilotFix.json ★
    # 上游可填根地址 / 带 /v1 / 完整 chat 路径，脚本会归一；
    # 任意 OpenAI 兼容服务（云 API / 中转站 / Ollama / 本地服务）都行。
    "upstream": "https://api.openai.com/v1",
    "api_key": "",              # 本地服务可留空；云 API / 中转站填自己的密钥
    "model": "gpt-4o-mini",     # 换成中转站给你的模型名，例如 deepseek-chat / qwen-plus
    "bridge_port": 18901,       # ProPilot 的「接口根地址」会指向这里
    "provider_id": 4,           # 4 = Custom
    "force_model": True,        # 强制纠正请求里硬编码的 gpt-4o
    "extra_models": 0,          # 模型下拉框额外列出几个上游模型，0 = 只列目标模型
    "trust_client_key": False,  # False=始终用配置里的密钥（不被 ProPilot 残留值污染）
}

ORIG_SHA = "35be5f09eca913947e512c2b3d6fb8803b7ad9e3c8d4a65c2399f946354f5414"
REG_PRODUCT = "Proteus 9 Professional"   # 装的是别的版本就改这一行
IMAGE_BASE = 0x180000000
LEA_FO = 0x7C289        # FUN_18007ce60 首条 LEA RDX,[rip+disp32]
STR_FO = 0x171182       # .rdata 内 110 字节全 0 空闲块

# ==========================================================================
#  路径：全部相对脚本自身，脚本放 DLL 同目录即可
# ==========================================================================
def base_dir():
    # GUI / 外部调用可用环境变量指定工作目录（ProPilot.dll 所在文件夹）
    override = os.environ.get("PROPILOTFIX_DIR")
    if override and os.path.isdir(override):
        return override
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def set_work_dir(path):
    """把工作目录切到 ProPilot.dll 所在文件夹。
    DLL / 备份 / 配置 / 日志全部跟着走，GUI 版靠它支持「工具放任意目录」。
    命令行版在模块加载时调用一次，行为与旧版完全一致。"""
    global HERE, DLL, ORIG, OLD_BAK, REG_BAK, CFG_FILE, LOG, BRIDGE_PID
    HERE = os.path.abspath(path)
    DLL = os.path.join(HERE, "ProPilot.dll")
    ORIG = os.path.join(HERE, "ProPilot.dll.orig")
    OLD_BAK = os.path.join(HERE, "ProPilot.dll.fz_bak")   # 兼容旧版脚本的备份
    REG_BAK = os.path.join(HERE, "ProPilotFix_registry.json")
    CFG_FILE = os.path.join(HERE, "ProPilotFix.json")
    LOG = os.path.join(HERE, "ProPilotFix.log")
    BRIDGE_PID = os.path.join(HERE, "ProPilotFix_bridge.pid")
    return HERE


# 模块级初始化与 GUI 运行时切换共用同一套逻辑
set_work_dir(base_dir())
KEY_ROOT = r"Software\Labcenter Electronics\%s\PROPILOT" % REG_PRODUCT
LC = r"LLM Configuration"

_log_lock = threading.Lock()


def log(msg=""):
    line = str(msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    with _log_lock:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        except Exception:
            pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def load_cfg(interactive=True):
    if os.path.isfile(CFG_FILE):
        try:
            with open(CFG_FILE, encoding="utf-8") as f:
                c = dict(DEFAULT_CFG)
                c.update(json.load(f))
            c.pop("window_title", None)   # 旧配置里残留也不生效，标题写死
            return c
        except Exception as e:
            log("[!] 配置文件读取失败，改用内置默认值: %r" % (e,))
            return dict(DEFAULT_CFG)

    c = dict(DEFAULT_CFG)
    if interactive:
        print("=" * 68)
        print("首次运行：请填 3 项（直接回车用默认值）")
        print("=" * 68)
        try:
            v = input("  上游地址 [%s]: " % c["upstream"]).strip()
            if v:
                c["upstream"] = v
            v = input("  API 密钥 [%s]: " % c["api_key"][:12] + "...").strip()
            if v:
                c["api_key"] = v
            v = input("  模型名   [%s]: " % c["model"]).strip()
            if v:
                c["model"] = v
            v = input("  代理端口 [%s]: " % c["bridge_port"]).strip()
            if v:
                c["bridge_port"] = int(v)
        except (EOFError, KeyboardInterrupt, ValueError):
            log("[!] 非交互或输入无效，使用内置默认值。之后可改 " + CFG_FILE)
    save_cfg(c)
    log("[i] 配置已写入 " + CFG_FILE + "（以后可用记事本直接改）")
    return c


def save_cfg(c):
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


# ==========================================================================
#  汉化映射：(文件偏移, 英文原文, [中文候选，按优先级])
#  每条写入前都会校验原文 + 尾部 NUL + 占位符 + 可用容量，对不上就跳过，
#  绝不盲写。带 %1~%4 的复合 printf 串刻意不改，避免破坏格式。
# ==========================================================================
UI_MAP = [
    (0x10D230, "Test Connection", ["测试连接"]),
    (0x10DF30, "Network error: %1\n\nPlease check your internet connection and API settings.",
     ["网络错误 %1\n\n请检查接口根地址、密钥及服务是否可达",
      "网络错误 %1\n\n请检查接口地址与密钥", "网络错误 %1\n\n连接失败"]),
    (0x10DF80, "Connection failed: %1\n\nPlease check your API key and network connection.\n%2",
     ["连接失败：%1\n\n请检查 API 密钥与接口根地址\n%2",
      "连接失败：%1\n\n请检查密钥与地址\n%2", "连接失败 %1\n\n检查设置\n%2"]),
    (0x106E18, "Network Error Code: %1 - %2", ["网络错误码 %1 - %2", "错误码 %1 - %2"]),
    (0x10D110, "OpenAI (ChatGPT)", ["OpenAI 接口", "OpenAI"]),
    (0x10D128, "Anthropic (Claude)", ["Claude 接口", "Claude"]),
    (0x10D140, "Provider:", ["服务商：", "服务商"]),
    (0x10D164, "Custom", ["自定义", "自建"]),
    (0x10D170, "Ollama (Local)", ["本地模型 (Ollama)", "本地模型"]),
    (0x10D1B8, "Base URL:", ["接口根地址", "根地址"]),
    (0x10D1C8, "Enter your API key", ["请输入 API 密钥", "请输入密钥"]),
    (0x10D1E0, "API Key:", ["密钥：", "密钥"]),
    (0x10D1F0, "Temperature:", ["随机性：", "随机性"]),
    (0x10D208, "Load latest models from API", ["从接口拉取模型列表", "拉取模型列表"]),
    (0x10D224, "Model:", ["模型：", "模型"]),
    (0x10D240, "Sets the maximum number of tokens used to generate the response.",
     ["设置生成回复时允许使用的最大令牌数", "设置最大生成令牌数", "最大令牌数"]),
    (0x10D288, "Max Completion Tokens:", ["最大生成令牌数：", "最大令牌数"]),
    (0x10D450, "LLM Provider", ["大模型服务", "模型服务"]),
    (0x10D9D8, "Ollama (local)", ["本地模型 (Ollama)", "本地模型"]),
    (0x10DD00, "Anthropic API Key:", ["Anthropic 密钥：", "Claude 密钥"]),
    (0x10DD78, "OpenAI API Key:", ["OpenAI 密钥：", "OpenAI 密钥"]),
    (0x10DDB0, "Gemini API Key:", ["Gemini 密钥：", "Gemini 密钥"]),
    (0x10DDF0, "DeepSeek API Key:", ["DeepSeek 密钥：", "DeepSeek 密钥"]),
    (0x10DE30, "Grok API Key:", ["Grok 密钥：", "Grok 密钥"]),
    (0x10DE60, "API Key (optional):", ["密钥（可选）：", "密钥 (可选):"]),
    (0x10DE90, "Moonshot API Key:", ["Moonshot 密钥：", "Moonshot 密钥"]),
    (0x10DED0, "Please enter an API key first.", ["请先填写 API 密钥", "请先填写密钥"]),
    (0x10DEF0, "Enter API key if required", ["如需鉴权请填写密钥", "需要时填写密钥"]),
    (0x10DF10, "Leave empty for local Ollama", ["本地 Ollama 可留空", "本地模型可留空"]),
    (0x10DFD0, "Connection successful! ProPilot can communicate with the LLM provider.",
     ["连接成功！ProPilot 已可与模型服务正常通信", "连接成功！模型服务可用", "连接成功"]),
    (0x10D380, "The Model Context Protocol (MCP) allows external desktop chat agents to interact with Proteus. "
     "You will need to install a plugin for your chat agent and configure it to address the Proteus MCP server.",
     ["模型上下文协议（MCP）允许外部桌面 AI 助手与 Proteus 交互。"
      "你需要为 AI 助手安装插件，并将其配置指向 Proteus 的 MCP 服务。",
      "MCP 协议允许外部 AI 助手连接 Proteus，需在助手端安装插件并配置地址。"]),
    (0x10D560, "Compatibility mode for MCP clients such as CherryStudio that read only "
     "the first content element in tool results",
     ["兼容模式：供 CherryStudio 等只读取工具结果首个内容元素的 MCP 客户端使用",
      "兼容只读取首个结果元素的 MCP 客户端"]),
    (0x10AF98, "0 tokens", ["0 令牌"]),
    (0x10AFA8, "Current AI Model", ["当前 AI 模型", "当前模型"]),
    (0x10B7B0, "%1 tokens", ["%1 令牌"]),
    (0x10B9E0, "No Model", ["无模型", "无"]),
    (0x103D60, "Unsupported LLM provider",
     ["模型服务未就绪：请检查服务商与接口根地址", "不支持该模型服务", "服务商不支持"]),
    (0x103DB8, "Loading models...", ["正在加载模型列表...", "加载模型中...", "加载模型中"]),
    (0x103DD0, "Authentication failed. Please check your API key in settings.",
     ["鉴权失败：请在设置中检查 API 密钥是否正确", "鉴权失败，请检查 API 密钥", "鉴权失败"]),
    (0x103D80, "Rate limit exceeded. You can retry in %1 seconds.",
     ["触发限流，请在 %1 秒后重试", "限流，%1 秒后重试"]),
    (0x103E10, "Rate limit exceeded. Please wait a moment before retrying.",
     ["触发限流，请稍后再试", "请求限流，请稍后重试"]),
    (0x103E50, "Network connection failed. Please check your internet connection.",
     ["网络连接失败：请检查接口地址、密钥与服务是否可达", "网络连接失败，请检查网络设置"]),
    (0x103FD0, "Service temporarily overloaded. Please wait a moment before retrying.",
     ["服务暂时过载，请稍后再试", "服务过载，请稍后重试"]),
    (0x104018, "Service temporarily overloaded. You can retry in %1 seconds.",
     ["服务暂时过载，请在 %1 秒后重试", "服务过载，%1 秒后重试"]),
    (0x104058, "Request error (HTTP %1). Please check your request format.",
     ["请求错误 (HTTP %1)：请检查接口地址与请求格式（根地址不要带 /v1）",
      "请求错误 (HTTP %1)，请检查请求格式"]),
    (0x1040A0, "Server error (HTTP %1). The service may be temporarily unavailable.",
     ["服务错误 (HTTP %1)：服务可能暂时不可用", "服务错误 (HTTP %1)"]),
    (0x1040F0, "An unexpected error occurred (HTTP %1).", ["发生未预期错误 (HTTP %1)", "未预期错误 (HTTP %1)"]),
    (0x104118, "No provider adapter available",
     ["模型适配器未就绪：请在设置里确认服务商、接口根地址与模型名",
      "模型适配器不可用", "适配器未就绪"]),
    (0x10413A, "Details: %1", ["详情：%1"]),
    (0x104150, "Output or completion token limit exceeded. Please adjust LLM settings and retry.",
     ["输出令牌超限：请调小最大令牌数后重试", "输出令牌超限，请调整设置后重试"]),
    (0x1041A8, "Failed to parse assistant response", ["解析模型响应失败", "响应解析失败"]),
    (0x1041E8, "Chat content violated safety protocols.", ["对话内容触发安全策略", "内容触发安全策略"]),
    (0x104210, "No provider adapter available for reconstruction",
     ["重建响应的模型适配器不可用", "重建适配器不可用"]),
    (0x104250, "Error: %1", ["错误：%1"]),
    (0x104260, "Provider returned empty reconstructed response",
     ["模型返回的重建响应为空", "重建响应为空"]),
    (0x1042BC, "The conversation has been halted.", ["对话已中止。", "对话已中止"]),
    # —— 界面上仍残留的英文，沿用同一套逐字节校验，容量不足自动跳过 ——
    (0x10AF28, "ProPilot Chat", ["ProPilot 对话", "对话窗口"]),
    (0x10AF50, "Configure ProPilot", ["配置 ProPilot", "设置"]),
    (0x10CFF0, "ProPilot Configuration", ["ProPilot 设置", "设置"]),
    (0x10B038, "ProPilot is thinking", ["ProPilot 正在思考", "正在思考"]),
    (0x10B000, "Ask ProPilot a question about your design...",
     ["向 ProPilot 提问设计问题…", "输入你的设计问题…"]),
    (0x10B6F8, "Ask ProPilot about your design...",
     ["就设计问题询问 ProPilot…", "询问设计问题…"]),
    (0x113D60, "Proteus AI Assistant", ["Proteus AI 助手", "AI 助手"]),
]

# 主窗口标题：单独一条，文案写死 WINDOW_TITLE
TITLE_FO = 0x10F120
TITLE_ENG = "ProPilot - Electronics Design Assistant"

PH_RE = re.compile(r"%[0-9]+[a-zA-Z]?|%[sd]|\{\{[^}]+\}\}")


def placeholders(s):
    return sorted(PH_RE.findall(s))


def capacity(d, off, n):
    p, extra = off + n, 0
    while p < len(d) and d[p] == 0 and extra < 256:
        p += 1
        extra += 1
    return n + extra - 1


def ui_items():
    """全部汉化条目 = 固定映射 + 写死的主窗口标题。"""
    items = list(UI_MAP)
    items.append((TITLE_FO, TITLE_ENG, [WINDOW_TITLE]))
    return items


def patch_ui(d, title=None):
    # title 参数只为兼容旧调用保留，一律忽略 —— 标题不允许被外部改
    ok = skip = bad = 0
    notes = []
    for off, eng, cands in ui_items():
        eb = eng.encode("ascii")
        if off + len(eb) + 1 > len(d):
            bad += 1
            notes.append("0x%06X 越界" % off)
            continue
        cur = d[off:off + len(eb)]
        if cur != eb or d[off + len(eb)] != 0:
            if any(x >= 0x80 for x in cur):
                skip += 1                      # 已是中文
            else:
                bad += 1                       # 版本不同，跳过但不动字节
                notes.append("0x%06X 未匹配 %r" % (off, bytes(cur[:36])))
            continue
        cap = capacity(d, off, len(eb))
        ph = placeholders(eng)
        pick = next((c for c in cands
                     if len(c.encode("utf-8")) <= cap and placeholders(c) == ph), None)
        if pick is None:
            bad += 1
            notes.append("0x%06X 容量不足 cap=%d" % (off, cap))
            continue
        cb = pick.encode("utf-8")
        d[off:off + cap + 1] = cb + b"\0" * (cap + 1 - len(cb))
        ok += 1
    return ok, skip, bad, notes


# ==========================================================================
#  PE 结构 / 模型 LEA 补丁
# ==========================================================================
def pe_sections(d):
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    n = struct.unpack_from("<H", d, pe + 6)[0]
    osz = struct.unpack_from("<H", d, pe + 20)[0]
    o = pe + 24
    out = []
    for k in range(n):
        s = o + osz + k * 40
        nm = d[s:s + 8].rstrip(b"\0").decode("ascii", "replace")
        vs, va, sz, ptr = struct.unpack_from("<IIII", d, s + 8)
        out.append((nm, va, vs, ptr, sz))
    return out


def o2v(secs, off):
    for nm, va, vs, ptr, sz in secs:
        if ptr <= off < ptr + sz:
            return IMAGE_BASE + va + off - ptr, nm
    return 0, ""


def v2o(secs, va):
    rva = va - IMAGE_BASE
    for nm, sva, vs, ptr, sz in secs:
        if sva <= rva < sva + max(vs, sz):
            return ptr + rva - sva
    return -1


def cstr(d, secs, va):
    o = v2o(secs, va)
    if o < 0:
        return ""
    e = o
    while e < len(d) and d[e] != 0 and e - o < 256:
        e += 1
    return d[o:e].decode("utf-8", "replace")


def patch_model(d, model):
    secs = pe_sections(d)
    lea_va, sec = o2v(secs, LEA_FO)
    if sec != ".text" or d[LEA_FO:LEA_FO + 3] != b"\x48\x8d\x15":
        return 0, "LEA 指令不匹配（版本不同？），跳过模型补丁"
    disp = struct.unpack_from("<i", d, LEA_FO + 3)[0]
    tgt_va = lea_va + 7 + disp
    cur = cstr(d, secs, tgt_va)
    if cur == model:
        return 0, "模型补丁已应用（首项 = %s）" % model

    mb = model.encode("utf-8") + b"\0"
    cur_fo = v2o(secs, tgt_va)
    # 之前本工具打过补丁：LEA 指向我们自己的空闲块，允许整块重写以更换模型。
    # 旧逻辑在这里直接 return，导致「改了模型但 DLL 下拉框还是旧模型」。
    cur_b = cur.encode("utf-8")
    own_block = (cur_fo == STR_FO and bool(cur)
                 and bytes(d[STR_FO:STR_FO + len(cur_b) + 1]) == cur_b + b"\0")
    if cur != "gpt-4o" and not own_block:
        return 0, "LEA 指向 %r（非 gpt-4o 且非本工具写入），跳过模型补丁" % cur

    if own_block:
        p = STR_FO + len(cur_b) + 1
        cap = p - STR_FO
        while p < len(d) and d[p] == 0 and cap < 256:
            p += 1
            cap += 1
        if len(mb) >= cap:
            return 0, "模型名过长（%d >= 可复用 %d），跳过模型补丁" % (len(mb), cap)
        d[STR_FO:STR_FO + cap] = b"\0" * cap      # 清掉旧串尾巴，避免读回旧值
        d[STR_FO:STR_FO + len(mb)] = mb
        where = "复用空闲块 FO=0x%X cap=%d" % (STR_FO, cap)
    else:
        p, cap = STR_FO, 0
        while p < len(d) and d[p] == 0 and cap < 256:
            p += 1
            cap += 1
        if d[STR_FO:STR_FO + len(mb)] != b"\0" * len(mb):
            return 0, "空闲块已被占用，跳过模型补丁"
        if len(mb) >= cap:
            return 0, "模型名过长（%d >= 空闲 %d），跳过模型补丁" % (len(mb), cap)
        str_va, s2 = o2v(secs, STR_FO)
        if not str_va or s2 != ".rdata":
            return 0, "空闲块不在 .rdata，跳过模型补丁"
        d[STR_FO:STR_FO + len(mb)] = mb
        struct.pack_into("<i", d, LEA_FO + 3, str_va - (lea_va + 7))
        where = "新串 FO=0x%X cap=%d" % (STR_FO, cap)

    check = cstr(d, secs, lea_va + 7 + struct.unpack_from("<i", d, LEA_FO + 3)[0])
    if check != model:
        return -1, "模型补丁校验失败，读回 %r" % check
    return 1, "下拉框首项 %r -> %r  (%s)" % (cur, model, where)


def diff_report(a, b):
    runs, start = [], None
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            if start is None:
                start = i
        elif start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(a)))
    # .text 里只允许改 LEA 的 disp32 那 4 字节
    text_end = 0
    for nm, va, vs, ptr, sz in pe_sections(a):
        if nm == ".rdata":
            text_end = ptr
            break
    bad_code = [r for r in runs if r[0] < text_end and
                not (LEA_FO + 3 <= r[0] and r[1] <= LEA_FO + 7)]
    return runs, bad_code


def verify_load():
    """原版 / 补丁版都 LoadLibrary 才算 PE 合法。
    两个都失败视为环境缺少 Qt 依赖，只警告不回滚。"""
    if not os.path.isfile(ORIG):
        return True
    tmp = tempfile.mkdtemp(prefix="ppfix_load_")
    try:
        os.add_dll_directory(HERE)
    except Exception:
        pass
    res = {}
    for name, src in (("orig", ORIG), ("patched", DLL)):
        dst = os.path.join(tmp, "ProPilot_%s.dll" % name)
        shutil.copy(src, dst)
        try:
            ctypes.WinDLL(dst)
            res[name] = True
        except Exception as e:
            res[name] = False
            log("    %-8s LoadLibrary 失败: %s %s" %
                (name, type(e).__name__, str(e)[:110]))
    if res.get("patched"):
        log("    LoadLibrary 对比：原版 OK / 补丁版 OK")
        return True
    if not res.get("orig"):
        log("    [!] 两个版本都加载失败（多为临时目录缺 Qt 依赖），仅警告")
        return True
    log("    [X] 仅补丁版加载失败")
    return False


# ==========================================================================
#  注册表（纯 winreg API，不调用 reg.exe —— 有些机器把它列进黑名单）
# ==========================================================================
HKCU = winreg.HKEY_CURRENT_USER if winreg else None


def reg_path(sub=""):
    return KEY_ROOT + (("\\" + sub) if sub else "")


def _walk(sub, acc):
    try:
        k = winreg.OpenKey(HKCU, reg_path(sub))
    except FileNotFoundError:
        return
    vals, i = [], 0
    while True:
        try:
            n, v, t = winreg.EnumValue(k, i)
        except OSError:
            break
        if isinstance(v, bytes):
            v = {"__b64__": base64.b64encode(v).decode("ascii")}
        vals.append([n, v, t])
        i += 1
    acc[sub] = vals
    subs, j = [], 0
    while True:
        try:
            subs.append(winreg.EnumKey(k, j))
        except OSError:
            break
        j += 1
    winreg.CloseKey(k)
    for s in subs:
        _walk((sub + "\\" + s) if sub else s, acc)


def reg_backup():
    if os.path.isfile(REG_BAK):
        return 0, 0
    acc = {}
    _walk("", acc)
    with open(REG_BAK, "w", encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, indent=1)
    return sum(len(v) for v in acc.values()), len(acc)


def reg_restore():
    if not os.path.isfile(REG_BAK):
        return 0, 0
    with open(REG_BAK, encoding="utf-8") as f:
        acc = json.load(f)
    n = 0
    for sub, vals in acc.items():
        with winreg.CreateKey(HKCU, reg_path(sub)) as k:
            keep = {v[0] for v in vals}
            i, drop = 0, []
            while True:
                try:
                    drop.append(winreg.EnumValue(k, i)[0])
                    i += 1
                except OSError:
                    break
            for name in drop:
                if name not in keep:
                    winreg.DeleteValue(k, name)
            for name, value, typ in vals:
                if isinstance(value, dict) and "__b64__" in value:
                    value = base64.b64decode(value["__b64__"])
                winreg.SetValueEx(k, name, 0, typ, value)
                n += 1
    return n, len(acc)


def reg_get(sub, name):
    try:
        with winreg.OpenKey(HKCU, reg_path(sub), 0, winreg.KEY_READ) as k:
            return winreg.QueryValueEx(k, name)
    except OSError:
        return None, None


def reg_set(sub, name, value, kind=None):
    if kind is None:
        _, kind = reg_get(sub, name)
        kind = kind or winreg.REG_SZ
    with winreg.CreateKeyEx(HKCU, reg_path(sub), 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, kind, value)


def reg_del(sub, name):
    try:
        with winreg.OpenKey(HKCU, reg_path(sub), 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except OSError:
        return False


def reg_fix(cfg, direct=True):
    ep = (direct_base_url(cfg) if direct
          else "http://127.0.0.1:%d" % int(cfg["bridge_port"]))
    pid = int(cfg["provider_id"])
    reg_set(LC, "activeProvider", pid, winreg.REG_DWORD)
    p = LC + (r"\Providers\%d" % pid)
    reg_set(p, "baseUrl", ep)
    reg_set(p, "model", cfg["model"])
    reg_set(p, "apikey", base64.b64encode(cfg["api_key"].encode("utf-8")).decode("ascii"))
    reg_set(p, "temperature", "0.5")
    reg_set(p, "maxTokens", 10000, winreg.REG_DWORD)
    reg_set(p, "enableWebSearch", "false")
    reg_set(p, "enableThinking", "false")
    reg_set(p, "enableStreaming", "false")
    log("    [%d] Custom  -> %s / %s   模式=%s" %
        (pid, ep, cfg["model"], "纯直连(只聊天)" if direct else "后台代理"))
    cur_model, _ = reg_get(p, "model")
    if cur_model in (None, "", "Loading models...", "No Model"):
        reg_set(p, "model", cfg["model"])
        log("    [%d] 模型占位符 %r 已复位为 %s" % (pid, cur_model, cfg["model"]))

    p3 = LC + r"\Providers\3"
    reg_set(p3, "baseUrl", ep)
    reg_set(p3, "model", cfg["model"])
    log("    [3] Ollama 备用槽同步")

    p1 = LC + r"\Providers\1"
    v, _ = reg_get(p1, "apikey")
    if isinstance(v, str) and v.startswith(("http://", "https://")):
        if reg_del(p1, "apikey"):
            log("    [1] 已删除误填在密钥框里的 URL")
    m, _ = reg_get(p1, "model")
    if m in (None, "", "Loading models...", "No Model"):
        reg_set(p1, "model", "gpt-4o-mini")
        log("    [1] 模型占位符已复位")


# ==========================================================================
#  内置兼容代理
# ==========================================================================
UP = {}


def init_bridge(cfg):
    u = urllib.parse.urlsplit(cfg["upstream"])
    https = u.scheme.lower() == "https"
    path = u.path.rstrip("/")
    for suf in ("/chat/completions", "/models"):
        if path.endswith(suf):
            path = path[:-len(suf)]
    if not path:
        path = "/v1"
    UP.update({
        "https": https,
        "host": u.hostname,
        "port": u.port or (443 if https else 80),
        "chat": path + "/chat/completions",
        "models": path + "/models",
        "api_key": cfg["api_key"],
        "model": cfg["model"],
        "force": bool(cfg.get("force_model", True)),
        "extra": int(cfg.get("extra_models", 0)),
        "trust_client": bool(cfg.get("trust_client_key", False)),
        "listen": int(cfg["bridge_port"]),
        "upstream": cfg["upstream"],
    })
    return UP


def direct_base_url(cfg):
    """纯直连：ProPilot 自己会拼 /v1/chat/completions 和 /v1/models，
    所以注册表 baseUrl 必须是「去掉 /v1 的上游根地址」。"""
    u = init_bridge(cfg)
    chat = u["chat"]
    suffix = "/v1/chat/completions"
    prefix = (chat[:-len(suffix)] if chat.endswith(suffix)
              else chat[:-len("/chat/completions")])
    netloc = u["host"]
    if (u["https"] and u["port"] != 443) or (not u["https"] and u["port"] != 80):
        netloc += ":%d" % u["port"]
    return "%s://%s%s" % ("https" if u["https"] else "http",
                          netloc, prefix.rstrip("/"))


def probe_direct(cfg):
    """接口自检：只读，不改 DLL / 注册表。
    按 ProPilot 纯直连的真实拼法（baseUrl + /v1/chat/completions）探测中转站，
    别人换自己的 URL / 密钥 / 模型后，点一下就知道能不能聊。"""
    errs = validate_cfg(cfg)
    if errs:
        for e in errs:
            log("[X] " + e)
        return 1
    base = direct_base_url(cfg)
    u = init_bridge(cfg)
    chat_url = base + "/v1/chat/completions"
    models_url = base + "/v1/models"
    key = str(cfg["api_key"])
    masked = "(空)" if not key else (key[:4] + "***" + key[-2:] if len(key) > 8 else "***")

    log("=" * 74)
    log("接口自检（只读，不改 DLL / 注册表 / 开机自启）")
    log("  你填的上游      : " + str(cfg["upstream"]))
    log("  直连根地址      : " + base)
    log("  ProPilot 聊天   : " + chat_url)
    log("  ProPilot 模型表 : " + models_url)
    log("  模型 / 密钥     : %s / %s" % (u["model"], masked))

    path_warn = False
    raw_path = urllib.parse.urlsplit(str(cfg["upstream"])).path.rstrip("/")
    if raw_path and not raw_path.endswith("/v1") and not raw_path.endswith("/chat/completions"):
        path_warn = True
        log("  [!] 上游路径不是 /v1：DLL 把 /v1/chat/completions 写死了，"
            "纯直连会拼成 %s" % chat_url)
        log("      若中转站要求别的路径，请用「兼容代理模式(手动)」")

    hdr = {"Authorization": "Bearer " + key,
           "Content-Type": "application/json",
           "Accept": "application/json",
           "Accept-Encoding": "identity",
           "User-Agent": "ProPilotFix/1.0"}

    try:
        req = urllib.request.Request(models_url, headers=hdr, method="GET")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            code = r.status
        ids = []
        try:
            ids = [m.get("id") for m in json.loads(raw).get("data", [])
                   if isinstance(m, dict) and m.get("id")]
        except Exception:
            pass
        log("  [1] 模型表 GET  : HTTP %d  模型数=%d  %.2fs" %
            (code, len(ids), time.time() - t0))
        if ids and u["model"] not in ids:
            log("      [!] 模型表里没有 %s；下拉框已写死该模型，聊天仍可能可用" % u["model"])
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (401, 403):
            log("  [1] 模型表 GET  : HTTP %s —— 认证失败，检查 API 密钥" % code)
        elif code in (404, 405, 501):
            log("  [1] 模型表 GET  : HTTP %s  中转站常屏蔽此端点，不影响聊天" % code)
        else:
            log("  [1] 模型表 GET  : %s" % ("HTTP %s" % code if code else repr(e)))
            if not code:
                log("      连不上：检查地址 / 网络 / 系统代理 / 证书")

    body = json.dumps({"model": u["model"],
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 16, "stream": False},
                      ensure_ascii=False).encode("utf-8")
    ok_chat = False
    try:
        req = urllib.request.Request(chat_url, data=body, headers=hdr, method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            code = r.status
        txt = ""
        try:
            j = json.loads(raw)
            txt = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not txt:
                txt = str(j)[:120]
        except Exception:
            txt = raw.decode("utf-8", "replace")[:120]
        log("  [2] 聊天 POST   : HTTP %d  %.2fs  回复=%r" %
            (code, time.time() - t0, txt[:80]))
        ok_chat = True
    except Exception as e:
        code = getattr(e, "code", None)
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:180]
        except Exception:
            pass
        log("  [2] 聊天 POST   : %s  %s" %
            ("HTTP %s" % code if code else repr(e), detail))
        if code in (401, 403):
            log("      -> 401/403：API 密钥不对，或该中转站要额外请求头（纯直连做不到，用兼容代理）")
        elif code == 404:
            log("      -> 404：路径不对。上游地址一般以 /v1 结尾；别的路径用兼容代理模式")
        elif code == 400:
            log("      -> 400：模型名或请求体不被接受，检查「模型名」是不是中转站给的那个")
        elif code == 429:
            log("      -> 429：限流，密钥和地址是对的，稍后再试")
        elif code in (502, 503, 504):
            log("      -> %s：网关/代理错误，检查网络或本机系统代理设置" % code)

    verdict = ("聊天接口可用 —— 点「一键永久修复」，重开 Proteus 9 直接发消息"
               if ok_chat else
               "聊天接口不通 —— 按上面提示改配置；纯直连不行就用「兼容代理模式(手动)」")
    if ok_chat and path_warn:
        verdict += "；但路径非 /v1，若 Proteus 里报 404 请切「兼容代理模式(手动)」"
    log("  结论: " + verdict)
    log("=" * 74)
    return 0 if ok_chat else 1


def bridge_alive(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/__health" % port,
                                    timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def spawn_bridge():
    """手动起独立无窗口代理进程。不写开机自启，重开机后需再点一次。
    --dir 把工作目录钉在 DLL 侧，否则 serve 会去 exe 所在目录找配置、拿到空密钥。"""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "serve", "--no-reg", "--dir", HERE]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "serve",
               "--no-reg", "--dir", HERE]
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, **kw)
        with open(BRIDGE_PID, "w", encoding="utf-8") as f:
            f.write(str(p.pid))
        return True, "后台代理已常驻 PID=%d（关掉本窗口也不会停）" % p.pid
    except Exception as e:
        return False, "后台代理启动失败：%r" % (e,)


def stop_bridge():
    """只结束本工具自己拉起的那个代理，不会误杀 GUI。"""
    try:
        pid = int(open(BRIDGE_PID, encoding="utf-8").read().strip())
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=0x08000000)
        else:
            os.kill(pid, 15)
        os.remove(BRIDGE_PID)
        return True, "已停止后台代理 PID=%d" % pid
    except FileNotFoundError:
        return True, "没有本工具启动的后台代理"
    except Exception as e:
        return False, "停止后台代理失败：%r" % (e,)


def validate_cfg(cfg):
    """配置防呆：别人的上游 / 模型和本机不一定一样，填错就别写 DLL 和注册表。"""
    errs = []
    up = str(cfg.get("upstream", "")).strip()
    if not up:
        errs.append("上游地址不能为空")
    else:
        try:
            if not urllib.parse.urlsplit(up).hostname:
                errs.append("上游地址解析不出主机名：%r" % up)
        except Exception as e:
            errs.append("上游地址无法解析：%r" % (e,))
    if not str(cfg.get("model", "")).strip():
        errs.append("模型名不能为空")
    try:
        p = int(cfg.get("bridge_port", 0))
        if not 1 <= p <= 65535:
            errs.append("代理端口超出 1~65535")
    except Exception:
        errs.append("代理端口必须是数字")
    return errs


def cfg_report(cfg):
    """把「实际生效」的配置打出来对账，专治「配置写了但代码没读」。"""
    u = init_bridge(cfg)
    k = u["api_key"]
    masked = "(空)" if not k else (k[:4] + "***" + k[-2:] if len(k) > 8 else "***")
    log("    上游      : " + u["upstream"])
    log("    转发聊天  : %s:%s%s" % (u["host"], u["port"], u["chat"]))
    log("    转发模型表: " + u["models"])
    log("    模型      : " + u["model"])
    log("    密钥      : %s   来源=%s" %
        (masked, "透传客户端" if u["trust_client"] else "本工具配置"))
    log("    代理端口  : %d   强制模型=%s   provider=%s" %
        (u["listen"], u["force"], cfg.get("provider_id", 4)))
    log("    直连根地址: " + direct_base_url(cfg))
    log("    直连聊天  : " + direct_base_url(cfg) + "/v1/chat/completions")
    log("    窗口标题  : %s（写死，不可改）   汉化条目=%d" %
        (WINDOW_TITLE, len(ui_items())))
    return u


def min_body():
    return json.dumps({"model": UP["model"],
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 16, "stream": False},
                      ensure_ascii=False).encode("utf-8")


def normalize(method, path, body):
    p = path.split("?", 1)[0].rstrip("/") or "/"
    modelish = ("/models", "/v1/models", "/api/tags")
    if method in ("GET", "HEAD") and (p.endswith(modelish) or p == "/"):
        return UP["models"], None
    if p.endswith("/chat/completions"):
        return UP["chat"], ensure_body(body)
    if method in ("GET", "HEAD"):
        return UP["models"], None
    # 测试连接按钮会把 baseUrl 当完整 URL 直接 POST（POST /v1 -> 404）
    return UP["chat"], ensure_body(body)


def ensure_body(body):
    fb = json.loads(min_body().decode("utf-8"))
    if not body:
        j = fb
    else:
        try:
            j = json.loads(body.decode("utf-8"))
        except Exception:
            j = fb
        if not isinstance(j, dict) or "messages" not in j:
            j = fb
    if UP["force"] or not j.get("model"):
        j["model"] = UP["model"]
    return json.dumps(j, ensure_ascii=False).encode("utf-8")


def build_models_payload(raw):
    ids = []
    try:
        up = json.loads(raw.decode("utf-8"))
        ids = [m.get("id") for m in up.get("data", [])
               if isinstance(m, dict) and m.get("id")]
    except Exception:
        pass
    pool = [UP["model"]]
    if UP["extra"] > 0:
        pool += [x for x in ids if x != UP["model"]][:UP["extra"]]
    chosen = []
    for mid in pool:
        if mid and mid not in chosen:
            chosen.append(mid)
    data = [{"id": m, "object": "model", "created": 0, "owned_by": "custom"} for m in chosen]
    models = [{"name": m, "model": m, "modified_at": "", "size": 0,
               "digest": "", "details": {"family": "custom"}} for m in chosen]
    return json.dumps({"object": "list", "data": data, "models": models},
                      ensure_ascii=False).encode("utf-8")


class BridgeServer(ThreadingHTTPServer):
    # Windows 下 allow_reuse_address=True 会让两个进程同时 LISTEN 同一端口，
    # 请求随机分流、日志分裂。关掉后第二个实例 bind 直接失败。
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ProPilotFix/1.0"

    def log_message(self, fmt, *args):
        pass

    def _handle(self, method):
        t0 = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        src = self.path
        dst, fwd = normalize(method, src, body)

        def _m(raw):
            if not raw:
                return "-"
            try:
                return str(json.loads(raw).get("model", "-"))
            except Exception:
                return "-"

        src_m, fwd_m = _m(body), _m(fwd)
        note = ("%s=>%s" % (src_m, fwd_m)) if src_m != fwd_m else fwd_m

        if src.split("?", 1)[0] in ("/__health", "/__ping"):
            raw = json.dumps({"ok": True, "upstream": UP["upstream"],
                              "chat": UP["chat"], "model": UP["model"],
                              "listen_port": UP["listen"]},
                             ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            log("HEALTH -> 200")
            return

        # 密钥以配置为准：ProPilot 注册表里可能残留上一次（或别人）的旧密钥，
        # 直接透传会导致「界面改成自己的接口，请求却仍带旧密钥」-> 401。
        cli_auth = self.headers.get("Authorization")
        if UP.get("trust_client") and cli_auth:
            auth = cli_auth
        else:
            auth = ("Bearer " + UP["api_key"]) if UP["api_key"] else ""
        hdrs = {
            "Content-Type": self.headers.get("Content-Type") or "application/json",
            "Accept": self.headers.get("Accept") or "*/*",
            "Accept-Encoding": "identity",
        }
        if auth:
            hdrs["Authorization"] = auth
        try:
            cls = http.client.HTTPSConnection if UP["https"] else http.client.HTTPConnection
            conn = cls(UP["host"], UP["port"], timeout=180)
            conn.request(method, dst,
                         body=fwd if method not in ("GET", "HEAD") else None, headers=hdrs)
            resp = conn.getresponse()
            status = resp.status
            ctype = resp.getheader("Content-Type") or "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")

            if "event-stream" in ctype.lower():
                self.end_headers()
                total = 0
                while True:
                    chunk = resp.read1(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    total += len(chunk)
                self.close_connection = True
                conn.close()
                log("%-6s %-24s -> %-26s %s  model=%s  SSE %dB  %.2fs" %
                    (method, src, dst, status, note, total, time.time() - t0))
                return

            raw = resp.read()
            conn.close()
            if status >= 400:
                log("      [上游错误] " + raw[:220].decode("utf-8", "replace"))
            extra = ""
            if (method in ("GET", "HEAD") and dst == UP["models"]
                    and 200 <= status < 300):
                try:
                    raw = build_models_payload(raw)
                    extra = "  模型表=双格式 首项=%s" % UP["model"]
                except Exception as e:
                    extra = "  模型表改造失败 %r" % (e,)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(raw)
            self.close_connection = True
            log("%-6s %-24s -> %-26s %s  model=%s  %dB  %.2fs%s" %
                (method, src, dst, status, note, len(raw), time.time() - t0, extra))
        except Exception as e:
            raw = json.dumps({"error": {"message": "桥接上游失败: %r" % (e,),
                                        "type": "bridge_error"}},
                             ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                pass
            log("%-6s %-24s -> %-26s 502  ERR %r" % (method, src, dst, e))

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def selftest(cfg):
    init_bridge(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    hdr = {"Content-Type": "application/json",
           "Authorization": "Bearer " + UP["api_key"]}

    def hit(method, path, body=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        # 空 body 的 POST 必须真的发 POST，不能让 urllib 退化成 GET
        req = urllib.request.Request(base + path, data=data, headers=hdr, method=method)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), time.time() - t0
        except urllib.error.HTTPError as e:
            return e.code, e.read(), time.time() - t0
        except Exception as e:
            return -1, str(e).encode("utf-8"), time.time() - t0

    chat = json.dumps({"model": UP["model"],
                       "messages": [{"role": "user", "content": "只回复两个字：正常"}],
                       "max_tokens": 32, "stream": False})
    sse = json.dumps({"model": UP["model"],
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 16, "stream": True})
    hard = json.dumps({"model": "gpt-4o",
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 16, "stream": False})
    cases = [
        ("GET  模型表 /v1/models", "GET", "/v1/models", None),
        ("GET  模型表 /models", "GET", "/models", None),
        ("GET  模型表 /api/tags", "GET", "/api/tags", None),
        ("POST 正常聊天", "POST", UP["chat"], chat),
        ("POST 根地址(测试连接)", "POST", "/", None),
        ("POST /v1(测试连接)", "POST", "/v1", None),
        ("POST /v1 带脏 body", "POST", "/v1", '{"foo":"bar"}'),
        ("POST 硬编码 gpt-4o", "POST", "/", hard),
        ("POST gpt-4o-mini", "POST", UP["chat"],
         hard.replace("gpt-4o", "gpt-4o-mini")),
        ("POST /v1/models", "POST", "/v1/models", chat),
        ("POST tools 格式", "POST", UP["chat"],
         json.dumps({"model": UP["model"], "messages": [{"role": "user", "content": "列出元件"}],
                     "max_tokens": 64, "stream": False,
                     "tools": [{"type": "function", "function": {
                         "name": "schematic_get_components",
                         "description": "List components",
                         "parameters": {"type": "object", "properties": {}}}}]})),
        ("POST SSE 流式", "POST", UP["chat"], sse),
        ("GET  健康检查", "GET", "/__health", None),
    ]
    log("=" * 78)
    log("端到端自测  上游=%s  模型=%s" % (UP["upstream"], UP["model"]))
    log("=" * 78)
    ok = 0
    for name, method, path, body in cases:
        code, raw, dt = hit(method, path, body)
        extra = ""
        try:
            j = json.loads(raw)
            if "choices" in j:
                msg = j["choices"][0]["message"]
                tc = msg.get("tool_calls")
                tail = ("tool_calls=" + str(tc[0]["function"]["name"])) if tc \
                    else ("回复=" + str(msg.get("content"))[:20])
                extra = "响应model=%s  %s" % (j.get("model", "?"), tail)
            elif "data" in j:
                extra = "模型数=%d 双格式=%s 首项=%s" % (
                    len(j["data"]), "models" in j,
                    j["data"][0]["id"] if j["data"] else "-")
            elif "error" in j:
                extra = str(j["error"])[:60]
        except Exception:
            extra = ("SSE 流=%dB" % len(raw)) if b"data:" in raw \
                else raw[:60].decode("utf-8", "replace")
        good = 200 <= code < 300
        if good and method == "GET" and path in ("/v1/models", "/models", "/api/tags"):
            try:
                j = json.loads(raw)
                good = bool(j.get("data") and j["data"][0].get("id") == UP["model"]
                            and j.get("models") and j["models"][0].get("name") == UP["model"])
            except Exception:
                good = False
        ok += 1 if good else 0
        log("  [%s] %-22s %-28s HTTP %-4s %.2fs  %s" %
            ("OK" if good else "XX", name, path, code, dt, extra))
    log("-" * 78)
    log("  结果 %d/%d %s" % (ok, len(cases), "全部通过" if ok == len(cases) else
                            "存在失败项：检查上游服务是否已启动、密钥与模型名是否正确"))
    srv.shutdown()
    return 0 if ok == len(cases) else 1


def serve(cfg):
    init_bridge(cfg)
    port = UP["listen"]
    log("=" * 74)
    log("ProPilot 兼容代理  127.0.0.1:%d  ->  %s" % (port, UP["upstream"]))
    log("ProPilot 设置里「接口根地址」填： http://127.0.0.1:%d  （不要带 /v1）" % port)
    log("此窗口请保持开着；关掉窗口 = 代理停止")
    log("=" * 74)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/__health" % port, timeout=1) as r:
            if r.status == 200:
                log("[!] 端口 %d 上已有代理在运行，本次不重复启动。" % port)
                return 0
    except Exception:
        pass
    try:
        srv = BridgeServer(("127.0.0.1", port), Handler)
    except OSError as e:
        log("[X] 端口 %d 绑定失败: %r" % (port, e))
        return 1
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        srv.server_close()
    return 0


def probe_upstream(cfg):
    init_bridge(cfg)
    try:
        cls = http.client.HTTPSConnection if UP["https"] else http.client.HTTPConnection
        c = cls(UP["host"], UP["port"], timeout=8)
        c.request("GET", UP["models"],
                  headers={"Authorization": "Bearer " + UP["api_key"],
                           "Accept-Encoding": "identity"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        ids = [m.get("id") for m in json.loads(raw).get("data", [])] if r.status == 200 else []
        hit = UP["model"] in ids
        log("    上游探测: HTTP %s  模型数=%d  目标模型存在=%s" % (r.status, len(ids), hit))
        if not hit and ids:
            log("    [!] 上游模型表里没有 %s，代理会强制纠正 model，一般仍可用" % UP["model"])
        return r.status == 200
    except Exception as e:
        log("    [!] 上游暂时连不上: %r" % (e,))
        log("        DLL 和注册表已永久写好，等服务起来直接开 Proteus 9 就能聊（不用启动本工具）")
        return False


# ==========================================================================
#  命令
# ==========================================================================
def cmd_check(cfg):
    log("=" * 74)
    log(TOOL + " 只读体检  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 74)
    if not os.path.isfile(DLL):
        log("[X] 当前目录找不到 ProPilot.dll： " + DLL)
        log("    请重新选择 ProPilot.dll 所在文件夹（一般是 Proteus 安装目录的 Bin）")
        return 1
    h = sha256(DLL)
    log("  DLL     : " + DLL)
    log("  大小    : %d B" % os.path.getsize(DLL))
    log("  SHA256  : " + h)
    log("  版本    : " + ("原版 9.2.45801.1" if h == ORIG_SHA else "已打补丁 / 未知版本"))
    if h != ORIG_SHA:
        log("  [i] 非原版哈希属正常（重复运行或已修过）；每条写入前都会逐字节校验")
    log("  备份    : " + (ORIG + "  SHA " + sha256(ORIG)[:16] if os.path.isfile(ORIG) else "无"))

    d = open(DLL, "rb").read()
    items = ui_items()
    zh = en = 0
    for off, eng, _ in items:
        cur = d[off:off + len(eng)]
        zh += 1 if any(x >= 0x80 for x in cur) else 0
        en += 0 if any(x >= 0x80 for x in cur) else (1 if cur == eng.encode("ascii") else 0)
    log("  汉化    : 中文 %d / 仍英文 %d / 共 %d 条（含主窗口标题）" % (zh, en, len(items)))
    log("  主标题  : %r" % d[TITLE_FO:TITLE_FO + 64].split(b"\0")[0]
        .decode("utf-8", "replace"))

    secs = pe_sections(d)
    lea_va, sec = o2v(secs, LEA_FO)
    if sec == ".text" and d[LEA_FO:LEA_FO + 3] == b"\x48\x8d\x15":
        disp = struct.unpack_from("<i", d, LEA_FO + 3)[0]
        log("  模型首项: %r  %s" %
            (cstr(d, secs, lea_va + 7 + disp),
             "(已是目标)" if cstr(d, secs, lea_va + 7 + disp) == cfg["model"] else "(仍是 gpt-4o)"))
    else:
        log("  模型首项: [!] LEA 指令不匹配，模型补丁会跳过")

    log("  生效配置（对账用）:")
    cfg_report(cfg)
    for e in validate_cfg(cfg):
        log("  [X] 配置错误：" + e)
    if winreg:
        try:
            log("  注册表  : activeProvider=%s" % reg_get(LC, "activeProvider")[0])
            for pid in (1, 3, 4):
                p = LC + (r"\Providers\%d" % pid)
                log("            [%d] baseUrl=%s  model=%s" %
                    (pid, reg_get(p, "baseUrl")[0], reg_get(p, "model")[0]))
        except Exception as e:
            log("  [!] 注册表读取失败: %r" % (e,))
    return 0


def cmd_fix(cfg, no_reg=False, direct=True):
    log("=" * 74)
    log(TOOL + " 一键修复  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 74)
    if not os.path.isfile(DLL):
        log("[X] 当前目录找不到 ProPilot.dll： " + DLL)
        log("    请在界面上重新选择 ProPilot.dll 所在文件夹，或把本工具放进该目录")
        return 1

    # 0) 配置防呆 + 生效值回显：界面改了这里必须跟着变，否则就是「写了没读」
    errs = validate_cfg(cfg)
    if errs:
        for e in errs:
            log("[X] 配置错误：" + e)
        log("    请在界面上改好上游 / 模型 / 端口再点修复（DLL 未改动）")
        return 1
    log("[0] 生效配置（对账用）")
    cfg_report(cfg)

    # 0b) 可写性检查：Proteus 在跑时 DLL 被占用，且退出时会把旧值写回注册表
    try:
        with open(DLL, "r+b"):
            pass
    except Exception as e:
        log("[X] ProPilot.dll 不可写：%r" % (e,))
        log("    请先【完全退出 Proteus 9】，或右键 FIX.bat 选「以管理员身份运行」")
        return 1

    # 1) 统一备份：优先复用旧脚本留下的原版备份，保证 rollback 能回到真原版
    if not os.path.isfile(ORIG):
        src = DLL
        if os.path.isfile(OLD_BAK) and sha256(OLD_BAK) == ORIG_SHA:
            src = OLD_BAK
            log("[1] 发现旧版脚本的原版备份，用作统一备份源")
        shutil.copy2(src, ORIG)
        log("[1] DLL 已备份 -> " + ORIG)
        log("    SHA256 = " + sha256(ORIG))
        if sha256(ORIG) != ORIG_SHA:
            log("    [!] 备份不是已知原版（可能本机已修过），rollback 会回到当前状态")
    else:
        log("[1] 备份已存在，永不覆盖 -> " + ORIG)

    if not no_reg and winreg:
        if not os.path.isfile(REG_BAK):
            nv, nk = reg_backup()
            log("[2] 注册表已备份 -> %s  (%d 值 / %d 键)" %
                (os.path.basename(REG_BAK), nv, nk))
        else:
            log("[2] 注册表备份已存在")
    else:
        log("[2] 跳过注册表备份（--no-reg 或非 Windows）")

    # 3) 读 DLL -> 全部改动先在内存里做完并校验，再一次性落盘
    data = bytearray(open(DLL, "rb").read())
    before = bytes(data)
    ui_ok, ui_skip, ui_bad, notes = patch_ui(data)
    log("[3] 汉化（含主窗口标题）：新增 %d 条，已是中文 %d 条，跳过 %d 条" %
        (ui_ok, ui_skip, ui_bad))
    for n in notes[:8]:
        log("      " + n)
    if len(notes) > 8:
        log("      ... 另有 %d 条" % (len(notes) - 8))

    md_ok, md_msg = patch_model(data, cfg["model"])
    log("[4] 模型下拉框：" + md_msg)
    if md_ok < 0:
        log("[X] 模型补丁校验失败，已中止（DLL 未改动）")
        return 1

    if bytes(data) != before:
        with open(DLL, "wb") as f:
            f.write(data)
        a, b = open(ORIG, "rb").read(), open(DLL, "rb").read()
        runs, bad_code = diff_report(a, b)
        nbytes = sum(e - s for s, e in runs)
        log("[5] 落盘校验：长度一致=%s  差异区间=%d  改动字节=%d (%.4f%%)" %
            (len(a) == len(b), len(runs), nbytes, 100.0 * nbytes / len(a)))
        log("    PE 头/节表未改=%s   .text 越界改动=%s" %
            (a[:0x400] == b[:0x400], bool(bad_code)))
        log("    新 SHA256 = " + sha256(DLL))
        if len(a) != len(b) or a[:0x400] != b[:0x400] or bad_code:
            log("[X] 结构校验失败，自动回滚")
            shutil.copy2(ORIG, DLL)
            return 1
        if not verify_load():
            log("[X] 加载校验失败，自动回滚")
            shutil.copy2(ORIG, DLL)
            return 1
    else:
        log("[5] DLL 无需改动（补丁已在）")

    # 6) 注册表
    if not no_reg and winreg:
        log("[6] 修注册表")
        reg_fix(cfg, direct=direct)
    else:
        log("[6] 跳过注册表修复")

    # 7) 上游探测
    log("[7] 上游连通性")
    probe_upstream(cfg)

    log("")
    if direct:
        log("修复完成【纯直连 · 只聊天 · 零插件】")
        log("  接口根地址已写成上游真实地址：%s" % direct_base_url(cfg))
        log("  聊天 /v1/chat/completions、模型表 /v1/models 由 ProPilot 自己拼")
        log("  只要上游服务在跑，开机直接开 Proteus 9 就能聊，不用启动任何工具")
        log("  注意：「测试连接」按钮会失败（它把根地址当完整 URL POST）——不用点它")
        log("  老上游不兼容时，点「兼容代理模式」手动起一次代理（不自启）")
    else:
        log("修复完成【兼容代理 · 手动，不开机自启】")
        log("  1) 代理是独立无窗口进程，关掉本工具也不停；但不会开机自启，重开机要再点一次")
        log("  2) 完全退出 Proteus 9 再重开 —— 不重启不会加载新 DLL / 新配置")
        log("  3) 设置里确认「接口根地址」= http://127.0.0.1:%d ，点「测试连接」" %
            int(cfg["bridge_port"]))
        log("  验收看日志里的  model=gpt-4o=>%s  与  模型表=双格式" % cfg["model"])
    log("  改坏了：在界面点「一键还原」/ rollback 命令")
    return 0


def cmd_rollback(no_reg=False):
    log("=" * 74)
    log("回滚  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 74)
    try:
        _, msg = stop_bridge()
        log("[0] " + msg)
    except Exception as e:
        log("[!] 停止后台代理时出错：%r" % (e,))
    if os.path.isfile(ORIG):
        shutil.copy2(ORIG, DLL)
        log("[1] DLL 已还原  SHA=" + sha256(DLL))
    else:
        log("[!] 无 DLL 备份，跳过")
    if not no_reg and winreg and os.path.isfile(REG_BAK):
        try:
            nv, nk = reg_restore()
            log("[2] 注册表已还原：%d 值 / %d 键" % (nv, nk))
        except Exception as e:
            log("[2] 注册表还原失败: %r" % (e,))
    else:
        log("[2] 无注册表备份或已跳过")
    log("[i] 已停止本工具起的代理；ProPilot 接口根地址已随注册表一并还原")
    return 0


def main():
    args = sys.argv[1:]
    no_reg = "--no-reg" in args
    args = [a for a in args if a != "--no-reg"]
    # 独立 serve 进程靠 --dir 把工作目录钉在 DLL 侧，否则会去 exe 目录读配置
    if "--dir" in args:
        i = args.index("--dir")
        if i + 1 < len(args):
            set_work_dir(args[i + 1])
            del args[i:i + 2]
        else:
            args.pop(i)
    cmd = (args[0] if args else "fix").lower()
    log("\n### %s  %s  %s ###" % (TOOL, cmd, time.strftime("%Y-%m-%d %H:%M:%S")))

    # 只在双击 FIX.bat（fix）或直接 serve 且是交互终端时问配置，
    # 避免脚本化调用 / 沙箱自测卡在 input() 上
    interactive = (not no_reg) and sys.stdin.isatty() and cmd in ("fix", "serve")
    if cmd in ("serve", "test"):
        cfg = load_cfg(interactive=interactive)
        return serve(cfg) if cmd == "serve" else selftest(cfg)
    if cmd == "rollback":
        return cmd_rollback(no_reg)
    cfg = load_cfg(interactive=interactive)
    if cmd == "check":
        return cmd_check(cfg)
    if cmd == "probe":
        log("接口自检（只读，不改任何东西）")
        return probe_direct(cfg)
    if cmd == "cfgcheck":
        log("生效配置自检（只读，不改任何东西）")
        cfg_report(cfg)
        errs = validate_cfg(cfg)
        for e in errs:
            log("[X] " + e)
        return 1 if errs else 0
    if cmd == "fix":
        return cmd_fix(cfg, no_reg)
    if cmd == "fixreg":
        if not winreg:
            log("[X] 仅 Windows 支持")
            return 1
        reg_backup()
        log("只修注册表模型值与接口根地址")
        reg_fix(cfg)
        return 0
    log("未知命令: " + cmd)
    log("可用: fix / probe / serve / check / cfgcheck / test / fixreg / rollback")
    return 2


if __name__ == "__main__":
    sys.exit(main())
