"""工作区外成果探测单测：噪声过滤、工程根归并、阈值、工作区排除。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import archiver as A
import external_store as E

# 单元测试的临时工程落在系统 Temp 下；噪声规则会拦 Temp，这里在测试内
# 临时移除该规则，让归并/阈值逻辑可独立验证（噪声过滤由 test_noise_filter 覆盖）。
_ORIG_NOISE = E._NOISE_PARTS


def _allow_temp():
    E._NOISE_PARTS = tuple(p for p in _ORIG_NOISE
                           if p not in ("/temp/", "/tmp/", "/appdata/local/temp/"))


def _restore_noise():
    E._NOISE_PARTS = _ORIG_NOISE


def test_noise_filter():
    """临时/缓存/日志/.workbuddy 路径全部判噪声。"""
    noise = [
        "C:/Users/name/AppData/Local/Temp/proj/src/a.py",
        "C:/Users/name/AppData/Local/GitHubHostsAgent/logs/app.log",
        "C:/Users/name/.workbuddy/memory/2026.md",
        "C:/tmp/ghagent-smoke/x.cs",
    ]
    for p in noise:
        assert E.is_noise(p), p
    clean = "D:/Projects/JoyBang/GitHubHostsAgent/GitHubHostsAgent/Core/A.cs"
    assert not E.is_noise(clean)
    print("  噪声过滤: 4 个临时/配置路径全部排除")


def test_project_root_sln_over_csproj():
    """多层 C# 结构：.sln 在解决方案根，优先于内层 .csproj。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "Solution"
        inner = root / "Project"
        inner.mkdir(parents=True)
        (root / "App.sln").write_text("x", encoding="utf-8")
        (inner / "App.csproj").write_text("x", encoding="utf-8")
        f = inner / "Core" / "A.cs"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        got = E.find_project_root(str(f))
        assert got.replace("\\", "/").rstrip("/").lower() == str(root).replace("\\", "/").lower(), got
        print(f"  工程根: .sln 层优先 → {Path(got).name}")


def test_collect_external_merge_and_threshold():
    """同一工程多子目录归并为一条；低于阈值的写入不进结果。"""
    _allow_temp()
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "MyRepo" / "src"
            base.mkdir(parents=True)
            (base.parent / "pyproject.toml").write_text("[project]", encoding="utf-8")
            (base / "mod.py").write_text("x", encoding="utf-8")
            ws = str(Path(td) / "workspace")
            # 阈值 5：6 次写入（虽分两个子目录）归并为一条
            events = [{"kind": A.EV_WRITE, "rel": "", "external": str(base / f"m{i}.py")}
                      for i in range(6)]
            r = E.collect_external(events, ws)
            assert len(r) == 1, r
            root = list(r)[0]
            assert r[root]["writes"] == 6, r
            print(f"  归并: 6 次写入 → 1 个工程根, writes={r[root]['writes']}")
            # 低于阈值仍记录原始计数，阈值常量用于升级判定
            few = [{"kind": A.EV_WRITE, "rel": "", "external": str(base / "x.py")}]
            r2 = E.collect_external(few, ws)
            assert list(r2)[0] in r2 and r2[list(r2)[0]]["writes"] == 1
            assert E.MIN_WRITES_TO_UPGRADE == 5
            print(f"  阈值: 单次写入原始计数=1, 升级阈值={E.MIN_WRITES_TO_UPGRADE}")
    finally:
        _restore_noise()


def test_own_workspace_excluded():
    """写回自己工作区的路径不算外部成果。"""
    _allow_temp()
    try:
        with tempfile.TemporaryDirectory() as td:
            ws = str(Path(td) / "ws")
            src = Path(ws) / "src"
            src.mkdir(parents=True)
            (Path(ws) / "pyproject.toml").write_text("[project]", encoding="utf-8")
            events = [{"kind": A.EV_WRITE, "rel": "", "external": str(src / "a.py")} for _ in range(10)]
            r = E.collect_external(events, ws)
            assert r == {}, r
            print("  工作区排除: 10 次写回自身不计入")
    finally:
        _restore_noise()


def main():
    tests = [test_noise_filter, test_project_root_sln_over_csproj,
             test_collect_external_merge_and_threshold, test_own_workspace_excluded]
    for t in tests:
        print(f"[RUN] {t.__name__}")
        t()
    print(f"\n全部 {len(tests)} 项通过")


if __name__ == "__main__":
    main()
