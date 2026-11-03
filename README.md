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

（跑完验收后填写）

## 未完成

（跑完验收后填写）
