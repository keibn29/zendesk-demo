from fastapi import APIRouter

from app.api.endpoints import health, slack, zendesk

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(slack.router)
api_router.include_router(zendesk.router)