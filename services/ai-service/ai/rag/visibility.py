from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
import structlog
from qdrant_client.http import models as qmodels
from shared.exceptions import UpstreamServiceError
from shared.schemas import SYSTEM_DOCUMENT_OWNER_ID

logger = structlog.get_logger(__name__)


def _norm_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return text


@dataclass(frozen=True)
class ChunkVisibility:
    owner_id: str | None = None
    organization_id: str | None = None
    is_system: bool = False

    @classmethod
    def system(cls) -> "ChunkVisibility":
        return cls(is_system=True)

    @classmethod
    def for_owner(
            cls, owner_id: Any,
            organization_id: Any = None) -> "ChunkVisibility":
        return cls(
            owner_id=_norm_id(owner_id),
            organization_id=_norm_id(organization_id), is_system=False)

    @classmethod
    def from_upload_event(
            cls, owner_id: Any, organization_id: Any) -> "ChunkVisibility":
        owner = _norm_id(owner_id)
        if owner == str(SYSTEM_DOCUMENT_OWNER_ID):
            return cls.system()
        if owner is None:
            logger.warning(
                "rag_visibility.upload_event_without_owner",
                organization_id=organization_id)
        return cls(
            owner_id=owner, organization_id=_norm_id(organization_id),
            is_system=False)

    @classmethod
    def from_payload(
            cls, payload: Mapping[str, Any]) -> "ChunkVisibility | None":
        owner = _norm_id(payload.get("owner_id"))
        is_system = payload.get("is_system")
        if owner is None and is_system is None:
            return None
        return cls(
            owner_id=owner,
            organization_id=_norm_id(payload.get("organization_id")),
            is_system=bool(is_system))

    def payload(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "organization_id": self.organization_id,
            "is_system": self.is_system
        }


@dataclass(frozen=True)
class VisibilityScope:
    user_id: str | None = None
    organization_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def for_user(
            cls, user_id: Any,
            organization_ids: Iterable[Any] = ()) -> "VisibilityScope":
        user = _norm_id(user_id)
        if user is None:
            return SYSTEM_ONLY_SCOPE
        orgs = frozenset(
            org for org in (_norm_id(o) for o in organization_ids) if org)
        return cls(user_id=user, organization_ids=orgs)

    async def resolve(self) -> "VisibilityScope":
        return self

    def allows(self, visibility: ChunkVisibility | None) -> bool:
        if visibility is None or visibility.is_system:
            return True
        if self.user_id is not None and visibility.owner_id == self.user_id:
            return True
        return (
            visibility.organization_id is not None
            and visibility.organization_id in self.organization_ids)

    def allows_payload(self, payload: Mapping[str, Any]) -> bool:
        return self.allows(ChunkVisibility.from_payload(payload))

    def qdrant_condition(self) -> qmodels.Filter:
        should: list[qmodels.Condition] = [
            qmodels.FieldCondition(
                key="is_system", match=qmodels.MatchValue(value=True)),
            qmodels.Filter(
                must=[
                    qmodels.IsEmptyCondition(
                        is_empty=qmodels.PayloadField(key="owner_id")),
                    qmodels.IsEmptyCondition(
                        is_empty=qmodels.PayloadField(key="is_system"))
                ])
        ]
        if self.user_id is not None:
            should.append(
                qmodels.FieldCondition(
                    key="owner_id",
                    match=qmodels.MatchValue(value=self.user_id)))
        if self.organization_ids:
            should.append(
                qmodels.FieldCondition(
                    key="organization_id",
                    match=qmodels.MatchAny(any=sorted(self.organization_ids))))
        return qmodels.Filter(should=should)


SYSTEM_ONLY_SCOPE = VisibilityScope()


async def resolve_visibility_scope(user_id: str | None) -> VisibilityScope:
    user = _norm_id(user_id)
    if user is None:
        return SYSTEM_ONLY_SCOPE
    from clients import auth_client

    try:
        org_ids = await auth_client.list_user_organization_ids(user)
    except UpstreamServiceError as exc:
        logger.warning(
            "rag_visibility.org_lookup_failed_fail_closed", user_id=user,
            error=str(exc)[:200])
        return VisibilityScope.for_user(user)
    return VisibilityScope.for_user(user, org_ids)


class LazyVisibilityScope:

    def __init__(self, user_id: str | None) -> None:
        self.user_id = user_id or None
        self._scope: VisibilityScope | None = None

    async def resolve(self) -> VisibilityScope:
        if self._scope is None:
            self._scope = await resolve_visibility_scope(self.user_id)
        return self._scope


RetrievalVisibility = VisibilityScope | LazyVisibilityScope


def visibility_for_state(state: Any) -> LazyVisibilityScope:
    return LazyVisibilityScope(getattr(state, "user_id", None) or None)
