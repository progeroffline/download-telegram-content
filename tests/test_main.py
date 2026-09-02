import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import PeerChannel

from main import (
    is_video_message,
    original_video_name,
    parse_channel,
    save_video_text,
    video_destination,
)


class ParseChannelTests(unittest.TestCase):
    def test_accepts_username_variants(self):
        self.assertEqual(parse_channel("channel_name"), "channel_name")
        self.assertEqual(parse_channel("@channel_name"), "channel_name")
        self.assertEqual(
            parse_channel("https://t.me/channel_name"), "channel_name"
        )

    def test_accepts_private_channel_link(self):
        channel = parse_channel("https://t.me/c/1234567890")
        self.assertIsInstance(channel, PeerChannel)
        self.assertEqual(channel.channel_id, 1234567890)

    def test_rejects_invalid_channel(self):
        with self.assertRaises(ValueError):
            parse_channel("not a channel!")


class VideoHelpersTests(unittest.TestCase):
    def test_detects_video_by_mime_type(self):
        video = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4"))
        image = SimpleNamespace(document=SimpleNamespace(mime_type="image/jpeg"))
        self.assertTrue(is_video_message(video))
        self.assertFalse(is_video_message(image))

    def test_uses_original_basename(self):
        message = SimpleNamespace(
            id=42,
            file=SimpleNamespace(name="../folder/original.mp4", ext=".mp4"),
        )
        self.assertEqual(original_video_name(message), "original.mp4")

    def test_builds_date_hierarchy(self):
        message = SimpleNamespace(
            id=42,
            date=datetime(2026, 9, 2, 13, 0, tzinfo=timezone.utc),
            file=SimpleNamespace(name="original.mp4", ext=".mp4"),
        )
        with patch("main.DOWNLOADS_DIR", new=Path("downloads")):
            self.assertEqual(
                video_destination(message),
                Path("downloads/2026/09/02/1600/original.mp4"),
            )

    def test_saves_message_text_next_to_video(self):
        message = SimpleNamespace(message="Описание видео — текст")
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "original.mp4"

            text_path = save_video_text(message, video_path)

            self.assertEqual(text_path, Path(directory) / "original.txt")
            self.assertEqual(
                text_path.read_text(encoding="utf-8"),
                "Описание видео — текст",
            )

    def test_saves_empty_file_when_message_has_no_text(self):
        message = SimpleNamespace(message=None)
        with TemporaryDirectory() as directory:
            video_path = Path(directory) / "original.mp4"

            text_path = save_video_text(message, video_path)

            self.assertEqual(text_path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
