# BioRadar

BioRadar 是医药与公共卫生情报雷达。它做两件事：

1. **回答研究问题**：你问一个问题，它到 Data-Fabric 里找材料、逐篇阅读、需要时让程序算数，再交给 BioCheck 写稿并逐句核验，最后给出带引用的报告和结构化判断（结论、置信、真实性、后果、核实优先级）。
2. **持续监测**：新材料一进 Fabric，它就分诊、归入专题，按资料时间推进专题状态（萌发 → 升温 → 降温 → 休眠 → 再唤醒），重要变化自动触发一次分析。

历史回放（例如 2019 年底的武汉）走同一套流程，只是每一步都带截止时间，Fabric 只返回截止前公开可见的版本。

## 谁维护什么

| 部分 | 维护人 | 位置 |
| --- | --- | --- |
| 运行、接入、部署 | 李明洋 | `services/bioradar/app.py`、`config.py`、`db.py`、`ops.py`、`adapters/`、`research/analyses.py`、`db/`、`ops/` |
| 研究方法（调查流程、提示词、计算、监测与专题规则） | 孙博文 | `services/bioradar/research/pipeline.py`、`prompts.py`、`schemas.py`、`compute.py`、`monitor.py` |
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

## 启动

需要 Python 3.12、Docker Compose、Node 22。

```sh
make bootstrap          # 安装 Python 依赖
make init               # 生成 ops/.env（随机数据库密码和登录凭据）
```

测量核对要用共享契约包 `extraction-contract`（和 Fabric 共用，这里固定 1.3.0：契约 v2、checker v6）。它不在任何包索引上，而是从同级目录的 extraction-contract 检出离线构建：

```sh
cd ../extraction-contract && bash scripts/build_and_verify.sh && cd -   # 可复现的 wheel，构建时会校验哈希
uv pip install --no-index --find-links "${EXTRACTION_CONTRACT_WHEELS:-../extraction-contract/dist}" --require-hashes -r requirements-contract.txt
```

`pyproject.toml` 的 find-links 指向 `../extraction-contract/dist`；放在别处时用 `EXTRACTION_CONTRACT_WHEELS` 指定。uv.lock 不会记录本地 wheel 的哈希，所以哈希只在上面这一步按 `requirements-contract.txt` 校验。

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
- WeKnora（李明洋）：雷达只通过 weknora-kdd 桥检索，`bioradar-weknora-kdd` 单元监听 127.0.0.1:18564（端口在 `~/.config/systemd/user/bioradar-weknora-kdd.service.d/port.conf`，代码是 WeKnora `main` 的 `extensions/kdd`，检出在 `~/zk_projects/.bioradar-kdd-integration/weknora`）。`radar.env` 填 `BIORADAR_WEKNORA_URL=http://127.0.0.1:18564`、`BIORADAR_WEKNORA_TOKEN`（桥 `runtime.json` 的 token）、`BIORADAR_WEKNORA_KNOWLEDGE_BASE_ID` 和 `BIORADAR_WEKNORA_SOURCE_INSTANCE=datafabric.zktest.kdd`（只要现役 Fabric 发布的条目）。现役 2026-11-03 起已开启。桥按来源范围检索（管线题查 pipeline+research，公卫题查 public_health+research），命中只是指针，原文回 Fabric 读；历史回放不走桥，因为桥只有今天的索引。
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
make check              # = make test + make lint + make web-check
```

改到数据库或队列时，再在真实 PostgreSQL/RQ 上跑一次提问到报告的完整流程。

测量核对（T-29 P3）的真实链路测试会走真实的 BioCheck v6 规则，需要一个 BioCheck 检出：
`BIORADAR_TEST_BIOCHECK_SRC=../BioCheck BIORADAR_TEST_BIOCHECK_PYTHON=../BioCheck/.venv/bin/python make test`。不设这两个变量时，这些测试显示为跳过，不会算作通过。

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

## 2026-11-03 讨论室修订（Opus 实现，Codex 复核）

**新能力**

- **跨时段观测账**（`measurements` 表，`research/series.py`）：监测分诊对每条进来的记录抽最多 3 个可比较的数值（对象写到项目或基地、指标、值、单位、存量/增量/累计/比例、口径、期间、计划/在建/投运或报告/确认、原文短引），程序校验短引在原文里、数值在短引里才入账。同一对象×指标×单位×类型×口径内，程序算状态推进、增减、新高新低、相邻期间的连续变化；同期同值同状态的重复提及只算一次（旧计划重提不算扩产）；更正从它自己可见的时间起替代旧值；迟到或更正的值会把其后各点重新评估并记 `revised`。只用 `available_at` 不晚于时钟的行，回放看不到未来。变化写成专题的「指标序列变化」事件，状态推进、新高新低、趋势会触发专题分析。
- **评估输出也核验**：判断结论和假设理由按分句连同依据送 BioCheck，和原文矛盾的撤下（原文留在 notes），未核验通过的最高给「可能」；假设不再给「已证实」，「很可能」要求本段有核验通过的支持事实。
- **规划范围**：程序判定每个规划名称来自题面、上一份报告还是纯探索；只有题面点名的进 `objects`，只挂在探索对象上的待查问题不能是必答；阅读的相关性只按题面判断。
- **读取排序（T-21）**：planner 的阅读候选按「更正/种子优先 → 与已读全文完全相同的副本靠后 → 标题点名题面对象 → 检索词覆盖率 → 越新越前」排，排序特征存进 progress，重启后顺序不变；只有两篇原文都在内存里比对过、全文完全相同才算副本。baseline 顺序不变。
- **信息活动基线（T-15）**：每个专题按前 7 个有记录的日子算均值+3σ（σ 为 0 时用均值+3），缺报的日子不当 0；按去重键计数（显式转载归原件，否则 Fabric 同源组，否则记录本身）。这是信息活动量，不是发病率。
- **图谱只读接入（T-20）**：见上面「zktest 常驻部署」的图谱一条。
- **同源转载（T-23）**：Fabric 的 reposts / supplements 作为 `source_relations` 传给 BioCheck 的写稿、核验和修订，两端按来源、记录、版本绑定，原件未读或版本不符的写明；更正、撤回仍走 `source_updates`。
- **用户、角色与人工复核（T-14）**：`users` 表分只读（viewer）、分析员（analyst，可提问、追问、复核、手动跑监测）、管理员（admin，另管用户：`GET/POST /api/users`、`PATCH /api/users/{name}` 改角色、换凭据或 `active=false` 停用）；凭据用 scrypt 存。会话 cookie 只用独立的 `BIORADAR_SESSION_SECRET` 签名（`make init` 随机生成；没配就不能登录，不拿登录凭据当签名密钥），12 小时过期，带 `session_version`：换凭据或停用立即结束这个人所有的旧会话，改角色在下一次请求生效。分析页逐句核验表可「确认 / 驳回」，材料可「标记有误」，专题页可调警示；记录进 `reviews` 表（这张表就是人工操作的审计记录）。驳回或标记有误时，所在专题记一笔 `analyst_review`，下一次专题分析的问题里列出这些意见要求逐条回应；没有专题的人工分析只留记录。
- **来源撤权**：以 Fabric 的 `/api/history/sources` 为准（撤权的来源从清单里消失），成功结果缓存 5 分钟，失败不缓存。所有对外出口在输出时统一检查：原文接口；分析详情、分析列表、导出——只要一份分析引用了当前不可读的来源，这条材料只给编号、来源和记录号，报告正文、逐句核验、判断与假设、机制评价的文字都暂不显示（编号、状态、置信度保留），并写明是哪几条材料、什么原因；专题列表与详情——由不可读来源的记录建立的专题不显示标题，这类记录的观测标题、分诊理由和专题事件里的标题、对象名不显示；监测页的观测列表同样处理。清单读不到时按「来源授权状态暂时无法确认」一律不给文字。数据库里存的内容和审计记录不改。
- **模型服务故障**：429/503 按 5/20/60 秒退避重发，受剩余请求额度约束；200 但没有正常 choices 记为错误，不再崩溃。

**实测**

- 现役采集：Fabric 原来只有 2 个 watch，其中 Europe PMC 自 10-19 起每小时失败（翻页中命中数下降被当作分页中断）；修复在 Fabric `feat/server_fix`（a7e0c1f、d316aad），现役跑热修分支 `live/epmc-hotfix-20261103`。改成 8 个 watch 后全部至少完成一轮，现役雷达当轮处理 295 条新材料、出现 3 个升温专题。
- T-12 冻结对照（20 份，`.rebuild-work/out/cmp-*`）：修改后 PASS 没有变多（前 1 份、后 0 份），只修好了 D-S1 planner 的 NOT_RUN；D 的 16 份全部停在步数上限。
- D-S1 读取分析（`.rebuild-work/out/t21-offline/`，72 条标注与输入分开）：旧读取顺序就是 record_id 字母序，前 20 篇有 14 篇英文同缩写噪声；离线新排序覆盖 3 个聚集事件、无关 2 篇。另发现修改前的规划把题面没提的「武汉」「华南海鲜」写进检索词和待查问题，读到的兰州、哈尔滨布病通报被判无关——D 只能作为历史材料处理的演示，不能当作干净的事前预测证明。
- DeepSeek 恢复后（同版本同配置补跑，首轮失败记录保留在 `out/r10-t13`）：T-16 的 16 份不可用结果在 `out/r10-t13-attempt2` 重跑，全部完成、0 失败；PHX 虚构公卫 240 条 × 4 截面 × 2 方法（`c0164f0` + BioCheck `16ca2a9`，`out/phx-main`）8/8 完成，W4-planner PASS，其余 REVIEW。这两组的报告内容由 Codex 按冻结材料独立复核，不当作 `7f302c2` 的模型验收。
- T-23 真实写稿（C01-W2，`7f302c2` + BioCheck `7af7044`，`out/t23-c01w2`）：planner PASS、baseline REVIEW，两份都写明 E4 转载自 E3、只算一个来源。
- 真实 BioCheck：after-C 那条把增长写成「均同比下降」的判断，整句核验漏判，按分句核验判为矛盾（`.rebuild-work/out/t13-repro/`）。

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
- **Fabric 改动在审核中**：`feat/radar-rebuild`（document_evidence、并发归档死锁修复）等徐子雯、唐蔚雯审核合进 `dev`。现役 Fabric 仍是 `82bad7b`，雷达只读它；pubmed 在现役许可里不可读，雷达线上配置没有 pubmed。
