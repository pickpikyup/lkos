# Local Knowledge OS（命令行版）

本地离线知识库：索引你的 PDF、Word（DOCX）、Markdown，用 Ollama 上的 Qwen3 4B 回答问题，
每个回答都附带文件名和页码、标题路径或行号形式的出处。文档内容不会离开这台电脑。

## 马上使用

前提：已经运行过 `scripts/setup_mac.sh`（建环境、下载模型），并且 Ollama 应用开着。

**双击项目里的 `LKOS.app`** 打开窗口（第一次会自动安装窗口组件，约一分钟；出问题时改双击 `LKOS.command`，终端里能看到原因）。

- 左下角“添加文件夹”，或把文件夹拖进窗口；索引在后台进行，提问时自动让路
- 直接输入问题回车；回答里的 [1]、[2] 点一下，右侧显示原文并高亮，“打开原文件”跳到原文
- 输入框左边切到“搜索”只找原文、不生成，零点几秒出结果
- ⌘N 新问题，Esc 停止生成，⌘1–⌘9 切换出处
- 资料库页：每个文件夹的进度、暂停 / 继续、立即更新、移除；没能索引的文件和处理办法

也可以在终端里用命令行：运行 `bin/lkos` 进入交互式问答：

```
问> /add ~/Documents/工作资料          ← 添加文件夹并建索引
问> 部署 DeepSeek V3 最少需要几台 Atlas 800I A2？
问> /open 1                             ← 打开出处的原文件
问> /help                               ← 全部命令
```

想在任何目录直接输入 `lkos`，把这行加入 `~/.zshrc`，然后重新打开终端：

```bash
alias lkos='"<项目文件夹>/bin/lkos"'
```

不进入问答界面也可以直接用命令：

```bash
lkos ask "问题"                 # 单次问答
lkos search "关键词"            # 只搜索
lkos source add <文件夹>        # 添加文件夹并建索引
lkos source list                # 已添加的文件夹
lkos source remove <编号>       # 移除文件夹的索引（不删原文件）
lkos update                     # 更新所有文件夹的索引
lkos status --failed            # 索引规模；列出没能索引的文件
```

第一次索引大量文档需要较长时间（约每秒 10 个文本块），可以随时 Ctrl-C 停下，下次 `/update` 接着做。
目前支持 PDF、DOCX、Markdown；扫描件（没有文字层的 PDF）、加密文件、旧版 .doc / .ppt 会被跳过。

设计依据见《Local Knowledge OS 需求文档》《完整设计文档 v1.0》与 `docs/m1-design.md`。

## 一、在 Mac 上运行

### 一键搭建与验证

装好 Ollama 后，在项目目录执行：

```bash
scripts/setup_mac.sh                      # 下载模型 → 建环境 → 全部测试（含真实模型）→ 样本演示 → 内存基准
scripts/setup_mac.sh --docs ~/Documents/测试文档   # 演示改为索引你自己的文件夹（最多 300 个文件）
```

下面是脚本执行的各个步骤，也可以手动逐步执行。

### 1. 安装 Ollama 并下载模型

```bash
# 从 https://ollama.com 下载安装 Ollama，打开应用后执行：
ollama pull qwen3:4b-instruct   # 不要用 qwen3:4b：该标签指向 Thinking-2507，关闭思考无效，回答前会先输出推理
ollama pull bge-m3
```

模型文件存放在 `~/.ollama`，由 Ollama 管理，不在本项目文件夹内。

### 2. 创建 Python 环境

macOS 自带的 Python 不支持 SQLite 加载扩展，sqlite-vec 无法使用，所以要用 uv 安装的 Python：

```bash
cd lkos
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[test]"
```

### 3. 初始化并检查模型

```bash
lkos init
```

两个模型都显示“就绪”即可继续。数据（数据库、配置、缓存）默认放在项目内的 `data/`，也可以用 `--home` 或环境变量 `LKOS_HOME` 指定其他位置。

### 4. 索引与问答

```bash
lkos index ~/Documents/测试文档 --limit 300   # 先用 200–500 个文件试
lkos status
lkos search "二期预算上限"
lkos ask "项目二期的预算上限是多少？"
lkos ask "What is the retention policy?" --show-context
lkos chat                                     # 连续提问，exit 退出
```

### 闲时索引

大量文档首次索引耗时很长（100 GB 估计 20–86 小时）。用 `--when-idle` 让索引只在空闲时运行：

```bash
lkos index ~/Documents/资料 --when-idle    # 或双击 run_index_idle.command
```

- 正在问答（`ask` / `chat` / `search` / `eval`）时暂停，答完继续：两者共用一块 GPU 和 8 GB 内存，同时跑只会互相拖慢；
- 你在用电脑时暂停，键盘鼠标空闲 2 分钟后继续（`indexing.idle_seconds`）；
- 使用电池时暂停，接通电源后继续（`indexing.require_ac_power`）；
- 最近修改的文件先索引（`indexing.order: recent_first`），中途停下时常用文档已可检索；Ctrl-C 随时停止，下次续传。

### 5. 评测（POC 验收）

参考 `examples/questions.example.yaml` 编写 30 个测试问题（中文 20、英文 5、法文 5），标注每题应命中的文件：

```bash
lkos eval my-questions.yaml                 # 检索命中率，目标 ≥ 80%
lkos eval my-questions.yaml --with-answer   # 加上引用有效率（目标 ≥ 90%）和首 token 时间（目标 ≤ 5s）
lkos eval my-questions.yaml --with-answer --model local/qwen3:1.7b --out logs/1.7b.json   # 换模型对比，保存每题回答
lkos eval my-questions.yaml --strategy flat # 用 v0.2 的选段方式对照（默认 doc_first：先选文档再选段落）
```

题目可以加 `expected_text`（含答案的原文）、`expected_page`（PDF 页码）或 `expected_heading`（标题），
评测时额外统计**块级命中率**（含答案的段落是否送进了模型，目标 ≥ 80%）；加 `expected_answer`（答案的几种写法）
时统计**答案正确率**。写法见 `lkos/evaluation/evaluate.py` 开头的说明。

M1 实测：双击 `run_m1.command`，用 `data/m1-questions.yaml` 对比 v0.2 与 M1 的命中率、答案正确率与首 token。

### 6. 内存基准（实施第 1 步）

先打开 10 个浏览器标签页，再执行下面的命令，同时观察 Activity Monitor 中的“内存压力”：

```bash
python scripts/benchmark_memory.py --rounds 10
```

## 二、测试

```bash
scripts/test.sh          # 单元测试 + 功能测试 + 覆盖率（不需要 Ollama）
scripts/test.sh --live   # 额外运行真实模型测试（需要 Ollama 和两个模型）
```

测试的临时文件都放在项目内的 `.pytest_tmp/`，可以随时删除。

| 类别 | 位置 | 内容 |
| --- | --- | --- |
| 单元测试 | `tests/unit/` | 配置、存储、解析器、切块、语言检测、切词、Gateway、Ollama（模拟 HTTP）、索引、检索、问答、引用校验、评测 |
| 功能测试 | `tests/functional/` | 按验收标准逐项运行 CLI：全流程、增量更新、改名识别、删除清理、失败文件、无效引用移除、离线模式禁止远端、本地模型故障不回退、Embedding 变更检测、评测命令 |
| 真实模型测试 | `tests/live/` | Ollama 可用性、Embedding 维度、关闭思考模式、端到端问答（默认跳过） |

单元测试和功能测试使用 `FakeProvider`，即确定性的假模型，不依赖 Ollama，结果可以复现。

## 三、配置

`data/config.yaml` 中只需写要覆盖的字段，完整默认值见 `lkos/config.default.yaml`。常用项：

```yaml
indexing:
  chunk_chars_zh: 500       # 中文段落每块约 500 字
  chunk_chars_other: 1200   # 英文、法文每块约 1,200 字符
  chunk_overlap_ratio: 0.15
retrieval:
  top_k: 4
  strategy: doc_first     # 先选文档再选段落 | flat：v0.2 做法
  per_doc_max: 3          # 同一文档最多送 3 个片段
  language_filter: soft   # soft：同语言加权但不排除 | strict：只检索同语言 | all：不区分
  vector_candidates: 200  # 二值粗筛候选数
privacy:
  remote_enabled: false
  offline_mode: true
```

更换 Embedding 模型或维度后需要重建索引：删除 `data/lkos.db` 后重新执行 `lkos index`，程序检测到不一致时也会提示。
v0.1 建的索引与 v0.2 格式不兼容，同样需要删除后重建。
修改切块参数不需要删除数据库：下次 `lkos index` 会自动重新切块（未改动文本的向量从缓存复用）。
旧配置里的 `chunk_max_chars` 仍然有效，表示所有语言同一长度；想用按语言切块，删掉这一行即可。

**8 GB 内存建议**：运行一次 `scripts/ollama_tune.sh`，开启 Flash Attention 与 8 bit KV 缓存（重启电脑后需再运行）。
用 `scripts/ollama_tune.sh --check` 查看当前设置。

## 四、与设计文档的差异

| 项目 | 设计文档 | 实现 | 原因 |
| --- | --- | --- | --- |
| 数据目录 | `~/Library/Application Support/LocalKnowledgeOS/` | 项目内 `data/`，可通过 `LKOS_HOME` 修改 | 要求所有代码和数据留在项目文件夹内 |
| 切块边界 | 不跨一级标题 | 不跨前两级标题 | Markdown 常把一级标题当文档标题，只按一级切会让整篇合成一块，引用过粗 |
| 未变化判断 | 每次都计算哈希 | 大小和修改时间都没变时跳过哈希 | 全量 2–4 GB 时节省大量读盘时间；内容哈希仍用于改名识别 |
| 模型引用 | `providers.ollama` | `providers.local`（`type: ollama`） | 与 `local/qwen3:4b-instruct` 这种引用格式保持一致 |
| 向量存储（v0.2） | float32 向量，sqlite-vec 全量余弦 | 二值向量粗筛 + int8 精排，按文本哈希去重缓存 | 面向 100 GB 级语料：向量体积约为原来的 1/2.5，100 万块检索约 0.3 s、进程内存约 50 MB（`scripts/benchmark_scale.py`） |
| Embedding 计算（v0.2） | 每块都计算 | 相同文本只算一次；修改文件时只重算改动的块 | 页眉页脚、模板页大量重复；增量更新大幅减少计算 |
| 语言处理（v0.2） | 只检索与问题同语言的块 | 默认同语言加权（soft），可切换 strict / all | 真实文档实测：硬过滤不提升同语言命中率，却会排除中英混合文档中另一种语言的答案 |
| 语言检测（v0.2） | 汉字占字母比例 ≥ 0.3 | 汉字数 ≥ 英文单词数 | 夹带大量型号、模型名的中文段落曾被误判为英文 |
| 上下文与片段（v0.2） | 8K 上下文、5 个片段、输出上限 1,024 | 4K 上下文、4 个片段、输出上限 512 | 8 GB 实测交换区增长约 8 GB；回答多在 50 tokens 以内 |
| 聊天模型标签 | `qwen3:4b` | `qwen3:4b-instruct`（Qwen3-4B-Instruct-2507，Q4_K_M） | `qwen3:4b` 实际指向 Thinking-2507，实测设置 `think: false` 后仍先输出推理内容；该次首 token 86.9 s，但主要耗在首个 token 之前（推测为冷启动加载 + 内存换页），与推理无直接关系 |

## 五、已知限制（POC 范围外）

- 扫描页没有文本层，会跳过，并在 `lkos status --failed` 中显示为 “no extractable text”（不做 OCR）。
- 不支持 .doc、.ppt、.pptx。
- 不做定时刷新，不做多轮对话记忆。
- Hermes 远端只保留接口；启用远端后调用会明确报错“尚未实现”。
