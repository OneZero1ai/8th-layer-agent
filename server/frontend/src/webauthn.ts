/**
 * Tiny WebAuthn helpers (FO-1d).
 *
 * The full @simplewebauthn/browser package is overkill for our needs — we
 * only call navigator.credentials.create() / .get() once each, and the
 * server hands us already-shaped PublicKeyCredentialOptions. The tricky
 * bits are the base64url ↔ ArrayBuffer round-trips on the wire, which
 * this module handles in ~50 LOC.
 *
 * Why not @simplewebauthn/browser: dropping a peer dep keeps the lock
 * file shallow and avoids version-skew with the backend's py_webauthn
 * helper module. We control the wire shape both sides.
 */

// --- base64url ↔ ArrayBuffer ---------------------------------------------

export function base64urlToBytes(b64u: string): Uint8Array<ArrayBuffer> {
  const padded = b64u + "=".repeat((4 - (b64u.length % 4)) % 4)
  const b64 = padded.replace(/-/g, "+").replace(/_/g, "/")
  const bin = atob(b64)
  // Use a fresh ArrayBuffer (not ArrayBufferLike — DOM types want
  // BufferSource = ArrayBufferView<ArrayBuffer>, not the wider
  // SharedArrayBuffer-allowing alias). Otherwise tsc 5.9 rejects.
  const buf = new ArrayBuffer(bin.length)
  const out = new Uint8Array(buf)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

export function bytesToBase64url(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let bin = ""
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
}

// --- option-shape converters ---------------------------------------------

interface ServerCredentialDescriptor {
  type: string
  id: string
  transports?: string[]
}

interface ServerLoginOptions {
  challenge: string
  rpId?: string
  timeout?: number
  allowCredentials?: ServerCredentialDescriptor[]
  userVerification?: UserVerificationRequirement
}

function decodeAllow(
  list: ServerCredentialDescriptor[] | undefined,
): PublicKeyCredentialDescriptor[] | undefined {
  if (!list) return undefined
  return list.map((c) => ({
    type: "public-key",
    id: base64urlToBytes(c.id),
    transports: c.transports as AuthenticatorTransport[] | undefined,
  }))
}

// --- assertion (login) ---------------------------------------------------

export async function passkeyLogin(
  username: string,
): Promise<{ token: string; username: string; sign_count: number }> {
  // 1. Begin — server hands us assertion options.
  const beginResp = await fetch("/api/v1/auth/passkey/login/begin", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username }),
  })
  if (!beginResp.ok) {
    const body = await beginResp.json().catch(() => ({}))
    throw new Error(
      body.detail || `passkey/login/begin failed: ${beginResp.status}`,
    )
  }
  const opts: ServerLoginOptions = await beginResp.json()

  // 2. Convert wire shape → browser API shape.
  const publicKey: PublicKeyCredentialRequestOptions = {
    challenge: base64urlToBytes(opts.challenge),
    rpId: opts.rpId,
    timeout: opts.timeout,
    allowCredentials: decodeAllow(opts.allowCredentials),
    userVerification: opts.userVerification,
  }

  // 3. Browser ceremony.
  const cred = (await navigator.credentials.get({
    publicKey,
  })) as PublicKeyCredential | null
  if (!cred) throw new Error("passkey ceremony was cancelled")

  const assertion = cred.response as AuthenticatorAssertionResponse

  // 4. Finish — encode the assertion + send.
  const credentialJson = {
    id: cred.id,
    rawId: bytesToBase64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bytesToBase64url(assertion.clientDataJSON),
      authenticatorData: bytesToBase64url(assertion.authenticatorData),
      signature: bytesToBase64url(assertion.signature),
      userHandle: assertion.userHandle
        ? bytesToBase64url(assertion.userHandle)
        : null,
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  }

  const finishResp = await fetch("/api/v1/auth/passkey/login/finish", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, credential: credentialJson }),
  })
  if (!finishResp.ok) {
    const body = await finishResp.json().catch(() => ({}))
    throw new Error(
      body.detail || `passkey/login/finish failed: ${finishResp.status}`,
    )
  }
  return finishResp.json()
}
