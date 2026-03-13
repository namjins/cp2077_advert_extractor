"""Validate a research markdown file against the extracted manifest.

Parses lines containing .xbm paths from a free-form markdown document (e.g.
a deep-research report listing known ad textures) and cross-references them
against the assets_manifest.csv to produce:
  - matched:            texture found in manifest, archive matches (or not specified)
  - missing_in_extract: texture path not found in the manifest at all
  - archive_mismatch:   texture found but in a different archive than the research claims
  - unparseable:        line mentions .xbm but no valid path could be extracted
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import logging
from pathlib import Path
import re

from .config import PipelineConfig
from .io_utils import atomic_write_csv, atomic_write_text, ensure_dir
from .manifest import read_manifest
from .models import AssetRecord

LIST_VALIDATION_COLUMNS = [
    "line_number",
    "raw_text",
    "research_archive_path",
    "research_relative_texture_path",
    "normalized_relative_texture_path",
    "status",
    "matched_archive_paths",
    "message",
]

STATUS_MATCHED = "matched"
STATUS_MISSING = "missing_in_extract"
STATUS_ARCHIVE_MISMATCH = "archive_mismatch"
STATUS_UNPARSEABLE = "unparseable"

_TEXTURE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\:-]+\.xbm", re.IGNORECASE)
_ARCHIVE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\:-]+\.archive", re.IGNORECASE)


@dataclass(slots=True)
class ResearchPathRow:
    line_number: int
    raw_text: str
    research_archive_path: str
    research_relative_texture_path: str
    normalized_relative_texture_path: str
    parse_error: str = ""


@dataclass(slots=True)
class ListValidationRow:
    line_number: int
    raw_text: str
    research_archive_path: str
    research_relative_texture_path: str
    normalized_relative_texture_path: str
    status: str
    matched_archive_paths: str
    message: str

    def to_row(self) -> dict[str, str]:
        return {
            "line_number": str(self.line_number),
            "raw_text": self.raw_text,
            "research_archive_path": self.research_archive_path,
            "research_relative_texture_path": self.research_relative_texture_path,
            "normalized_relative_texture_path": self.normalized_relative_texture_path,
            "status": self.status,
            "matched_archive_paths": self.matched_archive_paths,
            "message": self.message,
        }


@dataclass(slots=True)
class ListValidationResult:
    processed: int
    matched: int
    missing_in_extract: int
    archive_mismatch: int
    unparseable: int
    csv_path: Path
    summary_path: Path
    log_path: Path


def run_validate_list_stage(
    config: PipelineConfig,
    *,
    research_file: Path,
    logger: logging.Logger,
    log_path: Path,
) -> ListValidationResult:
    ensure_dir(config.paths.output_dir)

    logger.info("Starting validate-list stage research_file=%s", research_file)

    manifest_rows = read_manifest(config.discovery.approved_manifest)
    research_rows = parse_research_markdown(research_file)
    validation_rows = compare_research_paths(research_rows, manifest_rows)

    csv_path = config.paths.output_dir / "list_validation.csv"
    summary_path = config.paths.output_dir / "list_validation_summary.txt"

    atomic_write_csv(
        csv_path,
        LIST_VALIDATION_COLUMNS,
        [row.to_row() for row in validation_rows],
    )

    status_counts = Counter(row.status for row in validation_rows)
    mismatch_counts = Counter(
        row.message
        for row in validation_rows
        if row.status == STATUS_ARCHIVE_MISMATCH and row.message
    )

    _write_list_validation_summary(
        summary_path=summary_path,
        research_file=research_file,
        manifest_file=config.discovery.approved_manifest,
        rows=validation_rows,
        mismatch_counts=mismatch_counts,
    )

    logger.info(
        "Validate-list complete processed=%s matched=%s missing_in_extract=%s archive_mismatch=%s unparseable=%s",
        len(validation_rows),
        status_counts.get(STATUS_MATCHED, 0),
        status_counts.get(STATUS_MISSING, 0),
        status_counts.get(STATUS_ARCHIVE_MISMATCH, 0),
        status_counts.get(STATUS_UNPARSEABLE, 0),
    )

    if mismatch_counts:
        logger.warning("Top archive mismatch buckets:")
        for message, count in mismatch_counts.most_common(5):
            logger.warning("  count=%s %s", count, message)

    return ListValidationResult(
        processed=len(validation_rows),
        matched=status_counts.get(STATUS_MATCHED, 0),
        missing_in_extract=status_counts.get(STATUS_MISSING, 0),
        archive_mismatch=status_counts.get(STATUS_ARCHIVE_MISMATCH, 0),
        unparseable=status_counts.get(STATUS_UNPARSEABLE, 0),
        csv_path=csv_path,
        summary_path=summary_path,
        log_path=log_path,
    )


def parse_research_markdown(path: Path) -> list[ResearchPathRow]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed: list[ResearchPathRow] = []
    parseable_by_path: dict[str, int] = {}

    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if ".xbm" not in lowered:
            continue

        archive_token = _extract_archive_token(line)
        texture_tokens = _TEXTURE_TOKEN_RE.findall(line)

        if not texture_tokens:
            parsed.append(
                ResearchPathRow(
                    line_number=line_number,
                    raw_text=line.strip(),
                    research_archive_path=archive_token,
                    research_relative_texture_path="",
                    normalized_relative_texture_path="",
                    parse_error="Line mentions .xbm but no parseable texture path was found",
                )
            )
            continue

        for token in texture_tokens:
            normalized = normalize_texture_path(token)
            if not normalized:
                parsed.append(
                    ResearchPathRow(
                        line_number=line_number,
                        raw_text=line.strip(),
                        research_archive_path=archive_token,
                        research_relative_texture_path=token,
                        normalized_relative_texture_path="",
                        parse_error="Unable to normalize extracted texture path",
                    )
                )
                continue

            existing_idx = parseable_by_path.get(normalized)
            if existing_idx is not None:
                existing = parsed[existing_idx]
                if not existing.research_archive_path and archive_token:
                    existing.research_archive_path = archive_token
                continue

            parseable_by_path[normalized] = len(parsed)
            parsed.append(
                ResearchPathRow(
                    line_number=line_number,
                    raw_text=line.strip(),
                    research_archive_path=archive_token,
                    research_relative_texture_path=token,
                    normalized_relative_texture_path=normalized,
                )
            )

    return parsed


def compare_research_paths(
    research_rows: list[ResearchPathRow],
    manifest_rows: list[AssetRecord],
) -> list[ListValidationRow]:
    by_texture_path: dict[str, list[str]] = {}

    for manifest_row in manifest_rows:
        normalized_texture = normalize_texture_path(manifest_row.relative_texture_path)
        if not normalized_texture:
            continue
        normalized_archive = normalize_archive_path(manifest_row.archive_path)
        by_texture_path.setdefault(normalized_texture, []).append(normalized_archive)

    compared: list[ListValidationRow] = []

    for row in research_rows:
        if row.parse_error:
            compared.append(
                ListValidationRow(
                    line_number=row.line_number,
                    raw_text=row.raw_text,
                    research_archive_path=row.research_archive_path,
                    research_relative_texture_path=row.research_relative_texture_path,
                    normalized_relative_texture_path=row.normalized_relative_texture_path,
                    status=STATUS_UNPARSEABLE,
                    matched_archive_paths="",
                    message=row.parse_error,
                )
            )
            continue

        matches = sorted(set(by_texture_path.get(row.normalized_relative_texture_path, [])))
        if not matches:
            compared.append(
                ListValidationRow(
                    line_number=row.line_number,
                    raw_text=row.raw_text,
                    research_archive_path=row.research_archive_path,
                    research_relative_texture_path=row.research_relative_texture_path,
                    normalized_relative_texture_path=row.normalized_relative_texture_path,
                    status=STATUS_MISSING,
                    matched_archive_paths="",
                    message="Texture path not found in extracted manifest",
                )
            )
            continue

        normalized_research_archive = normalize_archive_path(row.research_archive_path)
        if normalized_research_archive and normalized_research_archive not in matches:
            compared.append(
                ListValidationRow(
                    line_number=row.line_number,
                    raw_text=row.raw_text,
                    research_archive_path=row.research_archive_path,
                    research_relative_texture_path=row.research_relative_texture_path,
                    normalized_relative_texture_path=row.normalized_relative_texture_path,
                    status=STATUS_ARCHIVE_MISMATCH,
                    matched_archive_paths="|".join(matches),
                    message=(
                        f"Research archive '{normalized_research_archive}' "
                        f"did not match extracted archive(s) {'|'.join(matches)}"
                    ),
                )
            )
            continue

        compared.append(
            ListValidationRow(
                line_number=row.line_number,
                raw_text=row.raw_text,
                research_archive_path=row.research_archive_path,
                research_relative_texture_path=row.research_relative_texture_path,
                normalized_relative_texture_path=row.normalized_relative_texture_path,
                status=STATUS_MATCHED,
                matched_archive_paths="|".join(matches),
                message="Path found in extracted manifest",
            )
        )

    return compared


def normalize_texture_path(raw: str) -> str:
    value = _strip_wrapping_punctuation(raw.strip())
    value = value.replace("\\.xbm", ".xbm")
    value = value.replace("\\\\", "\\")
    value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value).lower()

    base_idx = value.find("base/")
    ep1_idx = value.find("ep1/")
    if base_idx >= 0 and (ep1_idx == -1 or base_idx < ep1_idx):
        value = value[base_idx:]
    elif ep1_idx >= 0:
        value = value[ep1_idx:]

    while value.startswith("./"):
        value = value[2:]
    while value.startswith("/"):
        value = value[1:]

    value = value.rstrip(".;,)]}'\"")
    if not value.endswith(".xbm"):
        return ""
    if "/" not in value:
        return ""
    return value


def normalize_archive_path(raw: str) -> str:
    value = _strip_wrapping_punctuation(raw.strip())
    value = value.replace("\\\\", "\\")
    value = value.replace("\\", "/")
    value = re.sub(r"/+", "/", value).lower()

    idx = value.find("archive/pc/")
    if idx >= 0:
        value = value[idx:]

    while value.startswith("./"):
        value = value[2:]
    while value.startswith("/"):
        value = value[1:]

    return value.rstrip(".;,)]}'\"")


def _extract_archive_token(line: str) -> str:
    token = ""
    match = _ARCHIVE_TOKEN_RE.search(line)
    if match:
        token = normalize_archive_path(match.group(0))
    return token


def _strip_wrapping_punctuation(value: str) -> str:
    stripped = value.strip()
    changed = True
    while stripped and changed:
        changed = False
        if stripped[0] in "'\"([{<":
            stripped = stripped[1:].lstrip()
            changed = True
        if stripped and stripped[-1] in "'\".,;:!?)]}>":
            stripped = stripped[:-1].rstrip()
            changed = True
    return stripped


def _write_list_validation_summary(
    *,
    summary_path: Path,
    research_file: Path,
    manifest_file: Path,
    rows: list[ListValidationRow],
    mismatch_counts: Counter[str],
) -> None:
    counts = Counter(row.status for row in rows)
    lines = [
        "stage=validate-list",
        f"research_file={research_file}",
        f"manifest_file={manifest_file}",
        f"processed={len(rows)}",
        f"matched={counts.get(STATUS_MATCHED, 0)}",
        f"missing_in_extract={counts.get(STATUS_MISSING, 0)}",
        f"archive_mismatch={counts.get(STATUS_ARCHIVE_MISMATCH, 0)}",
        f"unparseable={counts.get(STATUS_UNPARSEABLE, 0)}",
    ]

    if mismatch_counts:
        lines.append("warnings:")
        for message, count in mismatch_counts.most_common(5):
            lines.append(f"- {count}x {message}")

    lines.append("outputs:")
    lines.append(f"- {summary_path.parent / 'list_validation.csv'}")
    lines.append(f"- {summary_path}")

    atomic_write_text(summary_path, "\n".join(lines) + "\n")
