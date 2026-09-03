"""Model package.

Importing this module registers every mapped class on ``Base.metadata``, which
is what Alembic's autogenerate and the test fixtures rely on.
"""

from app.models.alert import Alert, NotificationChannel
from app.models.base import Base
from app.models.change import (
    Change,
    ChangeActivity,
    ChangeComment,
    change_endpoints,
)
from app.models.diagnosis import Diagnosis
from app.models.endpoint import Endpoint, Environment, Tag, endpoint_tags
from app.models.incident import Incident
from app.models.monitoring import MonitoringResult, SslCertificate, WorkerHeartbeat
from app.models.system import AuditLog, SystemSetting
from app.models.user import Permission, Role, User, role_permissions

__all__ = [
    "Alert",
    "AuditLog",
    "Base",
    "Change",
    "ChangeActivity",
    "ChangeComment",
    "Diagnosis",
    "Endpoint",
    "Environment",
    "Incident",
    "MonitoringResult",
    "NotificationChannel",
    "Permission",
    "Role",
    "SslCertificate",
    "SystemSetting",
    "Tag",
    "User",
    "WorkerHeartbeat",
    "change_endpoints",
    "endpoint_tags",
    "role_permissions",
]
