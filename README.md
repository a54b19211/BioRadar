# BioRadar

BioRadar 把资料变化变成可追溯的调查：主动发现线索，接受研究问题，读取固定原文，选择下一步取证或计算，保存核验后的报告状态，并在新材料到来后另存判断。本轮工程链路已有实测，2026-11-01 的 A/B/C/D 业务验收未通过。

## 谁维护什么

| 部件 | 维护人 | 输入、输出和调用时机 |
| --- | --- | --- |
| Data-Fabric | 徐子雯 | 采集、清洗、身份依据、原文及版本、历史可见时间；资料准备与每次原文读取都依赖它。 |
| 图谱 | 唐蔚雯 | 对象、关系及其研究出处；需要识别对象、竞争关系或机制路径时由路由选择查询，执行器调用 Fabric 的受控图接口。Cypher 留在图服务中。 |
| WeKnora | 李明洋集成维护 | 接收 Fabric 固定版本，进行分块、embedding、召回和 rerank；明确名单的特征用于计算。检索命中回到原文版本，top-k 不充当完整统计分母。 |
| query-planner | 李明洋 | 根据已有回执、可执行动作和证据缺口选择下一步或建议停止。它在模型调用之间决策；雷达执行调用、硬预算和恢复。 |
| MechEvidence | 汪鑫怡 | 路由选中专业评价或问题要求机制评价时，判断证据怎样支持机制，返回冲突、适用范围和补查需求。 |
| BioCheck | 潘倩 | 生成前规划、实际写稿、全文核验、有限修订及所选支持片段复核。进入正式成稿链必须检查实际正文，路由不能把未核稿标成 PASS。 |
| Data Factory | 李明洋 | 离线公开任务、隐藏答案、路由评测和已保存失败诊断；不接管线上调查。 |
| 雷达运行 | 李明洋 | API、适配器、数据库、队列、统一模型执行和恢复。 |
| 雷达研究 | 孙博文 | 数据加工、算法、模式评价、线索与长期专题的研究计算。 |
| 雷达交付 | 刘金彦 | 页面、报告阅读与导出、人工判断和效果评测。 |

`research/datasets/` 处理单位、成员、时间与资格；`research/mining/` 执行纯计算及模式评价；`research/investigations/` 组织调查；`adapters/` 调用外部服务；`topics/` 保存长期状态；`delivery/` 与网页展示实际结果。管线、科研、商业、公卫已有不同数据口径。方向名称本身不提供数据或专用算法。

图谱和专业评价按问题选择；证据身份、来源许可、历史截点、调用记账和成稿核验由执行流程保证。图谱背景、原始研究和转载不能互相替代。专题或追问产生新分析，旧报告、引用、机器状态和人工意见继续保存。

## 启动与接入

交付版本在 `main`。zktest 的原 BioRadar、Fabric、WeKnora 目录仍给现役服务使用，保留原源码检出；本轮未部署合并版本。可用 `git show main:README.md` 阅读最新说明，在新克隆中启动学生实例。

需要 Python 3.12、Docker Compose 和 Node 22。先在新克隆运行：

```sh
make bootstrap
make init
export PATH="$PWD/.tools/bootstrap/bin:$PATH"
```

编辑新克隆的 `ops/.env`：设独立 `COMPOSE_PROJECT_NAME`，选未占用的 `POSTGRES_PORT`、`REDIS_PORT`、`API_PORT`、`WEB_PORT`，再运行 `make dev`。默认端口 15436/16379/18100/15176 在 zktest 已用于原服务，应先修改。`make status` 查看本实例，`make stop` 停本实例并保留数据卷。

Compose 提供基础框架。完整研究还需在 API、worker、monitor 的同一私有运行环境配置下列项目；直接启动 Python 进程时变量统一带 `BIORADAR_` 前缀，类型和默认值见 [config.py](services/bioradar/api/config.py)。

| 配置 | 要求 |
| --- | --- |
| `DATABASE_URL`、`REDIS_URL`、`QUEUE_PREFIX` | 同一学生实例，队列前缀独立。 |
| `ENGINE_WORKSPACE_ID`、`ENGINE_DEV_CREDENTIAL` | 页面/脚本与后端一致。 |
| `DATA_FABRIC_URL`、`DATA_FABRIC_HISTORY_READ_TOKEN` | 已授权的历史原文接口。 |
| `DATA_FABRIC_SOURCE_INSTANCE`、`DATA_FABRIC_DEMO_SOURCE_INSTANCE` | 真实和模拟空间对应实际来源。 |
| `ENGINE_SOURCE_ALLOWLIST`、`ENGINE_SOURCE_ID_ALLOWLIST` | JSON 实例列表和实例→source_id 列表，两层都需配置。 |
| `QUERY_PLANNER_PROJECT_DIR` | 同机 query-planner 的 main 目录。 |
| `MECH_EVIDENCE_TRANSPORT`、`MECH_EVIDENCE_SERVICE_URL`、`MECH_EVIDENCE_SERVICE_TOKEN` | HTTP 模式及汪的服务地址/令牌。 |
| `BIOCHECK_TRANSPORT`、`BIOCHECK_SERVICE_URL`、`BIOCHECK_SERVICE_TOKEN` | HTTP 模式及潘的服务地址/令牌。 |
| `WEKNORA_KDD_URL`、`WEKNORA_KDD_TOKEN`、`WEKNORA_KDD_KNOWLEDGE_BASE_ID`、`WEKNORA_KDD_SOURCE_SCOPE` | 固定版本索引、特征与检索；模拟库另用 DEMO 配置。 |
| `SEMANTIC_LLM_ENABLED`、`SEMANTIC_LLM_PROVIDER`、`SEMANTIC_LLM_BASE_URL`、`SEMANTIC_LLM_MODEL`、`SEMANTIC_LLM_API_KEY` | 雷达统一持有模型配置，汪/潘的 HTTP 服务只返回下一次请求及结果。 |

私有环境文件权限设为 0600。先设 `MONITOR_ENABLED=false`、`ANALYSIS_AUTO_ENABLED=false` 完成一次主动调查，再启用持续监测。直接进程方式还需以该环境执行迁移，分别启动 API、worker、monitor：

```sh
export PYTHONPATH="$PWD/services"
uv run --frozen alembic -c db/alembic.ini upgrade head
uv run --frozen uvicorn bioradar.api.app:app --host 127.0.0.1 --port 18110
# 另两个终端使用同一私有环境
uv run --frozen python -m bioradar.ops.worker
uv run --frozen python -m bioradar.ops.monitor
```

## 用一个真实问题完成任务

在“提问与调查”选择 `query_planner`，输入：“BCMA×CD3 双特异性抗体用于多发性骨髓瘤，哪些管线值得继续调研？比较证据阶段、竞争项目、失败原因与安全性限制。”只选择服务实际授权的来源。

检查每一步实际取回的原文版本、路由动作、汪的机制评价、潘的生成与核验，以及最终 PASS/FAIL/REVIEW/NOT_RUN。下载保存版本，再追问“哪些结论只来自摘要，补齐原文后是否需要修订？”；新结果应保留与旧分析的关系。自动发现和专题更新复用同一执行器。

可用 [research_e2e.py](scripts/research_e2e.py) 完成相同的首问、追问、回读和导出检查。脚本与 API 使用同一私有环境，要求 `BIORADAR_ENVIRONMENT=test`，并使用已创建和迁移的 `bioradar_research_*`、`bioradar_query_*` 或 `bioradar_kdd_*` 独立数据库；基础 Compose 的 development 库不满足这个脚本的条件。下面的来源名须在实例白名单中存在；脚本会产生实际模型费用。

```sh
uv run --frozen python scripts/research_e2e.py   --radar-url http://127.0.0.1:18110 --policy query_planner   --question 'BCMA×CD3双特异性抗体管线有哪些证据和主要风险？'   --followup '哪些关键结论只有摘要支持，应该补查什么？'   --source pubmed --source clinicaltrials --output artifacts/my-research.json
```

来源刷新通过 Fabric 的持久发布程序进入雷达。图谱只在已有身份与适用时点下使用；历史研究传 `historical_scope`，不得把当前图谱或后来结局带回过去。`scripts/history_e2e.py` 的 demo 检查历史读取与统计算术，真实研究使用授权资料与实际截点。

## 四项检查

```sh
make test CARD=BOOT
make contract-check
make lint
make web-check
```

数据库/队列改动另用 `make engine-integration`，研究执行改动用 `make test CARD=ENGINE`。模型或服务中断时先检查已保存回执；未知结果不能直接重发。

## 2026-11-01 实际结果

依据归档目录 `/home/zk_test/zk_projects/.branch-archive-20261101/runs.jsonl`；报告在同目录 `reports/`。32 项真实业务分析共 233 次新模型请求，已知 token 下界 5,863,856，另有 1 次用量未知；此数不包含独立 embedding/rerank 账本。共留下 10 份实际草稿，其中 7 份原生报告 FAIL、3 份核验中的候选；其余没有可交付正文。

| 标准 | 实际结果 |
| --- | --- |
| A/B/C 定向报告均 PASS | 0/3；各两次冒烟后停止，没有第三次重试。 |
| 四个 baseline 均出稿 | 0/4；A/C/D 无材料，B 方法失败。全量前三波因操作者白名单配置错误未及时可见，到第四波才全部可见，故这不是有效的四波及时性比较。 |
| 公卫自动专题完整生命周期 | 未达到；生成 5 个专题，其中 3 个公卫，但实际评价均为 cooling，未出现升温和重新唤醒。 |
| analysis_token_budget 为 0 | 未达到，出现 1 次。另有 1 次调用结果未知，保留阻塞并未重发。 |
| 新请求未恢复截断为 0 | 达到；B5 零调用重放保留的旧截断另列。 |
| D 两个“更早判断对”终点 | 均未证明。2019-12-31 漂移到兰州布病；1 月 1 日偏向季节性流感；1 月 20 日 00:00 的 FAIL 稿倾向武汉新冠聚集与境外输入，尚未作出高概率人传人判断。 |

D 导入 1,556 份文档：正例组 1,544、三组传言对照 6、季节性对照 6。2018、2019 各推进 52 日，对照组各 3 步。按日配置关闭自动分析，不能据此声称自动逐日研判成功；传言对照含疑似 MERS 官方排除材料，不能当作合格社媒传言集计算误报率。

实际路由留下 document_search 258、document_read 34、source_fetch 21、dataset_prepare 18、analysis_run 9、professional_assess 1 条动作回执。WeKnora 有 10 条搜索读到材料的回执，Fabric 有 113 条；图谱动作选中数为 0，图谱业务效果尚未验证。专业 HTTP 往返共 97 条（含恢复）：MechEvidence evaluate 5，BioCheck generate 26、verify 59、quality_feedback 7，不能当作 97 次独立完整专业任务。

减负后的四项检查通过（BOOT 114 项）。零调用比较包含 232 份模型输出、341 个路由决策、97 条专业往返、106 份阅读和 1 个恢复例，前后结果无变化。扩展 ENGINE 检查为 1,185 通过、26 失败、134 跳过。Fabric 新库 V001–V023 与雷达合成历史冒烟通过 18 项检查；首次白名单拒绝更正配置后沿用同批数据完成，LLM 调用为 0。

## 未完成与下一步

- 李明洋、孙博文：A 第二次冒烟仍有 warning_assessment 支持/反对集合冲突；B 在 mech_local_review 输出无效；C 查询引用无效。先用归档原回执定位，修复当前合约和方法，再设计新的独立业务验收。
- 李明洋、潘倩、汪鑫怡：全量 C 在 professional_rpc_operation_failed 停止；保留 2,836 字候选稿，尚未完成核验与交付。先恢复既有回执，未知调用先对账。
- 孙博文：专题错误合并出跨方向“更正”主题，生命周期未走全；D 未证明两个判断终点。修正专题单位和判断依据，补合格传言对照，再评价误报、漏报与修订时间。
- 李明洋：ENGINE 的 26 个失败涉及历史输入预算、专业恢复、监测配置、路由与回执测试。部分旧夹具仍按退役合约断言；需区分夹具迁移与真实运行错误，当前不能声称全套通过。
- 徐子雯：审核已推送且干净的 Fabric `feat/server_fix` 后合入 `dev`；补齐站点样本和 lineage 输入，解决全量单测 101 失败。用户决定保留远端 `tww-GP`，不再要求本轮创建 PR。
- 李明洋：WeKnora 扩展单测 38 项及 11 个子测试通过；合并现役源码后的原生 Go 测试停在依赖下载，尚未完成。补齐 Go 1.26 依赖后验证再安排部署。
- 唐蔚雯与雷达研究维护人：补真实对象/关系资料，证明路由何时选图谱、如何帮助取证。本轮图谱动作 0，不能以适配器测试代替业务证明。
- 代码体积：`analysis_service.py` 5705→5042 行，`query_policy.py` 1849→1806 行，未达 4500/1200 目标。当前恢复、专业调用、预算与候选覆盖仍在使用；方法版本、集合及计算结果身份也被保存/恢复协议依赖，未为减行数而改变行为。
- 本轮核查旧 WeKnora 容器版本时，误用不支持的 `--version` 启动了重复进程，已只终止该重复进程。初始化副作用不能排除；这是执行者越过现役服务边界，本轮不得写成手续完全合规。

## 代码与归档位置

BioRadar、query-planner、Data Factory、WeKnora 的保留主线已正常推送；BioCheck、MechEvidence 按用户最新要求只在本地保留 main。Fabric 本地 main/dev/feat/server_fix，远端另保留 tww-GP。旧引用各在一个 bundle 中，`refs-manifest.tsv` 保存提交、作者与日期。43 个本轮工作树已移除；kilo 的四个工作树未动：

```text
/home/zk_test/.local/share/kilo/worktree-pool/bioradar-2b5308b5d82e/coherent-fighter
/home/zk_test/.local/share/kilo/worktree-pool/biocheck-bf88dd9cd07c/quaint-mall
/home/zk_test/.local/share/kilo/worktree-pool/mechevidence-f4ede1a73252/rocky-jargon
/home/zk_test/.local/share/kilo/worktree-pool/zk-data-fabric-test-b7df1db052d1/soft-playroom
```

归档目录权限 0700，保存原始报告、单一运行记录、数据库快照、原回执和审计日志；模拟资料从运行环境清理，复查只从受控归档恢复。B5 测试库凭据已轮换，Fabric 的 `.env.bak` 已移出源码并按私有配置保存。
