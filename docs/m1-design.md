# M1 设计：检索与速度收尾（2026-11-03）

依据：完整设计文档 v1.0 第 5（切块）、7（选段）、13（评测）节；POC v0.2 实测中
“文档找对了、含答案的段落没送进模型”的 5 题，以及中文首 token 7–11 s。

M1 的门槛（在 Mac 上实测）：块级命中率 ≥ 80%，中文首 token 中位数 ≤ 5 s，文件级命中率不低于 v0.2。

## 1. 按语言切块

| | v0.2 | M1 |
| --- | --- | --- |
| 长度 | 所有语言 800 字符 | 中文 500 字，英文 / 法文 1,200 字符 |
| 重叠 | 100 字符 | 长度的 15%（中文 75 字，其他 180 字符） |

两种语言的片段 token 数接近（约 500 / 400），4 个片段约 2,000 tokens（v0.2 中文约 3,000），首 token 预计缩短 30% 以上。

```
Chunker(max_chars_by_lang={"zh": 500, "other": 1200}, overlap_ratio=0.15)
chunk(blocks):
    for segment in split_at_boundaries(blocks):      # 换页或前两级标题变化
        lang = detect(segment 前 3000 字)              # 段内多数语言
        max = by_lang.get(lang, by_lang["other"]); overlap = max * ratio
        pack(segment, max, overlap)                    # 沿用 v0.2 的合并 / 按句切 / 硬切
    每块再单独检测语言（检索时的语言加权仍按块）
signature() -> "zh500-other1200-ov0.15-d2"
```

配置 `indexing.chunk_chars_zh / chunk_chars_other / chunk_overlap_ratio`；旧配置 `chunk_max_chars` 仍可用，
表示所有语言同一长度（用于对照实验）。

**切块参数变化后的重建**：每个来源在 `index_meta` 记录 `chunker:<source_id>` = 签名。签名与当前不同
（或旧索引没有签名）时，这个来源的所有文件即使大小和时间未变也重新切块；未改动文本的向量仍从缓存复用。
索引完整结束（未中断）后才写入新签名，中断后下次继续重建。

## 2. 先选文档，再选段落

```
search(query, k):
    cands = RRF(向量 30 个, 关键词 30 个)                # 召回不变
    doc_rank = rank_documents(cands)                    # 每个文档取最好的 3 个块的分数之和
                                                        # soft 模式下同语言块 ×1.2 只在这里生效
    picked = select_chunks(cands, doc_rank, k, per_doc_max=3)
             # 按文档名次依次取，每个文档内按原始分数（不加权）取，最多 3 个
    chunks = merge_adjacent(load(picked))               # 同文件相邻序号的块合并，去掉重叠部分
    按文档名次、文档内按原文顺序排列返回
```

**补邻**（在问答里做，因为需要知道上下文预算）：装入片段后若还有预算，且最好的片段分数 ≥ 第二名的 1.5 倍
（例如它同时被向量和关键词找到，其他只被一种方法找到），把它前后各一个相邻块合并进来；超出预算就不补。

```
QAService.ask:
    hits = retriever.search(question, k)
    context = fit_context(hits, 预算)
    context = expand_best(context, retriever.neighbors, 剩余预算, ratio=1.5)
```

接口变化：

| 位置 | 变化 |
| --- | --- |
| `RetrievedChunk` | 新增 `file_id`、`ordinal`、`ordinal_end`（合并后的范围） |
| `Repository.load_chunks` | 带出 `file_id`、`ordinal` |
| `Repository.neighbors(file_id, ordinals)` | 新增：按序号取相邻块 |
| `lkos/retrieval/select.py` | 新增纯函数：`rank_documents`、`select_chunks`、`merge_adjacent`、`merge_texts` |
| `Retriever.search(..., strategy=None)` | `strategy` = `doc_first`（默认）/ `flat`（v0.2 做法，用于对照） |
| 配置 `retrieval` | `strategy`、`doc_score_top`（3）、`per_doc_max`（3）、`merge_adjacent`（true）、`neighbor_ratio`（1.5，0 = 关闭补邻） |
| 命令行 | `ask`、`search`、`eval` 新增 `--strategy` |

## 3. 块级评测

题目新增可选字段（不写则只评文件级）：

```yaml
- question: 部署 DeepSeek V3 的最小配置需要几台 Atlas 800I A2？
  expected_files: [昇腾和DeepSeek适配情况内部沟通FAQ V1.0版本 (脱敏版).docx]
  expected_text: 最小配置为4台        # 含答案的原文片段（去空白、全半角后比较）
  expected_page: 12                   # 可选：PDF 页码；DOCX / MD 用 expected_heading
  expected_answer: ["4台", "4 台", "四台"]   # 回答里出现任意一个即判为答对
```

| 指标 | 定义 |
| --- | --- |
| 块级命中 | 送进模型的片段中，有来自期望文件、且包含 `expected_text`（或位于期望页码 / 标题下）的片段 |
| 答案正确率 | 有 `expected_answer` 的题中，回答包含其中任一写法的比例 |
| 文档对但答没找到 | 文件级命中、回答却是“没有找到” |
| 无出处回答 | 回答不是“没有找到”，但没有任何有效引用 |

评测题目放在 `data/`（含文档内容，不入库）；仓库里的样例语料与测试覆盖每个指标的判定逻辑。

## 4. 测试计划

- 单元：按语言选长度、重叠比例、签名；签名变化触发重建、中断后不写签名；文档排序（加权只影响文档排序）、
  每文档上限、相邻合并去重叠、补邻在预算内 / 超预算不补 / 分数接近不补；评测各指标判定；配置校验。
- 功能：`lkos eval --strategy flat|doc_first` 输出块级命中与答案正确率；`--out` JSON 含新字段。
- Mac 实测（`run_m1.command`）：重建索引后，对比 v0.2（flat + 800 字）与 M1（doc_first + 按语言切块）的
  文件级 / 块级命中、答案正确率、首 token、内存。
