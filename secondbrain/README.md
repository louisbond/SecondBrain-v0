# SecondBrain v0.7.0

> **直接使用程序**请看 [用户使用说明](docs/USER_GUIDE.md)；本文件面向开发者。

> 自用优先的个人知识库：**采集 → 提炼 → 创作 → 运维**
> 桌面常驻服务 + 本机浏览器 UI，不上外网。

```
A 进货  →  B 提炼  →  C 创作  →  D 运维
（原文）    （AI摘要+手稿）  （笔记）    （回顾/标签/归档）
```

核心原则：**AI 起草、人工逐条核准**；Markdown 是唯一真相源，SQLite 索引随时可重建。

---

## 快速开始

### 开发态

```bash
# 1) 后端依赖
cd backend
python -m pip install -r requirements.txt

# 2) 前端构建
cd ../frontend
npm install
npm run build          # 产物在 frontend/dist

# 3) 启动
cd ../backend
python run.py
# → http://127.0.0.1:7688
```

前端热重载开发（两个终端）：

```bash
# 终端 A：后端
cd backend && python run.py --no-frontend

# 终端 B：前端
cd frontend && npm run dev     # → http://localhost:5173（已代理 /api）
```

### 开发与测试

```bash
# 一键跑全套检查（14 项：lint + 格式 + 类型 + 结构 + 单测 + 契约 + 构建
#                  + E2E + 边界 + 渲染 + 走查 + 审计）
python scripts/check.py

# 只跑不需要服务的（9 项，秒级）
python scripts/check.py --quick

# CI 模式：同 --quick，只跑平台无关项（浏览器 E2E 需 Windows Edge + 本机服务）
python scripts/check.py --ci

# 只跑需要本机服务的 5 项浏览器检查（Windows self-hosted runner 用）
python scripts/check.py --e2e

# 列出所有检查项
python scripts/check.py --list
```

需要服务的检查（E2E / 边界 / 渲染 / 走查 / 审计）会打 `http://127.0.0.1:7688`，
**先把后端起起来**（`cd backend && python run.py`），否则脚本会直接提示并退出。

| 检查项 | 命令 | 说明 |
|---|---|---|
| 后端 lint | `python -m ruff check .` | 规则配置在 `backend/pyproject.toml` |
| 前端 lint | `npm run lint` | ESLint（flat config，`eslint.config.js`） |
| 格式检查 | `npm run format:check` | Prettier（`.prettierrc.json`） |
| 后端类型检查 | `python -m mypy app` | 配置 `backend/pyproject.toml` |
| 前端类型检查 | `npm run typecheck` | `tsc --checkJs`，配置 `jsconfig.json` + `env.d.ts` |
| 后端单测 | `pytest tests/test_secondbrain.py` | 领域逻辑 + 服务 + 迁移 |
| 前后端契约 | `pytest tests/frontend_contract_test.py` | 接口形状与前端调用一致 |
| 前端构建 | `npm run build` | 也会自动同步 Vditor 运行时资源 |
| E2E 冒烟 | `python tests/e2e_smoke.py` | 主链路 |
| 边界用例 | `python tests/edge_case_test.py` | 空数据 / 异常输入 |
| 渲染检查 | `python tests/frontend_render_test.py` | 每页关键文案 + 无运行时错误 |
| 用户走查 | `python scripts/user_journey_test.py` | 真实点一遍主要页面 |
| 控件审计 | `python tests/control_audit.py` | 可点的控件逐个点，抓 JS 报错 |
| 前端结构 | `pytest tests/frontend_structure_test.py` | 单文件行数棘轮，防组件长回巨石 |

### 代码风格工具

```bash
# 后端
pip install ruff
cd backend && python -m ruff check .        # 检查
cd backend && python -m ruff check . --fix  # 自动修（安全的那些）

# 前端
npm run lint        # 检查
npm run lint:fix    # 自动修
npm run format      # 格式化
npm run format:check
```

- Ruff 规则写在 `backend/pyproject.toml`（选了 pycodestyle / pyflakes /
  bugbear / pyupgrade / isort 等；**行宽不管** —— 交给 formatter，
  否则存量 118 处行宽告警会把真问题淹掉）。
- ESLint 用 flat config（`frontend/eslint.config.js`）。**每条告警都值得看**，
  所以风格类规则（属性连字符、多词组件名…）一律关掉，只留会抓 bug 的：
  未定义变量、未用变量、`==`/`!=`、`no-eval`、空 catch。
- Prettier 只管排版，不改语义。

> Prettier 3 **内置** Vue 支持，不需要 `prettier-plugin-vue`（那个插件是给
> Prettier 2 用的，装上会报 `Cannot read properties of undefined (reading 'html')`）。

**约定**（成熟软件的基本要求）：

- **版本号只有一个源**：`backend/app/core/config.py` 的 `APP_VERSION`。
  后端接口、前端侧边栏、打包产物都用它，别再硬编码。
- **诊断逻辑只有一份**：`backend/app/services/diagnostics.py`。
  `/api/health`（轻量关键项，前端每 20 秒轮询）与
  `/api/workbench/diagnose`（全套，用户手动点）共用它。
- **所有网络请求都要有超时**：`frontend/src/api/client.js` 默认 30s，
  长任务单独放宽；超时与"连不上"给不同的提示。
- **前端异常不许静默**：`main.js` 里有全局 errorHandler / unhandledrejection
  兜底，任何未捕获错误都会弹提示。
- **用户数据绝不入库**：`.gitignore` 挡掉 `data/`、`logs/`、真实 config。

### 类型检查（v42）

```bash
cd backend && python -m mypy app               # 或 python scripts/check.py --ci
cd frontend && npm run typecheck              # tsc -p jsconfig.json --noEmit
```

- **后端 mypy**：配置在 `backend/pyproject.toml`。没上 `strict` ——
  存量几百处隐式 Any 会把真问题淹掉；目标是**把运行时才会炸的类型错误提前到 CI**。
  首次接入报 22 个错，其中 4 个是真 bug，已修：
  `TemplateService` 用了根本不存在的 `self.db`（被 except 吞掉，表现为
  「删掉的内置模板又自动回来」）、`db.query()` 已返回 list 却再调 `.fetchall()`、
  `ClassVar` 经实例赋值被遮蔽导致健康缓存失效、`list | None` 参数直接迭代。
- **前端 `checkJs`**：项目是 JS 不是 TS，用 `jsconfig.json` 开启 `checkJs`，
  `env.d.ts` 声明 `*.vue` 模块、`@/` 别名与 `window.__sbToast`。
  顺手把 `event.target.tagName` 改成 `instanceof Element` 收窄（真类型改进）。
- **不开 `warn_unused_ignores`**：那批 `# type: ignore` 是给*没装库*的环境写的，
  装了库或开了 `ignore_missing_imports` 就变"多余" —— 同一份代码在 CI 与本机
  报不同的错。已在**全新 venv 最小依赖**与本机两种环境里验证都全绿。

### 持续集成（v42）

`.github/workflows/ci.yml`：push / PR / 手动触发，Ubuntu + Python 3.13 + Node 22，
装 `backend/requirements-ci.txt` 与 `npm ci`，然后跑 `python scripts/check.py --ci`（9 项）。

**为什么 CI 只跑 9 项**：另外 5 项（E2E / 边界 / 渲染 / 走查 / 控件审计）需要真实
浏览器与本机 7688 服务，`cdp_client.py` 里的 Edge 路径也是 Windows 的 ——
塞进 Linux runner 只会得到一个长期红的 workflow。这 5 项在本地 Windows
用 `python scripts/check.py`（全套 14 项）跑。

`requirements-ci.txt` 是跨平台最小集：去掉 `winotify`（Windows 专用）与
faster-whisper / onnxruntime 等重型模型依赖 —— 它们都是**函数内惰性 import**，
核心包、单测、类型检查都不需要。仓库目前没有 remote，推上去后即自动生效。

#### 浏览器检查：Windows self-hosted runner

`.github/workflows/e2e-windows.yml` 把 CI 跑不了的 5 项也自动化了：

- 核心是 `python scripts/check.py --e2e` —— **只跑 5 项需要服务的检查**，
  不重复 Linux CI 已经做过的 lint / 类型 / 单测。
- 服务策略：若 `7688` 已有健康服务就复用（不启动也不关闭）；
  否则在 `RUNNER_TEMP` 里生成临时 `config.toml` + vault + db，自包含启动
  开发态 `run.py --no-browser`，并在 `finally` 里关掉 —— **不碰你的真实数据**。
- 触发：Actions 页手动 `Run workflow`（`scope=full` 可改跑全套 14 项），
  或 CI 成功后自动排队。`concurrency` 只保留最新一次，runner 离线不会堆积 job。

**一次性准备**（在本机注册 runner，只需做一次）：

1. 仓库 → Settings → Actions → Runners → **New self-hosted runner** → Windows x64
2. 按页面给的命令下载并 `config.cmd`，标签保持默认 `self-hosted / Windows / X64`
3. `run.cmd` 常驻（或装成 Windows 服务，避免每次手动开）
4. 本机装 Edge —— `cdp_client.py` 找的是标准安装路径

这个 workflow 的 PowerShell 脚本**刻意全用 ASCII**：Windows PowerShell 5.1 按 ANSI
读 `.ps1`，中文字符串会乱码并破坏引号；启动进程用 .NET `Process` 而非
`Start-Process`（PS 5.1 在环境里有 `Path`/`path` 重复键时会抛「已添加项」），
并用 setup-python 提供的 `pythonLocation` 而不是 PATH 里的 `python`
（开发机上常命中 Windows Store 别名）。整套起停流程已在本地模拟执行验证过。

### 前端结构（v41 + v42）

两个曾经的巨石组件已拆开，**视图只做编排，逻辑与界面各自归位**：

```
views/ManageView.vue        94 行  只画页签 + KeepAlive 切换
views/manage/               6 个页签自治组件（最大 314 行），各自持有状态、各自拉数据
views/ProcessView.vue       72 行  只画加工队列条 + 空态
views/process/              3 个核准子组件（ReviewCard / ParseSection / TaxonomySection）
composables/useReview.js   471 行  核准页全部状态与业务逻辑（无模板）
views/WorkbenchView.vue     93 行  采集区主壳（状态条 + 合并卡）
views/workbench/            2 个段组件 + useWorkbench.js（369 行）
views/SettingsView.vue      69 行  设置面板主壳（props + 分组）
views/settings/             ai / storage / system 三组 + useSettings.js（280 行）
```

- **设置页的 6 个页签是自治组件**：父组件不预加载全部数据，切到哪个才拉哪个；
  `KeepAlive` 保留编辑草稿，行为与拆分前一致，首屏请求从 7 个降到 2 个。
- **核准页用注入上下文而不是 props**：`form` / `current` / 十几个 toggle 被
  三个区块共同读写，走 props 需要 20+ 个 v-model。`provideReview()` 创建上下文、
  子组件 `useReview()` 取用（漏了 provide 会直接报错，不会静默拿到 undefined）。
- **单文件 ≤ 500 行**：v42 拆完最后两个超限文件（`WorkbenchView.vue` 714、
  `SettingsView.vue` 577）后，例外名单已**清空**，全项目统一走 500 行上限
  （当前最大 471 行）。四个页面主壳另锁 ≤160 行，防止逻辑慢慢搬回视图。
  v41 曾用例外名单锁住候选文件不许再长 —— 那不是放过，是不让技术债扩大。
- **契约测试按调用点找源码，不按文件路径**：`_read_containing("reviewApi.confirm(")`
  全量扫描 `src/`。以前写死 `views/ProcessView.vue`，逻辑一搬进 composable
  就 `ValueError: substring not found` —— 明明 payload 一个字没改，测试却假失败。
- **lint 必须全绿**：`scripts/check.py` 里有 lint 项，CI 直接用它的退出码。

### 打包分发（P10）

```bash
cd backend
pyinstaller secondbrain.spec --noconfirm
# 产物：dist/SecondBrain/SecondBrain.exe
```

`bin/` 目录放 `yt-dlp.exe` 与 `ffmpeg.exe`，会被一并打包，实现离线部署。

---

## 目录结构

```text
secondbrain/
├─ backend/
│  ├─ app/
│  │  ├─ core/         config / db / parser / tokenizer / vault / fts
│  │  ├─ adapters/     bilibili / douyin / kuaishou / xiaohongshu / web
│  │  ├─ providers/    ai_providers（ollama / openai_compat；v34 起 workbuddy 已删）
│  │  │                ai_service（prompt 模板 + JSON 契约 + 重试）
│  │  ├─ services/     ingest / review / search / notes / index / tag
│  │  │                recap / asr / embedding / backfill / notifier / autostart
│  │  ├─ workers/      queue（jobs 队列）/ pipeline（worker 编排）/ scheduler（周报）
│  │  └─ api/routes/   collect items jobs review search notes tags recap settings upload vault
│  ├─ tests/           单测 / E2E 冒烟 / 性能验证
│  ├─ run.py           启动入口
│  └─ secondbrain.spec PyInstaller 打包
├─ frontend/           Vue 3 + Vite + <script setup>
├─ config/config.toml  配置
├─ bin/                yt-dlp / ffmpeg（可选）
└─ data/               数据库与默认 vault
```

---

## Vault 结构

工具**独占写入** vault，Obsidian 只读查看：

```text
ObsidianVault/
├─ 01_Inbox/       A 层：原文全文 + 字幕 + 截图 + 来源
├─ 02_Processed/   B 层：AI 摘要 + 个人手稿 + 受控标签 + 相关链接
├─ 03_Notes/       C 层：创作笔记（form: fragment | article）
├─ assets/         截图等附件
├─ 99_Archive/     手动归档
└─ _reports/       周报（工具生成）
```

存量笔记**保留原位置原结构**，工具只建索引不迁移。

---

## 关键设计

### 双摘要：AI 在上，手稿在下

核准页是**垂直驾驶舱**（不并排）：

```
┌─ 标题（可编辑，默认取 AI 建议）
├─ 🤖 AI 摘要（要点式 / 结构化分区，标注模型名）
│    · 标签建议：☑ ☐ +待定标签（词表外需你确认）
│    · 分区建议：☑
├─ ✍️ 个人手稿（按钮「以 AI 摘要为底稿」一键预填）
├─ 🔗 相关笔记（embedding 召回，真实存在的库内笔记）
│    · LLM 关键词提示（仅展示，不生成链接）
├─ 📄 回查原文 → 新标签页打开 A 层 md
└─ [确认入库]  逐条处理，无批量全收
```

手稿留空也可入库（软门槛），但会进入「待补手稿」清单，由回顾卡片持续提醒。

### 字段语义（避免自相矛盾）

| 字段 | 含义 |
|---|---|
| `tags` | **仅在核准确认后写入** = AI 建议勾选 ∪ 用户手动新增。拒绝全部建议 → 空数组，不继承任何 AI 值 |
| `ai_suggested_tags` | AI 原始建议，**永远保留**供追溯 |
| `embedding_related` | embedding 余弦召回，**仅展示**，不参与链接解析 |
| `llm_related_keywords` | LLM 只输出关键词，**永不直接生成库内笔记链接** |
| `confirmed_related` | 唯一参与反链/图谱的链接字段 |
| `digested` | 手稿非空后置 true（软门槛） |

### 两级受控词表

`partitions`（大类）+ `tags`（细粒度）。AI 只能从词表选；词表外的建议进入
「待定标签」，由用户在核准页勾选后才入库。合并标签 A→B 会自动改写受影响
md 的 frontmatter，并触发 FTS 与向量增量更新。

### 中文搜索

FTS5 `unicode61` 对中文按字符切分，因此写入时构造 **jieba 词 + bigram_cjk**
的并集载荷，查询侧对称。两字词、子串均可命中。jieba 不可用时自动降级为纯
bigram，并在 UI 顶部显式告警 `search_mode=bigram`。

排序权重由 bm25 **列权重**表达（title 10 > tags 6 > summary 4 > manuscript 3 > body 1），
C 层结果永远排在 B 层之前。

### 分区（受控词表的大类）

- **词表来源**：标签页「冷启动扫描存量」→ 分区 = 存量 frontmatter 的 `partitions`
  **+ vault 顶层目录名**（排除工具目录与隐藏目录）。目录天然就是"大类"，
  比让用户从零手填分区更合用。
- **核准页可补选**：AI 建议之外，还能「从分区词表补选」或当场「新分区，回车创建并选中」。
- **分区关联必须落库**：`note_partitions` 表在入库 / 重建索引 / C 层笔记三处都要写，
  否则「按分区搜索」与分区计数永远是 0。

### AI Provider 与自动降级

| 链路 | 配置 | 角色 |
|---|---|---|
| ~~workbuddy~~ | ~~WorkBuddy 自带的 **CodeBuddy CLI**~~ → **v34 已删除**（CLI 读不到桌面端认证，永远 auth_required） | — |
| ollama | `http://127.0.0.1:11434/v1`，Qwen-7B | 自动降级备链 |
| openai_compat | 任意 OpenAI 兼容端点（火山方舟等） | 手动切换；**设置页可直接填 API Key** |

主链连续失败 ≥3 次 / 429 / 额度耗尽 → 自动切 ollama 并 toast 告知；
**恢复后需手动切回**（避免来回抖动）。

> **认证类错误不触发降级**：CLI 未登录属于"需要用户操作"，不是"主链故障"。
> 否则会降级到一个同样没配置的备链，反而更难排查。

#### 主链的三个已知硬约束（实机核查）

1. **CLI 认证态在系统 keychain**，探测不到，只能交互式登录（设置页有
   「打开终端完成 CLI 登录」按钮，会生成 .cmd 在新控制台里跑 `codebuddy`）。
2. **CLI 的默认端口 5452 是硬编码的**，而 5452 正是 WorkBuddy 桌面端占用的
   服务代理端口 → `EADDRINUSE`。主命令接受 `--port`，`daemon` 子命令不接受。
3. **受限环境（沙箱）里 CLI 读不到认证文件**，日志特征：
   `EPERM … auth/workbuddy-desktop.info` → 此时会表现为"未登录"。
   解决办法：让 SecondBrain 跑在沙箱外（自己双击 exe，或在自己终端里启动服务）。

#### 不想折腾 CLI？用方舟 OpenAI 兼容端点

设置页 → 「API 密钥（OpenAI 兼容端点）」→ 填 base_url / 模型 ID / API Key →
「保存并切换到该链」。密钥只写入 `config/secrets.json`（不进 config.toml），
端点与模型写入 `config.toml` 的 `[ai.openai_compat]`。这条链路不依赖 CLI、不依赖沙箱。

已实测可用（火山方舟 `https://ark.cn-beijing.volces.com/api/v3`）：

| 环节 | 实测 |
|---|---|
| `GET /models` | HTTP 200，返回该 key 可用的 138 个模型 |
| `POST /chat/completions` | HTTP 200，10s 返回 |
| 摘要契约（真实条目） | 约 120s 出 5 条要点 → 核准 → B 层落盘 → 搜索命中 |

> **模型选择**：默认填的 `doubao-seed-2-1-pro-260915` 质量好但**偏慢**（一次摘要约 2 分钟）。
> 批量场景可在设置页把模型改成 `doubao-seed-1-6-flash-250828` 一类 flash 模型，速度差一个量级。

> **三个必须记住的实现约束**（都是踩过的坑）：
> 1. `health()` 必须带 `Authorization` 头，否则带密钥的端点永远显示 401；
> 2. `ProviderRouter` **不能缓存** `AppConfig` 对象——`store.reload()` 是整体替换，
>    缓存旧对象会导致新填的 api_key 不生效（同样表现为 401）；
> 3. 401/403 属认证类错误，**不触发降级**（备链多半也没配）。

#### CodeBuddy CLI 接入（实机核查后的修正）

> **v34：workbuddy 链路连同 `cli_locator.py` 已整体删除**，下面这段调研作废，仅作历史记录。

设计文档 v0.5.0 附录 A 假设了 `wbuddy` / `codebuddy` 两个命令，实机核查结论：

| 设计文档 | 实际情况 |
|---|---|
| `wbuddy start` / `wbuddy status` | **`wbuddy` 命令不存在**（作者的臆测命名）。语义对应是 `codebuddy daemon start/status` |
| `codebuddy` 在 PATH | **不在 PATH**——WorkBuddy 桌面版自带同一套引擎，入口在 `<安装目录>/resources/app.asar.unpacked/cli/bin/codebuddy`（node 脚本，需 Node ≥ 18.20.8） |
| `codebuddy -p ... -y --output-format json` | ✅ **三个参数全部真实存在**，与设计文档一致 |

实现要点（`backend/app/core/cli_locator.py`）：

- **自动定位 CLI**：配置 `cli_path` → 内置 `bin/` → WorkBuddy 安装目录 → PATH；
- **注入 `--port`**：这个 WorkBuddy 定制版 CLI 的 HTTP server 默认端口是**硬编码 5452**，
  而 5452 正是 WorkBuddy 桌面端自己占用的服务代理端口 → `EADDRINUSE`。
  主命令（`-p`/`--version`）接受 `--port`，但 `daemon` 子命令不接受，
  所以只在主命令上注入一个自动分配的空闲端口；
- **剥离环境变量**：`CODEBUDDY_SERVICE_PROXY_URL`（端口冲突源）与
  `HTTP_PROXY`/`HTTPS_PROXY`（会让 CLI 内部 `setProxy` 抛
  `Right-hand side of 'instanceof' is not callable` 并刷一屏堆栈）；
- **`--tools ""`**：摘要任务用不上任何工具，禁用后显著提速与省 token；
- **健康检查用 `--version`**：快、免费、不需要认证；`-p` 调用失败时若报
  "Authentication required" 会给出明确的登录指引。

#### 首次使用需要登录一次

CLI 的登录是交互流程（选登录方式 → 浏览器认证），非交互子进程做不了。
设置页 → AI Provider → **「打开终端完成 CLI 登录」** 会弹出一个终端窗口，
在里面选「Log in via Chinese Site」（国内站），浏览器完成认证即可。
登录态写入 `%USERPROFILE%\.codebuddy`。

单次调用产出全部结构化字段（title/summary/summary_type/tags/new_tags/
partitions/keywords/confidence）。JSON 破损或字段缺失 → 自动重试 2 次
（重试时附上错误说明）→ 仍失败落 `ai_failed`，可在队列页手动重试。

### 平台适配器能力诚实分级

| 平台 | 等级 | 路径 | 兜底 |
|---|---|---|---|
| B站 | **完整可用** | bilibili web API + cookie（含 AI 字幕） | 无字幕→ASR；接口失效→yt-dlp→手动 |
| 通用网页 | 完整可用 | trafilatura → Playwright | 手动粘贴 |
| 小红书 | 部分可用 | 分享链解析 + `__INITIAL_STATE__` | 手动粘贴 |
| 抖音 / 快手 | **不稳定** | yt-dlp 分享短链 | 「简介+ASR+手动」为**常态路径** |

抖音/快手依赖 yt-dlp 的现实支持度，不做虚假承诺——UI 直接标明支持等级。

### 状态机

```text
queued → extracting ─┬→ extracted → ai_processing ─┬→ pending_review → reviewed
                     └→ extract_failed（手动粘贴→extracted）  └→ ai_failed
reviewed →（手动）→ archived
```

jobs 状态持久化于 SQLite：**休眠唤醒后自动续跑**。

---

## 可用性边界（如实标注）

- 笔记本**休眠或关机 = 服务下线**，ASR 队列暂停；唤醒后自动续跑。
  建议接通电源时把电源策略设为「永不睡眠」，以便过夜批处理。
- LAN 开关**默认关闭**；开启后强制 token 校验（`hmac.compare_digest` 比对），
  静态页壳可无 token 加载，API 必须 token。
- 端口默认 **7688**，被占用自动顺延（7689、7690…）并在 UI 顶部提示实际端口。
- embedding 不可用 → 相关召回降级为关键词匹配，不阻塞主流程。

---

## 测试

```bash
cd backend

# 1) 单元 + 集成测试（79 项）
python -m pytest tests/test_secondbrain.py -q

# 2) 端到端冒烟（对着运行中的服务，51 项）
python tests/e2e_smoke.py

# 3) 真实管线（真实 B站链接跑完整条链路，39 项）
python tests/live_pipeline_test.py

# 4) 边界场景（手改 md / 标签合并 / 归档 / 路径穿越 / 并发 / 上传，32 项）
python tests/edge_case_test.py

# 5) 前端渲染（headless Edge 渲染真实页面查 DOM，32 项）
python tests/frontend_render_test.py

# 6) 性能验证（对照 §11 量化指标，5 项）
python tests/perf_check.py 5000
```

> `frontend_render_test.py` 用本机 Edge 的 `--headless=new --dump-dom` 渲染，
> 能抓到组件运行时错误、API 调用失败、路由未匹配——比只检查 HTTP 200 强得多。

### 性能实测（5000 条）

| 指标 | 目标 | 实测 |
|---|---|---|
| 搜索 P95 | < 200ms | **35ms** |
| 反链查询 P95 | < 50ms | **0.007ms** |
| embedding 单条编码 P95 | < 300ms | **0.56ms**（降级后端） |
| 全库 FTS/反链重建 | < 60s | **11.5s** |
| embedding 全库批处理 | < 60min | **8.3s** |

> 真实 embedding 后端（bge-m3 ONNX）未安装时自动降级为确定性哈希向量，
> 流程可跑通但召回质量下降，设置页会明示。

---

## ASR 转写（已实测跑通）

真实中文 B站视频 → yt-dlp 抽音频 → faster-whisper 转写，**110s 出 756 字中文**。

两处需要留意：

1. **不依赖 ffmpeg**——`bestaudio` 直取原始音频流（B站本身就是 m4a），
   faster-whisper 用 PyAV 直接解码。只有平台提供分离流时才回落到
   `-x --audio-format m4a` 的转码路径（那步才需要 ffmpeg）。
2. **模型可离线**——`[asr] model_dir` 指向本地模型目录即可离线加载；
   留空则从 HuggingFace Hub 下载。国内网络可设 `[model] hf_endpoint` 指向镜像。

> PyAV ≥ 14 移除了 `av.open()` 的 `metadata_errors` 参数，而 faster-whisper 1.2.x
> 仍在传它——已在 `app/services/audio_compat.py` 做兼容处理（PyAV 13 没有
> Python 3.13 的 wheel，装不上，只能兼容）。

当前环境用的是 ModelScope 上的 `faster-whisper-tiny`（识别质量一般，
换成 small/medium 会明显更好）。

---

## 配置

见 `config/config.toml`（对应设计文档附录 A）。关键项：

```toml
[vault]
path = "D:/ObsidianVault"

[web]
port = 7688
allow_lan = false          # true 时强制 token

[ai]
provider = "workbuddy"     # workbuddy / ollama / openai_compat
auto_fallback = true

[ai.embedding]
model = "bge-m3"           # 本地 CPU
dim = 1024

[asr]
model_size = "small"       # small / medium
compute = "int8"
concurrency = 1
# model_dir = "D:/models/faster-whisper-small"   # 本地模型目录（离线加载）

[model]
# hf_endpoint = "https://hf-mirror.com"   # 模型下载镜像

[collect]
batch_limit = 100

[duplicate]
vector_threshold = 0.85
```

环境变量覆盖：`SECOND_BRAIN_VAULT`、`SECOND_BRAIN_CONFIG`、`SECOND_BRAIN_HOME`、
`SECOND_BRAIN_PORT`、`SECOND_BRAIN_ALLOW_LAN`。

---

## 开机自启

设置页 → 「开机自启」按钮，注册 Windows 任务计划程序（登录时启动）：

```bash
schtasks /Create /TN "SecondBrain" /TR "<python> -m uvicorn app.main:app --host 127.0.0.1 --port 7688" /SC ONLOGON /RL LIMITED /F
```

---

## 独立运行（不依赖任何会话）

打包产物在 `dist/SecondBrain/`，可以整体复制到任意目录后双击即用：

```text
SecondBrain/
├─ SecondBrain.exe        双击即用：自动起服务 + 自动开浏览器
├─ _internal/             前端 / 内嵌依赖 / yt-dlp / 配置模板
├─ config/
│  ├─ config.toml         用户可改（首次运行自动从模板生成）
│  └─ secrets.json        密钥（方舟 API Key）
├─ data/                  数据库、缓存（exe 同级）
├─ 注册开机自启.cmd        双击 → 注册登录自启任务（不需要管理员）
└─ 取消开机自启.cmd
```

**自己启动才是不受限制的**：双击 exe 启动的实例不在任何沙箱里，
CLI 登录（读 keychain）、开机自启注册都能正常工作。
由 AI 会话启动的实例会继承沙箱限制 → `schtasks` 报「拒绝访问」、CLI 读不到认证文件。

### phpStudy 反向代理（可选，固定入口）

已把站点配置装到
`D:\AllApp666\phpstudy_pro\Extensions\Nginx1.15.11\conf\vhosts\secondbrain.conf`，
在 phpStudy 面板里启动 Nginx 后即可用 `http://127.0.0.1:8088` 访问。
想用域名 `secondbrain.local`，需管理员权限在 hosts 里加一行
`127.0.0.1 secondbrain.local`。

> MySQL 用不上：md 是唯一真相源，SQLite 索引随时可重建。

## 已知限制

| 项 | 现状 |
|---|---|
| 抖音 / 快手抓取 | 依赖 yt-dlp 的现实支持度，不稳定；已降级为常态手动路径 |
| B站字幕 | 需要 cookie 才能拿到 AI 字幕；无 cookie 时只有标题+简介，靠 ASR 补 |
| 小红书 | 未登录有反爬，需 cookie |
| embedding | 未装 bge-m3 时降级为哈希向量，召回质量下降 |
| ASR 模型 | 当前用 tiny（识别一般），换 small/medium 更好；模型可本地离线加载 |
| AI 主链 | 需 CLI 交互式登录一次；**若服务跑在受限环境（沙箱）里，CLI 读不到认证文件会表现为"未登录"**——让服务在沙箱外启动即可；或改用设置页的 OpenAI 兼容端点 |
| 移动端 | 已砍（桌面端工具） |
| 托盘图标 | 未实现（可直接关浏览器 / `POST /api/shutdown`） |

## 分发与隐私（v43）

面向「把 exe 发给别人、各用各的本地数据、公开仓库」这个场景。

- **零配置启动**：配置模板 `config/config.example.toml` 里所有路径都留空，
  首次运行自动播种到用户目录 `~/.secondbrain/config.toml`。
  默认的 vault、数据库、日志、缓存都在 `~/.secondbrain/` 下，
  与 exe 装在哪无关（`C:/Program Files` 这类只读位置也能跑）。
- **升级不丢配置**：若 exe 同级 `config/config.toml` 已存在（老部署），
  仍优先读它；`active_config_path()` / `active_secret_path()` 保证
  「写入」和「读取」始终落在同一份文件上。
- **干净机器回归测试**：`TestFreshInstall`（在 `tests/test_secondbrain.py` 里）
  用临时 HOME + 临时安装目录模拟别人第一次双击，断言目录可写、
  播种的模板不含本机路径或密钥、老部署配置仍优先。
- **打包防御**：`secondbrain.spec` 在 `Analysis()` 前检查 datas，
  一旦混入 `config.toml` / `secrets.json` / `secondbrain.db` / `app.log` 直接报错。
- **数据不上云**：笔记、原文、密钥只在本机；对外网络请求仅来自用户主动的
  抓取、AI 端点调用和模型下载。详见用户说明的「隐私说明」。

```bash
# 打包（产物：dist/SecondBrain/SecondBrain.exe）
cd backend && pyinstaller secondbrain.spec --noconfirm

# 发布前本地验证
python scripts/check.py            # 全套 14 项
```

**已知限制**（第二轮再补）：没有安装包 / 卸载器，没有代码签名
（SmartScreen 会拦，用户说明里写了处理办法），没有自动更新通道。

许可：[MIT](../LICENSE) · [第三方组件声明](../THIRD_PARTY_NOTICES.md)
