# 会议监控验收记录

日期：2026-10-10（北京时间）。测试在 Windows 本地运行，Python 标准库；联网探测经授权在沙箱外执行，未关闭 TLS 证书校验，没有发送真实微信消息，也没有改写生产历史库。

## 真实接口测试

命令：`python tools/probe_conferences.py`。会议 Crossref 索引回溯起点为 2026-09-10，论文集发现扫描本年和此前 3 年。数量是本次适配器返回的元数据数，不是相关论文推荐数，也不代表所有论文均已收录。

| 路径 | 结果 |
|---|---|
| Crossref → IEDM | 成功，105 条，通过会议名称校验并转换为 Item |
| Crossref → IRPS | 成功，61 条，部分记录没有具体日期，保留空值 |
| Crossref → VLSI Symposium | 成功，51 条 |
| OpenAlex → 上述三会议的 DOI 补全 | 各抽测 1 篇，DOI 一致，摘要倒排索引存在 |
| OpenAlex → FAST 独立来源发现 | 未找到通过校验的 conference source，明确报告覆盖缺口 |
| DBLP → FAST | 未通过：本机 TLS 报 CERTIFICATE_VERIFY_FAILED；解析、分页及归属验证仅用 Mock 验证 |
| 原期刊 OpenAlex → IEEE Transactions on Electron Devices | 成功，限制 1 条，返回 1 条 |
| 原期刊 Crossref → 同刊 | 成功，限制 1 条，返回 1 条 |
| 原期刊 RSS → Nature Electronics | 成功解析 8 条 |

真实 DOI 抽样：

- IEDM：`10.1109/iedm50854.2024.10872993`
- IRPS：`10.1109/irps48203.2023.10117832`
- VLSI：`10.23919/vlsitechnologyandcir65189.2025.11074786`

其余会议的真实抓取、所有年度覆盖、GitHub 托管 runner 的实际运行以及三个微信渠道的实际发送，本次未验证。工作流保留定时、手动触发、Secrets 和状态提交，并增加离线测试步骤；不能以本地 Mock 测试替代云端真实验收。

复测时 IEDM 曾出现一次请求失败（探测脚本故意关闭重试），IRPS/VLSI 和期刊探测仍成功。因此上述成功记录不代表 API 始终可用；正式配置启用 1 次重试并记录失败诊断。

## 离线测试

命令：`python -m unittest discover -s tests -v`。

结果：28 项测试全部通过；`git diff --check` 无错误。还比较了原始与当前配置，原有全部配置段的值保持一致，仅新增 `conferences`。

覆盖原期刊 RSS、期刊评分保持一致、关闭会议时无会议请求、旧快照、无摘要/无 DOI/无日期、双源同 DOI、DOI 后补跨运行去重、不同年份/会议/DOI 不合并、首轮限量与第二次运行不推送、上一年度晚收录、分页/来源校验、失败隔离、缓存、倒排摘要、微信长度和密钥脱敏。端到端测试调用 `run.main`，使用临时数据库、真实报告生成以及 Mock 网络/推送。

## 重复推送检查

新增的 observations 身份关联与已有 articles 台账共同判重，完整状态仍在原有 `state/seen.sqlite`，由 Actions 的状态提交步骤持久化。首次超限历史也写台账，避免下一轮重新成为候选。干跑在内存副本中写状态，不消耗真实历史。

沿用原有先保存后推送：失败不自动重发，可能漏通知；微信消息限额之外的条目仍在日报中。状态丢失或回滚、工作流状态写回失败、两套独立部署同时运行会有重复风险。无 DOI 且来源年份或会议元数据冲突时，不能保证判定为同一篇。生产启用后应确认状态提交成功，不能声称严格 exactly-once。
