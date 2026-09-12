"""
Entry point for the ShipSync Agent.

First run (no config yet): shows the setup wizard, then starts the tray app.
Every run after that: goes straight to the tray app with the saved config.

Built as a --windowed PyInstaller exe (see build.ps1), so there is no
console to print errors to - an unexpected crash here would otherwise just
silently vanish, which is confusing for a non-technical user. Any
unhandled exception is shown in a message box instead.
"""

import sys
import traceback

from autostart import enable_autostart
from config import load_config


def _start(config: dict) -> None:
    enable_autostart()
    from tray_icon import run_tray_app

    run_tray_app(config)


def main() -> None:
    config = load_config()
    if config is None:
        from setup_wizard import run_setup_wizard

        run_setup_wizard(_start)
    else:
        _start(config)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import tkinter.messagebox as messagebox

        messagebox.showerror("ShipSync Agent - Fehler", traceback.format_exc())
        sys.exit(1)
