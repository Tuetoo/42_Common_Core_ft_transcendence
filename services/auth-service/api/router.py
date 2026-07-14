from fastapi import APIRouter
from api.internal import router as internal_router

from domains.auth.api import router as auth_router
from domains.organization.api import router as organization_router
from domains.social.api import router as social_router

api_router = APIRouter()
api_router.include_router(internal_router)
api_router.include_router(auth_router, prefix="/auth")
api_router.include_router(organization_router, prefix="/organizations")
api_router.include_router(social_router, prefix="/connections")
