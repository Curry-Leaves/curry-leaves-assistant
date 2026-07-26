"""App identity for outbound provider requests.

We connect Copilot / Codex by reusing their official editor/CLI OAuth clients (so a user's
existing subscription works without registering our own OAuth app). That means the provider's
consent screen shows GitHub's / OpenAI's name — we can't change that. What we CAN do is make
our own app visible in the request identity (User-Agent + an explicit client header) so a
provider's dashboards / audit logs attribute activity to Curry Leaves rather than a bare
"vscode" / "codex_cli".

Important: the provider-*gating* headers (Copilot's Editor-Version / Copilot-Integration-Id,
Codex's originator=codex_cli_rs) must stay exactly as the provider expects, or requests are
rejected. So this identity is only ever *appended* to User-Agent and added as an extra,
non-gating header — never a replacement for the gating strings.
"""
from __future__ import annotations

APP_NAME = "CurryLeavesAssistant"


def app_version() -> str:
    try:
        from importlib.metadata import version
        return version("curry-leaves-assistant")
    except Exception:
        return "0.0.0"


def app_ua() -> str:
    """The Curry Leaves identity token to append to a base User-Agent, e.g.
    ``GitHubCopilotChat/0.22.0 CurryLeavesAssistant/0.1.0``."""
    return f"{APP_NAME}/{app_version()}"


def with_app_identity(headers: dict, base_ua: str) -> dict:
    """Return a copy of ``headers`` with our app identity added: our token appended to the
    given base User-Agent, plus an explicit X-Curry-Leaves-Client header. Leaves every other
    (possibly gating) header untouched."""
    out = dict(headers)
    out["User-Agent"] = f"{base_ua} {app_ua()}".strip()
    out["X-Curry-Leaves-Client"] = app_ua()
    return out
