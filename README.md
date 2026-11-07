# BioRadar

BioRadar 是医药与公共卫生情报雷达。它做两件事：

1. **回答研究问题**：你问一个问题，它到 Data-Fabric 里找材料、逐篇阅读、需要时让程序算数，再交给 BioCheck 写稿并逐句核验，最后给出带引用的报告和结构化判断（结论、置信、真实性、后果、核实优先级）。
2. **持续监测**：新材料一进 Fabric，它就分诊、归入专题，按资料时间推进专题状态（萌发 → 升温 → 降温 → 休眠 → 再唤醒），重要变化自动触发一次分析。

历史回放（例如 2019 年底的武汉）走同一套流程，只是每一步都带截止时间，Fabric 只返回截止前公开可见的版本。

## 谁维护什么

| 部分 | 维护人 | 位置 |
| --- | --- | --- |
| 运行、接入、部署 | 李明洋 | `services/bioradar/app.py`、`config.py`、`db.py`、`ops.py`、`text.py`、`adapters/`、`research/analyses.py`、`research/views.py`、`research/lease.py`、`research/clock.py`、`research/lineage.py`、`db/`、`ops/` |
| 研究方法（调查流程、提示词、计算、监测与专题规则） | 孙博文 | `services/bioradar/research/pipeline.py`、`run/`（各阶段）、`state.py`、`prompts.py`、`schemas.py`、`compute.py`、`monitor.py`、`topics.py`、`triage.py`、`series_store.py`、`scheduling.py` |
| 页面、导出、验收评测 | 刘金彦 | `apps/web/`、`scripts/scenarios.py` |
| 资料、版本、材料关系 | 徐子雯（图谱：唐蔚雯） | 仓库 ZK_Data-Fabric-test |
| 下一步选择与停止 | 李明洋 / 孙博文 | 仓库 query-planner |
| 写稿与逐句核验 | 潘倩 | 仓库 BioCheck |
| 机制证据评价 | 汪鑫怡 | 仓库 MechEvidence |
| 语义检索 | 李明洋 | 仓库 WeKnora |
| 评测辅助 | 李明洋 | 仓库 medical-qa-data-factory（`eval radar`） |

## 一次分析是怎么跑的

```
提问 ─► plan        模型拟定待查问题 Q1..Qn 和检索词（关闭思考模式）
     ─► investigate 循环：query-planner 选下一步 → 雷达执行
                     search  到 Fabric 按词检索（可选 WeKnora 语义检索）
                     read    读一篇固定版本，模型摘出带逐字短引的事实 F1..Fn
                     fetch   （实时模式）让 Fabric 去上游抓新记录
     ─► compute     需要算数时，模型只写算式，程序用原文数值计算（A1..An）
     ─► mechanism   问题涉及机制时，把研究材料交给 MechEvidence
     ─► report      BioCheck 写稿 → 逐句核验 → 必要时修订再核验（最多两轮）
     ─► assess      模型按核验后的报告给结构化判断和情报等级
```

规则：编号全部由程序分配，模型只引用；短引由程序在原文里查找，找不到的事实直接丢弃；材料原文只在 `evidence` 表里存一次；模型调用（包括三个服务自己发起的）都记在 `llm_calls` 表里。每个阶段结束保存一次进度，worker 中断后从保存的阶段继续。

`method=planner` 是逐步规划；`method=baseline` 是固定流程（每组检索词搜一次，按顺序读，读满即停），用作对照。

## RQ 派发与数据库租约的分工

两套机制各管一件事，互不替代：

| | 管什么 | 不管什么 |
|---|---|---|
| RQ（Redis） | 送达和唤醒：分析建好后 `analyses.dispatch` 入队，worker 取到就跑 | 这一行归谁、会不会跑两遍、过期后怎么办 |
| 数据库租约（`research/lease.py`） | 归属、幂等、隔离、恢复：`SELECT … FOR UPDATE SKIP LOCKED` 认领并写入 `lease_token` / `lease_until`；过期的工人不能提交结果（任务、窗口和分析都带令牌）；工人崩溃后租约过期，下一个认领者接手一次；失败计一次尝试并退避，到上限转终态留待复核 | 把任务送到某个 worker |

- 分析（`analyses`）：RQ 派发。`execute` 在行锁里查租约，已有人持有或已结束就直接返回，同一个分析不会同时跑两份；
  monitor 每轮调用 `analyses.recover`，把租约过期（worker 崩溃）或任务没进 Redis 的分析重新入队。分析的租约带令牌（`analyses.lease_token`）：
  `execute` 认领时写入，`recover` 释放；分析的每一次写入（进度、证据、材料发送记录、模型调用记录、最终结果）都在行锁里核对令牌，
  每一次对外发送（模型、BioCheck、MechEvidence）之前也核对；核对是“令牌仍是自己的**且**租约未过期”的一条 UPDATE，同时续租，所以过期的租约不会被复活。被取代或过期的 worker 得到 `lease.Lost`、什么也不写并记一条日志；它在失去租约之前已经发出的请求，晚到的回答、用量和原文仍记入旧尝试的审计（`llm_calls.lease_token`、`llm_outputs`），计入这份分析的预算
  （见 [`docs/decisions/0007`](docs/decisions/0007-rq-dispatch-and-database-lease.md)）。
- 测量核对任务（`measurement_check_tasks`）和阅读窗口（`record_read_windows`）：不经 RQ，由 monitor 每轮轮询认领，
  结果写入前核对令牌，令牌已不是自己的就丢弃。
- 这些保证在真实 PostgreSQL 上由 `make test-pg`（`tests_pg/`）验证；SQLite 没有行锁，单元测试证明不了。测试会 `TRUNCATE`，所以只对脚本为本次运行新建的容器和专用库（`radar_pgtest_<随机>`）运行：运行前核对库里的运行标记、容器 ID、端口和库名与清单一致（`tests_pg/guard.py`），手工给的 URL（含回环地址、现役库）一律拒绝。

## 启动

需要 Python 3.12、Docker Compose、Node 22，以及 uv 0.13.0（在 PATH 上，例如 `~/.local/bin/uv`；或者 `make UV=<路径>`；`pyproject.toml` 的 `required-version` 拒绝别的版本）。

```sh
make bootstrap          # .venv：只按 uv.lock 安装（uv sync --frozen，Python 3.12）
make init               # 生成 ops/.env（随机数据库密码和登录凭据）
```

编辑 `ops/.env`：填 Fabric 地址和令牌、三个服务的地址和令牌、模型的地址/模型名/密钥（变量名就是 `services/bioradar/config.py` 里的设置名加 `BIORADAR_` 前缀）。JSON 值要加单引号，例如 `BIORADAR_SOURCES='["pubmed","clinicaltrials"]'`。然后：

```sh
make dev                # 启动 postgres、redis、api、worker、monitor、web
```

网页在 `http://127.0.0.1:15176`，第一次用用户名 `admin` 和 `ops/.env` 里的 `BIORADAR_DEV_CREDENTIAL` 登录，这一步建出第一个管理员；之后在 `/docs` 里用 `POST /api/users` 加人，用 `PATCH /api/users/admin` 换掉管理员凭据（换掉后开发凭据不再能登录）。接口文档在 `http://127.0.0.1:18100/docs`。

三个专业服务各自启动（见各自仓库的 README）：

```sh
cd ../query-planner && uvicorn planner.service:app --port 18561
cd ../BioCheck      && uvicorn app.service:app --port 18562
cd ../MechEvidence  && uvicorn mechevidence.service:app --port 18563
```

### zktest 常驻部署

- 进程：`ops/systemd/` 里的六个单元（`systemctl --user status 'bioradar-*'`）：api 18100、worker、monitor、planner 18561、biocheck 18562、mechevidence 18563。
- 配置：`~/.config/bioradar/radar.env`、`planner.env`、`biocheck.env`、`mechevidence.env`（0600）。登录凭据在 `~/.config/bioradar/login.txt`。
- 数据：PostgreSQL 15436 上的 `bioradar_live` 库，Redis 16379 的 3 号库。Fabric 用现役的 18511（只读）。
- 网页：`bioradar-web` 容器走主机网络，监听 127.0.0.1:15176，配置是 `ops/nginx-host.conf`。改了前端后：

  ```sh
  docker build --network host -f ops/Dockerfile.web -t bioradar-web:main .
  docker rm -f bioradar-web && docker run -d --name bioradar-web --network host --restart unless-stopped \
    -v $PWD/ops/nginx-host.conf:/etc/nginx/conf.d/default.conf:ro bioradar-web:main
  ```

- 监测：每 30 秒读一次 Fabric 变更流。一个来源第一次被监测时从它**当前的末尾**开始（`legacy_documents` 有 62 万条历史记录，不能全部分诊）；历史材料用回放分析。自动分析每小时最多 6 次（`BIORADAR_AUTO_ANALYSES_PER_HOUR`）。
- 改代码后：`git pull`，`uv sync --frozen`，有新迁移时 `alembic -c db/alembic.ini upgrade head`，再 `systemctl --user restart bioradar-api bioradar-worker bioradar-monitor`。
- WeKnora（李明洋）：雷达只通过 weknora-kdd 桥检索，`bioradar-weknora-kdd` 单元监听 127.0.0.1:18564（端口在 `~/.config/systemd/user/bioradar-weknora-kdd.service.d/port.conf`，代码是 WeKnora 的 `extensions/kdd`，检出是 WeKnora 仓库的 worktree `~/zk_projects/.rebuild-work/wt/kdd-bridge`，即单元的 WorkingDirectory）。`radar.env` 填 `BIORADAR_WEKNORA_URL=http://127.0.0.1:18564`、`BIORADAR_WEKNORA_TOKEN`（桥 `runtime.json` 的 token）、`BIORADAR_WEKNORA_KNOWLEDGE_BASE_ID` 和 `BIORADAR_WEKNORA_SOURCE_INSTANCE=datafabric.zktest.kdd`（只要现役 Fabric 发布的条目）。现役 2026-11-03 起已开启。桥按来源范围检索（管线题查 pipeline+research，公卫题查 public_health+research），命中只是指针，原文回 Fabric 读；历史回放不走桥，因为桥只有今天的索引。
- Fabric 采集（徐子雯）：公开源由 Fabric publisher 按 `~/.config/datafabric-publication/runtime.env` 的 `DATAFABRIC_HISTORY_WATCHES` 定时刷新（最多 8 个）。现在是 WHO、Europe PMC 六组（BCMA、III 期随机、暴发标题、GLP-1 类、ADC/CAR-T/双抗、监管审批）和 ClinicalTrials III 期，查看状态用 `/api/history/source-health?source_id=…`。

- 图谱（唐蔚雯）：`BIORADAR_GRAPH_LOOKUP=true` 时规划按题面对象查 reviewed_graph（每次最多 3 个对象、每个对象只看检索到的前 5 个图包，不是查全），图包指向的原文进阅读候选，实体身份状态（多为 unresolved）原样保留。现役默认关闭：当前图包里只有实体和 document_refs，没有已审核的关系边，开了只是多一条找原文的路，不提供关系判断。开启前确认 Fabric 里有 reviewed_graph 来源且雷达有读权限。
- 自动工作每日 token 软上限：现役设为 3000 万（`BIORADAR_DAILY_AUTO_TOKENS`，主持人同意放宽；默认 300 万）。

2026-11-03 从旧版切换过来。旧库 `bioradar`、旧配置备份 `~/.config/bioradar.before-rebuild-20261103/`、旧网页容器 `bioradar-zktest-web-1` 都保留着，需要退回时运行 `bash ~/.config/bioradar-rollback.sh`。同日 11:53 升到讨论室复核通过的 `7f302c2`（BioCheck `7af7044`，迁移 0003、0004），升级前的配置、提交号和 `bioradar_live` 全库备份在 `~/.config/bioradar-backups/20261103115246/`；退回这一步用 `.rebuild-work/tools/update_live_undo.sh <备份目录>`（加 `--restore-db` 才会用备份覆盖数据库）。

## 用一个真实问题跑一次

网页「提问与调查」里输入，例如：

> BCMA×CD3 双特异性抗体用于多发性骨髓瘤，哪些管线值得继续调研？比较证据阶段、竞争项目、失败原因与安全性限制。

方向选「管线」，方法选「逐步规划」。分析详情页能看到：每一步检索和阅读、读了哪些固定版本、每条事实的短引、BioCheck 逐句核验结果、判断表和模型调用账。可以继续「追问」（新分析会带上旧报告做比较），或「导出」成 Markdown。

用接口也一样：

```sh
curl -c c.txt -X POST localhost:18100/api/session -H 'content-type: application/json' -d '{"name":"<用户名>","credential":"<登录凭据>"}'
curl -b c.txt -X POST localhost:18100/api/analyses -H 'content-type: application/json' \
  -d '{"question":"……","direction":"pipeline","method":"planner"}'
curl -b c.txt localhost:18100/api/analyses/<id>
```

历史回放：加上 `"cutoff": "2019-12-31T00:00:00+08:00"`（必须带时区）。

## 检查

```sh
make bootstrap          # .venv 只按 uv.lock 安装
make test               # 全部测试（SQLite；BioCheck 检出在 BIOCHECK_SRC 时含真实 BioCheck 规则）
make test-pg            # 租约在真实 PostgreSQL 上的并发测试（tests_pg/）：临时 postgres:17-alpine 容器，按本次创建的 ID 拆除；没有 Docker 就失败，不跳过
make test-redis         # 队列在真实 Redis 上的测试（tests_redis/）：临时 redis:7.4-alpine 容器（口令、运行标记、清单与 test-pg 同一套），只对本次创建的容器运行
make check              # = make test + make test-pg + make test-redis + make lint + make metrics + make web-check
make check-install      # 在 /tmp 用声明的版本新建环境，核对四处契约版本一致，逐个导入 bioradar 模块
make check-migrations   # 起一个临时 postgres:17-alpine：upgrade head → 与模型比对 → downgrade base → upgrade head；容器按本次创建的 ID 拆除（连匿名卷，`docker rm -v`），清单留在 /tmp/radar-migcheck-<随机>.containers.jsonl
```

`make bootstrap`、`make check-install`（`uv sync --locked` 核对 uv.lock）在 uv 缓存里没有的包要从 PyPI 下载，`make web-check` 的 `npm ci` 要访问 npm。zktest 上直连有时超时，这时只给这几条命令临时加上本机已有的 HTTP 代理（mihomo，监听 127.0.0.1:7890），例如 `HTTPS_PROXY=http://127.0.0.1:7890 HTTP_PROXY=http://127.0.0.1:7890 make check-install`（`web-check` 的容器用主机网络并带上这两个变量）；不要把代理写进全局配置。uv 下载失败时报的是网络错误，不是 `The lockfile needs to be updated`（只有后者说明 uv.lock 过期）。

`make metrics` 是函数长度和圈复杂度的门槛（80 行 / 20，`scripts/code_metrics.py`）；超过的函数必须在
`scripts/metrics_exceptions.txt` 写明理由，清单只减不增。`tests/golden/` 是行为基线：每个流程场景存盘的
progress、证据和外发记录，以及对外视图（详情、列表、导出）逐字节比对；有意改行为时用
`BIORADAR_GOLDEN_UPDATE=1` 重新生成，并在同一个提交里审阅差异。设计取舍记在 `docs/decisions/`。

改到数据库或队列时，再在真实 PostgreSQL/RQ 上跑一次提问到报告的完整流程。

测量核对的真实链路测试会走真实的 BioCheck 规则（checker v16，契约 v3），需要一个 BioCheck 检出和它自己的环境：
`make test` 默认用同级的 `../clean-biocheck`（BioCheck 的集成线，先在那里 `make venv`），别处的检出用
`make test BIOCHECK_SRC=<检出> [BIOCHECK_PYTHON=<它的 python>]`。找不到检出时这些测试显示为跳过，不会算作通过。
已验证的组合：uv 0.13.0、CPython 3.12.15、BioCheck `refactor/biocheck-clean`：`make test` 493 个通过、0 个跳过（真实 planner 服务的测试要 `BIORADAR_TEST_PLANNER_URL`，由 `devtools/env/line_checks.sh` 从 `wt/clean-planner` 起一个服务来跑），`make test-pg` 另有 58 个通过（租约、令牌围栏与过期、晚到审计、recover 分类、monitor 互斥、锁连接掉线与晚到回答的审计、认领公平性，以及 15 个守卫），`make test-redis` 18 个通过（JSON 队列、任务白名单、旧 pickle 任务恢复、对 RQ 的查询，以及 12 个守卫）。

## 验收实验

`scripts/scenarios.py` 在隔离环境里跑四个实验：

- **A** 不明病原聚集（模拟）：四波材料、假传言、删帖信号、两次空轮、结案、再唤醒。
- **B** 新药突破（模拟）：机制原始实验与反证、早期队列、随机试验、同一试验的数字更正。
- **C** 产业趋势（模拟）：三家公司订单更正，利用率必须由程序计算。
- **D** 新冠 2019 年真实公开资料回放：四个截止点（2019-12-31、2020-01-01、01-20、01-21），加 2016–2017 年疑似 MERS 对照和 2018 年流感季对照，另做 2019-12-01 至 2020-01-21 逐日监测回放。

```sh
python scripts/scenarios.py abc --data <场景目录> --out <输出目录>
python scripts/scenarios.py d --data <场景目录>/D --out <输出目录>
python scripts/scenarios.py d --data <场景目录>/D --out <输出目录> --skip-import --cases none --daily 2019-12-01 2020-01-21
```

模拟场景的日期整体提前两个月（Fabric 不接受未来的可见时间），先后顺序和间隔不变。每次分析都带截止时间，看不到后面的波次。结果写在 `<输出目录>/runs.jsonl`（每次运行一行）和 `reports/`。密封答案放在雷达读不到的目录，评分用 data-factory 的 `eval radar` 做辅助，再由非作者独立复核。

## 2026-11 重构后的实际结果

2026-11-02 至 11-03 在 zktest 隔离环境里跑了 9 轮。下面是最终代码（BioRadar `rebuild` 分支最后几个提交）的结果，运行记录在 `/home/zk_test/zk_projects/.rebuild-work/out/`（`abc/`、`d/`、`d-daily/`，每行一次运行）。PASS 只表示 BioCheck 机器核验没发现问题；「独立核对」是非作者对照密封答案逐句读稿。

**A/B/C 定向分析**

| 分析 | BioCheck | 关键消息覆盖 | 独立核对 |
| --- | --- | --- | --- |
| A planner | PASS | 7/8（漏 SIM-A-43 删帖信号） | 关键事实全对：门诊 5 人不累加，两院 7 人，14 条更正为 12 人、净增 5 人，假传言未证实，转发不算独立来源。混进一句 B 场景转载标题（已注明无关） |
| B planner | PASS | 9/9 | 关键事实 9/9：更正后 HR 0.69（0.51–0.93）取代 0.62，同一 240 人非新试验，OS 44% 未成熟，毒性 40/120 对 26/120，低表达 6 人不外推，未获批、未正式发表 |
| C planner | REVIEW（最终代码跑了 4 次，都没有 PASS） | 11/11 | 数据和计算都对：更正后订单 6400 万元、32 套，同比 33.3%；收入 28.6%、回款 15.4%，回款覆盖率 92.9%→83.3%；程序算出工时利用率 60.0%→53.2%（−6.8 个百分点）。但总结句写错：把没被更正的收入、回款也说成「来自已被更正的初稿」，又说前文用过的衡简数据「未见支持」 |
| A/B/C baseline | A PASS、B PASS、C 4 次里 1 次 PASS（11/11） | — | 四个 baseline 都出稿 |

**专题生命周期**：A/B/C 三个专题按资料时间升温 → 两次空轮保持降温 → 结案后休眠；L4（另一医院 4 人新聚集）开成新专题并判为重要、升温，触发自动分析。重启（R1）后状态从数据库恢复。

**实验 D（2019 年真实公开资料回放，18 次分析，0 失败）**

- 聚集终点：2019-12-31 截止判为重要、「需要最高优先级核实」；但 01-01 和 01-21 两个截止点退回一般（固定材料 1,500 多篇，每次只读 20 篇，读到的多是季节性流感材料）。
- 人传人终点：01-20 判为「保留的竞争假设，未证实也未排除」；01-21 仍判待证实——读到的材料里没有当晚公开确认的原文。
- 对照：2016–2017 年三次香港疑似 MERS、2018 年英格兰和香港流感季，6 个截止点全部判为一般，零误报。
- 逐日监测（2019-12-01 至 2020-01-21，45 次自动分析，0 失败）：「武汉不明原因肺炎聚集」专题在材料第一天可见的 2020-01-01 建立，判为重要、升温；01-11 升为紧急。

**花费**（最终一轮）：A 约 32 万 token，B 约 34 万，C 每次约 23–26 万，专题自动分析 45 万；D 定向 303 万；逐日监测 620 万。重构以来全部运行约 5,250 万 token。没有一次因预算或截断失败。

## 来源能力与 MechEvidence v2（2026-11-07）

来源能力只来自 Fabric 的 `/sources`（`capabilities`），雷达不再保存任何来源集合；新分析和监看只用 Fabric 标为可选的来源，`legacy_documents` 可读不可选。`BIORADAR_MECHEVIDENCE_CONTRACT=v2` 时机制评价走版本绑定契约：发出前向 Fabric 重读材料，雷达签发授权并在 `/internal/v1/model-attempts/*` 回答服务每次模型请求，回执只有在契约规则与雷达自己的尝试账本都一致时才采用。设计与取舍见 [`docs/decisions/0009`](docs/decisions/0009-fabric-capabilities-and-mechevidence-v2.md)。环境变量：`BIORADAR_MODEL_ATTEMPT_TOKEN`（服务令牌，MechEvidence 的 `MECH_AUTHORIZATION_TOKEN`），`MECH_AUTHORIZATION_URL` / `MECH_OUTCOME_URL` 指向本服务的 `/internal/v1/model-attempts/authorize` 与 `/{attempt_id}/outcome`。

## 2026-11-03 讨论室修订

当时加的能力（观测账、评估核验、规划范围、读取排序、用户与角色、来源撤权、模型服务故障退避）和实测记录在 [`docs/decisions/0008-room-revision-record-2026-11-03.md`](docs/decisions/0008-room-revision-record-2026-11-03.md)；现行行为以代码和测试为准。

## 未完成

- **待真实模型验证**：T-22 的真实阅读验收、观测抽取（T-24）的真实准确率、规模测试和 24 小时实际运行；Codex 提的回归用例（BioCheck 覆盖检查误判「情报假设」标题、C04-W2 矛盾、C05-W2「已定稿未生效」、research_scope 等内部字段名要译成中文）。
- **T-22 未修完**：用 before 那份「题面没提武汉、待查问题却写武汉」的规划去读，阅读规则仍扳不过被收窄的待查问题（3 次实测结果不稳）；现在从规划处防（待查问题不得写题面未点名的地点、机构、病名），还要用新代码完整重跑 D-S1 验证。
- **WeKnora 只接了 WHO**：桥的检索在同一知识库同一范围超过 1000 篇时返回 400，Europe PMC（已有约 1,444 篇、每周新增数千）和 ClinicalTrials（每周约 500 个新版本）一开就会打坏该范围的检索，需要李明洋放开桥的单范围上限后再开。桥没有截止时间参数，历史回放不走桥。知识库里还有 10-30 集成测试从另一个 Fabric 实例发布的旧条目，按名字分不开，读不到的由 Fabric 挡掉。
- **观测账**：同名或更名对象不做合并（按原文名字精确分开）；比率不单独存分子分母；每条最多 3 个观测、超出部分不记也没有标记；期间写法不规范（「去年下半年」）的丢弃；页面只在专题事件里显示，没有序列图。
- **T-14 的边界**：一份分析里只要有一条材料不可读，整份正文都暂不显示（没有按句子拆分保留其余部分）；同时第一次登录建管理员有极小概率撞唯一约束，重试即可。现役上线时要在 `radar.env` 加 `BIORADAR_SESSION_SECRET`。
- **C 的 planner 定向稿没有 PASS**（第十九节「跑通」标准里唯一没达到的一条）：数据、更正、程序计算都对，问题出在写稿模型的总结句自相矛盾（见上表），BioCheck 的覆盖检查判为不完整；修订只改单句，改不了全篇层面的矛盾。另外 planner 仍会读进 A/B 场景的更正记录（演示来源里三个场景混在一起）。下一步：潘倩在 BioCheck 写稿提示里要求总结句只复述正文已写的结论；或者覆盖检查判为不完整时让雷达带着理由重写一次全稿。
- **公共卫生专题的「重新唤醒」**：最终代码的运行里，被唤醒的只有一个药品专题（沃森 13 价疫苗）；公共卫生专题走完萌发→升温→降温/休眠→唤醒，只在 11-02 第 4 轮（旧代码）的卫健委疫情通报专题上出现过。A 的 L4 按事件独立性开成了新专题而不是唤醒旧专题。下一步：孙博文确认 L4 应当唤醒原专题还是另开专题（`research/prompts.py` 的 TRIAGE），再用 `scenarios.py abc --only A` 验一次。
- **D 的判断不稳定、人传人没有在 01-21 判出**：大固定材料集一次只读 20 篇，挑选按检索词命中。下一步：D 的固定材料先按来源类型（官方通报优先）排读取顺序；人传人确认原文（卫健委 01-20 23:17）因许可没有全文，需要徐子雯确认能否作为可读材料导入 Fabric。
- **A 漏读删帖信号 SIM-A-43**：截图访问记录没有被任何检索词命中。下一步：监测分诊里出现 `information_events`（删帖、辟谣）的记录直接作为专题分析的种子。
- **跨专题材料混入**：演示来源 `external_archive` 里 A/B/C 三个场景混在一起，搜「更正」会命中别的场景的更正。已在阅读提示里要求按对象判断相关性；C 稿里仍偶尔出现别的场景的句子。
- **BioCheck、MechEvidence 的 GitHub 远端**（a54b19211 名下）在 zktest 上 `ls-remote` 没有返回分支，主分支以 zktest 上的仓库为准；需要仓库主人确认远端地址和权限。
- **Fabric 改动在审核中**：`feat/radar-rebuild`（document_evidence、并发归档死锁修复）等徐子雯、唐蔚雯审核合进 `dev`。现役 Fabric 是 `04a8888`（`live/epmc-hotfix-20261103`），雷达只读它；pubmed 在现役许可里不可读，雷达线上配置没有 pubmed。
- **整洁度第一波（2026-11-06）之后还剩的**：测量链路（`measure_checks`、`upkeep`、`continuation`、`series_store.adopt`、`domain`）按 v3 6.1 迁到 Fabric 派生数据层，之后再拆这些长函数；（来源目录的四份副本已删，见 0009）；三个服务共用一份 LLM 客户端。还没拆的长函数和理由列在 `scripts/metrics_exceptions.txt`。待核的报告在页面上只显示空正文，还没有显示待核原因（`apps/web/`）；被待核的上一轮报告仍会交给追问作为上一轮报告（见 `docs/decisions/0005`）。
