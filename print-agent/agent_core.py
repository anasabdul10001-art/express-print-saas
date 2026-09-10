"""
Talks to the ShipSync backend: polls for the next pending print job,
downloads its label PDF, prints it, and reports back whether it succeeded.

Mirrors the server-side contract in app/routers/print_agents.py and
app/routers/print_jobs.py: GET /print-agents/{id}/next-job (204 = nothing to
do) and PATCH /print-jobs/{id}/status ("printed" or "failed").
"""

import threading
import time

import requests

from config import API_BASE_URL
from printing import download_to_temp, print_pdf

POLL_INTERVAL_SECONDS = 7  # matches the cadence assumed in the backend's docstrings


class PrintAgentWorker:
    """
    Runs the poll loop on a background thread. `on_status` is called with a
    short human-readable string whenever something worth showing changes
    (used to update the tray icon's tooltip) - it must be safe to call from
    a non-main thread.
    """

    def __init__(self, config: dict, on_status):
        self.agent_id = config["agent_id"]
        self.api_key = config["api_key"]
        self.printer_name = config["printer_name"]
        self.on_status = on_status
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except requests.RequestException:
                self.on_status("Server nicht erreichbar - warte...")
            except Exception as exc:  # noqa: BLE001 - a single bad job must never kill the loop
                self.on_status(f"Fehler: {exc}")
            self._stop_event.wait(POLL_INTERVAL_SECONDS)

    def _poll_once(self) -> None:
        response = requests.get(
            f"{API_BASE_URL}/print-agents/{self.agent_id}/next-job",
            headers=self._headers(),
            timeout=15,
        )

        if response.status_code == 401:
            self.on_status("API-Key ungültig - bitte neu einrichten.")
            return
        if response.status_code == 403:
            self.on_status("Dieser Print-Agent wurde deaktiviert.")
            return
        if response.status_code == 204:
            self.on_status("Verbunden - wartet auf Aufträge.")
            return

        response.raise_for_status()
        job = response.json()
        self.on_status(f"Drucke Etikett...")
        self._handle_job(job)

    def _handle_job(self, job: dict) -> None:
        job_id = job["id"]
        try:
            pdf_response = requests.get(job["label_pdf_url"], timeout=30)
            pdf_response.raise_for_status()
            pdf_path = download_to_temp(pdf_response.content)

            print_pdf(pdf_path, self.printer_name, job.get("rotation_degrees", 0))
            self._report_status(job_id, "printed")
            self.on_status("Verbunden - wartet auf Aufträge.")
        except Exception as exc:  # noqa: BLE001 - report every failure back to the dashboard
            self._report_status(job_id, "failed", str(exc))
            self.on_status(f"Druckfehler: {exc}")

    def _report_status(self, job_id: str, status: str, error_message: str | None = None) -> None:
        try:
            requests.patch(
                f"{API_BASE_URL}/print-jobs/{job_id}/status",
                headers={**self._headers(), "Content-Type": "application/json"},
                json={"status": status, "error_message": error_message},
                timeout=15,
            )
        except requests.RequestException:
            pass  # best-effort - the job stays "processing" server-side, visible for manual follow-up
