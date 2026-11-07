# BioRadar 开发约定

先读 README.md。代码在 zktest 的 /home/zk_test/zk_projects/BioRadar，主线只有 main。

## 先把事情做完

- 交付单位是用户能完成的一条操作：提问 → 取证 → 报告 → 核验 → 追问或导出。只建接口、写计划不算完成。
- 有合理默认值就继续做。遇到错误先诊断、修复；需要外部条件时写清依赖，同时完成不受影响的部分。
- 交接先写已经能做什么、怎么用、实际验过什么；没做到的写进 README 的「未完成」，写具体原因和下一步。
- 测试数据、真实材料、机器判断和人工决定分开记录。不编造模型调用、研究成绩或接通状态。

## 代码要简单

- 一个 Python 应用（FastAPI + SQLAlchemy + 一个 RQ 队列）、一个 Vue 应用、PostgreSQL、Redis。普通函数优先。
- 不加通用 Agent、工作流平台、第二队列、插件框架、注册中心，也不加没有当前用途的抽象层。
- 哈希只用在三处：Fabric 给的来源版本号、分析输入去重（`analyses.input_hash`）、模型调用幂等。不为普通修改加封存、放行、修订单、审批记录。
- 模型输出要小：最多两层、少量必填字段。编号（E1、Q1、F1、A1、句子序号）由程序分配，模型只引用。无效的一条丢掉并记入 notes，不让整份输出失败。
- 结构化阶段（规划、阅读、计算、评估、分诊）关闭思考模式。
- 证据原文只存一次（`evidence` 表），模型调用只记一次（`llm_calls` 表）。progress 里不放请求和原始响应。
- 测试覆盖真实错误和容易误伤的正确例子，不写断言手续的测试。

## 维护分工

| 部分 | 维护人 | 文件 |
| --- | --- | --- |
| 运行、接入、预算、部署 | 李明洋 | `app.py`、`config.py`、`db.py`、`ops.py`、`text.py`、`adapters/`、`research/analyses.py`、`research/views.py`、`research/lease.py`、`research/clock.py`、`research/lineage.py`、`db/`、`ops/` |
| 研究方法：调查流程、提示词、计算、监测与专题规则 | 孙博文 | `research/pipeline.py`、`research/run/`、`research/state.py`、`research/prompts.py`、`research/schemas.py`、`research/compute.py`、`research/monitor.py`、`research/topics.py`、`research/triage.py`、`research/series_sync.py`、`research/domain.py`、`research/series.py`、`research/scheduling.py` |
| 页面、导出、效果评测 | 刘金彦 | `apps/web/`、`scripts/scenarios.py` |

一个文件一个维护人。跨模块先对齐输入输出，各改各的。

## 使用已有资产

- 徐子雯的 Data-Fabric 提供原文、版本、可见时间和材料关系（更正、撤回、转发、同源、删帖辟谣）；唐蔚雯维护图谱。雷达只读 Fabric，不写它的库。
- 李明洋维护 WeKnora 检索；命中只是指针，原文一律回 Fabric 按固定版本读取。
- query-planner（李明洋 / 孙博文）选下一步和决定停止；BioCheck（潘倩）写稿和逐句核验；MechEvidence（汪鑫怡）评价机制证据。三者都是独立的 HTTP 服务，自己调模型，响应里返回 `llm_calls`，雷达记账。
- 雷达不复制它们的算法。服务没配置时，对应步骤记为未运行，不用别的方法顶替。

## 研究执行

- 主动提问、追问、专题自动分析、历史回放都走 `research/analyses.py` → `research/pipeline.py`（`Run`：阶段表、存盘、记账；各阶段在 `research/run/`）。
- 进度的每个键在 `research/state.py`（RunState）里声明；对外视图只输出标了 SHOWN 的字段（`research/views.py`），新字段默认不外露。
- 只有 BioCheck 核验 PASS、且报告各节里设计 1.3 的五项（主要判断、业务影响、支持和反对的证据、局限、会改变判断的证据；`research/run/sections.py`）齐全的报告可交付，其余标为待核（`publishable: false`、`hold: pending_verification`）；每次带证据的外发前向 Fabric 确认来源仍可读（见 `docs/decisions/0005`）。
- 每个阶段结束保存一次进度；中断后从保存的阶段继续，正在进行的那次调用会重发一次（已知成本）。
- 历史回放必须带截止时间，Fabric 只返回截止前可见的版本。不得用后来才知道的名称或结局作检索词。
- 保留 baseline（固定流程）作对照。改 planner 方法时不悄悄改 baseline 的顺序和范围。
- PASS 只表示 BioCheck 本次机器核验没发现问题；独立复核、科研结论成立、人工采用是不同的结果，分开记录。

## 源码、配置与运行

- 只在 zktest 改源码。开始前看 Git 状态，不覆盖学生或别的会话的修改。正常提交，不强推，不重写历史。
- 凭据、SSH 密码、模型密钥只放私有配置（0600），不进代码、报告、前端或日志。
- 现役服务按 `ops/systemd/` 运行；测试用独立数据库、Redis 库号和端口，跑完关掉。

## 完成检查

交付前运行 `make check`（`make test`、`make test-pg`、`make lint`、`make metrics`、`make web-check`）。改到数据库或队列时，再在真实 PostgreSQL/RQ 上跑一次提问到报告的完整流程。
