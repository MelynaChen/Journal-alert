# NAND / SSD 研究主题筛选

`config.json` 的 `research` 为期刊和会议共用配置：5 类、16 个子方向、15 个机器标签。3D NAND 架构与 NAND 器件物理独立配置，共享 `nand_device`；其余标签与子方向一一对应。关闭 `research.enabled` 或使用没有此字段的旧配置，恢复原关键词算法。

## 两阶段处理

1. 识别研究主题：对标题、摘要分别进行 Unicode、大小写、连字符归一化和词边界匹配。需要上下文的信号必须在同一字段、命中前后各 140 字符内找到指定上下文。FTL、GC、FDP、PLC 需要设备或 NAND 上下文；PCM 需要非易失存储器上下文。通用 memory、storage、performance、optimization、reliability 不单独计分。单独的 SSD 信号是分类兜底；有明确 FTL、AI 等主题时采用具体主题。
2. 评分过滤：核心权重 5、技术权重 4、扩展权重 2；标题乘 3、摘要乘 1。扩展主题与设备/存储架构相邻时权重提高到 4。每个 signal.id 代表一个概念，同义词、重复出现和标题/摘要重复只计一次，标题优先。跨概念重叠文字保留命中依据但计 0 分，总分上限 60。

沿用当前阈值：最低收录 12、值得一读 15、必读 20。例如 `3D NAND retention loss`、`SSD garbage collection optimization`、`ZNS SSD write amplification` 均为 27 分；`SSD-based LLM inference`、`CXL-SSD cache architecture` 均为 15 分。交叉主题的长短词重叠不会重复加分，但主题标签和命中词仍可见。

普通新型存储材料、无设备上下文的 LSM-tree 等仅识别为外围主题，分数最高 6，默认过滤。`allow_peripheral` 可以允许这些候选继续进入阈值检查；若确实希望收录，还需相应降低 `tiers.min_score`。普通 AI、云网络、仅 DRAM 优化默认没有有效主题。若主动扩展 DRAM，需要在 contexts 中加入相应设备上下文，并增加 DRAM 专用子方向和信号，不能仅将 memory 设为高权重词。

排除词和 DOI 前缀仍然优先。研究模式把技术短语 `error correction` 从勘误排除词 `correction` 的检查中剔除，`Correction: ...` 仍排除。

## 配置结构

每个 category 有 `id`、`label`、`subtopics`；每个子方向有 `id`、`label`、可选的 `topic_id` / `topic_label`、`enabled` 和 `signals`。信号示例：

```json
{
  "id": "flash_management",
  "level": "technical",
  "context": "device",
  "terms": ["garbage collection", "gc", "wear leveling", "write amplification"]
}
```

`context` 指向 `research.contexts` 中的术语组。信号可单独设置 `weight`、`linked_weight`、`exclude_context`；`context_exclusions` 则拒绝上下文组的歧义用法，例如 SSD 的 Single Shot Detector。复数和其他同义表达需明确列入 terms。一个 signal 内所有术语最多贡献一次分数，要让独立概念分别计分，应拆成不同 ID 的 signal。

## 日报、日志与兼容性

日报按五大类组织，每篇只放入最高强度主题所属的一类，同时展示全部方向标签、期刊/会议、DOI/链接、等级、关键词及上下文。强度相同时按配置顺序选择主主题。微信摘要最多显示两个方向，仍受 `push.max_items` 和消息长度限制。

将 `log.level` 设为 `DEBUG` 可查看每篇论文的 `relevance` 日志：标题、topics、score、class、reason、matches（词、字段、分值、上下文）以及 rejected（未通过上下文的规则）。过滤原因包括 `no_valid_topic_or_context`、`peripheral_without_storage_context`、`below_min_score` 和排除词/DOI。

JSON 日快照保存分类和命中依据；旧快照无需重算，重渲染时进入“历史记录（未分类）”。卡片解析兼容旧分级章节和新主题章节。历史身份、去重和推送状态不依赖研究主题，不会因一篇有多个标签而重复推送。

## 验证与边界

执行 `python -m unittest discover -s tests -v`。`tests/test_research.py` 覆盖用户指定正反例、短词歧义、上下文距离、重复上限、DRAM 显式扩展、旧配置/快照、多标签日报、卡片和推送长度；既有测试覆盖期刊 RSS、跨来源去重、二次运行不重复推送及密钥脱敏。

这是可解释的词法筛选，不是语义模型。缺摘要、未知同义表达可能漏收；相邻但语义无关的设备词仍可能造成误收。当前只在同一字段验证上下文，因此标题缩写仅在摘要解释的论文可能漏收。新型存储和通用数据库论文的默认策略偏保守。数据源访问与覆盖缺口仍受外部服务约束，增加主题不会补齐缺失论文。
