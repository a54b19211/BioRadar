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

编辑 `ops/.env`：填 Fabric 地址和令牌、三个服务的地址和令牌、模型的地址/模型名/密钥（变量名就是 `services/bioradar/config.py` 里的设置名加 `BIORADAR_` 前缀）。JSON 值要加单引号，例如 `BIORADAR_SOURCES='["pubmed","clinicaltrials"]'`。然后：

```sh
make dev                # 启动 postgres、redis、api、worker、monitor、web
```

网页在 `http://127.0.0.1:15176`，用 `ops/.env` 里的 `BIORADAR_DEV_CREDENTIAL` 登录。接口文档在 `http://127.0.0.1:18100/docs`。

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

2026-11-03 从旧版切换过来。旧库 `bioradar`、旧配置备份 `~/.config/bioradar.before-rebuild-20261103/`、旧网页容器 `bioradar-zktest-web-1` 都保留着，需要退回时运行 `bash ~/.config/bioradar-rollback.sh`。

## 用一个真实问题跑一次

网页「提问与调查」里输入，例如：

> BCMA×CD3 双特异性抗体用于多发性骨髓瘤，哪些管线值得继续调研？比较证据阶段、竞争项目、失败原因与安全性限制。

方向选「管线」，方法选「逐步规划」。分析详情页能看到：每一步检索和阅读、读了哪些固定版本、每条事实的短引、BioCheck 逐句核验结果、判断表和模型调用账。可以继续「追问」（新分析会带上旧报告做比较），或「导出」成 Markdown。

用接口也一样：

```sh
curl -c c.txt -X POST localhost:18100/api/session -H 'content-type: application/json' -d '{"credential":"<登录凭据>"}'
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

## 未完成

- **C 的 planner 定向稿没有 PASS**（第十九节「跑通」标准里唯一没达到的一条）：数据、更正、程序计算都对，问题出在写稿模型的总结句自相矛盾（见上表），BioCheck 的覆盖检查判为不完整；修订只改单句，改不了全篇层面的矛盾。另外 planner 仍会读进 A/B 场景的更正记录（演示来源里三个场景混在一起）。下一步：潘倩在 BioCheck 写稿提示里要求总结句只复述正文已写的结论；或者覆盖检查判为不完整时让雷达带着理由重写一次全稿。
- **公共卫生专题的「重新唤醒」**：最终代码的运行里，被唤醒的只有一个药品专题（沃森 13 价疫苗）；公共卫生专题走完萌发→升温→降温/休眠→唤醒，只在 11-02 第 4 轮（旧代码）的卫健委疫情通报专题上出现过。A 的 L4 按事件独立性开成了新专题而不是唤醒旧专题。下一步：孙博文确认 L4 应当唤醒原专题还是另开专题（`research/prompts.py` 的 TRIAGE），再用 `scenarios.py abc --only A` 验一次。
- **D 的判断不稳定、人传人没有在 01-21 判出**：大固定材料集一次只读 20 篇，挑选按检索词命中。下一步：D 的固定材料先按来源类型（官方通报优先）排读取顺序；人传人确认原文（卫健委 01-20 23:17）因许可没有全文，需要徐子雯确认能否作为可读材料导入 Fabric。
- **A 漏读删帖信号 SIM-A-43**：截图访问记录没有被任何检索词命中。下一步：监测分诊里出现 `information_events`（删帖、辟谣）的记录直接作为专题分析的种子。
- **跨专题材料混入**：演示来源 `external_archive` 里 A/B/C 三个场景混在一起，搜「更正」会命中别的场景的更正。已在阅读提示里要求按对象判断相关性；C 稿里仍偶尔出现别的场景的句子。
- **BioCheck、MechEvidence 的 GitHub 远端**（a54b19211 名下）在 zktest 上 `ls-remote` 没有返回分支，主分支以 zktest 上的仓库为准；需要仓库主人确认远端地址和权限。
- **Fabric 改动在审核中**：`feat/radar-rebuild`（document_evidence、并发归档死锁修复）等徐子雯、唐蔚雯审核合进 `dev`。现役 Fabric 仍是 `82bad7b`，雷达只读它；pubmed 在现役许可里不可读，雷达线上配置没有 pubmed。
