import os
import tempfile
import unittest
from pathlib import Path

from mac_disk_scanner.app import CleanerApp, ConfirmScreen
from mac_disk_scanner.cache import ScanCache
from mac_disk_scanner.scanner import Scanner


class QuietApp(CleanerApp):
    # No startup scans and an empty private cache: only UI behaviour runs.
    QUICK_REGIONS = ()

    def __init__(self, cache_dir: str):
        super().__init__()
        self.cache = ScanCache(Path(cache_dir) / "scan-cache.json")


class ModalOpenTest(unittest.IsolatedAsyncioTestCase):
    async def test_background_updates_while_confirm_dialog_is_open(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            app = QuietApp(cache_dir)
            async with app.run_test(size=(170, 44)) as pilot:
                app.push_screen(ConfirmScreen("Delete", "x", destructive=True))
                await pilot.pause()

                # Timers and background tasks keep updating the main screen
                # while the dialog is on top.
                app.set_progress(5, "Waiting for confirmation")
                app._tick_status()
                app.refresh_ui()

                status = app.screen_stack[0].query_one("#status")
                self.assertIn("Waiting for confirmation", str(status.render()))

    async def test_delete_prompt_survives_status_ticks_and_cancel_keeps_files(self):
        with tempfile.TemporaryDirectory() as cache_dir, \
                tempfile.TemporaryDirectory() as tmp_base:
            base = Path(tmp_base).resolve()
            target = base / "build-target"
            target.mkdir()
            (target / "blob.bin").write_bytes(os.urandom(11 * 1024 * 1024))

            app = QuietApp(cache_dir)
            async with app.run_test(size=(170, 44)) as pilot:
                app.region_roots["temporary"] = Scanner(tmp_base=base).scan_private_tmp()
                app.switch_category("temporary")
                await pilot.pause()
                app.selected_path = str(target)
                app.refresh_table()
                await pilot.pause()

                await pilot.press("d")
                await pilot.pause(1.5)  # let the 1 s status tick fire
                self.assertIsInstance(app.screen, ConfirmScreen)

                await pilot.click("#cancel")
                await pilot.pause()

            self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
