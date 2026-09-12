"""
First-run (and "reconfigure from the tray menu") setup window. Collects the
three things a Print Agent installation needs - Agent-ID, API-Key (both
copied once from the dashboard's Print-Agents page), and which locally
installed printer to use - and verifies them against the backend before
saving, so a typo is caught here instead of silently failing every 7 seconds
in the background later.
"""

import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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


def _add_entry_with_paste(parent, initial_value: str) -> ttk.Entry:
    """
    An Entry plus a visible "Einfügen" button and a right-click paste menu.
    Ctrl+V *should* just work via Tk's own bindings, but some Windows setups
    (remote desktop / focus-stealing prevention on a freshly opened window)
    can swallow keyboard shortcuts before Tk sees them - a button click
    always reaches the app regardless, so it's the reliable fallback.
    """
    row = ttk.Frame(parent)
    entry = ttk.Entry(row, width=42)
    entry.insert(0, initial_value)
    entry.pack(side="left", fill="x", expand=True)

    def paste_from_clipboard():
        try:
            clip = row.clipboard_get()
        except tk.TclError:
            return
        entry.delete(0, tk.END)
        entry.insert(0, clip.strip())

    ttk.Button(row, text="Einfügen", width=10, command=paste_from_clipboard).pack(side="left", padx=(6, 0))

    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Einfügen", command=paste_from_clipboard)
    menu.add_command(label="Alles auswählen", command=lambda: entry.select_range(0, tk.END))
    entry.bind("<Button-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))

    row.pack(fill="x", pady=(0, 8))
    return entry


def run_setup_wizard(on_saved) -> None:
    """Blocks until the window is closed. Calls on_saved(config) after a successful save."""
    existing = load_config() or {}

    root = tk.Tk()
    root.title("ShipSync Agent - Einrichtung")
    root.geometry("480x560")
    root.resizable(False, False)

    # A freshly-opened window doesn't always grab real keyboard focus on
    # Windows (especially over remote desktop) - force it to the front so
    # typing/pasting reaches it immediately instead of the previous window.
    root.after(150, lambda: (root.lift(), root.focus_force(), root.attributes("-topmost", True)))
    root.after(400, lambda: root.attributes("-topmost", False))

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    ttk.Label(frame, text="ShipSync Agent einrichten", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    ttk.Label(
        frame,
        text="Agent-ID und API-Key findest du im Dashboard unter\n\"Print-Agents\" -> \"+ Neuer Print-Agent\".\nFalls Strg+V nicht reagiert, nutze den \"Einfügen\"-Knopf daneben.",
        foreground="#555555",
        justify="left",
    ).pack(anchor="w", pady=(4, 12))

    ttk.Label(frame, text="Agent-ID").pack(anchor="w")
    agent_id_entry = _add_entry_with_paste(frame, existing.get("agent_id", ""))

    ttk.Label(frame, text="API-Key").pack(anchor="w")
    api_key_entry = _add_entry_with_paste(frame, existing.get("api_key", ""))

    ttk.Label(frame, text="Drucker für Versandetiketten").pack(anchor="w")
    printers = list_printers()
    printer_var = tk.StringVar(value=existing.get("printer_name") or get_default_printer() or (printers[0] if printers else ""))
    printer_dropdown = ttk.Combobox(frame, textvariable=printer_var, values=printers, state="readonly", width=47)
    printer_dropdown.pack(fill="x", pady=(0, 12))

    ttk.Separator(frame).pack(fill="x", pady=(0, 12))

    watch_enabled_var = tk.BooleanVar(value=bool(existing.get("watch_folder")))

    def toggle_watch_fields():
        state = "normal" if watch_enabled_var.get() else "disabled"
        folder_entry.config(state=state)
        browse_button.config(state=state)
        rotation_dropdown.config(state="readonly" if watch_enabled_var.get() else "disabled")

    ttk.Checkbutton(
        frame,
        text="PDFs aus einem Ordner automatisch drucken (z.B. eBay-Etiketten aus \"Downloads\")",
        variable=watch_enabled_var,
        command=toggle_watch_fields,
    ).pack(anchor="w", pady=(0, 4))
    ttk.Label(
        frame,
        text="Praktisch, wenn du das Etikett-PDF weiterhin manuell von eBay/DHL herunterlädst,\naber alles danach (öffnen, Drucker wählen, drehen, Größe anpassen) loswerden willst.",
        foreground="#555555",
        justify="left",
    ).pack(anchor="w", pady=(0, 8))

    folder_row = ttk.Frame(frame)
    default_downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    folder_var = tk.StringVar(value=existing.get("watch_folder") or default_downloads)
    folder_entry = ttk.Entry(folder_row, textvariable=folder_var, width=38)
    folder_entry.pack(side="left", fill="x", expand=True)

    def browse_folder():
        chosen = filedialog.askdirectory(initialdir=folder_var.get() or default_downloads)
        if chosen:
            folder_var.set(chosen)

    browse_button = ttk.Button(folder_row, text="Durchsuchen...", command=browse_folder)
    browse_button.pack(side="left", padx=(6, 0))
    folder_row.pack(fill="x", pady=(0, 8))

    rotation_row = ttk.Frame(frame)
    ttk.Label(rotation_row, text="Drehung für diese Etiketten:").pack(side="left")
    rotation_var = tk.StringVar(value=str(existing.get("watch_rotation", 0)))
    rotation_dropdown = ttk.Combobox(
        rotation_row, textvariable=rotation_var, values=["0", "90", "180", "270"],
        state="readonly", width=6,
    )
    rotation_dropdown.pack(side="left", padx=(8, 0))
    rotation_row.pack(anchor="w", pady=(0, 12))

    toggle_watch_fields()

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

        watch_folder = folder_var.get().strip() if watch_enabled_var.get() else None
        if watch_enabled_var.get() and not os.path.isdir(watch_folder):
            status_label.config(text=f"Ordner nicht gefunden: {watch_folder}")
            return
        watch_rotation = int(rotation_var.get()) if watch_enabled_var.get() else 0

        save_button.config(state="disabled", text="Prüfe Verbindung...")
        root.update_idletasks()

        error = _test_credentials(agent_id, api_key)
        save_button.config(state="normal", text="Speichern und starten")

        if error:
            status_label.config(text=error)
            return

        save_config(agent_id, api_key, printer_name, watch_folder, watch_rotation)
        messagebox.showinfo("ShipSync Agent", "Einrichtung abgeschlossen. Der Agent läuft jetzt im Hintergrund.")
        root.destroy()
        on_saved({
            "agent_id": agent_id,
            "api_key": api_key,
            "printer_name": printer_name,
            "watch_folder": watch_folder,
            "watch_rotation": watch_rotation,
        })

    save_button = ttk.Button(frame, text="Speichern und starten", command=handle_save)
    save_button.pack(anchor="e")

    root.mainloop()
