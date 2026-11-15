# -*- coding: utf-8 -*-
"""
ProPilotFix —— ProPilot 9.2 自定义接口 / 汉化 一键永久修复（只聊天 · 一条路）

干什么：让 Proteus 9 自带的 ProPilot AI 助手用自己的 OpenAI 兼容接口正常聊天。
  · 改 ProPilot.dll：汉化 64 条界面 + 主窗口标题 + 模型下拉框首项
    （并把被旧版改坏的 URL 后缀自愈回 /v1/chat/completions）
  · 写注册表：接口根地址（填 .../v1 会自动归成根）、密钥、模型一并写好
  · 之后 ProPilot 直接请求你自己的上游：零常驻进程、零开机自启、只聊天

URL 是怎么拼的（真机实测）：ProPilot 固定用
      baseUrl + "/v1/chat/completions"
所以注册表里必须存「根地址」。你填 http://127.0.0.1:8000/v1，工具存
http://127.0.0.1:8000，ProPilot 实际请求 http://127.0.0.1:8000/v1/chat/completions —— 正好是你想聊的那个地址。
（曾试过「改 DLL 后缀 + 原样存 /v1」，真机结果是聊天打到根路径报 405，已废弃并自动纠回。）

命令（命令行版；日常推荐 GUI：ProPilotFixGUI.py）：
  fix       一键永久修复（默认）
  probe     接口自检：只读探测自己的地址能不能聊
  check     只读体检（DLL / 汉化 / 后缀 / 模型 / 注册表）
  cfgcheck  打印实际生效配置
  fixreg    只写注册表
  rollback  一键还原 DLL + 注册表
  --no-reg  任意命令后附加：跳过注册表读写（测试用）
  --dir X   指定 ProPilot.dll 所在目录

备份：ProPilot.dll.orig（只建一次，永不覆盖）+ ProPilotFix_registry.json
日志：ProPilotFix.log
配置：ProPilotFix.json（含密钥，切勿提交到 Git）
"""
import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import winreg
except ImportError:
    winreg = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOL = "ProPilotFix 3.2"

# 主窗口标题：写死在程序里，界面、配置文件、命令行都改不了
WINDOW_TITLE = "ProPilotFix"

# 设置对话框标题（原 "ProPilot Configuration"）：固定槽位只有 24 字节（含结尾 0），
# 所以签名用按容量择优的候选表，第一条放最完整的写法。
BRAND_TITLE_FO = 0x10CFF0
BRAND_TITLE_ENG = "ProPilot Configuration"
BRAND_TITLES = [
    "设置 · NoYa修复版",     # 23 字节，正好放满
    "设置 NoYa修复版",       # 21
    "NoYa修复版",            # 13
    "ProPilot 设置",         # 15（保底，老版本容量不足时用）
]

# ==========================================================================
#  配置 —— 换环境改这里，或改同目录的 ProPilotFix.json
# ==========================================================================
DEFAULT_CFG = {
    # 全部留空 —— 工具不需要你填任何东西。
    # 地址/密钥/模型都去 ProPilot「大模型服务」里自己填，工具只修 DLL。
    # 想用命令行批量写注册表时，才需要把下面填上。
    "upstream": "",
    "api_key": "",
    "model": "",
    "provider_id": 4,           # 4 = Custom（自定义）
}

ORIG_SHA = "35be5f09eca913947e512c2b3d6fb8803b7ad9e3c8d4a65c2399f946354f5414"
REG_PRODUCT = "Proteus 9 Professional"   # 装的是别的版本就改这一行
IMAGE_BASE = 0x180000000
LEA_FO = 0x7C289        # FUN_18007ce60 首条 LEA RDX,[rip+disp32]
STR_FO = 0x171182       # .rdata 内 110 字节全 0 空闲块

# URL 里会出现的字符：出现这些说明这个 /v1/... 片段是「完整 URL 模板」的一部分，
# 不是 Custom 的裸后缀，绝不改它。
URL_CHARS = frozenset(b"abcdefghijklmnopqrstuvwxyz"
                      b"ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      b"0123456789.-:/_")


# ==========================================================================
#  路径：全部相对脚本自身
# ==========================================================================
def base_dir():
    override = os.environ.get("PROPILOTFIX_DIR")
    if override and os.path.isdir(override):
        return override
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def set_work_dir(path):
    """把工作目录切到 ProPilot.dll 所在文件夹。
    DLL / 备份 / 配置 / 日志全部跟着走，GUI 版靠它支持「工具放任意目录」。"""
    global HERE, DLL, ORIG, OLD_BAK, REG_BAK, CFG_FILE, LOG
    HERE = os.path.abspath(path)
    DLL = os.path.join(HERE, "ProPilot.dll")
    ORIG = os.path.join(HERE, "ProPilot.dll.orig")
    OLD_BAK = os.path.join(HERE, "ProPilot.dll.fz_bak")
    REG_BAK = os.path.join(HERE, "ProPilotFix_registry.json")
    CFG_FILE = os.path.join(HERE, "ProPilotFix.json")
    LOG = os.path.join(HERE, "ProPilotFix.log")
    return HERE


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
    # 不再交互提问：工具不需要任何配置，地址/密钥/模型都在 ProPilot 里填。
    save_cfg(c)
    log("[i] 配置已写入 " + CFG_FILE + "（全空即可，一般不用动）")
    return c


def norm_cfg(cfg):
    """GUI 传的是空 dict，这里统一补齐所有键，避免 cfg["model"] 这类直取 KeyError。"""
    c = dict(DEFAULT_CFG)
    if isinstance(cfg, dict):
        c.update(cfg)
    c.pop("window_title", None)     # 标题写死，旧配置残留也不生效
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


def slot_cap(d, off):
    """不依赖原串长度：从 off 起读到第一个 0，再把后面的 0 也算成可用空间。"""
    n = 0
    while off + n < len(d) and d[off + n] != 0:
        n += 1
    extra, p = 0, off + n
    while p < len(d) and d[p] == 0 and extra < 256:
        p += 1
        extra += 1
    return n + extra - 1


def ui_items():
    items = list(UI_MAP)
    items.append((TITLE_FO, TITLE_ENG, [WINDOW_TITLE]))
    return items


def patch_ui(d, title=None):
    # title 参数只为兼容旧调用保留，一律忽略 —— 标题不允许被外部改
    ok = skip = bad = 0
    notes = []
    items = ui_items()
    # ① 主窗口标题：强制写死为 WINDOW_TITLE（不管当前是英文、旧中文还是别的）
    t_off, t_eng, _ = items[-1]
    t_cap = capacity(d, t_off, len(t_eng))
    t_new = WINDOW_TITLE.encode("utf-8")
    t_cur = d[t_off:t_off + t_cap + 1].split(b"\0")[0]
    if t_cur == t_new:
        skip += 1
    elif len(t_new) <= t_cap:
        d[t_off:t_off + t_cap + 1] = t_new + b"\0" * (t_cap + 1 - len(t_new))
        ok += 1
        notes.append("主标题 0x%06X: %r -> %r" %
                     (t_off, t_cur.decode("utf-8", "replace"), WINDOW_TITLE))
    else:
        bad += 1
        notes.append("主标题容量不足 cap=%d" % t_cap)

    # ①b 设置对话框标题：追加签名 "NoYa 修复版"（槽位固定，按容量择优）
    b_cap = slot_cap(d, BRAND_TITLE_FO)
    b_cur = d[BRAND_TITLE_FO:BRAND_TITLE_FO + b_cap + 1].split(b"\0")[0]
    b_pick = next((c.encode("utf-8") for c in BRAND_TITLES
                   if len(c.encode("utf-8")) <= b_cap), None)
    b_known = bool(b_cur) and (b_cur.startswith(b"ProPilot")
                               or any(x >= 0x80 for x in b_cur))
    if b_pick is None:
        bad += 1
        notes.append("设置标题容量不足 cap=%d" % b_cap)
    elif b_cur == b_pick:
        skip += 1
    elif b_known:
        d[BRAND_TITLE_FO:BRAND_TITLE_FO + b_cap + 1] = \
            b_pick + b"\0" * (b_cap + 1 - len(b_pick))
        ok += 1
        notes.append("设置标题 0x%06X: %r -> %r" %
                     (BRAND_TITLE_FO, b_cur.decode("utf-8", "replace"),
                      b_pick.decode("utf-8")))
    else:
        bad += 1
        notes.append("设置标题 0x%06X 未匹配 %r" % (BRAND_TITLE_FO, bytes(b_cur[:24])))

    for off, eng, cands in items[:-1]:
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


# ==========================================================================
#  ② 地址后缀自愈：把被旧版改坏的裸后缀纠回带 /v1 的原样
#  Custom 用的裸后缀 /chat/completions -> /v1/chat/completions
#                  /models          -> /v1/models
#  只动裸后缀，内置服务商（OpenAI/DeepSeek/xAI…）的完整 URL 模板一律不碰。
# ==========================================================================
def _bare_suffix(d, old, new):
    """返回 Custom 当前用的裸后缀：独立 C 串，前一个字节是字符串边界（不是 URL 字符）。"""
    for cand in (old, new):
        j = 0
        while True:
            j = d.find(cand, j)
            if j < 0:
                break
            if j == 0 or d[j - 1] not in URL_CHARS:
                return cand.decode()
            j += 1
    return "?"


def custom_chat_suffix(d):
    return _bare_suffix(d, b"/v1/chat/completions", b"/chat/completions")


def custom_models_suffix(d):
    return _bare_suffix(d, b"/v1/models", b"/models")


def patch_endpoint(d):
    """把 Custom 的裸后缀纠正回 /v1/chat/completions 与 /v1/models，返回 (改动数, 说明)。

    为什么要纠回：ProPilot 真实聊天时是「baseUrl(根) + /v1/chat/completions」，
    裸后缀必须自带 /v1。早期版本把后缀剥成 /chat/completions 是错的，
    会导致聊天实际打到 baseUrl + 空路径 -> 405「资源仅支持 GET 或 HEAD」。
    本函数同时充当自愈：已经改坏的 DLL 重跑一次就恢复正常。
    """
    changed, notes = 0, []
    for bare, full, label in ((b"/chat/completions", b"/v1/chat/completions", "聊天"),
                              (b"/models", b"/v1/models", "模型表")):
        j = 0
        while True:
            j = d.find(bare, j)
            if j < 0:
                break
            prev = d[j - 1] if j else 0
            # 前面紧贴 URL 字符说明是完整模板的一部分（https://api.x.ai/...），跳过
            if j and prev in URL_CHARS:
                j += 1
                continue
            span = len(full) + 1
            if d[j:j + span] == full + b"\0":
                # 已经是 /v1/xxx，跳过（幂等）
                j += len(full)
                continue
            if d[j:j + len(bare)] != bare or d[j + len(bare):j + span].strip(b"\0"):
                j += 1
                continue                 # 原槽位放不下，不乱写
            d[j:j + span] = full + b"\0"
            changed += 1
            notes.append("%s裸后缀 0x%X: %s -> %s" %
                         (label, j, bare.decode(), full.decode()))
            j += len(full)
    return changed, notes


# ==========================================================================
#  ③ 真凶补丁：Custom 服务商类把 baseUrl 原样当请求地址，从不拼 /v1/chat/completions
#
#  逆向证据（ProPilot.dll 9.2，.rdata 里的 vtable）：
#    Custom 类的 URL 构造 = 0x180086AE0：QString(baseUrl) -> QUrl -> req.setUrl()
#        结尾就是 setUrl，不加任何路径 -> POST http://host/  -> 405「资源仅支持 GET 或 HEAD」
#    标准 OpenAI 兼容类 = 0x180085070：
#        先 0x180084F00 归一（去掉末尾 '/'），再 append "/v1/chat/completions"，再 setUrl()
#    两者签名完全一致 (rcx=this, rdx=QNetworkRequest*, r8=provider)，
#    差别只在 vtable 里的一个函数指针 -> 把 Custom 的槽位指向标准实现即可。
#
#  同一张 vtable 里，模型表构造 0x180086CA0 只拼 "/models"（缺 /v1），
#  也一并换成标准实现 0x180085370（拼 "/v1/models"）。
#
#  这是「让用户自己在 ProPilot 里填地址就能连」的关键：不再依赖任何外部配置。
# ==========================================================================
VTB_CUSTOM_CHAT = 0x180086AE0      # Custom: url = baseUrl（bug 源头）
VTB_STD_CHAT = 0x180085070         # 标准: url = baseUrl + /v1/chat/completions
VTB_CUSTOM_MODELS = 0x180086CA0    # Custom: url = baseUrl + /models
VTB_STD_MODELS = 0x180085370       # 标准: url = baseUrl + /v1/models
VTB_CUSTOM_MODEL_NAME = 0x180086C50   # 同表特征：默认模型名 "custom-model"
VTB_MODELS_NEIGHBOR = 0x18007CFB0     # 模型表槽位的下一个条目（用于二次校验）


def _q(d, off):
    return struct.unpack_from("<Q", d, off)[0]


def _put_q(d, off, val):
    struct.pack_into("<Q", d, off, val)


def patch_provider_class(d):
    """把 Custom 服务商的 URL 构造函数换成标准实现，返回 (改动数, 说明列表)。

    靠 vtable 特征定位，不写死文件偏移，幂等：
      · chat 槽特征：值为 0x180086AE0，且 +0x38 处是 0x180086C50（同表的 custom-model）
      · models 槽特征：值为 0x180086CA0，且下一项是 0x18007CFB0
    """
    changed, notes = 0, []
    done_chat = done_models = False

    # ---- chat 槽 ----
    pat = struct.pack("<Q", VTB_CUSTOM_CHAT)
    i = 0
    while True:
        i = d.find(pat, i)
        if i < 0:
            break
        if i + 0x38 + 8 <= len(d) and _q(d, i + 0x38) == VTB_CUSTOM_MODEL_NAME:
            _put_q(d, i, VTB_STD_CHAT)
            changed += 1
            done_chat = True
            notes.append("URL 构造 vtable[chat] 0x%X: 原样拼 -> +/v1/chat/completions" % i)
        i += 8

    # ---- models 槽 ----
    pat = struct.pack("<Q", VTB_CUSTOM_MODELS)
    i = 0
    while True:
        i = d.find(pat, i)
        if i < 0:
            break
        if i + 16 <= len(d) and _q(d, i + 8) == VTB_MODELS_NEIGHBOR:
            _put_q(d, i, VTB_STD_MODELS)
            changed += 1
            done_models = True
            notes.append("URL 构造 vtable[models] 0x%X: +/models -> +/v1/models" % i)
        i += 8

    if not done_chat and b"".join(struct.pack("<Q", VTB_STD_CHAT)) in d:
        notes.append("chat 槽已是标准实现（补丁已在）")
    if not done_models and b"".join(struct.pack("<Q", VTB_STD_MODELS)) in d:
        notes.append("models 槽已是标准实现（补丁已在）")
    return changed, notes


def provider_class_ok(d):
    """校验：Custom vtable 的 chat 槽是否已指向标准实现。"""
    pat = struct.pack("<Q", VTB_STD_CHAT)
    i = 0
    while True:
        i = d.find(pat, i)
        if i < 0:
            return False
        if i + 0x38 + 8 <= len(d) and _q(d, i + 0x38) == VTB_CUSTOM_MODEL_NAME:
            return True
        i += 8


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


# ==========================================================================
#  地址：注册表存「根地址」，ProPilot 自己拼 /v1/chat/completions
# ==========================================================================
def upstream_base(cfg):
    """把用户填的地址归到「根地址」（不带 /v1）。

    ProPilot 真实请求 = baseUrl + "/v1/chat/completions"（硬约束，改不了），
    所以 baseUrl 必须是根地址。
      填 http://127.0.0.1:8000/v1        -> 存 http://127.0.0.1:8000
      填 http://127.0.0.1:8000           -> 存 http://127.0.0.1:8000
      填 http://127.0.0.1:8000/v1/chat/completions -> 存 http://127.0.0.1:8000
    """
    u = str(cfg.get("upstream", "")).strip().rstrip("/")
    for suf in ("/chat/completions", "/v1/models", "/models"):
        if u.endswith(suf):
            u = u[:-len(suf)]
    if u.endswith("/v1"):
        u = u[:-3]
    return u.rstrip("/")


def chat_url(cfg):
    return upstream_base(cfg) + "/v1/chat/completions"


def models_url(cfg):
    return upstream_base(cfg) + "/v1/models"


def _mask(key):
    k = str(key or "")
    return "(空)" if not k else (k[:4] + "***" + k[-2:] if len(k) > 8 else "***")


def reg_fix(cfg):
    ep = upstream_base(cfg)
    pid = int(cfg.get("provider_id", 4))
    reg_set(LC, "activeProvider", pid, winreg.REG_DWORD)
    p = LC + (r"\Providers\%d" % pid)
    reg_set(p, "baseUrl", ep)                  # ★ 根地址：ProPilot 自己会补 /v1/chat/completions
    reg_set(p, "model", cfg["model"])
    reg_set(p, "apikey",
            base64.b64encode(str(cfg.get("api_key", "")).encode("utf-8")).decode("ascii"))
    reg_set(p, "temperature", "0.5")
    reg_set(p, "maxTokens", 10000, winreg.REG_DWORD)
    reg_set(p, "enableWebSearch", "false")
    reg_set(p, "enableThinking", "false")
    reg_set(p, "enableStreaming", "false")
    log("    [%d] 接口根地址 -> %s   （你填 %s）" % (pid, ep, cfg.get("upstream")))
    log("    [%d] 模型 -> %s   密钥 -> %s   只聊天(Web/Thinking/Stream=关)" %
        (pid, cfg["model"], _mask(cfg.get("api_key"))))
    cur_model, _ = reg_get(p, "model")
    if cur_model in (None, "", "Loading models...", "No Model"):
        reg_set(p, "model", cfg["model"])
        log("    [%d] 模型占位符 %r 已复位为 %s" % (pid, cur_model, cfg["model"]))


def reg_normalize(pid=4):
    """只把 Custom 的 baseUrl 归一成「根地址」，其余值一个字都不动。

    为什么：补丁后 ProPilot 请求 = baseUrl + "/v1/chat/completions"，
    所以 baseUrl 必须是根地址。用户以前填过 .../v1 的，这里帮他改回根，避免 /v1/v1。
    """
    if not winreg:
        return
    p = LC + (r"\Providers\%d" % pid)
    cur, _ = reg_get(p, "baseUrl")
    if not cur:
        log("    [%d] 还没有地址 —— 打开 ProPilot「大模型服务」自己填即可" % pid)
        return
    new = upstream_base({"upstream": cur})
    if new == cur:
        log("    [%d] 地址已是根地址：%s" % (pid, cur))
        return
    reg_set(p, "baseUrl", new)
    log("    [%d] 地址归一：%s  ->  %s" % (pid, cur, new))


# ==========================================================================
def validate_cfg(cfg):
    errs = []
    up = str(cfg.get("upstream", "")).strip()
    if up:
        try:
            if not urllib.parse.urlsplit(up).hostname:
                errs.append("接口地址解析不出主机名：%r" % up)
        except Exception as e:
            errs.append("接口地址无法解析：%r" % (e,))
    return errs


def cfg_report(cfg):
    """把「实际生效」的配置打出来对账，专治「配置写了但代码没读」。
    cfg 里 upstream 为空时（GUI 常态）回落到注册表当前值，保证打印的是真实生效值。"""
    up = str(cfg.get("upstream", "")).strip()
    src = "命令行/配置"
    if not up and winreg:
        try:
            up = reg_get(LC + (r"\Providers\%d" % int(cfg.get("provider_id", 4))), "baseUrl")[0] or ""
            src = "ProPilot 注册表"
        except Exception:
            up = ""
    eff = dict(cfg)
    eff["upstream"] = up
    log("    地址来源   : " + (src if up else "（空，等你在 ProPilot 里填）"))
    log("    你填的地址 : " + up)
    log("    注册表存   : " + (upstream_base(eff) or "-") + "   （根地址）")
    log("    实际请求   : " + (chat_url(eff) if up else "-"))
    log("    模型表     : " + (models_url(eff) if up else "-"))
    log("    模型       : " + str(cfg.get("model", "") or
                               (reg_get(LC + (r"\Providers\%d" % int(cfg.get("provider_id", 4))), "model")[0]
                                if winreg else "")))
    log("    密钥       : " + _mask(cfg.get("api_key")))
    log("    窗口标题   : %s（写死，不可改）   汉化条目=%d" %
        (WINDOW_TITLE, len(ui_items())))


def probe_direct(cfg):
    """接口自检：只读，不改 DLL / 注册表。按「地址 + /chat/completions」真实探测。"""
    errs = validate_cfg(cfg)
    if errs:
        for e in errs:
            log("[X] " + e)
        return 1
    cu, mu = chat_url(cfg), models_url(cfg)
    key = str(cfg.get("api_key", ""))

    log("=" * 70)
    log("接口自检（只读，不改任何东西）")
    log("  实际请求 : " + cu)
    log("  模型表   : " + mu)
    log("  模型/密钥: %s / %s" % (cfg.get("model"), _mask(key)))

    hdr = {"Authorization": "Bearer " + key,
           "Content-Type": "application/json",
           "Accept": "application/json",
           "Accept-Encoding": "identity",
           "User-Agent": "ProPilotFix/2.0"}

    try:
        req = urllib.request.Request(mu, headers=hdr, method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            code = r.status
        ids = []
        try:
            ids = [m.get("id") for m in json.loads(raw).get("data", [])
                   if isinstance(m, dict) and m.get("id")]
        except Exception:
            pass
        log("  [1] 模型表 GET : HTTP %d  模型数=%d" % (code, len(ids)))
        if ids and cfg.get("model") not in ids:
            log("      [!] 模型表里没有 %s；已写死该模型，聊天仍可能可用" % cfg.get("model"))
    except Exception as e:
        code = getattr(e, "code", None)
        log("  [1] 模型表 GET : %s" % (("HTTP %s" % code) if code else repr(e)))

    body = json.dumps({"model": cfg.get("model"),
                       "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 16, "stream": False},
                      ensure_ascii=False).encode("utf-8")
    ok_chat = False
    try:
        req = urllib.request.Request(cu, data=body, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            code = r.status
        txt = ""
        try:
            j = json.loads(raw)
            txt = (j.get("choices") or [{}])[0].get("message", {}).get("content", "") or str(j)[:100]
        except Exception:
            txt = raw.decode("utf-8", "replace")[:100]
        log("  [2] 聊天 POST  : HTTP %d  回复=%r" % (code, txt[:70]))
        ok_chat = True
    except Exception as e:
        code = getattr(e, "code", None)
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        log("  [2] 聊天 POST  : %s  %s" %
            (("HTTP %s" % code) if code else repr(e), detail))
        if code in (401, 403):
            log("      -> 密钥不对，或该服务要额外请求头")
        elif code == 404:
            log("      -> 404：地址不对，一般要填到 /v1 为止")
        elif code == 400:
            log("      -> 400：模型名或请求体不被接受，检查模型名")
        elif code == 429:
            log("      -> 429：限流，密钥和地址是对的，稍后再试")
        elif code in (502, 503, 504):
            log("      -> %s：网关/网络错误，检查网络或系统代理" % code)

    log("  结论: " + ("聊天接口可用 —— 点「一键永久修复」" if ok_chat
                    else "聊天接口不通 —— 按上面提示改地址 / 密钥 / 模型"))
    log("=" * 70)
    return 0 if ok_chat else 1


def probe_upstream(cfg):
    """修复后快速看一眼上游通不通（只探测，不阻断修复）。"""
    try:
        req = urllib.request.Request(
            models_url(cfg),
            headers={"Authorization": "Bearer " + str(cfg.get("api_key", "")),
                     "Accept-Encoding": "identity"},
            method="GET")
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read()
            code = r.status
        ids = []
        try:
            ids = [m.get("id") for m in json.loads(raw).get("data", [])]
        except Exception:
            pass
        log("    上游探测: HTTP %s  模型数=%d  目标模型存在=%s" %
            (code, len(ids), cfg.get("model") in ids))
        return code == 200
    except Exception as e:
        code = getattr(e, "code", None)
        log("    [!] 上游暂时连不上: %s（地址/密钥/服务是否在跑）" %
            (("HTTP %s" % code) if code else repr(e)))
        log("        DLL 和注册表已永久写好，等服务起来直接开 Proteus 9 就能聊")
        return False


# ==========================================================================
#  命令
# ==========================================================================
def cmd_check(cfg):
    cfg = norm_cfg(cfg)
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
    _cs, _ms = custom_chat_suffix(d), custom_models_suffix(d)
    log("  Custom 后缀: 聊天 %s / 模型表 %s   %s" %
        (_cs, _ms, "OK" if (_cs, _ms) == ("/v1/chat/completions", "/v1/models")
         else "[!] 异常，重跑一次「一键修复」会自愈"))
    log("  Custom URL 构造: %s   %s" %
        ("标准(补 /v1/chat/completions)" if provider_class_ok(d) else "原版(地址原样发 -> 405)",
         "OK" if provider_class_ok(d) else "[!] 未修，点「一键修复」"))

    secs = pe_sections(d)
    tgt_model = str(cfg.get("model") or "").strip()
    if not tgt_model and winreg:
        try:
            cur_m = reg_get(LC + (r"\Providers\%d" % int(cfg.get("provider_id", 4))), "model")[0]
            if cur_m and cur_m not in ("Loading models...", "No Model"):
                tgt_model = cur_m
        except Exception:
            pass
    lea_va, sec = o2v(secs, LEA_FO)
    if sec == ".text" and d[LEA_FO:LEA_FO + 3] == b"\x48\x8d\x15":
        disp = struct.unpack_from("<i", d, LEA_FO + 3)[0]
        cur_lea = cstr(d, secs, lea_va + 7 + disp)
        if not tgt_model:
            tail = "(注册表里还没模型，去 ProPilot 里选一次即可)"
        elif cur_lea == tgt_model:
            tail = "(已是目标)"
        else:
            tail = "(仍是旧值，点「一键修复」写成本机的模型)"
        log("  模型首项: %r  %s" % (cur_lea, tail))
    else:
        log("  模型首项: [!] LEA 指令不匹配，模型补丁会跳过")

    log("  生效配置（对账用）:")
    cfg_report(cfg)
    for e in validate_cfg(cfg):
        log("  [X] 配置错误：" + e)
    if winreg:
        try:
            log("  注册表  : activeProvider=%s" % reg_get(LC, "activeProvider")[0])
            p = LC + (r"\Providers\%d" % int(cfg.get("provider_id", 4)))
            log("            [%d] baseUrl=%s  model=%s" %
                (int(cfg.get("provider_id", 4)),
                 reg_get(p, "baseUrl")[0], reg_get(p, "model")[0]))
        except Exception as e:
            log("  [!] 注册表读取失败: %r" % (e,))
    return 0


def cmd_fix(cfg, no_reg=False):
    cfg = norm_cfg(cfg)
    log("=" * 74)
    log(TOOL + " 一键修复  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 74)
    if not os.path.isfile(DLL):
        log("[X] 当前目录找不到 ProPilot.dll： " + DLL)
        log("    请在界面上重新选择 ProPilot.dll 所在文件夹，或把本工具放进该目录")
        return 1

    errs = validate_cfg(cfg)
    if errs:
        for e in errs:
            log("[X] 配置错误：" + e)
        log("    DLL 未改动")
        return 1

    try:
        with open(DLL, "r+b"):
            pass
    except Exception as e:
        log("[X] ProPilot.dll 不可写：%r" % (e,))
        log("    请先【完全退出 Proteus 9】，或右键本程序选「以管理员身份运行」")
        return 1

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

    data = bytearray(open(DLL, "rb").read())
    before = bytes(data)

    ep_ok, ep_notes = patch_endpoint(data)
    if ep_ok:
        log("[3] 地址后缀纠正：改 %d 处（恢复成 /v1/chat/completions）" % ep_ok)
        for n in ep_notes:
            log("      " + n)
    else:
        log("[3] 地址后缀正常（/v1/chat/completions 已在，无需改动）")

    vtb_ok, vtb_notes = patch_provider_class(data)
    if vtb_ok:
        log("[3b] 自定义服务商 URL 构造：改 %d 处（原版 bug：地址原样发，不拼路径）" % vtb_ok)
    else:
        log("[3b] 自定义服务商 URL 构造：补丁已在")
    for n in vtb_notes:
        log("      " + n)

    ui_ok, ui_skip, ui_bad, notes = patch_ui(data)
    log("[4] 汉化（含主窗口标题）：新增 %d 条，已是中文 %d 条，跳过 %d 条" %
        (ui_ok, ui_skip, ui_bad))
    for n in notes[:8]:
        log("      " + n)
    if len(notes) > 8:
        log("      ... 另有 %d 条" % (len(notes) - 8))

    model = str(cfg.get("model", "")).strip()
    if not model and winreg:
        cur, _ = reg_get(LC + (r"\Providers\%d" % int(cfg.get("provider_id", 4))), "model")
        if cur and cur not in ("Loading models...", "No Model"):
            model = cur
            log("[0] 模型名取自注册表当前值：%s" % model)
    if model:
        md_ok, md_msg = patch_model(data, model)
        log("[5] 模型下拉框：" + md_msg)
        if md_ok < 0:
            log("[X] 模型补丁校验失败，已中止（DLL 未改动）")
            return 1
    else:
        log("[5] 模型下拉框：跳过（注册表里还没有模型，去 ProPilot 里选一次即可）")

    if bytes(data) != before:
        with open(DLL, "wb") as f:
            f.write(data)
        a, b = open(ORIG, "rb").read(), open(DLL, "rb").read()
        runs, bad_code = diff_report(a, b)
        nbytes = sum(e - s for s, e in runs)
        log("[6] 落盘校验：长度一致=%s  差异区间=%d  改动字节=%d (%.4f%%)" %
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
        log("[6] DLL 无需改动（补丁已在）")

    up = str(cfg.get("upstream", "")).strip()
    if not no_reg and winreg:
        if up:
            log("[7] 写注册表（命令行指定了地址）")
            reg_fix(cfg)
        else:
            log("[7] 注册表：只把地址归一成「根地址」，其余一律不动")
            reg_normalize(int(cfg.get("provider_id", 4)))
    else:
        log("[7] 跳过注册表")

    if up and not no_reg:
        log("[8] 上游连通性")
        probe_upstream(cfg)

    log("")
    log("修复完成【原版 bug 已修 · 零插件零自启】")
    log("  在 ProPilot「大模型服务」里自己填：")
    log("    接口根地址 = 你服务的根地址，填到端口为止（不要带 /v1、不要带 /chat/completions）")
    log("    例如 http://127.0.0.1:18900   https://api.openai.com   https://api.deepseek.com")
    log("    密钥 / 模型 = 你自己的，填完点 OK，直接发消息就能聊")
    log("  修好之后 ProPilot 会自动补 /v1/chat/completions 与 /v1/models")
    log("  改坏了：点「一键还原」/ rollback 命令")
    return 0


def cmd_rollback(no_reg=False):
    log("=" * 74)
    log("回滚  " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 74)
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
    return 0


def main():
    args = sys.argv[1:]
    no_reg = "--no-reg" in args
    args = [a for a in args if a != "--no-reg"]
    if "--dir" in args:
        i = args.index("--dir")
        if i + 1 < len(args):
            set_work_dir(args[i + 1])
            del args[i:i + 2]
        else:
            args.pop(i)
    cmd = (args[0] if args else "fix").lower()
    log("\n### %s  %s  %s ###" % (TOOL, cmd, time.strftime("%Y-%m-%d %H:%M:%S")))

    interactive = (not no_reg) and sys.stdin.isatty() and cmd == "fix"
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
        log("只写注册表（接口地址 / 模型 / 密钥）")
        reg_fix(cfg)
        return 0
    log("未知命令: " + cmd)
    log("可用: fix / probe / check / cfgcheck / fixreg / rollback")
    return 2


if __name__ == "__main__":
    sys.exit(main())
