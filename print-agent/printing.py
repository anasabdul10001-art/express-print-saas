"""
Printer discovery and silent PDF printing.

Windows has no built-in way to print a PDF to a named printer without a
dialog popping up - "silent, pick-your-own-printer" PDF printing needs an
external tool. We shell out to a bundled, portable copy of SumatraPDF
(sumatrapdfreader.org), which supports exactly that via its command line
(-print-to "<printer>" -silent). This is the same trick most small
"local print agent" tools use, since writing a PDF renderer ourselves would
be a project on its own.

Rotation is handled separately with pypdf *before* handing the file to
SumatraPDF, because SumatraPDF's own rotation flags rotate the on-screen
view, not reliably the physical print orientation across printer drivers.
Rotating the PDF's page objects directly is unambiguous regardless of
printer.
"""

import os
import subprocess
import sys
import tempfile

import win32print
from pypdf import PdfReader, PdfWriter


def resource_path(relative_path: str) -> str:
    """
    Resolves a bundled file both in dev (running main.py directly) and in
    the built .exe (PyInstaller --onefile extracts bundled data to a temp
    dir at sys._MEIPASS at runtime).
    """
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


SUMATRA_PATH = resource_path(os.path.join("vendor", "SumatraPDF.exe"))


def list_printers() -> list[str]:
    """All printers Windows currently knows about (installed + networked)."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return sorted(p[2] for p in win32print.EnumPrinters(flags))


def get_default_printer() -> str | None:
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def _rotate_pdf(input_path: str, rotation_degrees: int) -> str:
    """Returns a path to a rotated copy, or the original path if no rotation is needed."""
    if rotation_degrees % 360 == 0:
        return input_path

    reader = PdfReader(input_path)
    writer = PdfWriter()
    for page in reader.pages:
        page.rotate(rotation_degrees)
        writer.add_page(page)

    rotated_path = input_path.replace(".pdf", "_rotated.pdf")
    with open(rotated_path, "wb") as f:
        writer.write(f)
    return rotated_path


def print_pdf(pdf_path: str, printer_name: str, rotation_degrees: int = 0) -> None:
    """
    Prints a PDF file to the given printer with no dialogs. Raises
    RuntimeError with a human-readable message on failure - the caller
    reports this back to the dashboard as the print job's error_message.
    """
    if not os.path.exists(SUMATRA_PATH):
        raise RuntimeError("SumatraPDF.exe fehlt in der Installation.")

    print_path = _rotate_pdf(pdf_path, rotation_degrees)

    result = subprocess.run(
        [
            SUMATRA_PATH,
            "-print-to", printer_name,
            "-silent",
            "-exit-when-done",
            print_path,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Drucken fehlgeschlagen: {result.stderr.strip() or result.stdout.strip() or 'unbekannter Fehler'}")


def download_to_temp(content: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="express_print_")
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path
