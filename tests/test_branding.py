"""Ensure the accepted branding ships in the addon as valid, matching PNG files."""
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1] / "smart_docan_bms"

class BrandingTests(unittest.TestCase):
    def test_both_png_assets_exist_and_are_identical(self):
        logos = [(ROOT / name).read_bytes() for name in ("icon.png", "logo.png")]
        self.assertEqual(logos[0], logos[1])
        for image in logos:
            self.assertEqual(image[:8], bytes([137, 80, 78, 71, 13, 10, 26, 10]))
            width, height = struct.unpack(">II", image[16:24])
            self.assertEqual((width, height), (192, 192))
            self.assertGreater(len(image), 1000)

if __name__ == "__main__":
    unittest.main()
