"""Shared response envelopes and pagination helpers."""

from __future__ import annotations

import math
from typing import Any, Generic, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for schemas populated from SQLAlchemy rows."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class Message(BaseModel):
    detail: str
    code: str | None = None


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None
    fields: dict[str, str] | None = Field(
        default=None, description="Per-field validation messages, when applicable."
    )


class PageMeta(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[T]):
    """Paginated collection.

    Every list endpoint returns this shape so the UI never has to load an
    unbounded number of rows.
    """

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(
        cls, items: Sequence[T], *, total: int, page: int, page_size: int
    ) -> "Page[T]":
        pages = max(1, math.ceil(total / page_size)) if page_size else 1
        return cls(
            items=list(items),
            meta=PageMeta(
                total=total,
                page=page,
                page_size=page_size,
                pages=pages,
                has_next=page < pages,
                has_previous=page > 1,
            ),
        )


class PaginationParams(BaseModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class BulkActionResult(BaseModel):
    requested: int
    succeeded: int
    failed: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
