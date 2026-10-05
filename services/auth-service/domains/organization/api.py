from uuid import UUID
from config import settings
from database import get_db
from deps import get_current_user, require_organization_create
from events_out import schedule_events
from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from domains.organization.schema import (InvitationAccept,
                                         InvitationAcceptResult,
                                         InvitationCreate, InvitationRead,
                                         MemberRoleUpdate, OrganizationCreate,
                                         OrganizationMemberRead,
                                         OrganizationRead, OrganizationUpdate)
from domains.organization.service import OrganizationService
from shared.email import send_organization_invitation_email, send_quietly
from shared.schemas import APIResponse
from shared.security import CurrentUser

router = APIRouter(tags=["organizations"])


@router.post(
    "", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[OrganizationRead])
async def create_organization(
    data: OrganizationCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_organization_create),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[OrganizationRead]:
    service = OrganizationService(db)
    result = await service.create(current_user.user_id, data)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.get("", response_model=APIResponse[list[OrganizationRead]])
async def list_my_organizations(
        current_user: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[OrganizationRead]]:
    result = await OrganizationService(db).list_mine(current_user.user_id)
    return APIResponse(data=result)


@router.get("/{org_id}", response_model=APIResponse[OrganizationRead])
async def get_organization(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[OrganizationRead]:
    result = await OrganizationService(db).get(org_id, current_user.user_id)
    return APIResponse(data=result)


@router.patch("/{org_id}", response_model=APIResponse[OrganizationRead])
async def update_organization(
    org_id: UUID, data: OrganizationUpdate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[OrganizationRead]:
    service = OrganizationService(db)
    result = await service.update(org_id, current_user.user_id, data)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    org_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = OrganizationService(db)
    await service.delete(org_id, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)


@router.get(
    "/{org_id}/members",
    response_model=APIResponse[list[OrganizationMemberRead]])
async def list_members(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[OrganizationMemberRead]]:
    result = await OrganizationService(db).list_members(
        org_id, current_user.user_id)
    return APIResponse(data=result)


@router.post(
    "/{org_id}/members/{user_id}/remove",
    status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    org_id: UUID, user_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = OrganizationService(db)
    await service.remove_member(org_id, current_user.user_id, user_id)
    schedule_events(background_tasks, service.pending_events)


@router.post(
    "/{org_id}/leave", status_code=status.HTTP_204_NO_CONTENT, description=(
        "Leave an organization as a non-owner member. The owner cannot leave "
        "(delete the organization instead; there is no user-initiated "
        "ownership transfer)."))
async def leave_organization(
    org_id: UUID, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    service = OrganizationService(db)
    await service.leave(org_id, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)


@router.patch(
    "/{org_id}/members/{user_id}/role",
    response_model=APIResponse[OrganizationMemberRead])
async def update_member_role(
    org_id: UUID, user_id: UUID, data: MemberRoleUpdate,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[OrganizationMemberRead]:
    service = OrganizationService(db)
    result = await service.update_member_role(
        org_id, current_user.user_id, user_id, data)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)


@router.post(
    "/{org_id}/invitations", status_code=status.HTTP_201_CREATED,
    response_model=APIResponse[InvitationRead])
async def create_invitation(
    org_id: UUID, data: InvitationCreate, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_organization_create),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[InvitationRead]:
    service = OrganizationService(db)
    result = await service.invite(org_id, current_user.user_id, data)
    schedule_events(background_tasks, service.pending_events)
    for email in service.pending_invitation_emails:
        background_tasks.add_task(
            send_quietly, "invitation_email_failed",
            send_organization_invitation_email, settings=settings, to=email.to,
            organization_name=email.organization_name,
            invitation_link=email.link)
    return APIResponse(data=result)


@router.get(
    "/{org_id}/invitations", response_model=APIResponse[list[InvitationRead]],
    description=(
        "List the open invitations of an organization (not accepted, not "
        "expired). Owner and admins only."))
async def list_invitations(
    org_id: UUID, current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[list[InvitationRead]]:
    result = await OrganizationService(db).list_invitations(
        org_id, current_user.user_id)
    return APIResponse(data=result)


@router.delete(
    "/{org_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT, description=(
        "Revoke an open invitation; its link stops working. Owner and admins "
        "only; an admin invitation can be revoked by the owner only."))
async def revoke_invitation(
    org_id: UUID, invitation_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> None:
    await OrganizationService(db).revoke_invitation(
        org_id, current_user.user_id, invitation_id)


@router.post(
    "/invitations/accept", response_model=APIResponse[InvitationAcceptResult],
    description=(
        "Accept an organization invitation. The body carries the token from "
        "the link in the invitation email (never put it in the URL path). "
        "Guests may accept; owner invitations are rejected with 409."))
async def accept_invitation(
    data: InvitationAccept, background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db, scope="function")
) -> APIResponse[InvitationAcceptResult]:
    service = OrganizationService(db)
    result = await service.accept_invitation(data.token, current_user.user_id)
    schedule_events(background_tasks, service.pending_events)
    return APIResponse(data=result)
