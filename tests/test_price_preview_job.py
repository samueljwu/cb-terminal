import threading
import unittest
from unittest.mock import Mock, patch

from cb_terminal.web.server import (
    CbTerminalRequestHandler,
    pricing_preview_job_status,
    start_pricing_preview_job,
)


class PricePreviewJobTests(unittest.TestCase):
    def test_closed_browser_socket_does_not_trigger_another_response(self):
        handler = object.__new__(CbTerminalRequestHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = Mock()
        handler.wfile.write.side_effect = ConnectionAbortedError(10053, "connection aborted")

        handler._send_json({"status": "complete"})

        handler.send_response.assert_called_once_with(200)
        handler.wfile.write.assert_called_once()

    def test_pricing_job_can_be_polled_while_calculation_runs(self):
        pricing_started = threading.Event()
        release_pricing = threading.Event()

        def slow_preview(payload):
            pricing_started.set()
            if not release_pricing.wait(5):
                raise TimeoutError("test pricing worker did not resume")
            return {"summary": {"row_count": 13}, "series": []}

        try:
            with patch("cb_terminal.web.server.preview_pricing_payload", side_effect=slow_preview):
                started = start_pricing_preview_job({"contract_path": "test-bond"})
                self.assertTrue(pricing_started.wait(2))
                duplicate = start_pricing_preview_job({"contract_path": "test-bond"})
                self.assertEqual(duplicate["job_id"], started["job_id"])
                pending = pricing_preview_job_status(started["job_id"])
                self.assertEqual(pending["status"], "pending")

                release_pricing.set()
                for _ in range(20):
                    completed = pricing_preview_job_status(started["job_id"])
                    if completed["status"] == "complete":
                        break
                    threading.Event().wait(0.05)
                self.assertEqual(completed["result"]["summary"]["row_count"], 13)
        finally:
            release_pricing.set()


if __name__ == "__main__":
    unittest.main()
