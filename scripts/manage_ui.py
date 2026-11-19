"""本地管理台 HTTP 服务：仅绑 127.0.0.1，零第三方依赖。

安全设计：
- 口令首次生成随机值只打印一次；也可用 ARCHIVER_MANAGE_PASSWORD 指定。
- 存储 salt + PBKDF2-SHA256 哈希，明文不落盘。
- 会话 Cookie 为 HttpOnly + SameSite=Strict；CSRF token 只经登录响应体返回，
  攻击者跨站读不到，因此双重提交校验有效。
- 连续 5 次登录失败锁定 60 秒；只监听回环地址，不提供远程访问。
"""
import argparse
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import archiver as A
from manage_store import ManageError, ManageStore, norm_key

AUTH_FILE = "manage.auth.json"
COOKIE_NAME = "dpa_sid"
PBKDF2_ROUNDS = 200_000
MAX_FAIL = 5
LOCK_SECONDS = 60
SESSION_TTL = 8 * 3600
BIND_HOST = "127.0.0.1"

HTML_FILE = Path(__file__).with_name("manage_web.html")

# 归档报告文件名白名单：report-YYYY-MM-DD_YYYY-MM-DD.html
_REPORT_NAME_RE = re.compile(r"^report-\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}\.html$")


def report_name_ok(name: str) -> bool:
    """严格校验归档报告文件名，杜绝 ../ 等路径穿越。"""
    return bool(name) and bool(_REPORT_NAME_RE.fullmatch(name))


def auth_path(target: Path) -> Path:
    return target / AUTH_FILE


def load_or_create_auth(target: Path) -> dict:
    """读取或首次创建口令哈希。返回 (配置, 明文口令或None)。"""
    p = auth_path(target)
    env_pw = os.environ.get("ARCHIVER_MANAGE_PASSWORD") or ""
    if p.exists():
        try:
            cfg = json.loads(A.read_text(p) or "{}")
            if cfg.get("salt") and cfg.get("hash"):
                # 环境变量改口令：仅在启动时允许覆盖，重写哈希
                if env_pw:
                    salt = secrets.token_hex(16)
                    cfg = {"salt": salt,
                           "hash": hashlib.pbkdf2_hmac(
                               "sha256", env_pw.encode(), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex(),
                           "rounds": PBKDF2_ROUNDS}
                    A.atomic_write(p, json.dumps(cfg, indent=2) + "\n")
                    return cfg, env_pw
                return cfg, None
        except Exception:
            pass
    salt = secrets.token_hex(16)
    pw = env_pw or secrets.token_urlsafe(12)
    cfg = {"salt": salt,
           "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt),
                                       PBKDF2_ROUNDS).hex(),
           "rounds": PBKDF2_ROUNDS}
    A.atomic_write(p, json.dumps(cfg, indent=2) + "\n")
    try:
        os.chmod(str(p), 0o600)
    except Exception:
        pass
    return cfg, pw


def verify_password(cfg: dict, password: str) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(cfg["salt"]),
                                     int(cfg.get("rounds", PBKDF2_ROUNDS))).hex()
        return hmac.compare_digest(digest, cfg["hash"])
    except Exception:
        return False


def change_password(new_pw: str) -> dict:
    """用新口令重写 salt+hash（明文不落盘），并更新进程内 Handler.cfg。

    调用方须先通过 authenticated + check_csrf，并校验旧口令。
    """
    salt = secrets.token_hex(16)
    cfg = {"salt": salt,
           "hash": hashlib.pbkdf2_hmac("sha256", new_pw.encode(), bytes.fromhex(salt),
                                       PBKDF2_ROUNDS).hex(),
           "rounds": PBKDF2_ROUNDS}
    A.atomic_write(auth_path(Handler.target), json.dumps(cfg, indent=2) + "\n")
    try:
        os.chmod(str(auth_path(Handler.target)), 0o600)
    except Exception:
        pass
    Handler.cfg = cfg
    return {"changed": True}


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}   # sid -> {"csrf": str, "until": float}
        self.failed = {}  # ip -> (count, until)

    def create(self) -> tuple:
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            self.data[sid] = {"csrf": csrf, "until": time.time() + SESSION_TTL}
        return sid, csrf

    def valid(self, sid: str) -> bool:
        if not sid:
            return False
        with self.lock:
            s = self.data.get(sid)
            if not s:
                return False
            if s["until"] < time.time():
                self.data.pop(sid, None)
                return False
            return True

    def csrf(self, sid: str) -> str:
        with self.lock:
            return (self.data.get(sid) or {}).get("csrf", "")

    def destroy(self, sid: str):
        with self.lock:
            self.data.pop(sid, None)

    def locked(self, ip: str) -> int:
        """返回还需等待的秒数，0 表示未锁定"""
        with self.lock:
            cnt, until = self.failed.get(ip, (0, 0))
            if until > time.time():
                return int(until - time.time()) + 1
            if cnt:
                self.failed.pop(ip, None)
            return 0

    def fail(self, ip: str):
        with self.lock:
            cnt, until = self.failed.get(ip, (0, 0))
            cnt += 1
            self.failed[ip] = (cnt, time.time() + LOCK_SECONDS if cnt >= MAX_FAIL else 0)

    def clear_fail(self, ip: str):
        with self.lock:
            self.failed.pop(ip, None)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DPAManage/1.0"
    target: Path = None
    cfg: dict = None
    sessions: Sessions = None

    # ------------------------------------------------------------ 基础设施
    def log_message(self, fmt, *args):
        # 本地工具：只在终端打印简短访问行，不写文件
        print("  %s - %s" % (self.client_address[0], fmt % args), flush=True)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_out(self, obj, code: int = 200, extra: dict = None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def err(self, msg: str, code: int = 400):
        self.json_out({"ok": False, "error": msg}, code)

    def cookie(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                return v
        return ""

    def authenticated(self) -> bool:
        return self.sessions.valid(self.cookie())

    def check_csrf(self) -> bool:
        sid = self.cookie()
        tok = self.headers.get("X-CSRF-Token") or ""
        return bool(sid) and hmac.compare_digest(tok, self.sessions.csrf(sid))

    def body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1024 * 1024:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def store(self) -> ManageStore:
        return ManageStore(self.target)

    def write_guard(self, fn):
        """所有写操作：进程内串行 + 与定时任务共享文件锁；异常统一转 JSON"""
        if not self.authenticated():
            return self.err("登录已过期，请重新登录", 401)
        if not self.check_csrf():
            return self.err("CSRF 校验失败，请刷新页面", 403)
        with LOCK:
            with A.Lock(self.target):
                try:
                    result = fn(self.store())
                    self.json_out({"ok": True, "data": result})
                except ManageError as e:
                    self.err(str(e), 400)
                except Exception as e:
                    self.err(f"操作失败：{e}", 500)

    # ------------------------------------------------------------ 路由
    def serve_report_html(self, filename: str):
        """认证后 serve 归档 HTML。文件名严格白名单，杜绝路径穿越。"""
        if not report_name_ok(filename):
            return self.err("非法报告文件名", 400)
        p = self.target / "report" / filename
        if not p.is_file():
            return self.err("报告不存在", 404)
        try:
            data = p.read_bytes()
        except Exception as e:
            return self.err(str(e), 500)
        self._send(200, data, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.serve_html()
            return
        if path.startswith("/report/"):
            if not self.authenticated():
                return self.err("未登录", 401)
            name = urlparse(self.path).path[len("/report/"):]
            return self.serve_report_html(name)
        if path == "/api/session":
            if self.authenticated():
                self.json_out({"ok": True, "csrf": self.sessions.csrf(self.cookie())})
            else:
                self.json_out({"ok": False}, 401)
            return
        if path == "/api/projects":
            if not self.authenticated():
                return self.err("未登录", 401)
            try:
                self.json_out({"ok": True, "data": self.store().projects()})
            except Exception as e:
                self.err(str(e), 500)
            return
        if path == "/api/detail":
            if not self.authenticated():
                return self.err("未登录", 401)
            key = (parse_qs(urlparse(self.path).query).get("key") or [""])[0]
            try:
                self.json_out({"ok": True, "data": self.store().detail(key)})
            except ManageError as e:
                self.err(str(e), 404)
            except Exception as e:
                self.err(str(e), 500)
            return
        if path in ("/api/overview", "/api/ranking"):
            if not self.authenticated():
                return self.err("未登录", 401)
            try:
                s = self.store()
                data = s.overview() if path.endswith("overview") else s.ranking()
                self.json_out({"ok": True, "data": data})
            except Exception as e:
                self.err(str(e), 500)
            return
        if path == "/api/report":
            if not self.authenticated():
                return self.err("未登录", 401)
            q = parse_qs(urlparse(self.path).query)
            try:
                days = max(1, min(int((q.get("days") or ["30"])[0]), 366))
                end = (q.get("end") or [""])[0]
                d = A.report_data(days=days, end=end)
                if not d.get("ok"):
                    return self.err(d.get("msg") or "报告生成失败", 400)
                self.json_out({"ok": True, "data": d})
            except Exception as e:
                self.err(str(e), 500)
            return
        if path in ("/api/reports", "/api/report-archive"):
            if not self.authenticated():
                return self.err("未登录", 401)
            q = parse_qs(urlparse(self.path).query)
            try:
                s = self.store()
                if path == "/api/reports":
                    offset = max(0, int((q.get("offset") or ["0"])[0]))
                    limit = max(0, min(int((q.get("limit") or ["0"])[0]), 500))
                    data = s.report_list(offset=offset, limit=limit)
                else:
                    date_str = (q.get("date") or [""])[0]
                    data = s.report_archive(date_str)
                self.json_out({"ok": True, "data": data})
            except Exception as e:
                self.err(str(e), 500)
            return
        self.err("未找到", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        ip = self.client_address[0]
        if path == "/api/login":
            wait = self.sessions.locked(ip)
            if wait:
                return self.err(f"失败次数过多，请 {wait} 秒后再试", 429)
            b = self.body_json()
            if verify_password(self.cfg, str(b.get("password") or "")):
                self.sessions.clear_fail(ip)
                sid, csrf = self.sessions.create()
                self.json_out({"ok": True, "csrf": csrf}, extra={
                    "Set-Cookie": f"{COOKIE_NAME}={sid}; HttpOnly; SameSite=Strict; "
                                  f"Path=/; Max-Age={SESSION_TTL}"})
            else:
                self.sessions.fail(ip)
                self.err("口令错误", 401)
            return
        if path == "/api/logout":
            self.sessions.destroy(self.cookie())
            self.json_out({"ok": True}, extra={
                "Set-Cookie": f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; "
                              "Path=/; Max-Age=0"})
            return
        if path == "/api/password":
            if not self.authenticated():
                return self.err("登录已过期，请重新登录", 401)
            if not self.check_csrf():
                return self.err("CSRF 校验失败，请刷新页面", 403)
            b = self.body_json()
            old, new = str(b.get("old") or ""), str(b.get("new") or "")
            if not verify_password(self.cfg, old):
                return self.err("原口令不正确", 400)
            if len(new) < 6:
                return self.err("新口令至少 6 个字符", 400)
            with LOCK:
                with A.Lock(self.target):
                    try:
                        self.json_out({"ok": True, "data": change_password(new)})
                    except Exception as e:
                        self.err(f"修改失败：{e}", 500)
            return

        routes = {
            "/api/rename": lambda s: s.rename(_KEY(self), _STR(self, "title")),
            "/api/rename-dir": lambda s: s.rename_dir(_KEY(self), _STR(self, "dirname")),
            "/api/pin": lambda s: s.set_pinned(_KEY(self), _BOOL(self, "pinned")),
            "/api/watch": lambda s: s.set_watching(_KEY(self), _BOOL(self, "watching")),
            "/api/note": lambda s: s.set_fields(_KEY(self),
                                                {"note": _STR(self, "note")}),
            "/api/delete": lambda s: s.delete(_KEY(self), _BOOL(self, "purge", False)),
        }
        if path in routes:
            self._body = self.body_json()
            return self.write_guard(routes[path])
        self.err("未找到", 404)

    def serve_html(self):
        try:
            body = HTML_FILE.read_bytes()
        except Exception:
            body = "<h1>管理页面缺失：manage_web.html</h1>".encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")


LOCK = threading.Lock()


def _KEY(h): return h._body.get("key") or ""
def _STR(h, k): return str(h._body.get(k) or "")
def _BOOL(h, k, default=False):
    v = h._body.get(k, default)
    return v if isinstance(v, bool) else bool(v)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    # 后台/管道启动时 stdout 默认块缓冲，地址和首次口令必须立刻可见
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="对话项目归档器 · 本地管理台")
    ap.add_argument("--target", default=str(A.DEFAULT_TARGET), help="归档输出目录")
    ap.add_argument("--port", type=int, default=8765,
                    help="监听端口，默认 8765；传 0 表示自动选空闲端口")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    a = ap.parse_args()

    target = Path(a.target)
    if not (target / A.STATE_FILE).exists():
        print(f"[error] 归档目录没有 state.json：{target}")
        return 2
    cfg, new_pw = load_or_create_auth(target)
    Handler.target = target
    Handler.cfg = cfg
    Handler.sessions = Sessions()

    srv = Server((BIND_HOST, a.port), Handler)
    port = srv.server_address[1]
    url = f"http://{BIND_HOST}:{port}/"
    print(f"管理台已启动：{url}")
    print(f"归档目录：{target}")
    if new_pw:
        print("=" * 60)
        print(f"首次生成的登录口令：{new_pw}")
        print("请立即记下；也可用环境变量 ARCHIVER_MANAGE_PASSWORD 指定自己的口令。")
        print("=" * 60)
    print("按 Ctrl+C 停止服务。")
    if not a.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
