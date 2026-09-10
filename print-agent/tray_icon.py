"""
The little icon in the Windows system tray. This is the whole "UI" once the
agent is set up - a color to glance at (green = fine, red = needs attention)
and a right-click menu for the two things a seller might need mid-run:
change settings, or quit.
"""

import threading

import pystray
from PIL import Image, ImageDraw

from agent_core import PrintAgentWorker
from folder_watcher import FolderWatcher


def _make_icon_image(color: str) -> Image.Image:
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    return image


ICON_OK = _make_icon_image("#22c55e")
ICON_WARN = _make_icon_image("#ef4444")


def run_tray_app(config: dict) -> None:
    state = {"status_text": "Startet..."}

    def on_status(text: str) -> None:
        state["status_text"] = text
        is_ok = text.startswith("Verbunden") or text.startswith("Drucke")
        icon.icon = ICON_OK if is_ok else ICON_WARN
        icon.title = f"Express Print Agent - {text}"
        # pystray rebuilds the menu lazily from these callables, so no
        # explicit "refresh" call is needed for the status line below.

    worker = PrintAgentWorker(config, on_status)
    watcher: FolderWatcher | None = None

    def start_folder_watcher(cfg: dict) -> None:
        nonlocal watcher
        if watcher:
            watcher.stop()
            watcher = None
        if cfg.get("watch_folder"):
            watcher = FolderWatcher(cfg["watch_folder"], cfg["printer_name"], cfg.get("watch_rotation", 0), on_status)
            watcher.start()

    def status_menu_text(_item):
        return state["status_text"]

    def open_settings(_icon, _item):
        worker.stop()
        if watcher:
            watcher.stop()
        # Import here, not at module load, so the setup wizard's tkinter
        # window only ever appears when explicitly requested.
        from setup_wizard import run_setup_wizard

        def restart_with(new_config: dict):
            nonlocal worker
            worker = PrintAgentWorker(new_config, on_status)
            worker.start()
            start_folder_watcher(new_config)

        threading.Thread(target=lambda: run_setup_wizard(restart_with), daemon=True).start()

    def quit_app(icon, _item):
        worker.stop()
        if watcher:
            watcher.stop()
        icon.stop()

    icon = pystray.Icon(
        "ExpressPrintAgent",
        ICON_OK,
        "Express Print Agent",
        menu=pystray.Menu(
            pystray.MenuItem(status_menu_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Einstellungen ändern", open_settings),
            pystray.MenuItem("Beenden", quit_app),
        ),
    )

    worker.start()
    start_folder_watcher(config)
    icon.run()
