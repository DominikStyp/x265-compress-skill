"""Phase 3 (concat) must show a progress bar driven by ffmpeg's `-progress`
stream, and — like every other ffmpeg spawn — must never leak the child on an
error/abort path.
"""
from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from encode_modules import chunking  # noqa: E402

# Two finished chunk pairs in the workdir, so the pre-concat completeness guard
# passes and there's something to concatenate.
_PROGRESS = [
    "out_time_us=5000000\n", "speed=2.0x\n", "progress=continue\n",
    "out_time_us=10000000\n", "speed=2.0x\n", "progress=end\n",
]


def _workdir(tmp: Path) -> Path:
    for i in (0, 1):
        (tmp / f"src_{i:04d}.mkv").write_bytes(b"s")
        (tmp / f"enc_src_{i:04d}.mkv").write_bytes(b"e")
    return tmp


class _FakeProc:
    def __init__(self, lines, rc=0):
        self.stdout = iter(lines)
        self.stderr = None
        self._rc = rc
        self._alive = True
        self.terminated = False

    def wait(self, timeout=None):
        self._alive = False
        return self._rc

    def poll(self):
        return None if self._alive else self._rc

    def terminate(self):
        self.terminated = True

    def kill(self):
        self._alive = False


class ConcatProgressTest(unittest.TestCase):
    def test_runs_with_progress_flag_and_renders_bar(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = _workdir(Path(td))
            dst = Path(td) / "out.mkv"
            # The fake ffmpeg must 'produce' its output: since the atomic-
            # write fix, concat muxes into a .concat-tmp temp (last argv
            # element) which concat_chunks os.replace()s onto dst.
            def popen_writing_output(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"e")
                return _FakeProc(_PROGRESS)

            popen = mock.MagicMock(side_effect=popen_writing_output)
            buf = io.StringIO()
            with mock.patch.object(chunking.subprocess, "Popen", popen), \
                 redirect_stdout(buf):
                chunking.concat_chunks(wd, dst, total_dur=10.0)
            cmd = " ".join(popen.call_args.args[0])
            self.assertIn("-progress", cmd)
            self.assertIn("pipe:1", cmd)
            out = buf.getvalue()
            self.assertIn("[3/4] Concatenating", out)
            self.assertIn("50.0%", out)
            self.assertIn("100.0%", out)
            self.assertIn("#", out)  # the bar fill

    def test_nonzero_exit_aborts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = _workdir(Path(td))
            dst = Path(td) / "out.mkv"
            popen = mock.MagicMock(return_value=_FakeProc(_PROGRESS, rc=1))
            with mock.patch.object(chunking.subprocess, "Popen", popen), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    chunking.concat_chunks(wd, dst, total_dur=10.0)

    def test_child_terminated_on_read_error(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            wd = _workdir(Path(td))
            dst = Path(td) / "out.mkv"

            class _Raising(_FakeProc):
                @property
                def stdout(self):
                    raise RuntimeError("pipe broke mid-read")

                @stdout.setter
                def stdout(self, v):
                    pass

            proc = _Raising(_PROGRESS)
            with mock.patch.object(chunking.subprocess, "Popen",
                                   return_value=proc), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    chunking.concat_chunks(wd, dst, total_dur=10.0)
            self.assertTrue(proc.terminated,
                            "concat leaked the ffmpeg child on a read error")


if __name__ == "__main__":
    unittest.main()
