import os
import tempfile
import unittest
from pathlib import Path

from ccm.layout import link_plan, apply_links


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ccm-layout-"))
        self.shared = self.tmp / "shared"
        self.shared.mkdir()
        (self.shared / "settings.json").write_text("{}")
        (self.shared / "plugins").mkdir()
        self.prof = self.tmp / "prof"
        self.prof.mkdir()

    def test_plan_statuses(self):
        os.symlink("/nowhere", self.prof / "settings.json")
        (self.prof / "plugins").mkdir()  # 实体冲突
        st = {a.item: a.status for a in
              link_plan(self.prof, self.shared, ["settings.json", "plugins", "ghost"])}
        self.assertEqual(st, {"settings.json": "wrong", "plugins": "conflict",
                              "ghost": "skip_no_source"})

    def test_apply_fixes_missing_and_wrong(self):
        os.symlink("/nowhere", self.prof / "settings.json")
        out = apply_links(self.prof, self.shared, ["settings.json", "plugins", "ghost"])
        st = {a.item: a.status for a in out}
        self.assertEqual(st, {"settings.json": "ok", "plugins": "ok",
                              "ghost": "skip_no_source"})
        self.assertEqual(os.readlink(self.prof / "settings.json"),
                         str(self.shared / "settings.json"))

    def test_conflict_never_destroys(self):
        (self.prof / "settings.json").write_text('{"mine": true}')
        out = apply_links(self.prof, self.shared, ["settings.json"])
        self.assertEqual(out[0].status, "conflict")
        self.assertEqual((self.prof / "settings.json").read_text(), '{"mine": true}')

    def test_idempotent(self):
        apply_links(self.prof, self.shared, ["settings.json"])
        out = apply_links(self.prof, self.shared, ["settings.json"])
        self.assertEqual(out[0].status, "ok")


if __name__ == "__main__":
    unittest.main()
