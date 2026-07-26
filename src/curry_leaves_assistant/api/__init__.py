"""HTTP surface — one APIRouter per domain, aggregated by app.py.

To add a new domain: create a module here with a top-level `router = APIRouter()`
and append it to ALL_ROUTERS.
"""
from . import (
    agents,
    artifacts,
    auth,
    backup,
    chat,
    dashboard,
    files,
    knowledge,
    providers,
    recordings,
    search,
    settings,
    skills,
    system,
    tasks,
    templates,
    tools,
    transcription,
    wakeword,
    ws,
)

ALL_ROUTERS = [
    system.router,
    auth.router,
    recordings.router,
    knowledge.router,
    skills.router,
    templates.router,
    settings.router,
    providers.router,
    tools.router,
    agents.router,
    dashboard.router,
    tasks.router,
    chat.router,
    transcription.router,
    wakeword.router,
    artifacts.router,
    search.router,
    files.router,
    backup.router,
    ws.router,
]
