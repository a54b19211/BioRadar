"""Service ownership is independent of paper scope or physical deployment count."""

PROVIDERS = (
    {
        "id": "data_fabric",
        "name": "Data-Fabric 数据服务",
        "owner": "徐子雯",
        "capabilities": ["data.batch"],
        "state": "not_connected",
        "note": "支持有界只读文档采用与 data.batch 转换；当前不是全量持续同步。",
    },
    {
        "id": "identity",
        "name": "共享身份与时态图谱",
        "owner": "唐蔚雯",
        "capabilities": ["entity.resolve"],
        "state": "not_connected",
        "note": "复用 Data-Fabric 与既有 PrimeKG；补身份映射、时态断言与变更引用。",
    },
    {
        "id": "knowledge",
        "name": "WeKnora 检索与查询调度",
        "owner": "李明洋",
        "capabilities": ["evidence.retrieve"],
        "state": "not_connected",
        "note": "复用现役检索；需验证来源版本、权限与截止时间的保留。",
    },
    {
        "id": "mechanism",
        "name": "机制证据评价",
        "owner": "汪鑫怡",
        "capabilities": ["mechanism.evaluate"],
        "state": "not_connected",
        "note": "等待方法源码与可调用样例；雷达不重复实现评价模型。",
    },
    {
        "id": "generation",
        "name": "约束生成与事实核验",
        "owner": "潘倩",
        "capabilities": ["answer.compose", "answer.check", "answer.revise"],
        "state": "not_connected",
        "note": "已核验规则型原型；局部字段匹配不能作为医学事实与发布核验通过。",
    },
    {
        "id": "cpubrain",
        "name": "CPUBrain 科研执行",
        "owner": "CPUBrain 维护者",
        "capabilities": ["execution.submit", "execution.status", "execution.cancel"],
        "state": "not_connected",
        "note": "对话入口共用雷达 API，实际科研执行保留在 CPUBrain。",
    },
)
