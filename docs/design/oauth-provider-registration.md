# Registering your own OAuth apps (Copilot & Codex)

Curry Leaves connects GitHub Copilot and OpenAI Codex by **reusing the official editor/CLI
OAuth clients**. That's why the consent screen shows *"GitHub Copilot Plugin by GitHub"* (or
OpenAI's Codex app) instead of *Curry Leaves* — the page is hosted by GitHub/OpenAI and shows
whoever owns the OAuth client id we send. Nothing we put in request headers changes that page.

To brand the consent screen you must register **your own** OAuth app with each provider and
swap in its client id. Below are the steps, matched to how each flow works — **plus the hard
caveat for each**, because branding the page and keeping model access working are two different
things.

Both client ids are already **env-overridable — you do not need to edit code.** Copilot uses
Curry Leaves' own registered GitHub OAuth App by default; Codex uses OpenAI's own Codex CLI
client. Each has a dedicated environment variable that overrides it.

Note that Copilot **already ships a Curry-Leaves-branded client id** — the registration this
document describes was done. Copilot API access additionally depends on editor-identity headers
that must stay as GitHub expects; Curry Leaves appends its own identity only to a non-gating
client header (and its user agent), so activity attributes to Curry Leaves without breaking the
gate.

Auth is also stored differently than an older draft of this doc implied: the GitHub token lives
inside the app's settings, not a separate credential file. And connect deliberately does **not**
verify against GitHub's first-party token-exchange endpoint — that endpoint is gated to GitHub's
first-party clients, so a custom client id fails there even when the raw token works fine against
the chat API. The token is stored raw and exchanged lazily at request time, which is what makes
custom client ids usable at all.

---

## 1. GitHub Copilot (device flow)

**How our flow works:** a device-code grant — the user is shown a code to enter on GitHub, we
poll for the access token, then exchange the GitHub token for a Copilot bearer.

### Register your own GitHub OAuth App
1. Go to **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App**
   (`https://github.com/settings/developers`).
2. Fill in:
   - **Application name:** `Curry Leaves` (this is what the consent page shows)
   - **Homepage URL:** your repo/site.
   - **Application description:** shown on the consent page — e.g. "Voice & meeting assistant."
   - **Authorization callback URL:** required by the form, but the **device flow doesn't use
     it** — put your homepage URL.
   - **Upload a logo** (the icon shown on the consent page).
3. **Enable Device Flow** (checkbox in the app settings) — required, or the device-code grant
   is rejected as an unsupported grant type.
4. Copy the app's **Client ID**.
5. Point the Copilot client id at your new id via its environment variable — see "Using your
   own client id" below.

### ⚠️ Critical caveat — this likely brands the page but breaks Copilot models
The Copilot API is gated to GitHub's **first-party editor OAuth clients**. A token minted by
*your* OAuth app can authenticate the GitHub user, but the Copilot token exchange may return
**403/404** ("no access"). In other words: your consent page will say "Curry Leaves", but the
run may then fail at the Copilot-token step and models won't load.

**Verify before shipping:** register the app, swap the id, run Connect, and watch whether the
Copilot token exchange returns a token. If it fails, Copilot branding is not viable without a
GitHub partnership — keep the current (GitHub-branded) client for Copilot.

---

## 2. OpenAI Codex (authorization-code flow, localhost redirect)

**How our flow works:** a standard OAuth authorization-code flow with PKCE — we open OpenAI's
authorize page, a one-shot local server catches the redirect back to localhost, and we exchange
the code for tokens.

### Register your own OpenAI OAuth client
1. Codex uses ChatGPT/OpenAI's OAuth, which is **not a self-serve developer registration** like
   GitHub's — OpenAI does not currently offer a public console to create an OAuth app against
   their auth server with an arbitrary redirect URI. The default Codex client is OpenAI's own
   Codex CLI.
2. If you have an OpenAI **platform** account, the closest self-serve options are:
   - **API key** (already supported here as the OpenAI provider) — no OAuth, no consent page,
     and it bills your API account. This is the clean "your own identity" path.
   - **Enterprise/partner OAuth** — only if you have a registered OpenAI OAuth integration; then
     point the Codex client id, issuer, redirect, and scope at your integration.

### ⚠️ Critical caveat
Like Copilot, the ChatGPT-subscription (Codex) backend is gated to OpenAI's own client. A
third-party OAuth client generally **cannot** call the Codex responses endpoint on a user's
ChatGPT subscription. So there's no supported way to brand this consent page while still using
someone's ChatGPT plan.
**Recommended:** for a branded, first-party experience with OpenAI models, use the **OpenAI
API-key provider** instead of Codex.

---

## Using your own client id (already supported)

Both client ids read from the environment, so dropping in your own app needs **no code change**
— just set the provider's client-id environment variable before launching.

Auth wiring is set up at boot, so this needs a **full restart** — hot reload won't pick it up.

---

## Bottom line

| Provider | Can brand consent page? | Keeps subscription access? | Recommendation |
|---|---|---|---|
| **Copilot** | **Yes — already done.** Ships a Curry-Leaves GitHub OAuth App id | Yes — the raw GitHub token is stored and exchanged lazily, sidestepping the first-party-gated exchange endpoint | Nothing to do; override via env if you want your own |
| **Codex** | No self-serve path against OpenAI's auth server | No — gated to OpenAI's own client | Use the **OpenAI API-key** provider for first-party branding |

The genuinely brandable, "your own identity" providers are the **keyed** ones — Anthropic,
OpenAI, Google, Groq, Together, OpenRouter, DeepSeek, Mistral, xAI, Perplexity, and any custom
OpenAI-compatible endpoint you add in Settings. They use *your* API key, so there's no
third-party consent screen at all — the request is already yours. OAuth is only the two special
cases above; the rest of the catalog never needs it. See the live provider catalog in Settings
for the current list.
