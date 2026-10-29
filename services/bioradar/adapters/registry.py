"""Service ownership is independent of paper scope or physical deployment count."""

PROVIDERS = (
    {
        "id": "data_fabric",
        "name": "Data-Fabric 数据服务",
        "owner": "徐子雯",
        "capabilities": ["data.batch"],
        "state": "not_connected",
        "note": "读取已发布原文版本、增量变化和调查查询结果。",
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
        "note": "使用 MechEvidence 评价证据关系、适用范围和机制链。",
    },
    {
        "id": "generation",
        "name": "约束生成与事实核验",
        "owner": "潘倩",
        "capabilities": ["answer.compose", "answer.check", "answer.revise"],
        "state": "not_connected",
        "note": "使用 BioCheck 编写、核查和修订最终报告，模型请求由雷达统一执行。",
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


def provider_statuses(settings):
    """Describe configuration here; per-investigation receipts show actual use."""
    from bioradar.adapters.biocheck import provider_status as biocheck_status
    from bioradar.adapters.weknora import provider_status as weknora_status

    providers = [dict(provider) for provider in PROVIDERS]
    by_id = {provider["id"]: provider for provider in providers}
    fabric_configured = bool(settings.data_fabric_url and settings.data_fabric_history_read_token)
    if fabric_configured:
        by_id["data_fabric"].update(
            state="configured", note="已配置版本读取和增量入口；本次读取情况见调查过程及后台状态。"
        )
        by_id["identity"].update(
            state="configured", note="通过 Fabric 读取对象与图谱资料；具体关系及原文绑定以调查结果为准。"
        )
    for identifier, status in (
        ("knowledge", weknora_status(settings)),
        ("generation", biocheck_status(settings)),
    ):
        by_id[identifier].update(
            state="configured" if status.get("available") else "not_connected",
            configuration=status,
        )
    from bioradar.adapters.mech_evidence import provider_status as mechanism_status

    status = mechanism_status(settings)
    by_id["mechanism"].update(
        state="configured" if status.get("available") else "not_connected",
        configuration=status,
    )
    return providers
