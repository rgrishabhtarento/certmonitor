"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.deps import ChangeManagementEnabled
from app.api.v1 import (
    auth,
    changes,
    dashboard,
    endpoints,
    importexport,
    incidents,
    rca,
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
# RCA and the local intelligence endpoints. Registered after incidents so the
# literal /incidents/{id}/rca paths resolve against the RCA router.
api_router.include_router(rca.router)
# Every route in this module belongs to the change-management feature, so the
# gate goes on the include. The RCA module cannot be gated this way: its
# router also carries the /intelligence routes the dashboard needs, so those
# routes are gated individually.
api_router.include_router(changes.router, dependencies=[ChangeManagementEnabled])
api_router.include_router(taxonomy.router)
api_router.include_router(users.router)
api_router.include_router(settings_routes.router)
api_router.include_router(importexport.router)

__all__ = ["api_router"]
