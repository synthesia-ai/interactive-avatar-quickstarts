"""Minimal frontend host: serves index.html and mints LiveKit room tokens.

Run: python server.py, then open http://localhost:8080
"""

import os
import uuid
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from livekit import api

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


async def token(request: web.Request) -> web.Response:
    # UNAUTHENTICATED, so demo-only: the server binds localhost and tokens expire in
    # 15 minutes, but a real deployment must put this behind your app's auth.
    # A fresh room per visit so the agent worker gets dispatched every time.
    room = f"avatar-quickstart-{uuid.uuid4().hex[:8]}"
    jwt = (
        api.AccessToken()  # reads LIVEKIT_API_KEY / LIVEKIT_API_SECRET from env
        .with_identity(f"user-{uuid.uuid4().hex[:8]}")
        .with_ttl(timedelta(minutes=15))  # only checked at join — sessions outlive it
        .with_grants(api.VideoGrants(room_join=True, room=room))
        # sync_streams keeps the avatar's audio and video in sync in the browser.
        # agents= dispatches the named worker into this room — required because
        # agent.py sets agent_name (explicit dispatch); the two must stay in sync.
        .with_room_config(
            api.RoomConfiguration(
                sync_streams=True,
                agents=[api.RoomAgentDispatch(agent_name="avatar-quickstart-rag")],
            )
        )
        .to_jwt()
    )
    return web.json_response({"url": os.environ["LIVEKIT_URL"], "token": jwt, "room": room})


async def index(request: web.Request) -> web.FileResponse:
    # no-store so the browser never serves a stale page during development.
    return web.FileResponse(
        Path(__file__).parent / "index.html",
        headers={"Cache-Control": "no-store"},
    )


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/token", token)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8080)  # localhost only, not your LAN
