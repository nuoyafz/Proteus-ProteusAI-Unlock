# ProPilotFix · ProPilot 一键永久修复工具

> **ProPilotFix** — Make ProPilot 9 (the AI assistant inside Proteus 9) work with **your own** OpenAI-compatible API. One-click permanent patch, Chinese UI, custom model, **no background process, no auto-start**.
>
> 仓库：https://github.com/nuoyafz/Proteus-ProteusAI-Unlock · 作者：nuoyafz

**搜索关键词**：Proteus 9 ProPilot 自定义 API · ProPilot 不能用自己的模型 · ProPilot 401 错误 · ProPilot 全英文 · ProPilot 连接失败 · ProPilot 中转站 · ProPilot Custom LLM · ProPilot OpenAI compatible · Proteus AI 助手 汉化。

让 Proteus 9 自带的 **ProPilot** AI 助手用你自己的 API 正常聊天：能连自定义接口 / 中转站、能改模型、界面全中文、主窗口标题写死为「ProPilotFix」。
**点一次就永久生效**：改完 DLL + 注册表，之后开机直接打开 Proteus 9 就能聊，不用启动任何工具、不用挂代理、不用设开机自启。

只解决「聊天」这一件事，不碰原理图、不碰 MCP、不常驻任何进程。

---

## 目录

- [下载](#下载)
- [适用版本](#适用版本)
- [三步使用](#三步使用)
- [一、原版到底出了什么问题](#一原版到底出了什么问题)
- [二、这个工具修了什么](#二这个工具修了什么)
- [三、和挂代理方案的区别](#三和挂代理方案的区别)
- [四、支持哪些 API 地址](#四支持哪些-api-地址)
- [五、常见问题与错误码](#五常见问题与错误码)
- [六、原理给爱折腾的人](#六原理给爱折腾的人)
- [七、从源码运行自己打包](#七从源码运行自己打包)
- [八、文件说明与还原](#八文件说明与还原)
- [九、免责声明](#九免责声明)

---

## 下载

| 文件 | 说明 |
| --- | --- |
| **[⬇ Releases · v1.0.0](https://github.com/nuoyafz/Proteus-ProteusAI-Unlock/releases/tag/v1.0.0)** | **推荐**。下载页面里的附件（文件名 `ProPilotFix.exe`） |
| [`release/ProPilotFix.exe`](release/ProPilotFix.exe) | 仓库内直链，约 10 MB。Windows 单文件 GUI，无需装 Python、无需安装 |
| `ProPilotFixGUI.py` + `ProPilotFix.py` | 源码，纯 Python 标准库，想自己改 / 自己打包用 |

> 两个 exe 内容完全一致（SHA256 `2d676699f0103b207059d9c0ed11ab5c2d4589ddb856b69d363b6ea09baee36e`）。下载后放桌面、微信下载目录、U 盘都行，**别放进 `ProPilot.dll` 目录**。

---

## 适用版本

| 项 | 值 |
| --- | --- |
| 目标 | **Proteus 9 Professional** 自带的 ProPilot（9.2.45801.1） |
| 原版 DLL SHA256 | `35be5f09eca913947e512c2b3d6fb8803b7ad9e3c8d4a65c2399f946354f5414` |
| 系统 | Windows（注册表 + PE 补丁） |
| 其它版本 | 每条写入前会**逐字节校验原文**，对不上的条目自动跳过（不会写坏）；模型补丁若 `LEA` 指令不匹配也会跳过。所以版本不同一般不会崩，但汉化/模型可能不全 |

装的是别的 Proteus 版本：改 `ProPilotFix.py` 里的 `REG_PRODUCT` 一行即可适配注册表路径。

---

## 三步使用

1. **完全退出 Proteus 9**（不退出 DLL 被占用，且它退出时会把旧配置写回注册表）。
2. 双击 `ProPilotFix.exe` → 程序自动搜索 `ProPilot.dll`（卸载表 → 常见安装路径 → 全盘限时扫描）；找不到就点「浏览…」手动选 Proteus 安装目录下的 `BIN` 文件夹。
   界面右上角显示「● 非管理员」时，点一下「点此以管理员重启」（DLL 装在 `C:\Program Files` 下需要写权限）。
3. 在「② 接口配置」里填**你自己的** 3 项，然后按顺序点两个按钮：

   | 顺序 | 按钮 | 作用 |
   | --- | --- | --- |
   | 先 | `● 接口自检` | **只读**，不改任何文件。按 ProPilot 真实拼法 GET 模型表 + POST 一句 `hi`，显示「聊天接口可用」再继续 |
   | 后 | `★ 一键永久修复` | 改 DLL（汉化 / 标题 / 模型）+ 写注册表，完成 |

   最后**重开 Proteus 9**，直接发消息就能聊。

> ⚠️ **不要点 ProPilot 设置里的「测试连接」按钮**。它把「接口根地址」当成完整端点直接 POST（POST `/v1` → 404），纯直连模式下必然失败，但**不影响正常聊天**。原因见[第一节](#一原版到底出了什么问题)。

**填什么**：上游地址（一般以 `/v1` 结尾）、API 密钥（本地服务可留空）、模型名（中转站给你的那个，例如 `deepseek-chat` / `qwen-plus`）。
界面下方绿色一行是「实际生效值」——会显示最终拼出的完整聊天 URL、模型、密钥来源；改完配置这行必须跟着变，点「配置自检」还能在日志里看完整对账。

---

## 一、原版到底出了什么问题

全是通过逆向 `ProPilot.dll`（REA / Ghidra）+ 真机日志复现定位的，不是猜的。

### 问题 1：自定义接口 / 中转站根本连不上（核心 bug）

ProPilot 的 Custom 服务商有 **3 条互相矛盾的 URL 拼装规则**：

| 动作 | 实际请求 | 字符串位置（文件偏移） |
| --- | --- | --- |
| 聊天 | `baseUrl + /v1/chat/completions` | `0x115EC0` |
| 拉模型表 | `baseUrl + /v1/models` | `0x115E88` |
| 点「测试连接」 | `baseUrl` **原样 POST** | `0x10D1A0` 附近的示例值 `https://api.example.com` |

也就是说注册表里的 `baseUrl` 必须是「**不带 `/v1` 的根地址**」。而界面提示语和绝大多数中转站文档都让人填 `.../v1`，用户照填就拼成 `/v1/v1/chat/completions` → 404；填根地址的话，「测试连接」又会 POST `/v1` → 404，让人以为「连不上」而反复改配置。

### 问题 2：界面全英文，官方汉化没生效

`source_en.ts` 里 ProPilot 相关条目的译文是**空的**，Qt 查不到译文就回退英文源串；安装包里也没有对应 `.qm`。结果：设置项、按钮、报错全是英文，中文用户连「接口根地址 / 密钥 / 模型」在哪都找不准。

### 问题 3：模型被写死 `gpt-4o`，自己填的模型保存不住

- `FUN_18007ce60` 里模型下拉框**硬编码 5 个 gpt 模型**，点 OK 就把首项 `gpt-4o`（`0x1157AC`）写回注册表 `model`，覆盖用户保存的值。
- 模型表拉取失败时，界面还会留下 `No Model`（`0x10B9E0`）/ `Loading models...`（`0x103DB8`）这种**占位符**并被存进注册表，之后每次请求都带着它 → 400。
- 结果：用中转站的 `xxx/yyy` 模型名，怎么填都变回 `gpt-4o`，或者干脆报「无模型」。

### 问题 4：换了接口和密钥还是 401

注册表 `Providers\1\apikey` 常被误填成 URL（或残留上一次、甚至别人机器上的旧密钥）；ProPilot 退出时还会把界面里的旧值**写回**注册表。于是「界面明明改成了自己的接口，实际请求却带着旧密钥」→ 401。

### 顺带：网上流传的「本地代理」绕法本身也有坑

把 `baseUrl` 指向 `127.0.0.1` 再写个转发程序，能绕开问题 1，但**必须常驻后台**：关掉窗口就断、重启电脑就失效，于是又得加开机自启，越修越重。而且早期脚本还有三个隐蔽 bug（见下）。

---

## 二、这个工具修了什么

| 原版问题 | 修复做法 | 效果 |
| --- | --- | --- |
| ① 自定义接口连不上 | 由配置反推出**正确的根地址**写进注册表；ProPilot 自己拼 `/v1/chat/completions` 和 `/v1/models` | **纯直连**：ProPilot → 你的上游，中间没有任何进程 |
| ② 全英文 | 原地替换 `ProPilot.dll` 的 `.rdata` 里 **64 条** UTF-8 字符串常量（含主窗口标题） | 设置、按钮、报错全中文；主窗口标题写死为「ProPilotFix」 |
| ③ 模型写死 | 把 `FUN_18007ce60` 首条 `LEA RDX,[rip+disp32]` 的 disp32 指向 `.rdata` 空闲块（`0x171182`），块内写入你的模型名；清空 `No Model` / `Loading models...` 占位符 | 下拉框首项就是你的模型，点 OK 写回的也是它 |
| ④ 密钥污染 | 清理误填成 URL 的 `apikey`；密钥统一以配置为准（base64 写入注册表），不被客户端残留头覆盖 | 换自己的接口/密钥立刻生效，不再 401 |
| 常驻代理 / 自启 | 默认**不启用**代理、不写任何开机自启项 | 一次修复永久生效，开机直接开 Proteus 就能聊 |

### 迭代过程中顺手修掉的 3 个真 bug

| Bug | 现象 | 修法 |
| --- | --- | --- |
| **换模型不生效** | 第一次修好后 `LEA` 指向自家空闲块，旧逻辑看到「当前不是 `gpt-4o`」就直接跳过，改几次模型 DLL 里还是旧模型 | 识别自家块并整块清零后重写，支持反复更换（真机 DLL 验证过） |
| **密钥串台** | 旧版代理优先透传 ProPilot 发来的 `Authorization`，残留旧密钥导致 401 | 默认始终用配置密钥；只有勾「透传客户端密钥」才用客户端的 |
| **配置写了没读** | 界面改了 URL，日志里还是旧地址 | 修复前 `validate_cfg()` 强校验 + `cfg_report()` 打印实际生效值 + 界面绿色回显；填错直接拒绝且不动 DLL |

### 安全性设计（改 DLL 不是闹着玩的）

- 每条字符串写入前校验：文件偏移处的**英文原文 + 尾部 NUL + `%1` 占位符 + 可用容量**，对不上就跳过，绝不盲写；带 `%1~%4` 的复合格式串刻意不改。
- 只改 `.rdata` 的字符串和 `.text` 里 `LEA` 的 **4 字节 disp32**；PE 头、节表、其它代码段一字节不动（落盘时做 `diff_report` 校验，越界即自动回滚）。
- 原版 / 补丁版都跑一次 `LoadLibrary` 对比，只有补丁版加载失败才回滚。
- `ProPilot.dll.orig` **只备份一次、永不覆盖**；注册表另存 `ProPilotFix_registry.json`；界面「一键还原」随时回退。
- 全程幂等：重复点修复不会叠加写入。

---

## 三、和「挂代理」方案的区别

```mermaid
flowchart LR
    subgraph 本工具默认：纯直连
        A[ProPilot 9] -->|baseUrl + /v1/chat/completions| B[你自己的上游 API]
    end
    subgraph 兼容代理（手动兜底）
        C[ProPilot 9] --> D[127.0.0.1:18901 转发] --> E[你自己的上游 API]
    end
```

| | **★ 一键永久修复**（默认） | 兼容代理模式（手动） |
| --- | --- | --- |
| 中间进程 | **无** | 需手动起一次独立无窗口进程 |
| 开机自启 | **无** | **无**（重开机要用得再点一次） |
| 上游要求 | 标准 `/v1/chat/completions` | 任意路径、可加额外请求头、可强制换模型 |
| 适用 | 绝大多数云 API / 中转站 / Ollama | 非 `/v1` 路径（如 `/v1beta/openai`）、需特殊头的老服务 |

想要「零插件」状态，任何时候再点一次「★ 一键永久修复」就切回来了。

---

## 四、支持哪些 API 地址

工具会自动把你填的地址归一成 ProPilot 需要的根地址。填之前点「● 接口自检」最稳。

| 你填的上游地址（示例） | 写进注册表的根地址 | 纯直连 |
| --- | --- | --- |
| `https://api.deepseek.com/v1` | `https://api.deepseek.com` | ✅ |
| `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://dashscope.aliyuncs.com/compatible-mode` | ✅ |
| `https://xxx.com/v1/chat/completions` | `https://xxx.com` | ✅ |
| `https://xxx.com`（裸根地址） | `https://xxx.com` | ✅ |
| `http://127.0.0.1:11434/v1`（Ollama） | `http://127.0.0.1:11434` | ✅ |
| `http://127.0.0.1:8080/v1`（本地服务） | `http://127.0.0.1:8080` | ✅ |
| 带端口 / 内网 http / 自签证书 | 原样保留 host:port | ✅ |
| `https://xxx.com/v1beta/openai`（非 `/v1` 路径） | — | ❌ 用「兼容代理模式(手动)」 |

> 判定依据就一条：**上游的聊天端点是不是 `<根地址>/v1/chat/completions`**。是 → 直连（表里的 ✅）；不是 → 兼容代理。地址会自动归一，按中转站文档填完整地址即可。
> 换自己的中转站 / 云 API / 密钥 / 模型，全部在界面上填，**不需要改代码**。

---

## 五、常见问题与错误码

| 现象 | 原因 / 处理 |
| --- | --- |
| `401 / 403` | API 密钥不对，或该中转站要求额外请求头（纯直连做不到，切兼容代理） |
| `404` | 路径不对。上游地址一般以 `/v1` 结尾；非 `/v1` 路径用兼容代理 |
| `400` | 模型名或请求体不被接受，确认「模型名」是中转站给的那个 |
| `429` | 限流，地址和密钥是对的，稍后再试 |
| `502 / 503 / 504` | 网关 / 代理错误，检查网络或本机系统代理设置 |
| 自检里模型表 `/v1/models` 返回 404/405 | **正常**，很多中转站屏蔽此端点，不影响聊天 |
| 点「测试连接」失败 | **预期行为**，别点它，直接发消息（见问题 1） |
| 修复后聊天还是不通 | ① 你的上游服务必须正在运行；② **完全退出并重开 Proteus 9**（不重启不加载新 DLL / 新配置）；③ 点「● 接口自检」看结论 |
| 换模型 / 换中转站 | 界面改完再点一次「★ 一键永久修复」；也可直接编辑 DLL 目录下的 `ProPilotFix.json` |
| 配置存哪 | `ProPilot.dll` 同目录的 `ProPilotFix.json`。**里面有你的 API 密钥，千万别上传 / 提交到 Git** |
| 窗口标题想改 | 标题「ProPilotFix」在 `ProPilotFix.py` 的 `WINDOW_TITLE` 里写死（界面和配置文件都改不了，防篡改）。想换成自己的名字：改这一行后重新打包 exe |

界面其它按钮：`只读体检`（体检 DLL/汉化/模型/注册表）、`配置自检`（打印实际生效值）、`端到端自测`（13 条 URL 路径）、`一键还原`、`打开所在文件夹`（看日志 / 备份）。

---

## 六、原理（给爱折腾的人）

**DLL 补丁**：只读 PE 节表做 VA ↔ 文件偏移换算，所有改动先在内存 `bytearray` 完成并校验，再一次性落盘。

| 常量 | 值 | 用途 |
| --- | --- | --- |
| `IMAGE_BASE` | `0x180000000` | PE 映像基址 |
| `TITLE_FO` | `0x10F120` | 主窗口标题 `ProPilot - Electronics Design Assistant` |
| `UI_MAP` | 64 条 | 界面 / 报错英文串 → 中文（候选按容量自动挑最短可用译文） |
| `LEA_FO` | `0x7C289` | `FUN_18007ce60` 首条 `48 8D 15 <disp32>` |
| `STR_FO` | `0x171182` | `.rdata` 里 110 字节全 0 空闲块，用来放自定义模型名 |

**注册表**（纯 `winreg` API，不调 `reg.exe`）：

```
HKCU\Software\Labcenter Electronics\Proteus 9 Professional\PROPILOT\LLM Configuration
    activeProvider = 4 (Custom, REG_DWORD)
    Providers\4\baseUrl = 上游根地址
    Providers\4\model   = 你的模型名
    Providers\4\apikey  = base64(你的密钥)
```

**纯直连地址推导**：从 `upstream` 解析 host / port / path → 去掉 `/chat/completions` → 去掉末尾 `/v1` → 得到根地址 → ProPilot 自己拼 `/v1/chat/completions` 与 `/v1/models`。

---

## 七、从源码运行 / 自己打包

```bash
# 运行 GUI（Python 3.10+，Windows，需 tkinter）
py -3.10 ProPilotFixGUI.py

# 命令行版（把 .py 放到 ProPilot.dll 同目录，或用 --dir 指定）
py -3.10 ProPilotFix.py probe      # 只读接口自检
py -3.10 ProPilotFix.py fix        # 一键永久修复
py -3.10 ProPilotFix.py check      # 只读体检
py -3.10 ProPilotFix.py rollback   # 还原

# 打包单文件 GUI exe
py -3.10 -m pip install pyinstaller
py -3.10 -m PyInstaller --noconfirm --clean --onefile --windowed --name "ProPilotFix" ProPilotFixGUI.py
# 产物：dist/ProPilotFix.exe
```

`ProPilotFixGUI.py` 会 `import ProPilotFix`，PyInstaller 会自动把核心模块打进同一个 exe，无需额外 `--add-data`。项目零第三方依赖（GUI 用 tkinter，网络用标准库）。

---

## 八、文件说明与还原

```
propilot-fix/
├── README.md              本说明
├── LICENSE                MIT
├── .gitignore             已排除密钥配置 / DLL / 备份 / 日志 / 编译产物
├── ProPilotFix.py         核心：DLL 补丁、注册表、直连推导、接口自检、兼容代理
├── ProPilotFixGUI.py      GUI（自动找 DLL、配置回显、一键操作、运行日志）
└── release/
    └── ProPilotFix.exe     开箱即用的单文件 GUI
```

运行时会在 **`ProPilot.dll` 同目录**生成（都不该提交）：

| 文件 | 说明 |
| --- | --- |
| `ProPilot.dll.orig` | 原版 DLL 备份，只建一次、永不覆盖 |
| `ProPilotFix_registry.json` | 注册表备份 |
| `ProPilotFix.json` | 你的配置，**含 API 密钥** |
| `ProPilotFix.log` | 运行日志（排障先看它） |

**还原**：界面点「一键还原」，或命令行 `rollback`。会恢复 DLL + 注册表，并停掉本工具手动起的兼容代理；不会动开机自启（本项目从不写自启项）。

---

## 九、免责声明

- 本项目**不包含、不分发任何 Labcenter Electronics 的二进制文件**（`ProPilot.dll` 等），只提供对**你自己机器上已合法安装**的 Proteus 9 做本地字符串与配置项修改的脚本。
- 本项目**不破解授权、不绕过付费、不修改软件功能逻辑**，仅修复自定义接口配置、补充界面汉化、修正模型名写回。与 Labcenter Electronics / Proteus 官方无任何关联。
- 修改 DLL 存在风险：工具已做原文校验、结构校验、加载校验、自动备份与失败回滚，但**使用风险自负**，建议先自行备份。
- 仅供学习与技术交流，请勿用于商业分发；使用第三方 API 时遵守对应服务商条款。
- 配置文件 `ProPilotFix.json` 含明文密钥，`.gitignore` 已排除，**请不要把它传上 GitHub**。

---

MIT License · 作者：nuoyafz · 主窗口标题写死为「ProPilotFix」，想换成自己的名字看[第五节](#五常见问题与错误码)。
