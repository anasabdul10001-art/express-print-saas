# Express Print Agent

The small Windows program that runs on a seller's own PC, next to their
label printer. It checks the Express Print backend every few seconds for
new print jobs and prints them automatically - no manual clicking, no print
dialog.

## How it works

1. First run: a small setup window asks for the **Agent-ID** and **API-Key**
   (both generated once in the dashboard under "Print-Agents" -> "+ Neuer
   Print-Agent"), and which installed Windows printer to use.
2. After that, it lives as a small icon in the system tray (green = fine,
   red = needs attention) and starts automatically with Windows.
3. Every ~7 seconds it asks the backend for the next pending job
   (`GET /print-agents/{id}/next-job`), downloads the label PDF, prints it
   silently via a bundled copy of SumatraPDF, and reports the result back
   (`PATCH /print-jobs/{id}/status`).

## Files

| File               | Purpose |
|--------------------|---------|
| `main.py`          | Entry point - wizard on first run, tray app after that |
| `setup_wizard.py`  | The first-run / "change settings" window |
| `agent_core.py`    | Polling loop + backend HTTP calls |
| `printing.py`      | Printer list + silent PDF printing (via SumatraPDF) + rotation |
| `tray_icon.py`     | The system tray icon and its menu |
| `autostart.py`     | Registers the program to start with Windows |
| `vendor/SumatraPDF.exe` | Bundled silent-printing tool (not committed to git - see below) |

## Building the .exe

`vendor/SumatraPDF.exe` isn't committed to git (it's a ~20MB binary from a
third party) - `build.ps1` downloads it automatically if missing. From
inside this folder, in PowerShell:

```powershell
.\build.ps1
```

This installs the Python dependencies and produces a single file:
`dist\PrintAgentSetup.exe`. That one file is the entire product for a
customer - no installer, nothing else to download.

## Known limitations (honest status, as of the print-agent client build)

- **Tested with a virtual printer only.** The silent-print mechanism was
  verified end-to-end on this machine using Windows' built-in virtual
  printers (no physical label printer was available here). It should work
  the same way with a real printer since it goes through the normal Windows
  print queue, but **a real print, on a real printer, has not been
  confirmed yet.**
- **"Sofort automatisch" (INSTANT) print mode still needs manual triggering.**
  The backend has no code yet that automatically creates a print job the
  moment an order arrives - see the note already in
  `app/routers/print_jobs.py` and the Settings page. This agent will
  faithfully print whatever job the backend hands it, but nothing currently
  creates that job without someone clicking "Etikett drucken" in the
  dashboard first.
- **No distribution channel yet.** There's currently no download link
  anywhere in the dashboard for customers to get `PrintAgentSetup.exe` -
  it needs to be hosted somewhere (e.g. a GitHub Release) and linked from
  the Print-Agents page.
