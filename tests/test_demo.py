"""Демо запускается отдельно, закрывает запись и не требует внешних сервисов."""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request


class PanelDemo(unittest.TestCase):
    def test_демо_изолировано_и_доступно_по_одноразовому_ключу(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "existing.db"
            sentinel.write_bytes(b"do not modify this database")
            env = {**os.environ, "AGENT_DB": str(sentinel)}
            process = subprocess.Popen(
                [sys.executable, "examples/panel_demo.py", "--port", "0"],
                cwd=Path(__file__).resolve().parents[1], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                ready, _, _ = select.select([process.stdout], [], [], 15)
                self.assertTrue(ready, "демо не вывело адрес за 15 секунд")
                url = process.stdout.readline().strip()
                self.assertTrue(url.startswith("http://127.0.0.1:"), url)
                parsed = urllib.parse.urlparse(url)
                base = f"{parsed.scheme}://{parsed.netloc}"
                key = urllib.parse.parse_qs(parsed.query)["k"][0]
                with urllib.request.urlopen(url, timeout=5) as response:
                    html = response.read().decode()
                self.assertIn("вымышленные данные", html)
                self.assertNotIn('src="https://telegram.org/', html)
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(base + "/api/state", timeout=5)
                denied.exception.close()
                self.assertEqual(denied.exception.code, 403)
                headers = {"X-Panel-Key": key}
                for endpoint in ("state", "transcript", "trades", "equity", "live", "limits"):
                    request = urllib.request.Request(base + "/api/" + endpoint, headers=headers)
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertIsInstance(json.load(response), dict)
                request = urllib.request.Request(
                    base + "/api/say", data=b'{"text":"test"}', headers=headers,
                )
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(request, timeout=5)
                denied.exception.close()
                self.assertEqual(denied.exception.code, 405)
                self.assertEqual(sentinel.read_bytes(), b"do not modify this database")
            finally:
                process.send_signal(signal.SIGINT)
                try:
                    _, errors = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    _, errors = process.communicate()
                self.assertEqual(process.returncode, 0, errors)
