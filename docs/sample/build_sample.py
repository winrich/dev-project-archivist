# -*- coding: utf-8 -*-
"""生成 README 里展示的样例报告页。

为什么要有这个脚本：README 需要一张"这东西长什么样"的图，但**不能拿真实归档当素材**——
真实项目名、真实 token 量、真实时间分布都属于使用者自己。所以这里用一套虚构数据，
交给**同一份渲染器**（`archiver.build_report_html`）生成。

关键点：不是手写一份仿冒的 HTML。样例页与用户实际得到的那份出自同一个函数，
改了样式，样例跟着变——它不会漂。

用法：
    python docs/sample/build_sample.py
    → 写出 docs/sample/report-demo.html（自包含，双击可看）

样例里出现的项目名是一组固定的虚构名（与 README 其余示例一致）：
BatchRenamer / order-sync / 报表导出器 / InvoiceParser / task-board / reader-private / CDN / some-plugin
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "scripts"))

import archiver  # noqa: E402

# 30 天，从 2026-10-20 到 2026-11-18。数字是编的，形状是真实的：
# 有连休、有周末低谷、有两三个明显的加班日、有两天完全空白。
DAILY_PROMPTS = [2, 0, 6, 10, 5, 2, 0, 8, 15, 6, 4, 1, 0, 11, 17,
                 7, 3, 0, 4, 15, 21, 8, 6, 2, 0, 12, 16, 6, 21, 3]

# 项目：名字、对话数、改动文件、会话数、token 占比、起止日
PROJECTS = [
    ("BatchRenamer",   62, 148, 19, 0.30, "2026-10-20", "2026-11-18"),
    ("order-sync",     41,  96, 14, 0.20, "2026-10-22", "2026-11-17"),
    ("报表导出器",      33,  71, 11, 0.16, "2026-10-25", "2026-11-16"),
    ("InvoiceParser",  27,  58,  9, 0.13, "2026-10-27", "2026-11-14"),
    ("task-board",     19,  44,  7, 0.09, "2026-11-01", "2026-11-12"),
    ("reader-private", 14,  31,  5, 0.07, "2026-10-29", "2026-11-09"),
    ("CDN",             9,  18,  3, 0.03, "2026-11-04", "2026-11-10"),
    ("some-plugin",     6,  12,  2, 0.02, "2026-11-06", "2026-11-08"),
]

TOK_IN = 18_400_000
TOK_OUT = 3_900_000
TOK_CACHE = 15_100_000
FILES_TOTAL = 478
SESSIONS = 70


def build() -> dict:
    dates = archiver._day_list("2026-11-18", len(DAILY_PROMPTS))
    total = float(sum(DAILY_PROMPTS)) or 1.0

    daily = []
    for i, (d, p) in enumerate(zip(dates, DAILY_PROMPTS)):
        share = p / total
        daily.append({
            "date": d,
            "prompts": p,
            "hits": [1] * p,                       # 渲染器只判真假，长度无意义
            "files": round(FILES_TOTAL * share),
            "tokens": int((TOK_IN + TOK_OUT) * share),
            "writes": round(FILES_TOTAL * share * 1.4),
            "edits": round(FILES_TOTAL * share * 2.1),
            "is_ext": False,
        })

    projects = [{
        "name": name,
        "prompts": p,
        "files": f,
        "sessions": s,
        "tokens": int((TOK_IN + TOK_OUT) * share),
        "first": first,
        "last": last,
    } for name, p, f, s, share, first, last in PROJECTS]

    return {
        "ok": True,
        "from": dates[0], "to": dates[-1], "days": len(dates),
        "totals": {
            "prompts": sum(p["prompts"] for p in projects),
            "sessions": SESSIONS,
            "projects": len(projects),
            "files": sum(p["files"] for p in projects),
            "writes": sum(d["writes"] for d in daily),
            "edits": sum(d["edits"] for d in daily),
            "tok_in": TOK_IN, "tok_out": TOK_OUT,
            "cache_read": TOK_CACHE,
            "cache_rate": TOK_CACHE / float(TOK_IN) * 100.0,
        },
        "daily": daily,
        "projects": projects,
        "blank": [d["date"] for d in daily if not d["hits"]],
    }


def main() -> int:
    d = build()
    path = HERE / "report-demo.html"
    path.write_text(archiver.build_report_html(d), encoding="utf-8", newline="\n")
    print(f"已写入 {path}")
    print(f"  {d['days']} 天 · {d['totals']['projects']} 个项目 · "
          f"{d['totals']['prompts']} 条对话 · {d['totals']['files']} 个改动文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
