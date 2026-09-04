"""Bulk endpoint import (CSV/Excel) and configuration export."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response

from app.api.deps import (
    DbSession,
    ExportEndpoints,
    ImportEndpoints,
    RuntimeConfig,
    parse_uuid_list,
    split_csv_param,
)
from app.core.config import settings
from app.core.enums import AuditAction
from app.core.logging import get_logger
from app.schemas.dashboard import (
    ImportConfirmRequest,
    ImportPreviewResponse,
    ImportResultResponse,
    ImportRowPreview,
)
from app.services import (
    audit_service,
    endpoint_service,
    import_export_service,
    preview_store,
)
from app.services.import_export_service import (
    ImportAnalysis,
    ImportError_,
    RowResult,
)

logger = get_logger(__name__)

router = APIRouter(tags=["Import & Export"])

_ALLOWED_SUFFIXES = (".csv", ".txt", ".tsv", ".xlsx", ".xlsm")


@router.get(
    "/import/template",
    summary="Download a CSV template",
    response_class=Response,
)
async def import_template(_user: ImportEndpoints) -> Response:
    """A filled-in example file, so the expected columns are unambiguous."""
    return Response(
        content=import_export_service.csv_template(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="infrasight-import-template.csv"'
        },
    )


@router.post(
    "/import",
    response_model=ImportPreviewResponse,
    summary="Upload and validate a CSV/Excel file (nothing is created yet)",
)
async def preview_import(
    user: ImportEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
    file: Annotated[UploadFile, File(description="CSV or .xlsx file")],
) -> ImportPreviewResponse:
    """Validate an upload and return a per-row preview.

    Nothing is written here. The response carries a token that
    ``POST /api/import/confirm`` exchanges for an actual import, which is what
    lets an operator review validation errors and duplicates first.
    """
    filename = file.filename or "upload.csv"
    if not filename.lower().endswith(_ALLOWED_SUFFIXES):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "Unsupported file type. Upload a .csv or .xlsx file "
                f"(received '{filename}')."
            ),
        )

    data = await file.read()
    await file.close()

    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="The file is empty."
        )
    if len(data) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                "File is larger than "
                f"{settings.MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )

    try:
        analysis = await import_export_service.analyse(
            session,
            filename=filename,
            data=data,
            config=config,
            max_rows=settings.MAX_IMPORT_ROWS,
        )
    except ImportError_ as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    payload = analysis.as_dict()
    token, expires_at = await preview_store.save(
        {
            "filename": analysis.filename,
            "total_rows": analysis.total_rows,
            "detected_columns": analysis.detected_columns,
            "unknown_columns": analysis.unknown_columns,
            "rows": [
                {
                    "row_number": row.row_number,
                    "raw": row.raw,
                    "payload": row.payload,
                    "errors": row.errors,
                    "warnings": row.warnings,
                    "duplicate_of": row.duplicate_of,
                }
                for row in analysis.rows
            ],
        }
    )

    logger.info(
        "import_previewed",
        filename=filename,
        total_rows=analysis.total_rows,
        valid=len(analysis.valid_rows),
        invalid=len(analysis.invalid_rows),
        by=user.username,
    )

    return ImportPreviewResponse(
        token=token,
        expires_at=expires_at,
        filename=payload["filename"],
        total_rows=payload["total_rows"],
        valid_count=payload["valid_count"],
        invalid_count=payload["invalid_count"],
        duplicate_count=payload["duplicate_count"],
        detected_columns=payload["detected_columns"],
        unknown_columns=payload["unknown_columns"],
        file_errors=payload["file_errors"],
        rows=[ImportRowPreview.model_validate(row) for row in payload["rows"]],
    )


@router.post(
    "/import/confirm",
    response_model=ImportResultResponse,
    summary="Create the endpoints from a validated preview",
)
async def confirm_import(
    payload: ImportConfirmRequest,
    user: ImportEndpoints,
    request: Request,
    session: DbSession,
    config: RuntimeConfig,
) -> ImportResultResponse:
    """Commit a previously validated preview.

    Each row is created in its own savepoint, so a row that fails at write
    time (a race with a concurrent create, say) is reported without discarding
    the rows that succeeded.
    """
    stored = await preview_store.load(payload.token)
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                "This import preview has expired or was already used. "
                "Upload the file again."
            ),
        )

    analysis = ImportAnalysis(
        filename=stored.get("filename", "upload"),
        total_rows=int(stored.get("total_rows", 0)),
        detected_columns=list(stored.get("detected_columns", [])),
        unknown_columns=list(stored.get("unknown_columns", [])),
    )
    for row in stored.get("rows", []):
        analysis.rows.append(
            RowResult(
                row_number=int(row["row_number"]),
                raw=row.get("raw") or {},
                payload=row.get("payload"),
                errors=list(row.get("errors") or []),
                warnings=list(row.get("warnings") or []),
                duplicate_of=row.get("duplicate_of"),
            )
        )

    # Re-check duplicates: endpoints may have been created between the preview
    # and the confirmation.
    await import_export_service.recheck_duplicates(session, analysis)

    outcome = await import_export_service.commit(
        session,
        analysis,
        config=config,
        created_by_id=user.id,
        only_rows=payload.row_numbers,
    )

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINTS_IMPORTED.value,
        user=user,
        resource_type="endpoint",
        details={
            "filename": analysis.filename,
            "created": len(outcome.created),
            "failed": len(outcome.failed),
            "skipped": len(outcome.skipped),
        },
        request=request,
    )
    await session.commit()
    await preview_store.discard(payload.token)

    return ImportResultResponse(**outcome.as_dict())


@router.get(
    "/export",
    summary="Export the monitoring configuration",
    response_class=Response,
)
async def export_endpoints(
    user: ExportEndpoints,
    request: Request,
    session: DbSession,
    export_format: Annotated[
        str, Query(alias="format", pattern="^(csv|xlsx)$")
    ] = "csv",
    search: Annotated[str | None, Query()] = None,
    environment: Annotated[list[str] | None, Query()] = None,
    tag: Annotated[list[str] | None, Query()] = None,
    endpoint_status: Annotated[list[str] | None, Query(alias="status")] = None,
    monitoring_enabled: Annotated[bool | None, Query()] = None,
) -> Response:
    """Export endpoints as CSV or Excel.

    The same filters as the endpoint list apply, so "export what I am looking
    at" works. Credentials are never included - only the authentication *type*
    is exported, because the file leaves the application.
    """
    stmt = endpoint_service.base_query()
    stmt = endpoint_service.apply_search(stmt, search)
    stmt = endpoint_service.apply_filters(
        stmt,
        environment_ids=parse_uuid_list(environment),
        tag_ids=parse_uuid_list(tag),
        statuses=split_csv_param(endpoint_status),
        monitoring_enabled=monitoring_enabled,
    )
    stmt = endpoint_service.apply_sort(stmt, "name", "asc")

    rows = list(
        (await session.execute(stmt)).scalars().unique().all()
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if export_format == "xlsx":
        content = import_export_service.export_excel(rows)
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        filename = f"infrasight-endpoints-{timestamp}.xlsx"
    else:
        content = import_export_service.export_csv(rows)
        media_type = "text/csv; charset=utf-8"
        filename = f"infrasight-endpoints-{timestamp}.csv"

    await audit_service.record(
        session,
        action=AuditAction.ENDPOINTS_EXPORTED.value,
        user=user,
        resource_type="endpoint",
        details={"format": export_format, "rows": len(rows)},
        request=request,
    )
    await session.commit()

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
