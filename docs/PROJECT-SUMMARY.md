# Local Knowledge OS · 项目摘要（新会话从这里开始）

更新：2026-11-03 · 仓库：`~/Project/claude space/lkos`（Git，main 分支）

## 目标与约束

- 本地离线知识库：MacBook Air M2 8 GB；索引约 100 GB 文档（PDF / Word / PPT / Markdown，中文为主，英、法为辅）；问答、总结、提取、分类、深度分析，回答必须带原文出处。
- 单机单用户（预留 `owner_id`）；远端（Hermes Proxy）只保留接口、默认关闭；不需要跨语言翻译，也不会用中文问纯英文文档，但中英混合文档会用两种语言提问。
- Embedding 固定 bge-m3（不更换）；聊天模型 qwen3:4b-instruct（`qwen3:4b` 是 Thinking 版，关闭思考无效）。

## 文档

| 文档 | 位置 |
| --- | --- |
| 需求文档 | https://claude.ai/artifact/D9pTz4bDXmkVsvop2NqbqE |
| POC 设计文档 | https://claude.ai/artifact/NpnHRhSJ6XT7V5WtCAXHwb |
| **完整设计文档 v1.0（MVP 依据）** | https://claude.ai/artifact/241GgcBDrTKNi5bHhnuHXQ |
| 设计变更与实测 | `docs/design-changes-v0.2.md`、`docs/poc-results-*.md` |

## POC 现状（v0.2，208 项测试通过）

- 已实现：PDF/DOCX/MD 解析（页码、标题路径+段落、行号）；增量（大小/时间/哈希、改名识别、删除清理、续传）；二值粗筛 + int8 精排向量（100 万块约 0.3 s）；Embedding 按文本哈希缓存；FTS5 + jieba；RRF；同语言加权（soft）；4K 上下文、4 片段、输出 512；引用校验；Model Gateway（不回退远端、调用日志）；闲时索引 `--when-idle`（问答 / 电池 / 使用中暂停，最近文件优先）；CLI：init / index / search / ask / chat / status / eval。
- 真实文档实测（17 文档、31 题）：检索 87%（范围内基本全中）；4B 引用率 74%、未见编造，但 5 题“文档对、段落没送进去”；首 token 中位数 6.4 s（中文 7–11 s）；问答时 4B 3.2 GB + bge-m3 0.69 GB，8 GB 到上限；索引 9.7 块/秒（96% 在向量计算），100 GB 首次约 20–86 小时；1.7B 快但有编造，不作默认。

## 下一步（设计文档第 14 节）

M1 检索与速度收尾（块级评测 60 题、先选文档再选段、中文切块约 500 字）→ M2 后台 daemon（schema v3、任务表、`lkos daemon`、每周定时与补跑、launchd）→ M3 格式扩展（PPTX、DOC 转换、PPT 跳过并提示、Vision OCR）→ M4 任务与命令行体验（总结、提取、分析、标签、历史、`lkos open`、`doctor`）→ M5 正式索引与验收。

已定（2026-11-03）：只用命令行（问答在 CLI 进程里跑，`lkos daemon` 常驻只做索引，两者通过 `data/qa.active` 与任务表协调，不提供 HTTP API / 网页）；数据继续放在项目内 `data/`；刷新周期默认每周；不装 LibreOffice（PPT 旧格式跳过并提示）。
待定：第一批正式索引的来源文件夹（M5 前确认）。

## 工作约定

- 一律用中文回答；先设计后编码；每个改动先跑测试，再单独提交 Git（作者 xuanqi，附 `Co-Authored-By: Claude`），不改写已有历史。
- 代码、测试、数据都留在项目文件夹内（POC 数据在 `data/`，不入库；`logs/` 不入库）。
- 我的沙箱（Linux VM）连不到 Mac 上的 Ollama：真实模型测试由用户双击 `.command` 运行（`run_realdocs.command`、`run_index_idle.command`、`run_poc.command`），我读 `logs/` 分析。
- 用 `sed -i` 改脚本会丢可执行权限，改完要 `chmod 755` 并确认。
- 网络：Shadowrocket fake IP 会让 Ollama 拉模型失败，需让 `*.r2.cloudflarestorage.com` 直连；PyPI 慢时用 `PIP_MIRROR`。
- 测试命令：`scripts/test.sh`（Mac）/ `PYTHON=.venv-linux/bin/python scripts/test.sh`（沙箱）；真实模型：`scripts/test.sh --live`。
