# -*- coding: utf-8 -*-
"""生成 README 里展示的「后台管理台」样例页。

为什么要有这个脚本：README 的「长什么样」不能拿真实归档当素材——真实项目名、
路径、token 量都属于使用者自己。所以这里用一套**虚构数据**，交给**真实的后台页面**
（scripts/manage_web.html）渲染：不是手写一份仿冒的 HTML，而是注入一个 mock 数据层
覆盖网络请求，页面本身的 HTML/CSS/JS 一行不改。改了后台样式，demo 跟着变，不会漂。

用法：
    python docs/sample/build_manage_demo.py
    → 写出 docs/sample/manage-demo.html（自包含，双击可看，无任何网络请求）

样例里出现的项目名是一组固定的虚构名（与 report-demo / README 其余示例一致）：
BatchRenamer / order-sync / 报表导出器 / InvoiceParser / task-board / reader-private / CDN
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[1] / "scripts"

# 虚构项目：名字、类型、语言、活跃分、等级、会话/文件、体积、起止日、近14天会话数
# 数字是编的，形状是真实的：有高热、活跃、平稳、沉寂四档，有外链标记，有置顶。
_PROJECTS = [
    ("BatchRenamer",   "代码项目", "C#",        91, "hot",    38, 214, 34.6, "2026-10-20", "2026-11-18", [0,1,0,3,2,1,0,4,6,2,3,1,5,4], True,  False, True,  True,  ""),
    ("order-sync",     "代码项目", "Python",    78, "active",  26, 121, 12.8, "2026-10-22", "2026-11-17", [1,0,2,0,3,2,0,2,4,1,3,2,1,3], False, False, True,  False, "Python 定时任务，虚构示例"),
    ("报表导出器",      "文档任务", "Python",    63, "active",  19,  47,  3.2, "2026-10-25", "2026-11-16", [0,0,1,2,0,1,0,3,1,2,0,1,2,1], False, False, True,  False, ""),
    ("InvoiceParser",  "代码项目", "C#/Python", 55, "active",  15,  88,  9.4, "2026-10-27", "2026-11-14", [1,1,0,0,2,1,1,0,2,3,1,0,1,2], False, False, True,  False, ""),
    ("task-board",     "代码项目", "TypeScript",41, "calm",    11,  63,  6.1, "2026-11-01", "2026-11-12", [0,1,0,1,0,0,1,2,0,1,0,1,0,1], False, False, True,  False, ""),
    ("reader-private", "混合产物", "Python",    28, "calm",     7,  29,  2.4, "2026-10-29", "2026-11-09", [0,0,1,0,0,1,0,0,1,0,1,0,0,0], False, False, True,  False, ""),
    ("CDN",            "代码项目", "Go",        14, "idle",     3,  12,  0.9, "2026-11-04", "2026-11-10", [0,0,0,1,0,0,0,0,1,0,0,0,0,0], False, False, False, True,  ""),
    ("some-plugin",    "文档任务", "JavaScript", 9, "idle",     2,   8,  0.5, "2026-11-06", "2026-11-08", [0,0,0,0,1,0,0,0,0,1,0,0,0,0], False, False, True,  False, ""),
]


def _spark(counts):
    # 前端只需 {date,count}；日期用固定虚构区间，倒推 14 天即可
    from datetime import date, timedelta
    end = date(2026, 11, 18)
    return [{"date": str(end - timedelta(days=13 - i)), "count": c}
            for i, c in enumerate(counts)]


def build_projects():
    out = []
    for (name, typ, lang, score, level, sess, files, mb,
         first, last, spark, upgraded, archived, watching, skip, purpose) in _PROJECTS:
        out.append({
            "key": "demo/" + name.lower(),
            "dir": "2026-11-01_" + name,
            "name": name,
            "workspace": "demo-workspace/" + name,
            "type": typ,
            "external_dirs": ([{"root": "demo-workspace/external/" + name,
                                "writes": 42, "files": 9,
                                "first": 1794551776238, "last": 1794633273600}]
                              if upgraded else []),
            "upgraded_from_shell": upgraded,
            "language": lang,
            "first_dev": first,
            "last_dev": last,
            "last_archived": last,
            "sessions": sess,
            "files": files,
            "bytes": int(mb * 1048576),
            "status": "正常",
            "archived": archived,
            "pinned": name == "BatchRenamer",
            "watching": watching,
            "skip": skip,
            "note": "",
            "purpose": purpose,
            "manual": False,
            "updated_at": "",
            "activity": {"score": score, "level": level,
                         "spark": _spark(spark),
                         "active_days": sum(1 for x in spark if x),
                         "parts": {"recency": score, "freq": score,
                                   "output": score, "consistency": score}},
        })
    return out


def build_overview(projects):
    from collections import Counter
    by_type = Counter(p["type"] for p in projects)
    by_level = Counter(p["activity"]["level"] for p in projects)
    by_lang = Counter()
    for p in projects:
        for s in p["language"].split("/"):
            by_lang[s.strip()] += 1
    total_files = sum(p["files"] for p in projects)
    total_bytes = sum(p["bytes"] for p in projects)
    spark = [sum(p["activity"]["spark"][i]["count"] for p in projects)
             for i in range(14)]
    return {
        "total": len(projects),
        "watching": sum(1 for p in projects if p["watching"] and not p["skip"]),
        "archived": sum(1 for p in projects if p["archived"]),
        "total_files": total_files,
        "total_bytes": total_bytes,
        "by_type": dict(by_type),
        "by_level": dict(by_level),
        "by_lang": dict(by_lang),
        "spark": spark,
        "recent": sorted(projects, key=lambda p: p["last_dev"], reverse=True)[:5],
    }


def build_ranking(projects):
    out = [{"key": p["key"], "name": p["name"], "level": p["activity"]["level"],
            "score": p["activity"]["score"], "spark": p["activity"]["spark"],
            "sessions": p["sessions"], "last_dev": p["last_dev"]}
           for p in projects if p["watching"] and not p["skip"]]
    out.sort(key=lambda x: -x["score"])
    return out


# 虚构报告档案（按日期检索用）
_REPORTS = [
    {"name": "report-2026-11-18_2026-11-18.html", "from": "2026-11-01", "to": "2026-11-18",
     "generated_at": "2026-11-18 22:10", "days": 18, "prompts": 143, "tokens": 2180000000},
    {"name": "report-2026-10-20_2026-11-17.html", "from": "2026-10-20", "to": "2026-11-17",
     "generated_at": "2026-11-17 21:40", "days": 29, "prompts": 201, "tokens": 3100000000},
]


def mock_script(view: str) -> str:
    """注入到真实页面最前面的 mock：覆盖 fetch，让所有 /api/* 返回虚构数据。"""
    projects = build_projects()
    payload = {
        "projects": projects,
        "overview": build_overview(projects),
        "ranking": build_ranking(projects),
        "reports": {"items": _REPORTS, "total": len(_REPORTS)},
        "archive": _REPORTS,
        "detail": None,
    }
    # detail 用第一个项目拼一份，避免点开报错
    p0 = projects[0]
    payload["detail"] = {
        **p0,
        "summary": "虚构项目：批量重命名工具，按规则整理素材文件名（演示数据）。",
        "tech": "C# / WPF / SQLite",
        "language_full": "C#",
        "renamed_from": "",
        "git_repo": True,
        "readme": "",
        "conversations": [
            {"title": "需求梳理", "time": "2026-11-18 09:30", "link": ""},
            {"title": "批量重命名规则实现", "time": "2026-11-17 14:20", "link": ""},
        ],
        "source_files": ["demo/MainWindow.xaml.cs", "demo/RenameService.cs"],
    }
    data_js = json.dumps(payload, ensure_ascii=False)
    return f"""<script>
/* ===== demo 注入：虚构数据层（由 build_manage_demo.py 生成，非真实归档） ===== */
window.__DEMO__ = {data_js};
window.__DEMO_VIEW__ = "{view}";
(function() {{
  const D = window.__DEMO__;
  const realFetch = window.fetch.bind(window);
  window.fetch = function(url, opts) {{
    const u = String(url);
    let data = null;
    if (u.indexOf('/api/session') === 0) data = {{ ok: true, csrf: 'demo' }};
    else if (u.indexOf('/api/projects') === 0) data = {{ ok: true, data: D.projects }};
    else if (u.indexOf('/api/overview') === 0) data = {{ ok: true, data: D.overview }};
    else if (u.indexOf('/api/ranking') === 0) data = {{ ok: true, data: D.ranking }};
    else if (u.indexOf('/api/reports') === 0) data = {{ ok: true, data: D.reports }};
    else if (u.indexOf('/api/report-archive') === 0) data = {{ ok: true, data: D.archive }};
    else if (u.indexOf('/api/detail') === 0) data = {{ ok: true, data: D.detail }};
    if (data) return Promise.resolve({{ status: 200, json: () => Promise.resolve(data) }});
    return realFetch(url, opts);
  }};
  // 启动后切到指定视图（截图用）
  window.addEventListener('load', () => setTimeout(() => {{
    const b = document.querySelector('#nav button[data-view="' + window.__DEMO_VIEW__ + '"]');
    if (b) b.click();
    // 项目页切到「全部」标签，展示含外链/已取消关注的完整列表
    if (window.__DEMO_VIEW__ === 'projects') {{
      const all = document.getElementById('tabAll');
      if (all) all.click();
    }}
  }}, 30));
}})();
</script>
"""


def build(view: str = "projects") -> str:
    html = (SCRIPTS / "manage_web.html").read_text(encoding="utf-8")
    inject = mock_script(view)
    # 注入到 <head> 之后、原页面脚本之前，确保 fetch 覆盖先生效
    marker = "<style>"
    idx = html.find(marker)
    if idx < 0:
        raise RuntimeError("manage_web.html 结构变化：找不到 <style> 注入点")
    return html[:idx] + inject + html[idx:]


def main() -> int:
    for view, name in (("projects", "manage-demo.html"),
                       ("overview", "manage-demo-overview.html")):
        path = HERE / name
        path.write_text(build(view), encoding="utf-8", newline="\n")
        print(f"已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
