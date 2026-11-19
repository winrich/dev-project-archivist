"""端到端测试：真实 HTTP 服务 + 临时归档目录，验证管理台全部操作。"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as A
import manage_ui


def build_target(tmp: Path):
    st = {"version": 1, "target": str(tmp), "last_run": "",
          "projects": {
              "c:/test/ws1": {
                  "dir": "2026-01-01_旧显示名", "workspace": "C:/test/ws1",
                  "name": "旧显示名", "type": "代码项目", "language": "Python",
                  "first_ts": 1767225600000, "last_ts": 1767225600000,
                  "sessions": 1, "files": 2, "bytes": 100, "fingerprint": "x",
                  "archived": True, "last_archived": "2026-01-02 00:00",
                  "status": "active"}}}
    A.atomic_write(tmp / "state.json", json.dumps(st, ensure_ascii=False))
    A.atomic_write(tmp / "overrides.json", '{"_说明":"test"}')
    adir = tmp / "2026-01-01_旧显示名"
    adir.mkdir()
    A.atomic_write(adir / "_summary.json", '{"title":"LLM名","purpose":"测试用途"}')
    A.atomic_write(adir / A.META_DIR / "conversations.md", "")
    return tmp


def main():
    tmp = Path(tempfile.mkdtemp(prefix="dpa_e2e_"))
    build_target(tmp)
    cfg, pw = manage_ui.load_or_create_auth(tmp)
    manage_ui.Handler.target = tmp
    manage_ui.Handler.cfg = cfg
    manage_ui.Handler.sessions = manage_ui.Sessions()

    srv = manage_ui.Server(("127.0.0.1", 0), manage_ui.Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    print(f"测试服务 {base}  口令={pw}")

    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(path):
        try:
            with opener.open(base + path, timeout=5) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(path, body, csrf=None):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"X-CSRF-Token": csrf} if csrf else {})})
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    results = []
    def check(name, ok, detail=""):
        results.append((name, ok))
        print(("  OK  " if ok else " FAIL ") + name + (" | " + str(detail) if detail else ""))

    # 1. 未登录访问被拒
    code, d = get("/api/projects")
    check("未登录 401", code == 401, code)

    # 2. 错误口令
    code, d = post("/api/login", {"password": "bad"})
    check("错误口令 401", code == 401, d.get("error"))

    # 3. 登录
    code, d = post("/api/login", {"password": pw})
    csrf = d.get("csrf", "")
    check("登录成功并拿到 csrf", code == 200 and bool(csrf), code)

    # 4. 无 CSRF 的写操作被拒
    code, d = post("/api/pin", {"key": "c:/test/ws1", "pinned": True})
    check("CSRF 缺失 403", code == 403, code)

    # 5. 列表
    code, body = get("/api/projects")
    d = json.loads(body)
    rows = d.get("data", [])
    check("列表含 1 项目", code == 200 and len(rows) == 1, len(rows))

    key = "c:/test/ws1"
    # 6. 改名只改显示名
    code, d = post("/api/rename", {"key": key, "title": "新显示名"}, csrf)
    after = d.get("data", {})
    check("改名后显示名变化", after.get("name") == "新显示名", after.get("name"))
    check("改名后目录不变", after.get("dir") == "2026-01-01_旧显示名", after.get("dir"))

    # 7. 目录改名独立入口
    code, d = post("/api/rename-dir", {"key": key,
                                       "dirname": "2026-01-01_新目录名"}, csrf)
    after = d.get("data", {})
    check("目录改名生效", after.get("dir") == "2026-01-01_新目录名", after.get("dir"))
    check("目录改名不改显示名", after.get("name") == "新显示名", after.get("name"))
    check("旧目录已不存在", not (tmp / "2026-01-01_旧显示名").exists())

    # 8. 置顶
    code, d = post("/api/pin", {"key": key, "pinned": True}, csrf)
    check("置顶生效", d.get("data", {}).get("pinned") is True)

    # 9. 取消关注
    code, d = post("/api/watch", {"key": key, "watching": False}, csrf)
    check("取消关注生效", d.get("data", {}).get("watching") is False)

    # 10. 备份存在
    check("改名备份已生成", (tmp / "_trash" / "_manage_backups").exists())

    # 11. 删除（软删除）
    code, d = post("/api/delete", {"key": key, "purge": False}, csrf)
    check("删除返回成功", code == 200 and d.get("data", {}).get("deleted"), d)
    trash = list((tmp / "_trash").glob("2026-01-01_新目录名__*"))
    check("目录已移入 _trash", len(trash) == 1, [str(x.name) for x in trash])

    # 12. HTML 页可访问
    code, html = get("/")
    check("管理页面可访问", code == 200 and "manage_web" not in html[:200]
          and "对话项目归档" in html, len(html))

    srv.shutdown()
    srv.server_close()
    print("\n通过 %d / %d" % (sum(1 for _, ok in results if ok), len(results)))
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
