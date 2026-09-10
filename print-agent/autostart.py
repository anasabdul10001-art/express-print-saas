"""
Registers the agent to launch automatically when Windows starts, via the
per-user registry Run key (HKCU) rather than the Startup folder or a
Scheduled Task - HKCU needs no admin rights and is the standard place a
small per-user tool adds itself.
"""

import sys
import winreg

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ExpressPrintAgent"


def enable_autostart() -> None:
    # sys.executable is the running .exe itself when frozen by PyInstaller;
    # in dev (running main.py directly) this would point at the Python
    # interpreter instead, which is fine for testing but not meant for the
    # built exe's actual install.
    target = f'"{sys.executable}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, target)


def is_autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
