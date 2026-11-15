# ProPilotFix

**修好 Proteus 9 自带 AI 助手 ProPilot 的「自定义服务商」连不上问题** —— 一次性补丁 ProPilot.dll，之后你在 ProPilot 设置里填自己的地址 / 密钥 / 模型就能聊。

> 仓库：https://github.com/nuoyafz/Proteus-ProteusAI-Unlock · 作者：nuoyafz
>
> 下载：[Releases](https://github.com/nuoyafz/Proteus-ProteusAI-Unlock/releases)

**搜索关键词**：Proteus 9 ProPilot 自定义 API · ProPilot 405 Method Not Allowed · ProPilot 资源仅支持 GET 或 HEAD · ProPilot 连接失败 · ProPilot 401 · ProPilot 全英文 · ProPilot 中转站 · ProPilot Custom LLM · ProPilot OpenAI compatible · Proteus AI 助手 汉化。

---

## 一、原版到底哪里坏了

用「自定义」服务商（填自己的 OpenAI 兼容地址）时，ProPilot 会报：

```
请求错误（HTTP 405），请检查请求格式
详情：资源仅支持 GET 或 HEAD
```

或者测试连接时：

```
连接失败：Error transferring http://127.0.0.1:xxxxx - server replied: Method Not Allowed
```

**根因（逆向 ProPilot.dll 得到，不是猜的）**：ProPilot 给每个服务商实现了一个类，类里有个虚函数负责「拼请求 URL」。内置服务商那套实现是：

```
url = 归一化(你填的地址) + "/v1/chat/completions"   →  setUrl()
```

而「自定义」这个类实现的却是：

```
url = 你填的地址（原样）                            →  setUrl()
```

**它从不追加任何路径。** 所以不管你填 `http://host` 还是 `http://host/v1`，它都直接 POST 那个地址本身：

| 你填的地址 | 它实际 POST | 结果 |
| --- | --- | --- |
| `http://127.0.0.1:18900` | `POST /` | **405** 资源仅支持 GET 或 HEAD（服务器根路径只收 GET） |
| `http://127.0.0.1:18900/v1` | `POST /v1` | **404** no route for POST /v1 |

同一个 vtable 里，模型列表的构造函数也只拼 `/models`（缺 `/v1`），所以模型下拉框刷新也拿不到列表。

### 逆向定位（ProPilot 9.2，ImageBase 0x180000000）

| 项 | RVA | 说明 |
| --- | --- | --- |
| Custom 的 URL 构造 | `0x180086AE0` | `QString(baseUrl)` → `QUrl` → `QNetworkRequest::setUrl()`，**不拼路径** |
| 标准（OpenAI 兼容）URL 构造 | `0x180085070` | `0x180084F00` 归一（去尾部 `/`）→ `append("/v1/chat/completions")` → `setUrl()` |
| Custom 的模型表构造 | `0x180086CA0` | `append("/models")`（缺 `/v1`） |
| 标准模型表构造 | `0x180085370` | `append("/v1/models")` |
| 两个类所在的 vtable | `.rdata` `0x106C38` / `0x106A90` | 槽位布局完全一致 |

两个函数签名完全相同 `(this, QNetworkRequest*, provider)`，都调用同一个导入 `Qt5Network.dll!QNetworkRequest::setUrl(QUrl const&)`，差别只是 vtable 里的**一个函数指针**。

---

## 二、修了什么

把「自定义」类的两个槽位指向标准实现：

```
vtable[chat]   : 0x180086AE0  →  0x180085070     （→ 自动补 /v1/chat/completions）
vtable[models] : 0x180086CA0  →  0x180085370     （→ 自动补 /v1/models）
```

只改 4 个字节（两处指针的低 2 字节），PE 头 / 节表 / `.text` 一字节不动，可逆可回滚。

修好之后行为变成：

| 你在 ProPilot 里填 | ProPilot 实际请求 |
| --- | --- |
| `http://127.0.0.1:18900` | `http://127.0.0.1:18900/v1/chat/completions` ✅ |
| `https://api.openai.com` | `https://api.openai.com/v1/chat/completions` ✅ |
| `https://api.deepseek.com` | `https://api.deepseek.com/v1/chat/completions` ✅ |
| `https://xxx.com/compatible-mode` | `https://xxx.com/compatible-mode/v1/chat/completions` ✅ |

顺带保留的其它补丁：

| 补丁 | 内容 |
| --- | --- |
| 界面汉化 | 64 条文案原地中文化（`source_en.ts` 里 ProPilot 条目译文是空的，Qt 查不到译文就回退英文） |
| 主窗口标题 | 写死为 `ProPilotFix` |
| 设置对话框标题 | 加签名，显示为 `设置 · NoYa修复版`（原 `ProPilot Configuration`，槽位固定 24 字节，按容量择优写入） |
| 模型下拉框首项 | 跟随注册表里你已保存的模型（原版点 OK 会把首项 `gpt-4o` 写回注册表，覆盖你的模型） |

**工具不写地址 / 密钥 / 模型**：那些全部在 ProPilot 里填。它只把注册表里已有的地址归一成根地址（去掉误填的 `/v1`），其余一个字不动。

---

## 三、怎么用

1. **完全退出 Proteus 9**（任务管理器里没有 `PDS.exe` / ProPilot 相关进程）
2. 双击 `ProPilotFix.exe` → 自动找 `ProPilot.dll`（找不到就点「浏览…」选 `...\Proteus 9 Professional\BIN`）
3. 点 **「★ 一键修复 ProPilot」**
4. 打开 Proteus 9 → ProPilot 设置 → **大模型服务** → 服务商选 **自定义**：

| 字段 | 填什么 |
| --- | --- |
| 接口根地址 | **填到端口为止**，例如 `http://127.0.0.1:18900`、`https://api.openai.com`、`https://api.deepseek.com`。**不要带 `/v1`，不要带 `/chat/completions`** |
| 密钥 | 你自己的（本地服务可留空） |
| 模型 | 你自己的模型名 |

5. 点 OK → 直接在聊天框发消息。

> 工具不需要你填任何配置。修复是一次性的，之后**没有后台进程、没有开机自启**。

### 地址填错也能救

如果你已经在 ProPilot 里填了 `.../v1`，再点一次「一键修复」即可 —— 它会把注册表里的地址归一成根地址（`.../v1` → `...`），避免 `.../v1/v1/chat/completions` 这种双 `/v1`。

### 还原

点「一键还原」，DLL 恢复到修复前（首次修复会生成 `ProPilot.dll.orig` 备份，永不覆盖）。

命令行等价物：

```
ProPilotFix.exe --auto                  自动搜索并修复
ProPilotFix.exe --auto --dir D:\X\BIN   指定 DLL 目录
ProPilotFix.exe --find                  只列出找到的 DLL 目录
ProPilotFix.exe --selftest-ui           界面自检
```

---

## 四、常见问题

**Q：修完还是 405？**
A：先确认 Proteus 完全重启过（DLL 是加载时读进内存的，改文件后必须重开）；再确认地址没带 `/v1`。

**Q：能连中转站吗？**
A：能。只要它的聊天端点是 `<你填的地址>/v1/chat/completions`（绝大多数中转站和云厂商 compatible-mode 都是），填根地址即可。

**Q：会误伤内置服务商吗？**
A：不会。内置 OpenAI / DeepSeek / xAI / Gemini / Kimi / Ollama 用的是各自的完整 URL 模板，和本补丁改的 vtable 槽位无关。

**Q：要装 Python 吗？**
A：不用。`release/ProPilotFix.exe` 是单文件，双击即用。源码是纯标准库 Python，想自己打包也行。

**Q：安全吗？**
A：补丁只改 4 个字节的函数指针，不动代码段，原 DLL 备份保留，随时回滚。工具不联网、不上传任何东西。

---

## 五、文件

| 文件 | 说明 |
| --- | --- |
| [`release/ProPilotFix.exe`](release/ProPilotFix.exe) | 单文件 GUI，约 9.9 MB，免安装 |
| `ProPilotFixGUI.py` | 界面（tkinter） |
| `ProPilotFix.py` | 核心：DLL 补丁 / 汉化 / 注册表 / 回滚 |
| `llms.txt` | 给 AI 检索用的摘要 |

自行打包：

```bash
pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name ProPilotFix ProPilotFixGUI.py
```

---

## 免责声明

仅供学习研究与个人使用。修改的是你本机已购买的 Proteus 安装目录内的 DLL，请自行备份（工具会自动备份 `ProPilot.dll.orig`）。因使用本工具造成的任何问题由使用者自行承担。Proteus / ProPilot 版权归 Labcenter Electronics 所有。

MIT License.
