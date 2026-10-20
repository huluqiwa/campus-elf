"""学校编号生成（用户确认：`SCH-` + 8 位随机，排除易混淆字符）。"""

import secrets

# 排除 0/O、1/I/L，降低口头/手抄错误
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8


def generate_school_code() -> str:
    """生成形如 `SCH-7K3M9Q2P` 的学校编号（唯一性由数据库约束兜底）。"""

    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return f"SCH-{body}"
