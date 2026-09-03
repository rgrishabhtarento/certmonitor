"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    dashboard,
    endpoints,
    importexport,
    incidents,
    settings as settings_routes,
    taxonomy,
    users,
)

api_router = APIRouter()

# Order matters where paths could shadow each other: literal segments such as
# /endpoints/filters and /endpoints/bulk are registered inside their own
# module before the /{endpoint_id} catch-all.
api_router.include_router(auth.router)
api_router.include_router(endpoints.router)
api_router.include_router(dashboard.router)
api_router.include_router(incidents.router)
api_router.include_router(taxonomy.router)
api_router.include_router(users.router)
api_router.include_router(settings_routes.router)
api_router.include_router(importexport.router)

__all__ = ["api_router"]
