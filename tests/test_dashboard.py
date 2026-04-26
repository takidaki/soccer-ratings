import errno
import unittest
from unittest.mock import patch

from soccer_ratings.dashboard import DashboardBindError, run_dashboard


class DashboardServerTests(unittest.TestCase):
    @patch("soccer_ratings.dashboard.ThreadingHTTPServer")
    def test_run_dashboard_explains_port_conflict(self, mock_server) -> None:
        mock_server.side_effect = OSError(errno.EADDRINUSE, "Address already in use")

        with self.assertRaises(DashboardBindError) as raised:
            run_dashboard(host="127.0.0.1", port=8001)

        message = str(raised.exception)
        self.assertIn("http://127.0.0.1:8001 is already in use", message)
        self.assertIn("python3 app.py dashboard --port 8002", message)
        self.assertIn("lsof -nP -iTCP:8001 -sTCP:LISTEN", message)


if __name__ == "__main__":
    unittest.main()
