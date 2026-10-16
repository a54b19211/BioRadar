# 接口说明

运行中的 `/openapi.json` 和 `contracts/bioradar-engine.openapi.json` 列出实际接口。`bioradar-core.openapi.json` 保留完整产品目标，不能据此认为 `/api/v1` 已经可用。所有新增功能继续使用 `/api/engine` 下的开发预览接口。

## 使用方式

浏览器先登录本地研究空间，后续用会话访问；命令行使用本地用户凭据。来源许可由服务器配置，页面不能自行增加权限。上游API key只在服务端使用。

写请求带 `Idempotency-Key`。同一次提交因网络原因重试时沿用原键；改变内容使用新键。修改调查时同时提交当前revision，旧页面的数据不能直接盖掉新记录。

## 已有资料和计算

| 请求 | 用途 |
|---|---|
| POST /snapshots | 采用一份有来源的资料包 |
| GET /snapshots；GET /snapshots/{id} | 查看已采用资料及其本次内容 |
| POST /runs；GET /runs/{id} | 提交并查看线索计算 |
| POST /runs/{id}/feedback | 保存人工对线索的意见，不改原计算 |
| POST /decision-runs；GET /decision-runs/{id} | 提交并查看按给定条件的方案比较 |

资料格式见 engine-snapshot.schema.json，计算条件见 engine-policy.schema.json；方案比较见 decision-input.schema.json 和 decision-policy.schema.json。示例在contracts/examples。旧结果保留原输入、计算版本和依据；不拿今天默认值改旧结果。

## 调查记录

调查记录同步保存在同一数据库；它不新增一种后台计算任务。入口可以是已有线索，也可以是研究人员直接提出的问题。

| 请求 | 用途 |
|---|---|
| POST /investigations | 建立调查 |
| GET /investigations | 列表 |
| GET /investigations/{id} | 查看当前问题、材料、判断和历史 |
| POST /investigations/{id}/entries | 追加材料、笔记、判断或进度变化 |

创建字段：title、question、questions、is_demo，以及可选origin。origin绑定实际run_id、result_hash、candidate_key，由服务器确认候选，页面不重新提交一份候选正文。

追加内容分四种，均携带expected_revision：

- note：text，保存研究笔记。
- material：snapshot_id、record_ref、quote、stance、text。stance为support/oppose/context/uncertain，是研究人员对本问题的分类，不是专业评价结果。
- judgment：text、conditions、basis_entry_ids、assessment。依据只用本调查已加入的材料ID；assessment为provisional或unable_to_determine，不能提交“专业已批准”。
- status：status、text、question_updates。状态为open/paused/closed；关闭表示停止跟进。question_updates指定已有问题及pending/checking/resolved/blocked状态。

服务器保存提交账号、时间、当时依据和上一版判断；目前使用本地开发账号，尚未接学校统一身份。只要存在未纳入最新判断的材料，needs_review就保留复查提示，旧判断及其依据不改变。外部专业核验尚未接通时professional_status始终如实显示not_reviewed。

判断写得是否正确由专业审阅确认；程序负责不丢依据、不串记录、保留变化。

## 两次资料比较

GET /snapshot-comparisons?before_id=...&after_id=... 比较两份已经成功采用的资料。比较仅覆盖所选范围，不能据此宣称扫描过全库。

输出区分新增、内容改变、内容相同、本批未出现；来源属性或身份补充另行说明。记录身份取来源实例和原记录标识，不用新映射的疾病/靶点ID替换文献身份。读取失败在读取阶段返回错误，不伪装成空包参加比较。

## 引用和日期

record_ref由实际采用资料提供，保留provider、object_type、object_id、adopted_version、version_source、content_hash、authorization_ref。附材料时复制已有引用，不自行编造。同一调查的已采用来源不能混合真实资料与教学资料；直接提出的问题第一次采用教学材料后，整项调查明确标为教学。quote必须来自这一版本的实际文本；无文本的结构化记录仍要说明采用范围。

资料发生/公开日期、上游首次可见、本次观察时间各自保留。今天读到旧资料不等于今天有新进展。内容标识用于关联版本，不能证明专业结论正确。

## 与共享课题的交接

| 维护人 | 雷达提供 | 需要返回 |
|---|---|---|
| 徐子雯 | 来源范围或指定记录名单 | 清洗资料、原定位、日期含义、版本与读取状态 |
| 唐蔚雯 | 需要辨认的对象或关系 | 身份映射、关系及其时间、来源和版本 |
| 李明洋的WeKnora服务 | 一个具体核实问题和查找范围 | 有出处的材料、检索范围与未查到的部分 |
| 汪鑫怡 | 问题、研究条件和证据 | 机制证据支持程度、反面材料、适用条件和评价依据 |
| 潘倩 | 证据、已采用评价、表达条件或待检查稿件 | 有依据的文本、问题定位及候选修订 |

共享交换格式保留在provider-exchange.schema.json。该格式已经定义，不表示每项服务都已实现。接入时先验证一个真实成功结果和一个失败结果，再启用页面上的实际调用。调查目标、继续/暂停、方案比较和判断版本归雷达。

## 方法范围

现有发现方法覆盖词组与文本主题、技术/疾病组合、状态变化、管线格局、合作网络、证据阶段变化、反证影响、阶段停留、事件影响路径及区域指标比较。各方法分别说明输入条件；正文可以开始探索，缺图谱或专业评价只影响依赖它的方法。

方案比较检查资源条件、相对现状差异、多目标取舍及声明情景下的损失范围。它不提供默认价值权重、成功概率或未经专业模型计算的政策效果。现有方法与未来研究目标分别列入项目说明和资产盘点。
