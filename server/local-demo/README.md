# 8th-Layer L2 — local Docker demo

The same `cq-server` container that runs on AWS Fargate, brought up on
your laptop via Docker Compose. Demonstrates three things at once:

1. **Substrate portability.** The container has no AWS-specific code —
   it runs identically on Fargate, K8s, or plain Docker. Only the deploy
   *artifacts* (CloudFormation, EFS, ALB) are AWS-specific.
2. **AIGRP peer-mesh convergence.** New L2s find each other via
   seed-peer bootstrap; periodic polling refreshes signatures.
3. **Enterprise trust boundary.** Two L2s with different
   `EnterprisePeerKey`s on the same machine can't see each other —
   401 on the wrong key.

## Three shapes

| Shape | What it brings up | What it proves | AWS cost |
|---|---|---|---|
| `acme` | acme-eng + acme-sol + orion-local-l2 | Full local mesh in `acme`; separate `orion-local` Enterprise on the same machine is invisible to `acme`; cross-Enterprise 401 boundary | $0 |
| `orion-bridge` | One local L2 joining the **AWS-hosted** orion mesh as a stub peer | Substrate portability — local Docker peer in a Fargate-hosted mesh | uses existing `test-orion-eng-l2` |
| `all` | Both above | Everything | uses existing `test-orion-eng-l2` |

## Prerequisites

- **Docker** (Desktop, Colima, OrbStack — anything OCI-compatible)
- **AWS profile** with `bedrock:InvokeModel` perms on Titan v2 (the
  `8th-layer-app` profile we use for production deploys is fine).
  Mounted read-only into containers via `~/.aws`.
- **For shape B/C**: the AWS `test-orion-eng-l2` stack must be running
  already, and the `EnterprisePeerKey` for orion must be in SSM at
  `/8l-aigrp/orion/peer-key`. `bin/init-env.sh` pulls these
  automatically.

## First-time setup

```bash
cd server/local-demo

# Generate fresh secrets and pull AWS-side params from SSM:
bash bin/init-env.sh

# Pull the cq-server image (~150 MB):
docker compose pull
```

The `init-env.sh` script writes `.env` with two distinct
`EnterprisePeerKey`s — one for `acme`, one for `orion-local`. They are
intentionally different to demonstrate the trust boundary.

## Shape A — local two-Enterprise mesh

```bash
docker compose --profile acme up -d
```

This brings up:
- `acme-engineering-l2` on `localhost:4001` (genesis node for `acme`)
- `acme-solutions-l2` on `localhost:4002` (joins `acme-engineering-l2`)
- `orion-local-l2` on `localhost:4003` (separate Enterprise, separate network)

Wait ~30 seconds for the AIGRP poll loop to converge, then verify:

```bash
bash bin/verify-mesh.sh
```

Expected output (key parts):

```
=== acme-engineering-l2  (localhost:4001) ===
[health] ok
[/aigrp/peers]  enterprise=acme  self=acme/engineering  peer_count=1
  - acme/solutions  endpoint=http://acme-solutions-l2:3000  last_sig=2026-04-30T...

=== acme-solutions-l2  (localhost:4002) ===
[health] ok
[/aigrp/peers]  enterprise=acme  self=acme/solutions  peer_count=1
  - acme/engineering  endpoint=http://acme-engineering-l2:3000  last_sig=2026-04-30T...

=== orion-local-l2  (localhost:4003) ===
[health] ok
[/aigrp/peers]  enterprise=orion-local  self=orion-local/engineering  peer_count=0

=== boundary check: ... ===
[boundary] orion-local-l2 returned HTTP 401 using acme's peer key (401 = boundary working)
```

Two converged 1-peer meshes. `orion-local` doesn't know about `acme` and
vice versa — different `EnterprisePeerKey`. Hitting `orion-local` with
acme's key returns 401.

To seed an admin user on any container:

```bash
bash bin/seed-admin.sh acme-engineering-l2
# prints: admin/<random password>
```

You can then propose a KU + run a semantic query against `localhost:4001`
just like any other cq Remote.

## Shape B — local L2 joins the AWS-hosted orion mesh

```bash
docker compose --profile orion-bridge up -d
```

This brings up `orion-bridge-l2` on `localhost:4004`. The container's
`SeedPeerUrl` points at the AWS `test-orion-eng-l2` ALB; on startup it
hits `/aigrp/hello` against that endpoint with the orion peer key.

### Stub mode (default) — no tunnel required

By default, `ORION_BRIDGE_SELF_URL` is empty in `.env`. This flags the
local L2 as a **stub L2**: it can poll peers outbound, but peers can't
poll it back (since `localhost:4004` is not reachable from the AWS L2).

Stub trade-offs:

| Capability | Full L2 | Stub L2 |
|---|---|---|
| Pulls peer signatures into local cache | ✅ | ✅ |
| Its agents query the mesh via AIGRP-pull | ✅ | ✅ |
| Peers see this L2 in their `/aigrp/peers` | ✅ | ✅ (with `endpoint=<stub>`) |
| Peers actually poll this L2's `/aigrp/signature` | ✅ | ❌ (no address to poll) |
| Peers route forward-query requests to this L2 | ✅ | ❌ (can't reach it) |
| Contributes its KUs to peers' answer mix | ✅ | ❌ |

Stub mode is the right choice when:
- You're a developer reading from the corporate mesh
- You're a customer-edge node behind NAT with no inbound port available
- You don't need the rest of the mesh asking *you* for knowledge

Verify after `docker compose --profile orion-bridge up -d`:

```bash
bash bin/verify-mesh.sh
```

Expected: `orion-bridge-l2`'s peer table includes `orion/engineering`
(the AWS L2). The AWS L2's peer table includes the bridge entry but
with `endpoint=<stub: no inbound>`.

### Full L2 mode — set up a tunnel

If you want bidirectional reachability (peers can poll *you* back), pick
a tunnel:

**Option 1 — ngrok** (~1 minute):

```bash
ngrok http 4004
# copy the https://abcd.ngrok.io URL
```

Edit `.env` and set:

```
ORION_BRIDGE_SELF_URL=https://abcd.ngrok.io
```

Then restart: `docker compose --profile orion-bridge up -d --force-recreate orion-bridge-l2`.

**Option 2 — Cloudflare Tunnel** (free, more permanent):

```bash
cloudflared tunnel --url http://localhost:4004
```

**Option 3 — Tailscale Funnel** (works on tailnet members):

```bash
tailscale funnel 4004
```

After the tunnel is up and `.env` is updated, the AWS L2 will poll the
tunnel URL on its next AIGRP cycle (≤5 min) and the bridge becomes a
full peer.

## Shape C — both at once

```bash
docker compose --profile all up -d
```

Brings up acme + orion-local + orion-bridge. Shows the full picture
on one machine: two-Enterprise local mesh, plus a stub bridge into
AWS. `bin/verify-mesh.sh` probes all four ports.

## Teardown

```bash
docker compose --profile all down -v
```

`-v` drops the volumes (each L2's SQLite DB). Skip `-v` if you want to
preserve KUs across restarts.

## What's NOT in scope for this demo

- **Cross-Enterprise discovery** between `acme` and `orion-local` —
  that's the AI-BGP protocol's job (separate spec, future work).
- **Forward-query** — the cross-L2 routing that fires when local L2
  can't satisfy a query. This demo focuses on the AIGRP peer-mesh
  layer underneath. Forward-query is the next stroke in the rollout.
- **Persistence of admin password** — `bin/seed-admin.sh` prints the
  password once; nothing stores it. For demo purposes only.

## Common questions

**Why does the orion-bridge container talk to AWS via HTTP not HTTPS?**
The current `mvp.yaml`/`l2.yaml` deploy both run plain HTTP behind the
ALB. Production behind CloudFront uses HTTPS at the edge but talks
HTTP origin → ALB. For the demo, the laptop talks HTTP directly to the
ALB. TLS hardening on the ALB is `EnableTLS=true` parameter work.

**Why do `acme-engineering-l2` and `acme-solutions-l2` share the same
JWT secret and pepper but production stacks have separate ones?** The
demo is a single-operator setup — sharing simplifies the bring-up.
Production deploys mint per-stack values automatically.

**Why does `orion-local-l2` use a different Enterprise name (`orion-local`)
than the AWS `orion`?** Intentional separation. If both used `orion`,
the local one would try to join the AWS mesh by default. We want the
local one to be a *separate* Enterprise to prove the boundary; using a
different name makes the `EnterprisePeerKey` mismatch the operative
proof rather than naming collision.
