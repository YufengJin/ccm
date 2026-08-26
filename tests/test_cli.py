import io
import unittest
from contextlib import redirect_stdout, redirect_stderr

from ccm.cli import main, _dispatch_guard
from ccm.errors import CcmError


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = None
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = main(list(argv))
            except SystemExit as e:  # argparse --version 走 SystemExit
                rc = e.code or 0
        return rc, out.getvalue(), err.getvalue()

    def test_version(self):
        rc, out, _ = self.run_cli("--version")
        self.assertEqual(rc, 0)
        self.assertIn("0.1.0", out)

    def test_no_args_prints_help(self):
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("usage", out)

    def test_ccm_error_becomes_rc1(self):
        def boom():
            raise CcmError("坏了")
        err = io.StringIO()
        with redirect_stderr(err):
            rc, msg = _dispatch_guard(boom)
        self.assertEqual(rc, 1)
        self.assertIn("坏了", err.getvalue())


if __name__ == "__main__":
    unittest.main()
