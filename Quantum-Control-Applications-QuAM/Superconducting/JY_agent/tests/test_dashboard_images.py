from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from jy_agent.approval_web import CACHEABLE_ASSET_CACHE_CONTROL, _asset_url
from jy_agent.service import AgentService, _asset_token
from tests.test_core import make_settings, sample_state


class DashboardImageTests(unittest.TestCase):
    """2026-10-04: result images reloaded slowly on every Dashboard refresh."""

    def test_urls_carry_a_version_and_thumbnail_flag(self) -> None:
        item = {"asset_indices": [7, 8], "asset_tokens": ["abc", "def"]}
        self.assertEqual(_asset_url("s", item, 0), "/session/s/assets/7?v=abc")
        self.assertEqual(
            _asset_url("s", item, 1, thumb=True), "/session/s/assets/8?v=def&amp;thumb=1"
        )
        self.assertIn("immutable", CACHEABLE_ASSET_CACHE_CONTROL)

    def test_token_changes_when_the_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plot.png"
            Image.new("RGB", (40, 40), "white").save(path)
            first = _asset_token(path)
            Image.new("RGB", (80, 40), "white").save(path)
            self.assertNotEqual(first, _asset_token(path))

    def test_thumbnail_is_small_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(make_settings(Path(folder), sample_state(0.2, 0.1)))
            source = Path(folder) / "Data" / "big.png"
            Image.new("RGB", (1500, 600), "white").save(source)
            thumb = service.dashboard_asset_thumbnail(source)
            with Image.open(thumb) as image:
                self.assertEqual(image.width, 360)
            self.assertEqual(service.dashboard_asset_thumbnail(source), thumb)

    def test_asset_lookup_reuses_the_last_review_plot_list(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = AgentService(make_settings(Path(folder), sample_state(0.2, 0.1)))
            plot = Path(folder) / "Data" / "p.png"
            Image.new("RGB", (10, 10), "white").save(plot)
            service.dashboard_ui_revision = lambda session_id: 5  # type: ignore[method-assign]
            service._dashboard_plot_cache["s"] = (5, [str(plot)])

            def fail(session_id):
                raise AssertionError("full review should not run")

            service.dashboard_review = fail  # type: ignore[method-assign]
            self.assertEqual(service.dashboard_review_asset("s", 0), plot.resolve())


if __name__ == "__main__":
    unittest.main()
