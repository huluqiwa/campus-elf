"""测试内读取应用私有指标注册表（不暴露任何业务接口）。"""

from app.core.metrics import REGISTRY


def counter_value(name: str, labels: dict[str, str]) -> float:
    """读取计数器当前值；从未自增过的序列按 0 处理。"""

    sample = REGISTRY.get_sample_value(name, labels)
    return 0.0 if sample is None else sample
