"""Local PDF text extraction helpers.

The project keeps PDF extraction optional.  If PyMuPDF is unavailable, callers
still receive an auditable ExtractionResult with warnings rather than a silent
failure.  Runtime drafting should then fail closed or use explicitly supplied
reviewed page-text fixtures; it must not fall back to issuer-specific seeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


@dataclass(frozen=True)
class PageExtractionMetadata:
    """Auditable per-page extraction quality metadata."""

    page_number: int
    char_count: int
    text_density: float = 0.0
    method: str = "pymupdf"
    status: str = "empty"
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionEnvironment:
    text_backend: str
    text_backend_available: bool
    ocr_backend: str
    ocr_backend_available: bool
    recommended_runtime: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "text_backend": self.text_backend,
            "text_backend_available": self.text_backend_available,
            "ocr_backend": self.ocr_backend,
            "ocr_backend_available": self.ocr_backend_available,
            "recommended_runtime": self.recommended_runtime,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ExtractionResult:
    source_path: Path
    text: str
    method: str
    page_count: int = 0
    warnings: list[str] = field(default_factory=list)
    pages: list[PageText] = field(default_factory=list)
    page_metadata: list[PageExtractionMetadata] = field(default_factory=list)
    status: str = "unknown"

    @classmethod
    def from_pages(
        cls,
        *,
        source_path: str | Path,
        pages: list[PageText],
        method: str,
        warnings: list[str] | None = None,
    ) -> "ExtractionResult":
        ordered_pages = sorted(pages, key=lambda page: page.page_number)
        text = "".join(f"\n\n--- Page {page.page_number} ---\n{page.text}" for page in ordered_pages)
        metadata = [
            PageExtractionMetadata(
                page_number=page.page_number,
                char_count=len(page.text.strip()),
                status="text" if page.text.strip() else "empty",
                method=method,
            )
            for page in ordered_pages
        ]
        result = cls(
            source_path=Path(source_path),
            text=text,
            method=method,
            page_count=len(ordered_pages),
            warnings=warnings or [],
            pages=ordered_pages,
            page_metadata=metadata,
        )
        return _with_status(result)

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


def inspect_extraction_environment() -> ExtractionEnvironment:
    warnings: list[str] = []
    try:
        import fitz  # type: ignore  # noqa: F401

        text_available = True
    except Exception as exc:
        text_available = False
        warnings.append(f"PyMuPDF/fitz unavailable: {exc}")

    # OCR is intentionally optional.  The project does not shell out to OCR yet,
    # but exposing the preflight keeps scanned-PDF failures distinct from missing
    # text-layer extraction support.
    ocr_available = False
    try:  # pragma: no cover - usually absent in CI/dev environments
        import pytesseract  # type: ignore  # noqa: F401

        ocr_available = True
    except Exception:
        pass

    return ExtractionEnvironment(
        text_backend="pymupdf",
        text_backend_available=text_available,
        ocr_backend="pytesseract",
        ocr_backend_available=ocr_available,
        recommended_runtime="Install optional prospectus dependencies with: uv pip install --python .venv/bin/python -e '.[prospectus]'",
        warnings=warnings,
    )


def classify_extraction_result(result: ExtractionResult) -> str:
    if result.method.startswith("unavailable:"):
        return "backend_missing"
    if result.method.startswith("failed:"):
        return "backend_failed"
    if not result.has_text:
        return "needs_ocr"
    sparse_pages = [page for page in result.page_metadata if page.status in {"empty", "low_text"}]
    if result.page_metadata and len(sparse_pages) == len(result.page_metadata):
        return "needs_ocr"
    if sparse_pages:
        return "partial_low_text"
    return "text_extracted"


def _with_status(result: ExtractionResult) -> ExtractionResult:
    status = classify_extraction_result(result)
    return ExtractionResult(
        source_path=result.source_path,
        text=result.text,
        method=result.method,
        page_count=result.page_count,
        warnings=result.warnings,
        pages=result.pages,
        page_metadata=result.page_metadata,
        status=status,
    )


def extract_text_from_pdf(path: str | Path, *, max_pages: int | None = None) -> ExtractionResult:
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    try:
        import fitz  # type: ignore
    except Exception:
        return _with_status(ExtractionResult(
            source_path=pdf_path,
            text="",
            method="unavailable:pymupdf",
            page_count=0,
            warnings=["PyMuPDF/fitz is not installed; PDF text extraction was skipped."],
        ))

    warnings: list[str] = []
    page_texts: list[PageText] = []
    try:
        doc_context = fitz.open(pdf_path)  # type: ignore[attr-defined]
    except Exception as exc:
        return _with_status(ExtractionResult(
            source_path=pdf_path,
            text="",
            method="failed:pymupdf",
            page_count=0,
            warnings=[f"PyMuPDF/fitz could not open PDF; extraction was skipped: {exc}"],
        ))

    page_metadata: list[PageExtractionMetadata] = []
    with doc_context as doc:
        page_count = len(doc)
        limit = min(page_count, max_pages) if max_pages is not None else page_count
        for index in range(limit):
            page_number = index + 1
            page_warnings: list[str] = []
            try:
                page = doc[index]
                text = page.get_text("text")
            except Exception as exc:  # pragma: no cover - backend-specific
                warning = f"page {page_number}: extraction failed: {exc}"
                warnings.append(warning)
                page_metadata.append(PageExtractionMetadata(page_number=page_number, char_count=0, method="pymupdf", status="failed", warnings=[warning]))
                continue
            stripped = text.strip()
            char_count = len(stripped)
            try:
                rect = page.rect
                area = max(float(rect.width) * float(rect.height), 1.0)
            except Exception:  # pragma: no cover - backend-specific
                area = 1.0
            density = char_count / area
            if not stripped:
                status = "empty"
                page_warnings.append("no text-layer characters extracted")
            elif char_count < 40:
                status = "low_text"
                page_warnings.append("sparse text layer; page may require OCR/manual review")
            else:
                status = "text"
            page_metadata.append(PageExtractionMetadata(page_number=page_number, char_count=char_count, text_density=density, method="pymupdf", status=status, warnings=page_warnings))
            if stripped:
                page_texts.append(PageText(page_number, text))
    if not page_texts:
        warnings.append("needs_ocr: no extractable text found with PyMuPDF; document may be scanned or image-only.")
    elif any(page.status in {"empty", "low_text"} for page in page_metadata):
        warnings.append("partial_low_text: some pages have sparse/no text and may need OCR or manual review.")
    result = ExtractionResult.from_pages(source_path=pdf_path, pages=page_texts, method="pymupdf", warnings=warnings)
    return _with_status(ExtractionResult(
        source_path=pdf_path,
        text=result.text,
        method=result.method,
        page_count=page_count,
        warnings=result.warnings,
        pages=result.pages,
        page_metadata=page_metadata,
    ))
