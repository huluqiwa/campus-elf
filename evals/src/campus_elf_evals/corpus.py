"""TD-294 实验语料生成器（种子化确定性；简报 F2-A）。

产出：`evals/fixtures/corpus/<doc>.md` ×10（30–50KB 总量，段落/表格/代码混合、
长原子表 >500）+ `manifest.json`（每文档章节清单与事实句——评估用例对齐用）。
生成产物入库（可复现、可评审）；重跑同种子零漂移。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass

FIXTURES_DIR = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "corpus"
SEED = 20261117


@dataclass(frozen=True)
class Section:
    """章节：标题 + 事实句（检索命中判定的对齐锚）+ 填充段落数。"""

    title: str
    facts: tuple[str, ...]
    filler_paragraphs: int = 2


@dataclass(frozen=True)
class Doc:
    """文档：标题 + 章节 + 可选表格/代码块。"""

    title: str
    sections: tuple[Section, ...]
    table: tuple[str, ...] | None = None  # GFM 表行（首行表头，含分隔行自动补）
    table_section: str | None = None  # 表格归属章节（None=末尾追加独立章节）
    table_is_long_atomic: bool = False  # True=整表原子块 >500 码点（表格不拆语义验证；表行最长 258）
    code: tuple[str, ...] | None = None  # 代码块行（归属最后章节）

    @property
    def table_title(self) -> str:
        return self.table_section or "附表"


def _pad(sentence: str, filler: str, target: int) -> str:
    """把事实句填充到目标长度（保留事实句原样开头——检索判定锚不变形）。"""
    body = f"{sentence}{filler}"
    while len(body) < target:
        body += filler
    return body[:target] if len(body) > target + 60 else body


def _filler(doc_title: str, section_title: str, index: int) -> str:
    return (
        f"{doc_title}中关于{section_title}的第{index}条补充说明：执行细则参照学校"
        "现行管理制度汇编执行，遇政策调整以最新发文为准，由归口部门负责解释与修订；"
        "各单位应组织师生学习并留存学习记录备查，年度检查纳入目标考核。"
    )


def render_doc(doc: Doc) -> str:
    """Doc → markdown（# 文档标题 / ## 章节 / 事实句+填充段落 / 表格 / 代码块）。"""
    parts: list[str] = [f"# {doc.title}", ""]
    for section in doc.sections:
        parts.append(f"## {section.title}")
        parts.append("")
        for i, fact in enumerate(section.facts, start=1):
            parts.append(_pad(fact, _filler(doc.title, section.title, i), 260 + i * 40))
            parts.append("")
        for j in range(section.filler_paragraphs):
            parts.append(_pad(
                f"{section.title}的执行要点第{j + 1}条：责任到人、留痕可查、定期复核。",
                _filler(doc.title, section.title, j + 10),
                180,
            ))
            parts.append("")
    if doc.table is not None:
        parts.append(f"## {doc.table_title}")
        parts.append("")
        for k, row in enumerate(doc.table):
            parts.append(row)
            if k == 0:
                header_cells = row.count("|") - 1
                parts.append("|" + " --- |" * header_cells)
        parts.append("")
    if doc.code is not None:
        parts.append("## 附：操作示例")
        parts.append("")
        parts.append("```python")
        parts.extend(doc.code)
        parts.append("```")
        parts.append("")
    return "\n".join(parts)


def build_corpus() -> dict[str, str]:
    """种子化语料（当前固定内容集——确定性由「无随机数」保证，SEED 仅记录版本）。"""
    docs: list[Doc] = [
        Doc(
            title="奖学金评定办法",
            sections=(
                Section("申请条件", ("本科奖学金申请者上一学年学业平均绩点须达到 3.5 及以上，且无任何违纪处分记录。",)),
                Section("公示与申诉", ("评定结果公示期为 5 个工作日，学生对结果有异议可在公示期内向学生工作处提交书面申诉。",)),
            ),
            table=(
                "| 等级 | 绩点要求 | 综合素质分要求 | 名额比例 | 单项金额 | 补充说明 |",
                f"| 特等 | 3.9 以上 | 95 分及以上 | 3% | 5000 元 | {'另须获省级以上奖项一次，' * 12}国家奖学金获得者优先推荐 |",
                f"| 一等 | 3.7 以上 | 90 分及以上 | 8% | 3000 元 | {'连续两学期获得者上浮百分之十，' * 12}与国家奖学金不可重复获得 |",
                f"| 二等 | 3.5 以上 | 85 分及以上 | 15% | 2000 元 | {'转专业学生按新专业同年级标准评定，' * 12}绩点按修读课程学分加权 |",
            ),
            table_section="评定标准",
            table_is_long_atomic=True,
        ),
        Doc(
            title="学生请假管理办法",
            sections=(
                Section("审批人", ("学生请假 3 天以内由辅导员审批，3 天以上 7 天以内由学院分管领导审批。",)),
                Section("时限与销假", ("请假累计不得超过 15 个工作日，返校后 2 个工作日内须完成销假手续。",)),
                Section("逾期处理", ("未按期销假且无正当理由的，按旷课处理，每学时记入学籍预警系统。",)),
            ),
        ),
        Doc(
            title="图书馆借阅规则",
            sections=(
                Section("借阅数量与期限", ("本科生每人可借图书 10 册，借阅期限为 30 天，到期前可在线办理一次续借。",)),
                Section("续借与预约", ("续借延长 15 天且仅限一次，已被预约的图书不得续借，预约保留 3 天。",)),
                Section("赔偿标准", ("遗失图书按原价 3 倍赔偿，成套图书遗失其中一册按整套价格的两倍赔偿。",)),
            ),
        ),
        Doc(
            title="考试纪律条例",
            sections=(
                Section("考场规则", ("考生须提前 10 分钟入场，开考 15 分钟后不得入场，考试结束前 30 分钟方可交卷离场。",)),
                Section("违纪处理", ("携带手机等通讯设备进入座位视为作弊，该课程成绩记零分并给予警告处分。",)),
                Section("成绩复核", ("对成绩有异议可在成绩公布后 5 个工作日内向教务处提交复核申请，复核只核查加分与登分。",)),
            ),
        ),
        Doc(
            title="宿舍管理规定",
            sections=(
                Section("门禁与晚归", ("宿舍楼门禁时间为 23:00，晚归须凭校园卡登记，一学期晚归累计 3 次通报学院。",)),
                Section("用电安全", ("宿舍内禁止使用大功率电器，额定功率超过 800 瓦的设备一律没收并给予警示。",)),
                Section("访客登记", ("访客须在 21:00 前完成登记并按时离开，留宿访客一经发现通报处理。",)),
            ),
        ),
        Doc(
            title="校园网服务指南",
            sections=(
                Section("入网认证", ("校园网采用网页认证方式，认证账号为学号，初始密码为证件号后 6 位。",)),
                Section("流量计费", ("每月免费流量 20GB，超出部分按 1 元每 GB 计费，当月结余不累计到次月。",)),
                Section("故障报修", ("网络故障可拨打服务热线 8866 或在自助平台提交工单，响应时限为 4 个小时。",)),
            ),
            code=(
                "import sys",
                "",
                "def check_auth(student_id: str) -> bool:",
                "    # 认证成功返回 True；重试三次失败将锁定 30 分钟",
                "    return len(student_id) == 10 and student_id.isdigit()",
                "",
                "sys.exit(0 if check_auth('2026000001') else 1)",
            ),
        ),
        Doc(
            title="心理咨询中心须知",
            sections=(
                Section("预约方式", ("心理咨询实行预约制，可通过电话 8777 或现场登记预约，每次咨询 50 分钟。",)),
                Section("保密原则", ("咨询内容严格保密，仅在出现自伤或伤人风险时才会突破保密并通知紧急联系人。",)),
                Section("危机干预", ("危机个案由值班咨询师 30 分钟内响应，并按学校危机干预预案逐级上报。",)),
            ),
        ),
        Doc(
            title="社团注册与年审",
            sections=(
                Section("注册条件", ("成立社团须有 20 名以上在读学生联名发起，并聘请一名在职教师担任指导老师。",)),
                Section("年审材料", ("年审须提交年度活动总结、经费明细和下一年度计划，截止日期为每年 9 月 30 日。",)),
                Section("经费报销", ("活动经费报销须凭正规发票，单次超过 1000 元须经团委书记签批。",)),
            ),
        ),
        Doc(
            title="食堂餐饮管理",
            sections=(
                Section("供餐时间", ("食堂早餐供餐时间为 6:30 至 9:00，午餐为 11:00 至 13:00，晚餐为 17:00 至 19:30。",)),
                Section("食品安全", ("食品留样保存 48 小时以上，每餐次留样不少于 125 克，并做好留样记录。",)),
                Section("投诉渠道", ("餐饮投诉可扫描餐台二维码提交，受理后 3 个工作日内反馈处理结果。",)),
            ),
        ),
        Doc(
            title="校车运行时刻",
            sections=(
                Section("线路与班次", ("校车工作日发车 8 班次，首班 7:20 自东门发车，末班 21:30 自主校区返回。",)),
                Section("乘车凭证", ("乘坐校车须出示校园卡或乘车码，每车限载 45 人，超员时按到达顺序等候下一班。",)),
                Section("假期调整", ("寒暑假期间校车停运，开学前 2 天恢复运行，具体时刻以后勤处通知为准。",)),
            ),
        ),
    ]
    return {doc.title: render_doc(doc) for doc in docs}


def build_manifest(corpus: dict[str, str]) -> dict[str, object]:
    """语料清单：每文档字节数与章节路径（评估判定与用例对齐）。"""
    manifest: dict[str, object] = {"seed": SEED, "documents": []}
    docs_total = 0
    for title, markdown in corpus.items():
        docs_total += len(markdown.encode("utf-8"))
        sections = [
            line[3:].strip() for line in markdown.splitlines() if line.startswith("## ")
        ]
        (manifest["documents"] if isinstance(manifest["documents"], list) else []).append(
            {"title": title, "bytes": len(markdown.encode("utf-8")), "sections": sections}
        )
    manifest["total_bytes"] = docs_total
    return manifest


def write_corpus(out_dir: pathlib.Path = FIXTURES_DIR) -> dict[str, object]:
    """生成并写盘（fixtures/corpus/*.md + manifest.json）；返回清单。"""
    corpus = build_corpus()
    out_dir.mkdir(parents=True, exist_ok=True)
    for title, markdown in corpus.items():
        (out_dir / f"{title}.md").write_text(markdown, encoding="utf-8", newline="\n")
    manifest = build_manifest(corpus)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    return manifest


def main() -> None:
    manifest = write_corpus()
    print(f"corpus docs={len(manifest['documents'])} total_bytes={manifest['total_bytes']}")
    for doc in manifest["documents"]:  # type: ignore[union-attr]
        print(f"  - {doc['title']} {doc['bytes']}B sections={doc['sections']}")  # type: ignore[index]


if __name__ == "__main__":
    main()
