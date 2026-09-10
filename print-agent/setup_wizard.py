"""
First-run (and "reconfigure from the tray menu") setup window. Collects the
three things a Print Agent installation needs - Agent-ID, API-Key (both
copied once from the dashboard's Print-Agents page), and which locally
installed printer to use - and verifies them against the backend before
saving, so a typo is caught here instead of silently failing every 7 seconds
in the background later.
"""

import tkinter as tk
from tkinter import messagebox, ttk

import requests

from config import API_BASE_URL, load_config, save_config
from printing import get_default_printer, list_printers


def _test_credentials(agent_id: str, api_key: str) -> str | None:
    """Returns an error message, or None if the credentials check out."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/print-agents/{agent_id}/next-job",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
    except requests.RequestException:
        return "Server nicht erreichbar. Bitte Internetverbindung prüfen und erneut versuchen."

    if response.status_code == 401:
        return "API-Key ungültig. Bitte den Key nochmal aus dem Dashboard kopieren."
    if response.status_code == 403:
        return "Dieser Print-Agent wurde im Dashboard deaktiviert."
    if response.status_code not in (200, 204):
        return f"Unerwartete Antwort vom Server ({response.status_code})."
    return None


def run_setup_wizard(on_saved) -> None:
    """Blocks until the window is closed. Calls on_saved(config) after a successful save."""
    existing = load_config() or {}

    root = tk.Tk()
    root.title("Express Print Agent - Einrichtung")
    root.geometry("440x330")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="Express Print Agent einrichten", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text="Agent-ID und API-Key findest du im Dashboard unter\n\"Print-Agents\" -> \"+ Neuer Print-Agent\".",
        foreground="#555555",
        justify="left",
    ).pack(anchor="w", pady=(4, 12))

    ttk.Label(frame, text="Agent-ID").pack(anchor="w")
    agent_id_entry = ttk.Entry(frame, width=50)
    agent_id_entry.insert(0, existing.get("agent_id", ""))
    agent_id_entry.pack(fill="x", pady=(0, 8))

    ttk.Label(frame, text="API-Key").pack(anchor="w")
    api_key_entry = ttk.Entry(frame, width=50, show="*")
    api_key_entry.insert(0, existing.get("api_key", ""))
    api_key_entry.pack(fill="x", pady=(0, 8))

    ttk.Label(frame, text="Drucker für Versandetiketten").pack(anchor="w")
    printers = list_printers()
    printer_var = tk.StringVar(value=existing.get("printer_name") or get_default_printer() or (printers[0] if printers else ""))
    printer_dropdown = ttk.Combobox(frame, textvariable=printer_var, values=printers, state="readonly", width=47)
    printer_dropdown.pack(fill="x", pady=(0, 12))

    status_label = ttk.Label(frame, text="", foreground="#b00020", wraplength=400, justify="left")
    status_label.pack(anchor="w", pady=(0, 8))

    def handle_save():
        agent_id = agent_id_entry.get().strip()
        api_key = api_key_entry.get().strip()
        printer_name = printer_var.get().strip()

        if not agent_id or not api_key:
            status_label.config(text="Bitte Agent-ID und API-Key ausfüllen.")
            return
        if not printer_name:
            status_label.config(text="Bitte einen Drucker auswählen.")
            return

        save_button.config(state="disabled", text="Prüfe Verbindung...")
        root.update_idletasks()

        error = _test_credentials(agent_id, api_key)
        save_button.config(state="normal", text="Speichern und starten")

        if error:
            status_label.config(text=error)
            return

        save_config(agent_id, api_key, printer_name)
        messagebox.showinfo("Express Print Agent", "Einrichtung abgeschlossen. Der Agent läuft jetzt im Hintergrund.")
        root.destroy()
        on_saved({"agent_id": agent_id, "api_key": api_key, "printer_name": printer_name})

    save_button = ttk.Button(frame, text="Speichern und starten", command=handle_save)
    save_button.pack(anchor="e")

    root.mainloop()
