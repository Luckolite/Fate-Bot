import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from cogs.fun.fun import (
    Fun,
    SLIME_VIDEO_FRAMES,
    SLIME_VIDEO_SIZE,
    _render_slime_animation,
)


class SlimeCommandTests(unittest.TestCase):
    @staticmethod
    def avatar_bytes() -> bytes:
        avatar = Image.new("RGB", (256, 256), (69, 116, 210))
        draw = ImageDraw.Draw(avatar)
        draw.ellipse((48, 42, 208, 202), fill=(245, 193, 110))
        draw.ellipse((91, 98, 107, 114), fill=(30, 32, 38))
        draw.ellipse((149, 98, 165, 114), fill=(30, 32, 38))
        draw.arc((92, 112, 168, 164), 10, 170, fill=(80, 37, 35), width=7)
        output = io.BytesIO()
        avatar.save(output, format="PNG")
        return output.getvalue()

    def test_slime_command_is_registered(self):
        self.assertEqual(Fun.slime.name, "slime")
        self.assertIn("cartoon car", Fun.slime.description)

    def test_slime_renderer_creates_complete_animation(self):
        with tempfile.TemporaryDirectory(prefix="fate-slime-check-") as directory:
            count = _render_slime_animation(self.avatar_bytes(), directory)
            paths = sorted(Path(directory).glob("[0-9][0-9][0-9][0-9].png"))
            self.assertEqual(count, SLIME_VIDEO_FRAMES)
            self.assertEqual(len(paths), SLIME_VIDEO_FRAMES)

            with Image.open(paths[0]) as first, Image.open(paths[22]) as impact:
                self.assertEqual(first.size, SLIME_VIDEO_SIZE)
                self.assertEqual(impact.size, SLIME_VIDEO_SIZE)
                self.assertNotEqual(first.tobytes(), impact.tobytes())

            with Image.open(Path(directory) / "slime.gif") as animation:
                self.assertEqual(animation.size, SLIME_VIDEO_SIZE)
                self.assertEqual(animation.n_frames, SLIME_VIDEO_FRAMES)


if __name__ == "__main__":
    unittest.main()
