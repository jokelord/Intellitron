"""Provider-neutral request lifecycle status endpoint."""

from fastapi import APIRouter, Request, Response

router = APIRouter(tags=["request-status"])


@router.get("/v1/request-status/{request_id}")
async def get_request_status(
    request_id: str,
    raw_request: Request,
    response: Response,
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    registry = raw_request.app.state.request_lifecycle_registry
    return {"request_id": request_id, "status": registry.get_status(request_id)}
