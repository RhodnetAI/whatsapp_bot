import csv
import io
import importlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from docx import Document
from fastapi import HTTPException, UploadFile
from unstructured_client import UnstructuredClient  # type: ignore[import]
from unstructured_client.models.operations.partition import PartitionRequestTypedDict  # type: ignore[import]
from unstructured_client.models.shared.partition_parameters import PartitionParametersTypedDict  # type: ignore[import]

from app.core.config import settings

logger = logging.getLogger("whatsapp")

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_CONTENT_CHARS = 60_000
SUPPORTED_EXTENSIONS = {
    ".txt",
    ".pdf",
    ".doc",
    ".docx",
    ".csv",
    ".json",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
}


def _extension_for_filename(filename: str) -> str:
    return Path(filename or "").suffix.lower()


def _decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(status_code=400, detail="Unable to decode document as text")


def _extract_docx(data: bytes) -> str:
    document = Document(io.BytesIO(data))
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _guess_content_type(filename: str | None, extension: str) -> str:
    if filename and filename.lower().endswith(".pdf"):
        return "application/pdf"
    if filename and filename.lower().endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if filename and filename.lower().endswith(".doc"):
        return "application/msword"
    if filename and filename.lower().endswith(".csv"):
        return "text/csv"
    if filename and filename.lower().endswith(".json"):
        return "application/json"
    if filename and (filename.lower().endswith(".html") or filename.lower().endswith(".htm")):
        return "text/html"
    if filename and filename.lower().endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _extract_locally(extension: str, data: bytes) -> str:
    if extension in TEXT_EXTENSIONS:
        return _decode_text_bytes(data)
    if extension == ".pdf":
        return _extract_pdf(data)
    if extension == ".docx":
        return _extract_docx(data)
    if extension == ".csv":
        return _extract_csv(data)
    if extension == ".json":
        return _extract_json(data)
    if extension == ".doc":
        with tempfile.NamedTemporaryFile(delete=True, suffix=".doc") as temp_file:
            temp_file.write(data)
            temp_file.flush()
            try:
                import textract  # type: ignore

                extracted = textract.process(temp_file.name)
                return _decode_text_bytes(extracted)
            except ModuleNotFoundError:
                if b"\x00" in data:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "DOC extraction is not available in this environment. "
                            "Convert the file to DOCX or PDF, or install textract support."
                        ),
                    )
                return _decode_text_bytes(data)
    return _decode_text_bytes(data)


async def _extract_with_unstructured(data: bytes, filename: str, content_type: str | None) -> str:
    client = UnstructuredClient(
        api_key_auth=settings.unstructured_api_key,
        server_url=settings.unstructured_api_url,
    )
    file_content_type = content_type or _guess_content_type(filename, _extension_for_filename(filename))
    request: PartitionRequestTypedDict = {
        "partition_parameters": {
            "files": [
                {
                    "file_name": filename,
                    "data": data,
                    "content_type": file_content_type,
                }
            ]
        }
    }

    response = await client.general.partition_async(request=request)
    elements = getattr(response, "elements", None) or []
    text_parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_text = element.get("text")
        if isinstance(element_text, str) and element_text.strip():
            text_parts.append(element_text.strip())

    if not text_parts:
        raise HTTPException(status_code=500, detail="Unstructured extraction returned no text")

    return "\n\n".join(text_parts)


def _extract_pdf(data: bytes) -> str:
    pypdf_module = importlib.import_module("pypdf")
    PdfReader = getattr(pypdf_module, "PdfReader")
    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        extracted = page.extract_text() or ""
        if extracted:
            parts.append(extracted)
    return "\n".join(parts)


def _extract_csv(data: bytes) -> str:
    text = _decode_text_bytes(data)
    reader = csv.reader(io.StringIO(text))
    rows = [", ".join(cell for cell in row) for row in reader]
    return "\n".join(rows)


def _extract_json(data: bytes) -> str:
    text = _decode_text_bytes(data)
    parsed: Any = json.loads(text)
    return json.dumps(parsed, indent=2, ensure_ascii=False)


async def extract_document_text(document: UploadFile) -> tuple[str, str]:
    filename = document.filename or "document"
    extension = _extension_for_filename(filename)

    if extension not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{extension or 'unknown'}'.",
        )

    data = await document.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="File exceeds 20 MB limit")

    if settings.unstructured_api_key and settings.unstructured_api_url:
        try:
            text = await _extract_with_unstructured(data, filename, document.content_type)
        except Exception as exc:
            logger.warning(
                "Unstructured extraction failed for %s, falling back to local extraction: %s",
                filename,
                str(exc),
            )
            text = _extract_locally(extension, data)
    else:
        text = _extract_locally(extension, data)

    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines())
    normalized = normalized.strip()

    if not normalized:
        raise HTTPException(status_code=400, detail="Document did not contain readable text")

    if len(normalized) > MAX_CONTENT_CHARS:
        raise HTTPException(status_code=400, detail="Document exceeds 60,000 character limit")

    return normalized, filename
