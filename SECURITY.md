# Security Policy

Curry Leaves is built to keep your data **on your own machine** — recordings, transcripts,
knowledge, and memory all live as plain files under `~/.curry-leaves/`, and nothing is sent to a
cloud service except the AI provider you explicitly configure. We take security reports seriously.

## Supported versions

Security fixes land on the latest released version. Please upgrade to the most recent
`curry-leaves-assistant` release before reporting, and confirm the issue still reproduces there.

| Version | Supported |
|---|---|
| Latest release | ✅ |
| Older releases  | ❌ (please upgrade) |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately through either channel:

- **GitHub Security Advisories** — [open a private report](https://github.com/Curry-Leaves/curry-leaves-assistant/security/advisories/new)
  (preferred; keeps the discussion attached to the repo).

Please include, as far as you can:

- A description of the vulnerability and its impact.
- The version, OS, AI provider/model, and how the app was installed (pip / Docker / source).
- Step-by-step instructions or a minimal proof of concept to reproduce it.
- Any relevant logs (scrub secrets — provider API keys, PINs, tokens).

### What to expect

- **Acknowledgement** within few business days.
- An initial assessment and severity triage, and we'll keep you updated on progress.

Please give us reasonable time to release a fix before any public disclosure.

## Scope

Things especially worth reporting for a local-first app like this:

- Remote code execution, path traversal, or arbitrary file read/write via the API or WebSocket.
- Authentication/authorization bypass (PIN gate, session/token handling).
- Leakage of secrets — provider API keys, OAuth tokens, or the PIN — via logs, traces, or artifacts.
- The revocable public-artifact links exposing more than the shared artifact, or being guessable.
- SSRF or injection reachable through configured MCP servers or an OpenAI-compatible base URL.
- Any way a to-do, recording, or agent instruction could cause the agent pool to exfiltrate data
  off-machine without the user's configured provider.

### Out of scope

- Vulnerabilities in your configured third-party AI provider, MCP server, or model — report those
  upstream.
- Findings that require an attacker to already have full access to the machine and the
  `~/.curry-leaves/` directory (the data is intentionally plain files the owner can read).
- Missing hardening that has no demonstrated impact (e.g. best-practice headers with no exploit).

## Handling your own data

Curry Leaves stores everything locally in plain, human-readable files. Treat `~/.curry-leaves/` as
sensitive — it holds transcripts, your knowledge base, and, in config, provider credentials. Back it
up and share it as carefully as you would any personal notes.
