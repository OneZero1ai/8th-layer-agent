"""Server-rendered HTML claim page for invite acceptance (FO-1c, #191).

Mounted at ``GET /invite/{token}`` (singular ``invite`` to keep clear
of the JSON ``/invites/{token}`` API endpoint). Renders a minimal
single-page form: the invitee enters a username + password, the form
POSTs to ``/api/v1/invites/{token}/claim``, and on success the browser
follows the redirect to ``/`` with the session cookie now set.

Why a server-rendered HTML page instead of a React component:

* The React shell (FO-1d) ships behind the auth wall — the user has
  to claim the invite *before* they have a session cookie, so the
  claim flow has to work without the React bundle loaded. A
  server-rendered page avoids the chicken-and-egg.
* The claim flow happens exactly once per invitee. Pulling in the
  React bundle to render a 30-line form is over-engineering.
* The styling can borrow brand tokens directly from the CSS variable
  set ``server/frontend/src/index.css`` defines so the page matches
  the L2 admin shell visually without sharing JS.

Style notes:

* ``data-theme="8th-layer"`` is the dark-mode default. We inline the
  custom-property block so the page works even when the static
  frontend bundle isn't mounted.
* JS is for *enhancement only* — capturing the form submit so we can
  display inline error states from the API. The form's native POST
  still works without JS (it'll just navigate to a JSON error page
  on failure, which is acceptable for the no-JS path).
"""

from __future__ import annotations

import html

import jwt as pyjwt
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from .auth import _get_jwt_secret
from .deps import get_store
from .invites import _get_by_jti, validate_invite_jwt
from .store._sqlite import SqliteStore

router = APIRouter(tags=["claim-page"])


_BASE_CSS = """
:root {
  --bg-from: #0a0612;
  --bg-via: #07070b;
  --bg-to: #040810;
  --ink: #e6e6e6;
  --ink-dim: rgba(230, 230, 230, 0.65);
  --ink-mute: rgba(230, 230, 230, 0.42);
  --rule: rgba(255, 255, 255, 0.10);
  --rule-strong: rgba(255, 255, 255, 0.18);
  --cyan: #5bd0ff;
  --violet: #a685ff;
  --rose: #ff5c7c;
  --surface: rgba(255, 255, 255, 0.025);
  --surface-raised: rgba(255, 255, 255, 0.05);
  --backdrop: radial-gradient(120% 80% at 50% 0%, #1a0e2c 0%, #0a0612 38%, #07070b 64%, #040810 100%);
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; min-height: 100vh; }
body {
  background: var(--backdrop);
  color: var(--ink);
  font-family: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px;
  line-height: 1.5;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.shell { width: 100%; max-width: 460px; }
.brand {
  font-family: "Fraunces", ui-serif, Georgia, serif;
  font-size: 22px;
  letter-spacing: 0.01em;
  margin-bottom: 28px;
  background: linear-gradient(95deg, var(--cyan) 0%, var(--violet) 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.card {
  background: var(--surface-raised);
  border: 1px solid var(--rule);
  border-radius: 14px;
  padding: 28px;
  backdrop-filter: blur(12px);
}
h1 { font-size: 20px; margin: 0 0 6px; font-weight: 600; }
.lede { color: var(--ink-dim); margin: 0 0 20px; font-size: 14px; }
.meta {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 6px 14px;
  margin-bottom: 22px;
  padding-bottom: 18px;
  border-bottom: 1px solid var(--rule);
  font-size: 13px;
}
.meta dt { color: var(--ink-mute); }
.meta dd { margin: 0; color: var(--ink); }
label {
  display: block;
  font-size: 12px;
  color: var(--ink-mute);
  margin: 14px 0 6px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
input[type=text], input[type=password] {
  width: 100%;
  padding: 10px 12px;
  background: var(--surface);
  color: var(--ink);
  border: 1px solid var(--rule-strong);
  border-radius: 8px;
  font-size: 14px;
  font-family: inherit;
}
input:focus { outline: none; border-color: var(--cyan); box-shadow: 0 0 0 2px rgba(91, 208, 255, 0.18); }
button {
  width: 100%;
  margin-top: 22px;
  padding: 11px 16px;
  background: linear-gradient(95deg, var(--cyan) 0%, var(--violet) 100%);
  color: #0a0612;
  border: 0;
  border-radius: 8px;
  font-weight: 600;
  font-size: 14px;
  cursor: pointer;
  font-family: inherit;
}
button:hover { filter: brightness(1.08); }
button:disabled { opacity: 0.5; cursor: progress; }
.error {
  margin-top: 14px;
  padding: 10px 12px;
  background: rgba(255, 92, 124, 0.08);
  border: 1px solid rgba(255, 92, 124, 0.3);
  border-radius: 8px;
  color: var(--rose);
  font-size: 13px;
  display: none;
}
.error.show { display: block; }
.invalid { padding: 28px; text-align: center; }
.invalid h1 { color: var(--rose); }
"""


def _render_invalid(reason: str, status_code: int) -> HTMLResponse:
    """Render the unified 'this link is no longer valid' page."""
    safe = html.escape(reason)
    body = f"""<!doctype html>
<html data-theme="8th-layer"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invitation unavailable | 8th-Layer.ai</title>
<style>{_BASE_CSS}</style>
</head><body>
<div class="shell">
  <div class="brand">8th-Layer.ai</div>
  <div class="card invalid">
    <h1>Invitation unavailable</h1>
    <p class="lede">{safe}</p>
    <p class="lede" style="margin-top:18px;">
      If you believe this is an error, ask the person who invited you
      to send a fresh invitation.
    </p>
  </div>
</div>
</body></html>"""
    return HTMLResponse(body, status_code=status_code)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def claim_page(
    token: str,
    store: SqliteStore = Depends(get_store),
) -> HTMLResponse:
    """Render the HTML claim form for an invite JWT.

    Lifecycle errors (expired/revoked/claimed/forgery) collapse to a
    single 'unavailable' page with the matching HTTP status — same
    discriminant logic the JSON endpoint uses, just rendered as HTML.
    """
    invite = validate_invite_jwt(token, store)
    if invite is None:
        # Match the JSON GET endpoint's per-status mapping by re-decoding.
        try:
            payload = pyjwt.decode(
                token,
                _get_jwt_secret(),
                algorithms=["HS256"],
                audience="invite",
                issuer="8th-layer.ai",
                options={"require": ["jti"]},
            )
        except pyjwt.ExpiredSignatureError:
            return _render_invalid("This invitation has expired.", 410)
        except pyjwt.PyJWTError:
            return _render_invalid("This invitation link is not valid.", 404)
        row = _get_by_jti(store, payload["jti"])
        if row is None:
            return _render_invalid("This invitation link is not valid.", 404)
        if row.revoked_at is not None:
            return _render_invalid("This invitation has been revoked.", 410)
        if row.claimed_at is not None:
            return _render_invalid("This invitation has already been claimed.", 410)
        return _render_invalid("This invitation has expired.", 410)

    # Look up the inviter's username for the metadata block.
    from .invite_routes import _lookup_username

    inviter = _lookup_username(store, invite.issued_by) or "an admin"
    safe_email = html.escape(invite.email)
    safe_role = html.escape(invite.role)
    safe_target = html.escape(invite.target_l2_id) if invite.target_l2_id else "(Enterprise scope)"
    safe_inviter = html.escape(inviter)
    # Path is interpolated into both the form action and the JS fetch
    # URL; html.escape covers the form-action surface.
    safe_token = html.escape(token, quote=True)
    # Decide where to send the user after a successful claim. Admins
    # land on /admin (FO-1d's admin shell route); everyone else on /.
    redirect_target = "/admin" if invite.role in ("enterprise_admin", "l2_admin") else "/"
    body = f"""<!doctype html>
<html data-theme="8th-layer"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accept invitation | 8th-Layer.ai</title>
<style>{_BASE_CSS}</style>
</head><body>
<div class="shell">
  <div class="brand">8th-Layer.ai</div>
  <div class="card">
    <h1>Accept your invitation</h1>
    <p class="lede">Set a password to finish creating your account. You&apos;ll sign in with your email.</p>
    <dl class="meta">
      <dt>Email</dt><dd>{safe_email}</dd>
      <dt>Role</dt><dd>{safe_role}</dd>
      <dt>Scope</dt><dd>{safe_target}</dd>
      <dt>Invited by</dt><dd>{safe_inviter}</dd>
    </dl>
    <form id="claim" method="post" action="/api/v1/invites/{safe_token}/claim" autocomplete="off">
      <!-- Hidden username for password-manager hints — agent#249. -->
      <input type="email" name="email" value="{safe_email}" autocomplete="username" readonly hidden>
      <label for="password">Password</label>
      <input id="password" name="password" type="password" required minlength="8" maxlength="128"
             autocomplete="new-password" autofocus>
      <button type="submit">Create account</button>
      <div class="error" id="err"></div>
    </form>
  </div>
</div>
<script>
(function() {{
  var form = document.getElementById('claim');
  var err = document.getElementById('err');
  var btn = form.querySelector('button');
  form.addEventListener('submit', async function(ev) {{
    ev.preventDefault();
    err.classList.remove('show');
    btn.disabled = true;
    btn.textContent = 'Creating account…';
    try {{
      var resp = await fetch(form.action, {{
        method: 'POST',
        credentials: 'same-origin',
        headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
        body: JSON.stringify({{
          password: form.password.value
        }})
      }});
      if (resp.ok) {{
        window.location.href = {redirect_target!r};
        return;
      }}
      var detail = 'Could not accept invitation.';
      try {{ var j = await resp.json(); if (j && j.detail) detail = j.detail; }} catch (_) {{}}
      err.textContent = detail;
      err.classList.add('show');
      btn.disabled = false;
      btn.textContent = 'Create account';
    }} catch (e) {{
      err.textContent = 'Network error — please try again.';
      err.classList.add('show');
      btn.disabled = false;
      btn.textContent = 'Create account';
    }}
  }});
}})();
</script>
</body></html>"""
    return HTMLResponse(body)


__all__ = ["router"]
