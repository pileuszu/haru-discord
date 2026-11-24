import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, Mock, patch

from yuna_tts import Yuna, YunaBusy


def pcm_response(*blocks):
    return b"".join(struct.pack("<I", len(block)) + block for block in (*blocks, b""))


class YunaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.yuna = Yuna()
        self.stdout = asyncio.StreamReader()
        self.process = Mock(returncode=None, stdout=self.stdout)
        self.process.stdin.drain = AsyncMock()
        self.process.communicate = AsyncMock()

    async def test_warm_once_and_forward_only_requested_samples(self):
        self.stdout.feed_data(pcm_response(b"\xff\x01") + pcm_response(b"\x02\x03", b"\x04\x05"))
        self.stdout.feed_eof()
        write = AsyncMock()
        with patch("yuna_tts.worker_binary", AsyncMock(return_value="worker")), patch(
            "yuna_tts.asyncio.create_subprocess_exec", AsyncMock(return_value=self.process)
        ) as spawn:
            await self.yuna.render()
            await self.yuna.render()
            await self.yuna.render("12예요.", write)
        self.assertEqual(spawn.await_count, 1)
        self.assertEqual([call.args[0] for call in write.await_args_list], [b"\x02\x03", b"\x04\x05"])
        phrases = [call.args[0] for call in self.process.stdin.write.call_args_list]
        self.assertEqual([phrase[4:].decode() for phrase in phrases], ["준비", "12예요."])
        self.assertEqual([struct.unpack("<I", phrase[:4])[0] for phrase in phrases], [len(phrase)-4 for phrase in phrases])

    async def test_broken_frame_discards_worker_before_next_phrase(self):
        self.yuna.process = self.process
        self.stdout.feed_data(struct.pack("<I", 4) + b"\x01")
        self.stdout.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.yuna.render("첫 구절", AsyncMock())
        self.process.kill.assert_called_once()
        self.assertIsNone(self.yuna.process)
        self.assertEqual(self.yuna.jobs, 0)

    async def test_disconnected_waiter_does_not_enqueue_old_text(self):
        self.yuna.process = self.process
        async with self.yuna.lock:
            connected = True
            waiting = asyncio.create_task(self.yuna.render("이전 답", AsyncMock(), lambda: connected))
            await asyncio.sleep(0)
            self.assertEqual(self.yuna.jobs, 1)
            connected = False
        await waiting
        self.process.stdin.write.assert_not_called()
        self.yuna.jobs = 3
        with self.assertRaises(YunaBusy):
            await self.yuna.render("새 답", AsyncMock())


if __name__ == "__main__":
    unittest.main()
