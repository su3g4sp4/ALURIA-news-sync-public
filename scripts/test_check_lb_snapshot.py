# python3 -m unittest scripts/test_check_lb_snapshot.py
import importlib.util, os, unittest
spec = importlib.util.spec_from_file_location("chk", os.path.join(os.path.dirname(__file__), "check-lb-snapshot.py"))
chk = importlib.util.module_from_spec(spec); spec.loader.exec_module(chk)

def snap():
    meta = {"version": "r1", "syncedAt": "T1", "models": ["a", "b"]}
    boards = {k: {"version": "r1", "syncedAt": "T1"} for k in chk.BOARDS}
    models = {"a": {"runId": "r1"}, "b": {"runId": "r1"}}
    return meta, boards, models

class T(unittest.TestCase):
    def test_ok(self): self.assertEqual(chk.check(*snap()), [])
    def test_stale_board(self):
        m, b, d = snap(); b["coding"]["syncedAt"] = "T0"; self.assertTrue(chk.check(m, b, d))
    def test_old_version_board(self):
        m, b, d = snap(); b["knowledge"]["version"] = "r0"; self.assertTrue(chk.check(m, b, d))
    def test_stale_detail(self):
        m, b, d = snap(); d["b"]["runId"] = "r0"; self.assertTrue(chk.check(m, b, d))
    def test_detail_time(self):
        m, b, d = snap(); d["a"]["syncedAt"] = "T0"; self.assertTrue(chk.check(m, b, d))
    def test_missing(self):
        m, b, d = snap(); del d["a"]; del b["overall"]; self.assertEqual(len(chk.check(m, b, d)), 2)

if __name__ == "__main__": unittest.main()
