"""Browser PCM WebSocket endpoint."""

from fastapi import APIRouter, WebSocket


def create_asr_router(websocket_path: str) -> APIRouter:
    router = APIRouter(tags=["asr"])

    @router.websocket(websocket_path)
    async def asr_stream(websocket: WebSocket) -> None:
        service = getattr(websocket.app.state, "asr_service", None)
        if service is None:
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "fatal_error",
                    "code": "disabled",
                    "message": "ASR is unavailable",
                }
            )
            await websocket.close(code=1013)
            return
        await service.run_websocket(websocket)

    return router
