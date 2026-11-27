"""manage_store 单元测试：改名分离、置顶/关注、事务回滚、字段校验。"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import archiver as A
from manage_store import ManageError, ManageStore, backup_state, norm_key, restore_backup


KEY = "c:/test/ws1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpa_store_test_"))
        st = {"version": 1, "target": str(self.tmp), "last_run": "", "projects": {
            KEY: {"dir": "2026-01-01_原名", "workspace": "C:/test/ws1",
                  "name": "原名", "type": "代码项目", "language": "Python",
                  "first_ts": 1767225600000, "last_ts": 1767225600000,
                  "sessions": 1, "files": 2, "bytes": 100, "fingerprint": "x",
                  "archived": True, "last_archived": "2026-01-02 00:00",
                  "status": "active"}}}
        A.atomic_write(self.tmp / "state.json", json.dumps(st, ensure_ascii=False))
        A.atomic_write(self.tmp / "overrides.json", '{"_说明":"test"}')
        adir = self.tmp / "2026-01-01_原名"
        adir.mkdir()
        A.atomic_write(adir / "_summary.json", '{"title":"LLM名","purpose":"用途"}')
        (adir / A.META_DIR).mkdir()
        A.atomic_write(adir / A.META_DIR / "conversations.md", "")
        self.s = ManageStore(self.tmp)

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    def one(self):
        return self.s.projects()[0]


class TestNormalize(unittest.TestCase):
    def test_norm_key_backslash_and_case(self):
        self.assertEqual(norm_key("C:\\Users\\X\\WS/"), "c:/users/x/ws")

    def test_norm_key_empty(self):
        self.assertEqual(norm_key(""), "")


class TestRename(Base):
    def test_rename_only_display_name(self):
        self.s.rename(KEY, "新名字")
        self.assertEqual(self.one()["name"], "新名字")
        self.assertEqual(self.one()["dir"], "2026-01-01_原名")

    def test_rename_dir_only_directory(self):
        before = self.one()["name"]
        self.s.rename_dir(KEY, "2026-01-01_新目录")
        self.assertEqual(self.one()["dir"], "2026-01-01_新目录")
        self.assertEqual(self.one()["name"], before)

    def test_rename_empty_rejected(self):
        with self.assertRaises(ManageError):
            self.s.rename(KEY, "   ")

    def test_rename_sanitizes_illegal_chars(self):
        # 显示名用 safe_name 清洗，斜杠变空格而非拒绝（与目录名入口不同）
        self.s.rename(KEY, "a/b")
        self.assertNotIn("/", self.one()["name"])

    def test_rename_dir_illegal_rejected(self):
        for bad in ("", ".", "..", "a/b", "a:b", "a*?"):
            with self.subTest(bad=bad), self.assertRaises(ManageError):
                self.s.rename_dir(KEY, bad)

    def test_rename_dir_conflict_rejected(self):
        (self.tmp / "已存在").mkdir()
        with self.assertRaises(ManageError):
            self.s.rename_dir(KEY, "已存在")
        self.assertEqual(self.one()["dir"], "2026-01-01_原名")

    def test_rename_dir_illegal_rejected(self):
        for bad in ("", ".", "..", "a/b", "a:b", "a*?"):
            with self.subTest(bad=bad), self.assertRaises(ManageError):
                self.s.rename_dir(KEY, bad)

    def test_manual_survives_llm_title(self):
        self.s.rename(KEY, "人工名")
        st = self.s.state.data["projects"][KEY]
        self.assertEqual(st["name_source"], "manual")

    def test_set_fields_title_same_semantics(self):
        self.s.set_fields(KEY, {"title": "接口改名"})
        self.assertEqual(self.one()["dir"], "2026-01-01_原名")
        self.assertEqual(self.one()["name"], "接口改名")


class TestWatchPinDelete(Base):
    def test_pin(self):
        self.s.set_pinned(KEY, True)
        self.assertTrue(self.one()["pinned"])

    def test_unwatch(self):
        self.s.set_watching(KEY, False)
        self.assertFalse(self.one()["watching"])

    def test_watch_default_true(self):
        self.assertTrue(self.one()["watching"])

    def test_delete_soft(self):
        self.s.delete(KEY)
        self.assertFalse(self.one()["archived"])
        self.assertFalse(self.one()["watching"])
        self.assertEqual(len(list((self.tmp / "_trash").glob("2026-01-01_原名__*"))), 1)

    def test_delete_keeps_backup(self):
        self.s.delete(KEY)
        self.assertTrue((self.tmp / "_trash" / "_manage_backups").exists()
                        or (self.tmp / "overrides.json").exists())

    def test_note_saved(self):
        self.s.set_fields(KEY, {"note": "已移交"})
        self.assertEqual(self.one()["note"], "已移交")


class TestValidation(Base):
    def test_unknown_field_rejected(self):
        with self.assertRaises(ManageError):
            self.s.set_fields(KEY, {"bogus": 1})

    def test_bad_type_rejected(self):
        with self.assertRaises(ManageError):
            self.s.set_fields(KEY, {"type": "随便"})

    def test_non_bool_rejected(self):
        with self.assertRaises(ManageError):
            self.s.set_fields(KEY, {"pinned": "yes"})

    def test_nonexistent_key(self):
        with self.assertRaises(ManageError):
            self.s.rename("no/such/key", "x")

    def test_audit_logged(self):
        self.s.rename(KEY, "审计测试")
        text = A.read_text(self.tmp / "CHANGELOG.md")
        self.assertIn("改名", text)
        self.assertIn("审计测试", text)


class TestBackup(Base):
    def test_backup_restore(self):
        backup = backup_state(self.tmp)
        self.assertTrue((backup / "state.json").exists())
        st = json.loads(A.read_text(self.tmp / "state.json"))
        self.s.rename(KEY, "改坏了")
        restore_backup(self.tmp, backup)
        self.assertEqual(json.loads(A.read_text(self.tmp / "state.json")), st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
