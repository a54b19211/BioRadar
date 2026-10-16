# 开发说明

## 环境和启动

需要 Python 3.12 和 Docker Compose。前端构建使用项目已有的 Node 22 容器。由李明洋先在开发机执行：

```bash
make init
make bootstrap
make dev
```

Web 默认地址为 `http://127.0.0.1:15176`，API 为 `http://127.0.0.1:18100`。远程使用由李明洋提供转发方式，例如 `ssh -L 15176:127.0.0.1:15176 <开发机>`，然后在自己电脑访问本地页面。目前只用于本地研究。

登录凭据保存在 `ops/.env` 的 `ENGINE_DEV_CREDENTIAL`，与服务器密码、上游 API key 分开。由李明洋单独提供给使用者，不写进文档或截图。浏览器输入后换取短期会话。

## 跑一次例子

启动后执行：

```bash
make example
```

命令通过实际接口和后台计算建立一项教学调查，最后打印页面地址。登录后能看到初始线索、五条采用材料和前后两次研究人员判断。每次运行新建一项调查，资料和同条件计算可以复用。结果位置记录在 `artifacts/example.json`，其中不含登录凭据。

例子用的是明确标为测试的材料，作用是解释程序怎样运行，没有生成真实医学或投资建议。所有文字判断由示例脚本明确填写，不是模型评价。

自己操作时：

1. 在“情报雷达”导入 `contracts/examples/engine-fixture.snapshot.json`，选择该资料并运行分析。计算条件可参考同目录的 `engine-fixture.policy.json`。
2. 打开“规范实体组合：K”线索，填写研究问题，保存为调查。
3. 打开调查，选择已经采用的资料，加入材料、分类和说明；保存一次研究人员判断。
4. 再加入一条反面材料，查看待复查提示。记录第二次判断，检查第一版和它当时采用的依据仍在。
5. 刷新页面、暂停或继续调查，最后打印已保存的研究记录。

资料更新例子使用 `engine-fixture.snapshot.json` 与 `engine-fixture-v2.snapshot.json`。页面中选择这两份资料比较，可以看到一条新增、一条内容变更、十四条内容相同、一条本批未出现。新增项是旧资料补录；其中一条内容相同的记录只补充了身份信息。两者都不能当作新近独立研究。

## 接真实资料

采集、清洗和来源版本继续由 Data-Fabric 维护。李明洋先确认现役地址与来源许可，再使用现有读取命令。记录名单是一个 JSON 字符串数组，当前一次最多 20 条：

```bash
.venv/bin/python -m bioradar.ops.snapshot_data_fabric \
  --source-instance datafabric-test \
  --manifest <记录名单.json> \
  --output <采用资料.json>
```

服务地址从服务器环境变量 `BIORADAR_DATA_FABRIC_URL` 读取，来源许可按 `BIORADAR_ENGINE_SOURCE_ALLOWLIST` 和 `BIORADAR_ENGINE_SOURCE_ID_ALLOWLIST` 配置；默认仅包含已有教学来源和核对过的 ClinicalTrials 来源。配置由李明洋维护，页面不能自行扩大许可。

当前读取一批资料完整成功才输出资料包；失败直接报错，不输出空包，也不覆盖以前的资料。两批比较只处理已经成功采用的内容，不能判断某条未出现记录究竟是未选择、没读到还是已删除。稳定全库更新需要上游继续提供相应接口。

真实材料放在本地受控位置，不进入示例或分发包。已核验的三个真实摘要只证明接入和引用可用，不代表已验证一个研究方向。

## 代码怎么找

| 位置 | 主要用途 |
|---|---|
| `services/bioradar/adapters/` | 读取外部系统并转换资料 |
| `services/bioradar/research/mining/` | 现有 11 类线索计算 |
| `services/bioradar/research/decision/` | 按给定目标和条件比较方案 |
| `services/bioradar/research/comparison.py` | 比较两次已采用资料 |
| `services/bioradar/research/investigations/` | 保存调查问题、材料、研究判断和进度 |
| `services/bioradar/research/engine/` | 研究接口、计算记录和后台执行 |
| `apps/web/src/pages/`、`components/engine/` | 页面和线索、材料、调查组件 |
| `db/migrations/`、`tests/` | 数据库结构变更及功能检查 |

例如要修改“保存为调查”，先看 `CandidateDetail.vue` 发出的请求，再看调查 `service.py` 的保存，再看 `InvestigationsPage.vue` 怎样读取。沿着这个操作修改即可，不再为它建立通用流程平台。负责人的目录划分见[分工安排](OWNERSHIP.md)。

调查页面拆成三部分：`InvestigationsPage.vue` 负责选择调查和保存，`InvestigationEntryForm.vue` 负责填写材料、判断和笔记，`InvestigationRecord.vue` 负责阅读当前内容和历史。改输入表单时，通常只需要先看第二个文件。

## 检查和运行问题

| 改了什么 | 怎么检查 |
|---|---|
| Python 业务逻辑 | 运行对应测试和 `make lint`，检查一个正常行为及相关错误行为 |
| 接口字段 | `make engine-contract` 导出，再 `make web-check` 生成类型和构建；不手改生成文件 |
| 数据库和后台运行 | `make engine-integration` 使用本地 PostgreSQL；`make smoke` 检查实际数据库与后台执行 |
| 页面 | `make web-check`；切换记录、保存、刷新、退出时实际操作一次 |
| 文档 | 核对链接、操作步骤和实现状态，不重复跑与文字无关的测试 |

代码交付前统一执行 `make test CARD=BOOT`、`make contract-check`、`make lint`、`make web-check`；数据库或队列有变化时执行 `make smoke`。数据库测试出现 skip 说明没有实际检查相应环境。

本次交接的检查结果保存在 [工程检查记录](../evaluation/handoff-verification.json)，包含代码审查、实际操作范围和仍未接通的服务。

应用版本、挖掘计算版本和决策计算版本分别维护。计算含义改变时升级相应版本，保留旧输入和结果。只保存调查笔记不需要改算法版本。

遇到运行问题先看 `make status` 和对应服务日志。Redis 暂不可用时，已受理的计算仍在数据库。恢复同一服务后，可以使用已有恢复入口：

```bash
docker compose --env-file ops/.env -f ops/compose.yaml exec -T api \
  python -m bioradar.research.engine.recover --limit 100
```

停止本项目使用 `make stop`，保留数据库卷。不要为调试雷达而清理上游数据或重启共享平台。调查记录直接保存在数据库，不需要另外启动一种后台作业。

## 检查研究结果是否有用

刘金彦负责评测工具，导师或独立研究人员提供专业判断。固定相同资料、截止时间、候选审阅数量和研究时间，比较普通问答、现有知识库和雷达。除了已选线索，还要抽查未选材料，记录漏报、误报、重复、反例遗漏和实际耗时。

开发数据与最终评价数据分开，同一研究的转载或不同版本不能分散到两边。历史回放只使用当时可获得的资料；当前填写的决策假设不能伪装成过去已经知道。代码检查验证程序行为，真实战略效果另行评价。

## 仓库怎么安排

建议在研究院统一的 Git 组织下，保留四个产品仓库。本次只是明确协作方式，没有搬动现有仓库。

| 仓库 | 放什么 | 怎样分工 |
|---|---|---|
| BioRadar | 雷达后端、Vue 页面、运行配置和本项目文档 | 按现有李明洋、孙博文、刘金彦的目录归属 |
| Data-Fabric | 采集、清洗、资料版本、数据接口，以及现有身份和图谱模块 | 徐子雯维护数据生产与服务，唐蔚雯维护身份、关系和图谱投影；在同仓按模块分工 |
| WeKnora 定制版 | 现有问答、检索与调度修改 | 李明洋维护，保留与开源上游同步的条件 |
| CPUBrain | 现有科研执行和工作流 | 保持现有维护、运行和发布方式 |

同一产品的前后端放在一起，方便一次修改一起检查。仓库和部署不是一回事：同仓可以有 API、Worker 两个进程；不同仓库也可以在一台开发机联调。跨产品通过接口交换，服务只写自己负责的数据，不通过合仓获得任意写其他数据库的权利。

潘倩、汪鑫怡先保留现有原型，各交付清楚的专业能力模块和调用样例。需要独立部署、共享依赖或共同发布时再确定服务仓库，不按学生人数先建一批平台。Data-Fabric 也不因徐子雯和唐蔚雯分工就复制成两套主数据工程。

李明洋统筹以下几件事，各仓库维护人负责自己的改动：

- 为每个目录登记维护人，重要分支经过审阅和必要检查后合并。使用 GitHub 时可用 [CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners) 指定目录审阅人。
- 接口由提供服务的仓库维护唯一版本，下游固定版本生成类型；跨仓修改分别提交，安排兼容过渡。不要复制几份接口定义后各改各的。
- 在 BioRadar 现有 `ops/` 中记录一组能共同运行的服务版本和联调命令。它只是部署记录，不发展成新的业务平台，也不把其他工程源码塞进去。
- 确认两台机器上的 Data-Fabric 哪一条是发布主线；环境差异用配置表达。已有本地修改先整理为可审阅提交，避免长期维护两份不知道差在哪里的程序。
- 数据、模型文件和凭据留在相应存储与配置中。项目内复制的其他工程先查清实际依赖，改用明确版本的服务或依赖后，再移除重复源码。
