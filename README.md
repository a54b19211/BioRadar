# BioRadar

BioRadar 是医药与公共卫生情报雷达。它做两件事：

1. **回答研究问题**：你问一个问题，它到 Data-Fabric 里找材料、逐篇阅读、需要时让程序算数，再交给 BioCheck 写稿并逐句核验，最后给出带引用的报告和结构化判断（结论、置信、真实性、影响、核实优先级）。
2. **持续监测**：新材料一进 Fabric，它就分诊、归入专题，按资料时间推进专题状态（萌发 → 升温 → 降温 → 休眠 → 再唤醒）；Fabric 抽出的测量对象（已核一致、有冲突、未核都带标记）按专题进序列，只有已核且口径完整的才参与比较，重要变化自动触发一次分析。

历史回放（例如 2019 年底的武汉）走同一套流程，只是每一步都带截止时间，Fabric 只返回截止前公开可见的版本。

代码地图、开发约定和设计取舍在 [docs/设计.md](docs/设计.md)。部署和共用设施（WeKnora、PostgreSQL 17、systemd 与监控）见仓库 a54b19211/ZK-radar-deploy，这里不重复。

## 依赖的服务

| 部分 | 仓库 | 雷达怎么用 |
| --- | --- | --- |
| 资料、版本、材料关系、测量对象 | fabric | 只读 HTTP（`BIORADAR_FABRIC_URL`），不写它的库 |
| 下一步选择与停止 | a54b19211/Bio-Planner | `BIORADAR_PLANNER_URL`，默认端口 18561 |
| 写稿与逐句核验 | BioCheck | `BIORADAR_BIOCHECK_URL`，默认端口 18562 |
| 机制证据评价 | MechEvidence | `BIORADAR_MECHEVIDENCE_URL`，默认端口 18563 |
| 语义检索 | WeKnora，经 fabric 的 `weknora/` 模块 | 雷达只开关 `BIORADAR_SEMANTIC_SEARCH`，不直连 |
| 人工审核图谱 | Atlas，经 Fabric 来源 `reviewed_graph` | `BIORADAR_GRAPH_LOOKUP`，默认关闭 |

三个专业服务都是独立 HTTP 服务，自己调模型，响应里返回 `llm_calls`，雷达记账。服务没配置时，对应步骤记为未运行，不用别的方法顶替。

## 一次分析是怎么跑的

```
提问 ─► plan        模型拟定待查问题 Q1..Qn 和检索词（关闭思考模式）
     ─► investigate 循环：planner 选下一步 → 雷达执行
                     search  到 Fabric 按词检索（可选语义检索）
                     read    读一篇固定版本，模型摘出带逐字短引的事实 F1..Fn
                     fetch   （实时模式）让 Fabric 去上游抓新记录
     ─► compute     需要算数时，模型只写算式，程序用原文数值计算（A1..An）
     ─► mechanism   问题涉及机制时，把研究材料交给 MechEvidence
     ─► report      BioCheck 按槽写稿 → 按断言单元逐句核验（`/v2/verify`）→ 必要时修订再核验（最多两轮）
     ─► assess      模型按核验后的报告给结构化判断和情报等级
```

编号全部由程序分配，模型只引用；短引由程序在原文里查找，找不到的事实直接丢弃；材料原文只在 `evidence` 表里存一次；模型调用（包括三个服务自己发起的）都记在 `llm_calls` 表里。每个阶段结束保存一次进度，worker 中断后从保存的阶段继续。

`method=planner` 是逐步规划；`method=baseline` 是固定流程（每组检索词搜一次，按顺序读，读满即停），用作对照。`complete` 只表示停止调查，不等于报告 PASS。没通过核验的报告照常显示草稿和逐句核验意见，页面和导出标「报告待核」和原因。

planner 要先于雷达部署：investigate 开始时雷达读 planner 的 `/health`，没有 `channels`（0.2.0 之前）就以 `planner_outdated` 停下。

## 启动

需要 Python 3.12、Docker Compose、Node 22，以及 uv 0.13.0（在 PATH 上，或 `make UV=<路径>`；`pyproject.toml` 的 `required-version` 拒绝别的版本）。

```sh
make bootstrap          # .venv：只按 uv.lock 安装（uv sync --frozen，Python 3.12）
make init               # 生成 ops/.env（随机数据库密码、登录凭据、会话密钥）
# 编辑 ops/.env：Fabric 地址和令牌、三个服务的地址和令牌、模型的地址/模型名/密钥
make dev                # 启动 postgres、redis、api、worker、monitor、web
```

网页在 `http://127.0.0.1:15176`，第一次用用户名 `admin` 和 `ops/.env` 里的 `BIORADAR_DEV_CREDENTIAL` 登录，这一步建出第一个管理员；之后用 `POST /api/users` 加人，用 `PATCH /api/users/admin` 换掉管理员凭据。接口文档在 `http://127.0.0.1:18100/docs`。

三个专业服务各自启动（见各自仓库的 README），例如 `uvicorn mechevidence.service:app --port 18563`。

常驻运行用 `ops/systemd/` 的样例单元（api 18100、worker、monitor，以及 planner、biocheck、mechevidence），代码在 `/data/projects/<仓库>`，配置在 `~/.config/bioradar/*.env`（0600）；网页镜像用 `ops/Dockerfile.web` 构建，主机网络下的 nginx 配置是 `ops/nginx-host.conf`。改代码后：`uv sync --frozen`，有新迁移时 `alembic -c db/alembic.ini upgrade head`，再重启 api、worker、monitor。

## 配置

变量名是 `services/bioradar/config.py` 里的设置名加 `BIORADAR_` 前缀；JSON 值要加单引号，例如 `BIORADAR_SOURCES='["pubmed","clinicaltrials"]'`。默认值都指向本机（PostgreSQL `127.0.0.1:15436`、Redis `127.0.0.1:16379`、Fabric `127.0.0.1:18511`）。常用的：

| 变量 | 说明 |
| --- | --- |
| `BIORADAR_DATABASE_URL`、`BIORADAR_REDIS_URL` | 数据库与 RQ 队列 |
| `BIORADAR_FABRIC_URL`、`BIORADAR_FABRIC_READ_TOKEN` | Fabric 读接口 |
| `BIORADAR_PLANNER_URL`、`BIORADAR_BIOCHECK_URL`、`BIORADAR_MECHEVIDENCE_URL`（及各自 `_TOKEN`） | 专业服务；空表示该步不运行 |
| `BIORADAR_BIOCHECK_VERIFY` | `v2`（默认，按断言单元核验）或 `v1` |
| `BIORADAR_MECHEVIDENCE_CONTRACT` | `v1`（默认）或 `v2`（版本绑定契约，雷达签发每次模型请求的授权） |
| `BIORADAR_MODEL_ATTEMPT_TOKEN` | v2 时 MechEvidence 回调雷达授权端点用的令牌（即它的 `MECH_AUTHORIZATION_TOKEN`）；`MECH_AUTHORIZATION_URL` / `MECH_OUTCOME_URL` 指向本服务的 `/internal/v1/model-attempts/authorize` 与 `/{attempt_id}/outcome` |
| `BIORADAR_LLM_PROVIDER`、`BIORADAR_LLM_BASE_URL`、模型名与密钥 | 雷达自己的模型调用（`openai` 库，任何 OpenAI 兼容服务） |
| `BIORADAR_SEMANTIC_SEARCH` | 经 Fabric `POST /api/fabric/search` 做语义检索；命中只是指针，原文回 Fabric 按固定版本读 |
| `BIORADAR_GRAPH_LOOKUP` | 规划时按题面对象查人工审核图谱（每次最多 3 个对象、每个前 5 个图包）；图包目前只有实体和 document_refs，没有已审核的关系边 |
| `BIORADAR_MONITOR_SOURCES`、`BIORADAR_BACKFILL_RUN_IDS` | 监测读哪些来源；哪些 Fabric 回填批次只算背景 |
| `BIORADAR_AUTO_ANALYSES_PER_HOUR`、`BIORADAR_DAILY_AUTO_TOKENS` | 自动分析的频率和每日 token 软上限 |
| `BIORADAR_SESSION_SECRET` | 会话 cookie 签名密钥（`make init` 随机生成） |

来源能力只来自 Fabric 的 `/sources`（`capabilities`）：新分析和监测只用 Fabric 标为可选的来源，`legacy_documents` 可读不可选。监测每 30 秒读一次 Fabric 变更流，一个来源第一次被监测时从当前末尾开始；历史材料用回放分析。

## 接口

```sh
curl -c c.txt -X POST localhost:18100/api/session -H 'content-type: application/json' -d '{"name":"<用户名>","credential":"<登录凭据>"}'
curl -b c.txt -X POST localhost:18100/api/analyses -H 'content-type: application/json' \
  -d '{"question":"……","direction":"pipeline","method":"planner"}'
curl -b c.txt localhost:18100/api/analyses/<id>
```

- `direction`：`pipeline`、`public_health`、`policy` 等；决定检索范围（`research/run/search.py` 的 `SEARCH_SCOPES`）。
- 历史回放：加 `"cutoff": "2019-12-31T00:00:00+08:00"`（必须带时区）。`"replay_mode"`：`current`（不带 cutoff）、`operational_replay`（当时系统实际已有的）、`public_history_reconstruction`（有公开可得证明的）；后两种必须带 cutoff，不给时有 cutoff 按 `public_history_reconstruction`。追问沿用上一轮的模式。
- 追问、导出（Markdown）、专题、观测、用户管理（`/api/users`，角色 viewer / analyst / admin）等其余接口见 `/docs`。
- 分析详情里能看到每一步检索和阅读、读了哪些固定版本、每条事实的短引、逐句核验结果、判断表和模型调用账；`publishable`、`hold`、`hold_reason` 标明是否已核。

## 检查

```sh
make test               # 全部单元测试（SQLite，Fabric、模型和各服务都是替身）
make test-pg            # 真实 PostgreSQL 上的租约、测量变更流、冻结表测试（临时 postgres:17-alpine 容器；没有 Docker 就失败）
make test-redis         # 队列在真实 Redis 上的测试（临时 redis:7.4-alpine 容器）
make lint               # ruff：基础规则 E4, E7, E9, F, W, I，本仓库另开 E（行宽 110）
make metrics            # 函数 80 行 / 圈复杂度 20；例外写在 scripts/metrics_exceptions.txt，只减不增
make check              # 以上全部 + make web-check
make check-install      # /tmp 里只按 uv.lock 新建环境，逐个导入 bioradar 模块
make check-migrations   # 临时 PostgreSQL 上 upgrade → 比对模型 → downgrade → upgrade
```

`tests/golden/` 是行为基线（存盘的 progress、证据、外发记录和对外视图，逐字节比对）；有意改行为时用 `BIORADAR_GOLDEN_UPDATE=1` 重新生成，并在同一个提交里审阅差异。真实 planner 的测试要 `BIORADAR_TEST_PLANNER_URL`，没有时显示为跳过。`test-pg` 和 `test-redis` 只对脚本本次新建的容器运行（`tests_pg/guard.py` 核对），手工给的 URL 一律拒绝。访问 PyPI / npm 超时时，只给那几条命令临时加 `HTTPS_PROXY` / `HTTP_PROXY`，不写进全局配置。

## 验收实验

`scripts/scenarios.py` 在隔离环境里跑四个实验：A 不明病原聚集（模拟）、B 新药突破（模拟）、C 产业趋势（模拟，利用率必须由程序计算）、D 新冠 2019 年真实公开资料回放（四个截止点，加 MERS 和流感季对照，另有逐日监测回放）。

```sh
python scripts/scenarios.py abc --data <场景目录> --out <输出目录>
python scripts/scenarios.py d --data <场景目录>/D --out <输出目录>
python scripts/scenarios.py d --data <场景目录>/D --out <输出目录> --skip-import --cases none --daily 2019-12-01 2020-01-21
```

模拟场景的日期整体提前两个月（Fabric 不接受未来的可见时间）。结果写在 `<输出目录>/runs.jsonl` 和 `reports/`。密封答案放在雷达读不到的目录，评分由非作者独立复核。PASS 只表示 BioCheck 机器核验没发现问题，不等于独立复核通过。

## 未完成

- 真实模型验收：真实阅读、Fabric 测量抽取的准确率、规模测试和 24 小时连续运行还没做。
- 规划可能把题面没点名的地点、机构、病名写进待查问题（现在由规划规则拦），要用新代码完整重跑 D 验证。
- C 的 planner 定向稿没有稳定 PASS：数据和计算都对，写稿模型的总结句会自相矛盾；可以让 BioCheck 写稿时总结句只复述正文，或覆盖检查判不完整时带理由重写全稿。
- D 的判断不稳定：大固定材料集一次只读 20 篇；下一步按来源类型（官方通报优先）排读取顺序。
- 监测分诊里出现删帖、辟谣（`information_events`）的记录应直接作为专题分析的种子（A 漏读过删帖信号）。
- 演示来源 `external_archive` 里三个场景混在一起，检索「更正」会命中别的场景。
- 测量迁移的后续：雷达旧测量数据没导入 Fabric（旧点只作历史显示）；planner 契约还不收 `measurement_ref` / `check_ref`；序列不判阶段推进；页面没有序列图。
- 一份分析里只要有一条材料不可读，整份正文都暂不显示（没有按句子保留其余部分）。
- 公共卫生专题的「重新唤醒」在新代码上还没复现过；A 的新聚集该唤醒旧专题还是另开，要看 `research/prompts.py` 的 TRIAGE 再验一次。
