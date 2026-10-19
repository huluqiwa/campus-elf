"""CPU 嵌入/重排最小基线基准（M0-F）。

用途：记录 TEI（BGE-M3 embed / bge-reranker-v2-m3 rerank）在本机的数量级，
供 M3 切块与批量参数、以及上线选型参考。**不是性能门禁**，不做调优。

用法（backend 的 uv 环境提供 httpx）：

    cd backend
    uv run python ../benchmarks/embed_rerank_bench.py --markdown

输出：benchmarks/results/m0_embed_rerank.json（git 忽略）+ 可选 Markdown 摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "benchmarks" / "results" / "m0_embed_rerank.json"

# 合成语料：接近学校知识库片段的长度（80–160 字），不含真实数据。
SAMPLE_TEMPLATES = [
    "图书馆开放时间为工作日 08:00-22:00，周末 09:00-18:00，法定节假日闭馆；本科生可借 10 册，借期 30 天。",
    "奖学金评定依据学业成绩与综合素质测评，成绩排名前 10% 且无违纪记录者可申请一等，名额按学院分配。",
    "请假流程：学生提交申请，班主任审批，超过三天需辅导员复核；销假需在返校后 24 小时内完成。",
    "宿舍用电安全规定：禁止使用大功率电器，离开宿舍须断电；违规将按学生手册第 12 条处理。",
    "选课系统开放时间：每学期第 1-2 周补退选，逾期不受理；重修课程需在教务系统单独申请。",
    "校园卡补办：携带学生证到一站式服务大厅办理，工本费 20 元，补办期间可使用电子校园卡。",
    "心理咨询预约：每周一至周五 09:00-17:00 可电话或线上预约，紧急情况可拨打 24 小时热线。",
    "考试纪律：携带学生证入场，迟到 15 分钟不得入场；违纪按《考试违规处理办法》处理。",
]


@dataclass
class CaseResult:
    """单个基准用例的统计结果。"""

    name: str
    batch: int
    repeats: int
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def p50_ms(self) -> float:
        return round(statistics.median(self.latencies_ms), 1)

    @property
    def p95_ms(self) -> float:
        ordered = sorted(self.latencies_ms)
        index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return round(ordered[index], 1)

    @property
    def mean_ms(self) -> float:
        return round(statistics.fmean(self.latencies_ms), 1)

    @property
    def items_per_second(self) -> float:
        total_items = self.batch * self.repeats
        total_seconds = sum(self.latencies_ms) / 1000
        return round(total_items / total_seconds, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "batch": self.batch,
            "repeats": self.repeats,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "mean_ms": self.mean_ms,
            "items_per_second": self.items_per_second,
        }


def _texts(count: int) -> list[str]:
    """生成 count 条互不相同的合成文本。"""

    return [f"{SAMPLE_TEMPLATES[i % len(SAMPLE_TEMPLATES)]}（样本 {i}）" for i in range(count)]


def _embed_payload(batch: int) -> dict[str, Any]:
    """构造 embed 请求负载。"""

    return {"inputs": _texts(batch)}


def _rerank_payload(batch: int) -> dict[str, Any]:
    """构造 rerank 请求负载。"""

    return {"query": "奖学金申请条件是什么", "texts": _texts(batch)}


def _probe_batch_limit(
    client: httpx.Client,
    url: str,
    payload_builder: Any,
    candidates: tuple[int, ...] = (32, 16, 8, 4, 2, 1),
) -> tuple[int, dict[str, bool]]:
    """探测服务端允许的最大批大小（TEI 会按 token 预算拒绝过大批）。

    参数:
        client: HTTP 客户端。
        url: 目标端点。
        payload_builder: 按批大小构造负载的函数。
        candidates: 待探测的批大小（从大到小）。

    返回值:
        (最大可用批大小, 各候选是否可用的映射)。

    异常:
        RuntimeError: 所有候选都被拒绝（服务配置异常）。
    """

    outcomes: dict[str, bool] = {}
    for size in candidates:
        response = client.post(url, json=payload_builder(size))
        outcomes[str(size)] = response.status_code < 400
    # 从大到小取第一个可用者即服务端上限。
    for size in sorted(candidates, reverse=True):
        if outcomes[str(size)]:
            return size, outcomes
    raise RuntimeError(f"所有候选批大小均被拒绝: {outcomes}")


def _run_case(
    client: httpx.Client,
    *,
    url: str,
    name: str,
    payload_builder: Any,
    batch: int,
    repeats: int,
) -> CaseResult:
    """执行一个基准用例（含预热）。"""

    result = CaseResult(name=name, batch=batch, repeats=repeats)
    for _ in range(3):  # 预热：避免把模型首次加载计入
        client.post(url, json=payload_builder(batch))
    for _ in range(repeats):
        started = time.perf_counter()
        response = client.post(url, json=payload_builder(batch))
        if response.status_code >= 400:
            # 带上响应体，便于区分「输入非法」与「服务未就绪」。
            raise RuntimeError(
                f"{name} batch={batch} HTTP {response.status_code}: {response.text[:300]}"
            )
        result.latencies_ms.append((time.perf_counter() - started) * 1000)
    return result


def _machine_info() -> dict[str, Any]:
    """记录机器规格（脱离规格的数字没有意义）。"""

    return {
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "note": "TEI 容器为 CPU 推理，compose 限制 mem_limit 2.5GB / cpus 2（deploy/docker-compose.yml）",
    }


def main() -> int:
    """运行基准并写出 JSON/Markdown。"""

    parser = argparse.ArgumentParser(description="TEI embed/rerank 最小基线基准")
    parser.add_argument("--embed-url", default=os.environ.get("TEI_EMBED_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--rerank-url", default=os.environ.get("TEI_RERANK_URL", "http://127.0.0.1:8082"))
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--batches", default="1,8,32", help="待测批大小；超出服务端上限的会记录为跳过")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown", action="store_true", help="打印 Markdown 摘要表")
    args = parser.parse_args()

    embed_url = f"{args.embed_url.rstrip('/')}/embed"
    rerank_url = f"{args.rerank_url.rstrip('/')}/rerank"
    requested_batches = [int(item) for item in args.batches.split(",") if item.strip()]

    results: list[CaseResult] = []
    skipped: list[dict[str, Any]] = []
    with httpx.Client(timeout=120) as client:
        # 健康检查：避免把「服务没起来」误报成慢。
        for url in (embed_url.replace("/embed", "/health"), rerank_url.replace("/rerank", "/health")):
            client.get(url).raise_for_status()

        embed_limit, embed_probe = _probe_batch_limit(client, embed_url, _embed_payload)
        rerank_limit, rerank_probe = _probe_batch_limit(client, rerank_url, _rerank_payload)

        for batch in requested_batches:
            if batch > embed_limit:
                skipped.append({"case": "embed", "batch": batch, "reason": f"超过服务端上限 {embed_limit}"})
                continue
            results.append(
                _run_case(
                    client,
                    url=embed_url,
                    name=f"embed_batch{batch}",
                    payload_builder=_embed_payload,
                    batch=batch,
                    repeats=args.repeats,
                )
            )

        for batch in requested_batches:
            if batch > rerank_limit:
                skipped.append(
                    {"case": "rerank", "batch": batch, "reason": f"超过服务端上限 {rerank_limit}"}
                )
                continue
            results.append(
                _run_case(
                    client,
                    url=rerank_url,
                    name=f"rerank_candidates{batch}",
                    payload_builder=_rerank_payload,
                    batch=batch,
                    repeats=args.repeats,
                )
            )

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "machine": _machine_info(),
        "repeats": args.repeats,
        "service_batch_limit": {
            "embed": {"max_batch": embed_limit, "probe": embed_probe},
            "rerank": {"max_batch": rerank_limit, "probe": rerank_probe},
        },
        "cases": [case.as_dict() for case in results],
        "skipped": skipped,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.markdown:
        print("| 用例 | 批大小 | p50 (ms) | p95 (ms) | 吞吐 (items/s) |")
        print("|---|---|---|---|---|")
        for case in results:
            print(
                f"| {case.name} | {case.batch} | {case.p50_ms} | {case.p95_ms} | {case.items_per_second} |"
            )

    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
