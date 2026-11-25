"""Uploaded receipts and invoices: which files are accepted and how they are handed back."""

from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

MAX_FILE_BYTES = 8 * 1024 * 1024
FILE_TYPES = ((b"%PDF", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


def file_type(content: bytes) -> str:
    """Decided by the file's first bytes, never by its name."""
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="Datei zu groß (max. 8 MB).")
    kind = next((kind for magic, kind in FILE_TYPES if content.startswith(magic)), None)
    if not kind:
        raise HTTPException(status_code=400, detail="Bitte die Rechnung als PDF, JPG oder PNG hochladen.")
    return kind


def download(name: str, content_type: str, data: bytes) -> Response:
    return Response(content=data, media_type=content_type, headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "X-Content-Type-Options": "nosniff"})
