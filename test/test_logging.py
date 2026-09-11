import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from wdr_tiles.logging import log_session, safe_command
from wdr_tiles.settings import Settings
from wdr_tiles.state import run_command


class LoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.settings = Settings.load(Path(self.temp.name))
        self.events_file = self.settings.output_dir / "logs/pipeline.jsonl"

    def events(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in self.events_file.read_text().splitlines()]

    def test_native_output_and_append_only_run_ids(self) -> None:
        with redirect_stderr(io.StringIO()):
            for _ in range(2):
                with log_session(self.settings, "test"):
                    run_command(
                        self.settings,
                        "small",
                        [
                            sys.executable,
                            "-c",
                            "import sys; print('stdout'); print('stderr', file=sys.stderr)",
                        ],
                    )
        events = self.events()
        self.assertEqual(len({event["run_id"] for event in events}), 2)
        self.assertTrue(all("timestamp" in event for event in events))
        self.assertEqual(
            sum(event["event"] == "native.completed" for event in events), 2
        )
        raw = (self.settings.output_dir / "logs/small.log").read_text()
        self.assertEqual(raw.count("stdout"), 2)
        self.assertEqual(raw.count("stderr"), 2)

    def test_failure_keeps_exit_code_native_output_and_traceback(self) -> None:
        with (
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(ValueError, "exit 7"),
        ):
            with log_session(self.settings, "test-failure"):
                run_command(
                    self.settings,
                    "failure",
                    [
                        sys.executable,
                        "-c",
                        "print('failure detail'); raise SystemExit(7)",
                    ],
                )
        events = self.events()
        failed = next(event for event in events if event["event"] == "native.failed")
        self.assertEqual(failed["returncode"], 7)
        self.assertIn("failure detail", Path(str(failed["native_log"])).read_text())
        self.assertEqual(events[-1]["event"], "pipeline.failed")
        self.assertIn("Traceback", str(events[-1]["exception"]))

    def test_slow_process_emits_status(self) -> None:
        child = MagicMock()
        child.pid = 123
        child.wait.side_effect = [subprocess.TimeoutExpired("test", 60), 0]
        with (
            redirect_stderr(io.StringIO()),
            patch("wdr_tiles.state.subprocess.Popen") as popen,
        ):
            popen.return_value.__enter__.return_value = child
            with log_session(self.settings, "heartbeat"):
                run_command(self.settings, "slow", ["test"])
        running = next(
            event for event in self.events() if event["event"] == "native.running"
        )
        self.assertEqual(running["pid"], 123)
        self.assertIn("elapsed_seconds", running)

    def test_interrupt_stops_child_and_records_interruption(self) -> None:
        child = MagicMock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        with (
            redirect_stderr(io.StringIO()),
            patch("wdr_tiles.state.subprocess.Popen") as popen,
        ):
            popen.return_value.__enter__.return_value = child
            with self.assertRaises(KeyboardInterrupt):
                with log_session(self.settings, "interrupt"):
                    run_command(self.settings, "slow", ["test"])
        child.terminate.assert_called_once()
        self.assertEqual(self.events()[-1]["event"], "pipeline.interrupted")

    def test_logged_urls_omit_credentials_and_query(self) -> None:
        self.assertEqual(
            safe_command(["curl", "https://user:secret@example.com/file?token=secret"]),
            ["curl", "https://example.com/file?redacted"],
        )


if __name__ == "__main__":
    unittest.main()
