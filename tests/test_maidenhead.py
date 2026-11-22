"""Unit tests for src/maidenhead.py. Run with:
  PYTHONPATH=src python3 -m unittest tests/test_maidenhead.py -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from maidenhead import grid_square  # noqa: E402


class TestGridSquare(unittest.TestCase):
    def test_known_locations(self):
        # Reference values cross-checked against the standard algorithm
        # (e.g. W1AW Newington CT is FN31pr).
        self.assertEqual(grid_square(41.714775, -72.727260), "FN31pr")
        self.assertEqual(grid_square(47.612345, -122.334567), "CN87to")
        self.assertEqual(grid_square(-33.8688, 151.2093), "QF56od")
        self.assertEqual(grid_square(0.0, 0.0), "JJ00aa")

    def test_extremes_do_not_overflow(self):
        self.assertEqual(grid_square(90.0, 180.0), "RR99xx")
        self.assertEqual(grid_square(-90.0, -180.0), "AA00aa")


if __name__ == "__main__":
    unittest.main()
