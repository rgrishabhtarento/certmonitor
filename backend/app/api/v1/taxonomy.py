"""Tag and environment management."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    DbSession,
    ReadEndpoints,
    WriteEnvironments,
    WriteTags,
)
from app.core.enums import AuditAction
from app.models.endpoint import Endpoint, Environment, Tag, endpoint_tags
from app.schemas.common import Message
from app.schemas.endpoint import (
    EnvironmentRead,
    EnvironmentWrite,
    TagRead,
    TagWrite,
)
from app.services import audit_service

router = APIRouter(tags=["Tags & Environments"])


# ------------------------------------------------------------------- tags
@router.get("/tags", response_model=list[TagRead], summary="List tags")
async def list_tags(
    session: DbSession,
    _user: ReadEndpoints,
    with_counts: Annotated[bool, Query()] = True,
) -> list[TagRead]:
    if not with_counts:
        rows = (
            await session.execute(select(Tag).order_by(Tag.name))
        ).scalars().all()
        return [TagRead.model_validate(row) for row in rows]

    rows = (
        await session.execute(
            select(Tag, func.count(endpoint_tags.c.endpoint_id))
            .outerjoin(endpoint_tags, endpoint_tags.c.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    result = []
    for tag, count in rows:
        model = TagRead.model_validate(tag)
        model.endpoint_count = int(count or 0)
        result.append(model)
    return result


@router.post(
    "/tags",
    response_model=TagRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tag",
)
async def create_tag(
    payload: TagWrite, user: WriteTags, request: Request, session: DbSession
) -> TagRead:
    existing = (
        await session.execute(select(Tag).where(Tag.name == payload.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A tag named '{payload.name}' already exists.",
        )

    tag = Tag(
        name=payload.name, color=payload.color, description=payload.description
    )
    session.add(tag)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A tag named '{payload.name}' already exists.",
        ) from exc

    await audit_service.record(
        session,
        action=AuditAction.TAG_CREATED.value,
        user=user,
        resource_type="tag",
        resource_id=tag.id,
        resource_name=tag.name,
        request=request,
    )
    await session.commit()
    return TagRead.model_validate(tag)


@router.put("/tags/{tag_id}", response_model=TagRead, summary="Update a tag")
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagWrite,
    user: WriteTags,
    request: Request,
    session: DbSession,
) -> TagRead:
    tag = (
        await session.execute(select(Tag).where(Tag.id == tag_id))
    ).scalar_one_or_none()
    if tag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found."
        )

    if payload.name != tag.name:
        clash = (
            await session.execute(
                select(Tag.id).where(Tag.name == payload.name, Tag.id != tag_id)
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A tag named '{payload.name}' already exists.",
            )

    before = tag.name
    tag.name = payload.name
    tag.color = payload.color
    tag.description = payload.description

    await audit_service.record(
        session,
        action=AuditAction.TAG_CREATED.value,
        user=user,
        resource_type="tag",
        resource_id=tag.id,
        resource_name=tag.name,
        details={"renamed_from": before} if before != tag.name else None,
        request=request,
    )
    await session.commit()
    return TagRead.model_validate(tag)


@router.delete(
    "/tags/{tag_id}", response_model=Message, summary="Delete a tag"
)
async def delete_tag(
    tag_id: uuid.UUID,
    user: WriteTags,
    request: Request,
    session: DbSession,
    force: Annotated[
        bool, Query(description="Delete even when endpoints still use the tag.")
    ] = False,
) -> Message:
    """Remove a tag.

    A tag still applied to endpoints is protected unless ``force`` is set, so a
    mis-click cannot silently strip categorisation off a fleet.
    """
    tag = (
        await session.execute(select(Tag).where(Tag.id == tag_id))
    ).scalar_one_or_none()
    if tag is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found."
        )

    in_use = int(
        (
            await session.execute(
                select(func.count(endpoint_tags.c.endpoint_id)).where(
                    endpoint_tags.c.tag_id == tag_id
                )
            )
        ).scalar()
        or 0
    )
    if in_use and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Tag '{tag.name}' is applied to {in_use} endpoint(s). "
                "Pass force=true to delete it anyway."
            ),
        )

    name = tag.name
    await session.delete(tag)
    await audit_service.record(
        session,
        action=AuditAction.TAG_DELETED.value,
        user=user,
        resource_type="tag",
        resource_id=tag_id,
        resource_name=name,
        details={"endpoints_affected": in_use},
        request=request,
    )
    await session.commit()
    return Message(detail=f"Tag '{name}' deleted.")


# ----------------------------------------------------------- environments
@router.get(
    "/environments",
    response_model=list[EnvironmentRead],
    summary="List environments",
)
async def list_environments(
    session: DbSession,
    _user: ReadEndpoints,
    include_inactive: Annotated[bool, Query()] = True,
) -> list[EnvironmentRead]:
    stmt = (
        select(Environment, func.count(Endpoint.id))
        .outerjoin(Endpoint, Endpoint.environment_id == Environment.id)
        .group_by(Environment.id)
        .order_by(Environment.sort_order, Environment.name)
    )
    if not include_inactive:
        stmt = stmt.where(Environment.is_active.is_(True))

    rows = (await session.execute(stmt)).all()
    result = []
    for environment, count in rows:
        model = EnvironmentRead.model_validate(environment)
        model.endpoint_count = int(count or 0)
        result.append(model)
    return result


@router.post(
    "/environments",
    response_model=EnvironmentRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an environment",
)
async def create_environment(
    payload: EnvironmentWrite,
    user: WriteEnvironments,
    request: Request,
    session: DbSession,
) -> EnvironmentRead:
    existing = (
        await session.execute(
            select(Environment).where(Environment.name == payload.name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An environment named '{payload.name}' already exists.",
        )

    environment = Environment(
        name=payload.name,
        display_name=payload.display_name or payload.name.title(),
        description=payload.description,
        color=payload.color,
        sort_order=payload.sort_order,
        is_active=payload.is_active,
    )
    session.add(environment)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.ENVIRONMENT_CREATED.value,
        user=user,
        resource_type="environment",
        resource_id=environment.id,
        resource_name=environment.name,
        request=request,
    )
    await session.commit()
    return EnvironmentRead.model_validate(environment)


@router.put(
    "/environments/{environment_id}",
    response_model=EnvironmentRead,
    summary="Update an environment",
)
async def update_environment(
    environment_id: uuid.UUID,
    payload: EnvironmentWrite,
    user: WriteEnvironments,
    request: Request,
    session: DbSession,
) -> EnvironmentRead:
    environment = (
        await session.execute(
            select(Environment).where(Environment.id == environment_id)
        )
    ).scalar_one_or_none()
    if environment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found."
        )

    if payload.name != environment.name:
        clash = (
            await session.execute(
                select(Environment.id).where(
                    Environment.name == payload.name,
                    Environment.id != environment_id,
                )
            )
        ).first()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An environment named '{payload.name}' already exists.",
            )

    environment.name = payload.name
    environment.display_name = payload.display_name or payload.name.title()
    environment.description = payload.description
    environment.color = payload.color
    environment.sort_order = payload.sort_order
    environment.is_active = payload.is_active

    await audit_service.record(
        session,
        action=AuditAction.ENVIRONMENT_UPDATED.value,
        user=user,
        resource_type="environment",
        resource_id=environment.id,
        resource_name=environment.name,
        request=request,
    )
    await session.commit()
    return EnvironmentRead.model_validate(environment)


@router.delete(
    "/environments/{environment_id}",
    response_model=Message,
    summary="Delete an environment",
)
async def delete_environment(
    environment_id: uuid.UUID,
    user: WriteEnvironments,
    request: Request,
    session: DbSession,
    force: Annotated[bool, Query()] = False,
) -> Message:
    """Delete an environment.

    Endpoints assigned to it are not deleted - their environment becomes
    unset - but the operation is still gated behind ``force`` so it cannot
    happen by accident.
    """
    environment = (
        await session.execute(
            select(Environment).where(Environment.id == environment_id)
        )
    ).scalar_one_or_none()
    if environment is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Environment not found."
        )

    in_use = int(
        (
            await session.execute(
                select(func.count(Endpoint.id)).where(
                    Endpoint.environment_id == environment_id
                )
            )
        ).scalar()
        or 0
    )
    if in_use and not force:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Environment '{environment.name}' is assigned to {in_use} "
                "endpoint(s). Pass force=true to delete it and leave those "
                "endpoints unassigned."
            ),
        )

    name = environment.name
    await session.delete(environment)
    await audit_service.record(
        session,
        action=AuditAction.ENVIRONMENT_DELETED.value,
        user=user,
        resource_type="environment",
        resource_id=environment_id,
        resource_name=name,
        details={"endpoints_unassigned": in_use},
        request=request,
    )
    await session.commit()
    return Message(detail=f"Environment '{name}' deleted.")
