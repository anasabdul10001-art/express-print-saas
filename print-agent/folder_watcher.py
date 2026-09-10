"""
Optional "hot folder" feature: watches a folder the seller already downloads
their shipping label PDFs into (from eBay/the carrier's own site - there is
no API access to fetch that PDF ourselves) and prints anything new that
shows up there automatically, with no dialogs. This is what removes the
manual "open the file, pick the printer, rotate it, resize it, print" ritual
entirely - the seller's only remaining step is the same download click they
already do today.

Deliberately a simple polling loop rather than a filesystem-events library
(e.g. watchdog) - one more dependency for a folder that gets maybe a few
new files a day isn't worth it, and polling is trivial to reason about.
"""

import os
import threading
import time

from printing import print_pdf

POLL_INTERVAL_SECONDS = 3
STABLE_CHECKS_REQUIRED = 2  # a file's size must be unchanged this many checks running before we treat it as fully downloaded
PRINTED_SUBFOLDER = "Gedruckt"
FAILED_SUBFOLDER = "Fehler"


class FolderWatcher:
    def __init__(self, folder: str, printer_name: str, rotation_degrees: int, on_status):
        self.folder = folder
        self.printer_name = printer_name
        self.rotation_degrees = rotation_degrees
        self.on_status = on_status
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_sizes: dict[str, tuple[int, int]] = {}  # filename -> (size, stable_count)

    def start(self) -> None:
        os.makedirs(os.path.join(self.folder, PRINTED_SUBFOLDER), exist_ok=True)
        os.makedirs(os.path.join(self.folder, FAILED_SUBFOLDER), exist_ok=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception as exc:  # noqa: BLE001 - one bad scan must never kill the watcher
                self.on_status(f"Ordner-Überwachung: Fehler ({exc})")
            self._stop_event.wait(POLL_INTERVAL_SECONDS)

    def _scan_once(self) -> None:
        try:
            entries = [
                f for f in os.listdir(self.folder)
                if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(self.folder, f))
            ]
        except OSError:
            self.on_status(f"Ordner nicht erreichbar: {self.folder}")
            return

        current_names = set(entries)
        for stale_name in list(self._pending_sizes):
            if stale_name not in current_names:
                del self._pending_sizes[stale_name]

        for name in entries:
            full_path = os.path.join(self.folder, name)
            try:
                size = os.path.getsize(full_path)
            except OSError:
                continue  # file disappeared/still being created between listdir() and getsize()

            prev_size, stable_count = self._pending_sizes.get(name, (None, 0))
            if size == prev_size:
                stable_count += 1
            else:
                stable_count = 0
            self._pending_sizes[name] = (size, stable_count)

            if stable_count >= STABLE_CHECKS_REQUIRED:
                del self._pending_sizes[name]
                self._handle_new_label(full_path, name)

    def _handle_new_label(self, full_path: str, name: str) -> None:
        self.on_status(f"Drucke Etikett: {name}")
        try:
            print_pdf(full_path, self.printer_name, self.rotation_degrees, fit_to_page=True)
            self._move_to(full_path, name, PRINTED_SUBFOLDER)
            self.on_status("Verbunden - überwacht Download-Ordner.")
        except Exception as exc:  # noqa: BLE001 - a bad PDF must not stop watching the rest
            self._move_to(full_path, name, FAILED_SUBFOLDER)
            self.on_status(f"Etikett-Druck fehlgeschlagen: {exc}")

    def _move_to(self, full_path: str, name: str, subfolder: str) -> None:
        destination = os.path.join(self.folder, subfolder, name)
        if os.path.exists(destination):
            base, ext = os.path.splitext(name)
            destination = os.path.join(self.folder, subfolder, f"{base}_{int(time.time())}{ext}")
        try:
            os.replace(full_path, destination)
        except OSError:
            pass  # best-effort tidy-up; leaving the file in place is harmless
