from uuid import UUID
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db
from deps import get_current_user
from domains.organization.schema import (
    InvitationAcceptResult, InvitationCreate, InvitationRead, MemberRoleUpdate,
    OrganizationCreate, OrganizationMemberRead, OrganizationRead,
    OrganizationRoleCreate, OrganizationRoleRead, OrganizationUpdate)
from domains.organization.service import OrganizationService
from shared.schemas.base import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["organizations"])


@router.post("", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[OrganizationRead])
async def create_organization(
    data: OrganizationCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[OrganizationRead]:
    result = await OrganizationService(db).create(current_user.user_id, data)
    return APIResponse(data=result)


@router.get("", response_model=APIResponse[list[OrganizationRead]])
async def list_my_organizations(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[OrganizationRead]]:
    result = await OrganizationService(db).list_mine(current_user.user_id)
    return APIResponse(data=result)


@router.get("/{org_id}", response_model=APIResponse[OrganizationRead])
async def get_organization(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[OrganizationRead]:
    result = await OrganizationService(db).get(org_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/{org_id}", response_model=APIResponse[OrganizationRead])
async def update_organization(
    org_id: UUID, data: OrganizationUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[OrganizationRead]:
    result = await OrganizationService(db).update(
        org_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> None:
    await OrganizationService(db).delete(org_id, current_user.user_id)


@router.get("/{org_id}/members",
            response_model=APIResponse[list[OrganizationMemberRead]])
async def list_members(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[OrganizationMemberRead]]:
    result = await OrganizationService(db).list_members(
        org_id, current_user.user_id)
    return APIResponse(data=result)


@router.post("/{org_id}/members/{user_id}/remove",
             status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(org_id: UUID, user_id: UUID,
                        current_user: CurrentUser = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)) -> None:
    await OrganizationService(db).remove_member(org_id, current_user.user_id,
                                                user_id)


@router.patch("/{org_id}/members/{user_id}/role",
              response_model=APIResponse[OrganizationMemberRead])
async def update_member_role(
    org_id: UUID, user_id: UUID, data: MemberRoleUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[OrganizationMemberRead]:
    result = await OrganizationService(db).update_member_role(
        org_id, current_user.user_id, user_id, data)
    return APIResponse(data=result)


@router.post("/{org_id}/invitations", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[InvitationRead])
async def create_invitation(
    org_id: UUID, data: InvitationCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[InvitationRead]:
    result = await OrganizationService(db).invite(
        org_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.post("/invitations/{token}/accept",
             response_model=APIResponse[InvitationAcceptResult])
async def accept_invitation(
    token: str, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[InvitationAcceptResult]:
    result = await OrganizationService(db).accept_invitation(
        token, current_user.user_id)
    return APIResponse(data=result)


@router.post("/{org_id}/roles", status_code=status.HTTP_201_CREATED,
             response_model=APIResponse[OrganizationRoleRead])
async def create_role(
    org_id: UUID, data: OrganizationRoleCreate,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[OrganizationRoleRead]:
    result = await OrganizationService(db).create_role(
        org_id, current_user.user_id, data)
    return APIResponse(data=result)


@router.get("/{org_id}/roles",
            response_model=APIResponse[list[OrganizationRoleRead]])
async def list_roles(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> APIResponse[list[OrganizationRoleRead]]:
    result = await OrganizationService(db).list_roles(
        org_id, current_user.user_id)
    return APIResponse(data=result)
