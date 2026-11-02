# Local Knowledge OS · POC（命令行版）

本地知识库 POC：索引一个文件夹里的 PDF、DOCX、Markdown，用 Ollama 上的 Qwen3 4B 回答问题，每个回答都附带文件名和页码、标题路径或行号形式的引用。

设计依据见《Local Knowledge OS 需求文档》和《Local Knowledge OS POC 设计文档》。

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

### 5. 评测（POC 验收）

参考 `examples/questions.example.yaml` 编写 30 个测试问题（中文 20、英文 5、法文 5），标注每题应命中的文件：

```bash
lkos eval my-questions.yaml                 # 检索命中率，目标 ≥ 80%
lkos eval my-questions.yaml --with-answer   # 加上引用有效率（目标 ≥ 90%）和首 token 时间（目标 ≤ 5s）
```

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
  chunk_max_chars: 800
  chunk_overlap_chars: 100
retrieval:
  top_k: 5
  language_filter: auto   # auto：只匹配与问题同语言的文本块；all：不过滤
privacy:
  remote_enabled: false
  offline_mode: true
```

更换 Embedding 模型或维度后需要重建索引：删除 `data/lkos.db` 后重新执行 `lkos index`，程序检测到不一致时也会提示。

## 四、与设计文档的差异

| 项目 | 设计文档 | 实现 | 原因 |
| --- | --- | --- | --- |
| 数据目录 | `~/Library/Application Support/LocalKnowledgeOS/` | 项目内 `data/`，可通过 `LKOS_HOME` 修改 | 要求所有代码和数据留在项目文件夹内 |
| 切块边界 | 不跨一级标题 | 不跨前两级标题 | Markdown 常把一级标题当文档标题，只按一级切会让整篇合成一块，引用过粗 |
| 未变化判断 | 每次都计算哈希 | 大小和修改时间都没变时跳过哈希 | 全量 2–4 GB 时节省大量读盘时间；内容哈希仍用于改名识别 |
| 模型引用 | `providers.ollama` | `providers.local`（`type: ollama`） | 与 `local/qwen3:4b-instruct` 这种引用格式保持一致 |
| 聊天模型标签 | `qwen3:4b` | `qwen3:4b-instruct`（Qwen3-4B-Instruct-2507，Q4_K_M） | `qwen3:4b` 实际指向 Thinking-2507，实测设置 `think: false` 后仍先输出推理内容；该次首 token 86.9 s，但主要耗在首个 token 之前（推测为冷启动加载 + 内存换页），与推理无直接关系 |

## 五、已知限制（POC 范围外）

- 扫描页没有文本层，会跳过，并在 `lkos status --failed` 中显示为 “no extractable text”（不做 OCR）。
- 不支持 .doc、.ppt、.pptx。
- 不做定时刷新，不做多轮对话记忆。
- Hermes 远端只保留接口；启用远端后调用会明确报错“尚未实现”。
