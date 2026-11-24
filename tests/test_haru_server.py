"""Haru native startup keeps language and model precision explicit."""
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import discord_server


class ServerArgsTests(unittest.TestCase):
    def test_configuration_import_does_not_load_mlx_or_change_umask(self):
        # Configuration tools and test runners may import this module without
        # Metal, and should not inherit the socket server's restrictive umask.
        code = """
import os, sys
sys.path.insert(0, sys.argv[1])
os.umask(0o022)
sys.modules['haru_mlx'] = None
import discord_server
discord_server.model_path = lambda: '/model'
assert discord_server.server_args(['/tmp/voice.sock']) == [
    '--model', '/model', '--quantize', '8', '--language', 'ko', '--socket', '/tmp/voice.sock']
assert os.umask(0o077) == 0o022
"""
        subprocess.run([sys.executable, "-B", "-c", code, str(Path(discord_server.__file__).parent)], check=True)

    def test_precision_and_language_reach_upstream(self):
        with patch.object(discord_server, "model_path", return_value=Path("/model")):
            self.assertEqual(discord_server.server_args(["/tmp/voice.sock", "--language", "ko", "--quantize", "8"]),
                             ["--model", "/model", "--quantize", "8", "--language", "ko", "--socket", "/tmp/voice.sock"])
            self.assertEqual(discord_server.server_args(["/tmp/voice.sock", "--language", "ja", "--quantize", "none"]),
                             ["--model", "/model", "--quantize", "none", "--language", "ja", "--socket", "/tmp/voice.sock"])

    def test_invalid_precision_rejected(self):
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                discord_server.server_args(["/tmp/voice.sock", "--quantize", "4"])


if __name__ == "__main__":
    unittest.main()
