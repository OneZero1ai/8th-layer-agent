# Transactional mail migration (Decision 34)

How to migrate an existing L2 from the pre-Decision-34 self-SES posture
to the central transactional-mail service.

## Why

Pre-Decision-34, every L2 called SES from its own AWS account using
the L2 task role's `ses:SendEmail` IAM permission. Customer L2s had
to verify `8th-layer.ai` as a domain identity in their account, exit
SES sandbox, and own the DKIM + SPF DNS records — none of which is
plausible for a marketplace one-click deploy.

Decision 34 (in `8th-layer-core/docs/decisions/34-...`) moves all
transactional mail to a central service running on the control-plane
deployment. L2s post HMAC-signed JSON to
`https://directory.8th-layer.ai/api/v1/transactional/send`; the
control plane dispatches via SES from account `124074140789`, where
DKIM + SPF + bounce/complaint handling are already wired.

## What changes per L2

Three things land on each existing L2:

1. **A `tx_send_key` SSM parameter** in the L2's own AWS account, at
   `/8th-layer/l2/{enterprise}/{group}/tx_send_key`. Same key value
   is written into the control-plane SSM under the same path (for the
   HMAC resolver to verify with).
2. **A CFN stack update** that adds the `TX_SEND_KEY` task secret
   binding + `TX_SEND_BASE_URL` env var + drops the `SesSendInvites`
   IAM policy.
3. **A cq-server image bump** to a build that includes the
   HTTP-client-based `email_sender.py` (Decision 34 build).

Steps 1 and 3 unblock new sends. Step 2 is the IAM cleanup; it can
land in a separate pass with the standard `update-stack` discipline
(no image bump required).

## Migration sequence

### Pre-flight

1. Confirm the central service is up:
   ```
   curl -sS https://directory.8th-layer.ai/api/v1/health
   ```
2. Confirm the control-plane SES is configured:
   ```
   aws --profile 8th-layer-app ses get-account-sending-enabled --region us-east-1
   ```
3. Confirm the bounce/complaint SNS topics exist in 124074140789:
   ```
   aws --profile 8th-layer-app sns list-topics --region us-east-1 \
       | grep -E "ses-(bounces|complaints)"
   ```

### Per-L2 backfill

Mint the per-L2 key and write to both accounts:

```
./bin/backfill-tx-keys.py \
    --aws-profile <l2-aws-profile> \
    --control-plane-profile 8th-layer-app \
    --enterprise <ent> \
    --group <grp> \
    --execute
```

Verify:

```
aws --profile <l2-aws-profile> ssm get-parameter \
    --name /8th-layer/l2/<ent>/<grp>/tx_send_key \
    --with-decryption | jq -r .Parameter.Value
```

### Roll the image (image-bump-only deploy)

```
aws cloudformation update-stack \
    --profile <l2-aws-profile> \
    --stack-name <l2-stack> \
    --use-previous-template \
    --parameters \
        ParameterKey=CqServerImage,ParameterValue=public.ecr.aws/w2i0e4m6/cq-server:<decision-34-build> \
        ParameterKey=...rest-with-UsePreviousValue
```

At this point the L2 task picks up the new image; the new
`email_sender.py` reads `TX_SEND_KEY` from env (still empty —
parameter exists but the task definition hasn't been updated to bind
it) and falls back to legacy SES IF `CQ_EMAIL_LEGACY_SES=1` is set,
or errors otherwise.

To enable the legacy fallback during the migration window:

```
# Edit the task definition out-of-band (or set CQ_EMAIL_LEGACY_SES
# in the CFN template as an interim override).
```

### Stack-template update (drops SES IAM + binds TX_SEND_KEY)

```
aws cloudformation update-stack \
    --profile <l2-aws-profile> \
    --stack-name <l2-stack> \
    --template-body file://deploy/aws/marketplace-l2.yaml \
    --parameters \
        ParameterKey=CqServerImage,ParameterValue=<decision-34-build> \
        ...rest-with-UsePreviousValue \
    --capabilities CAPABILITY_NAMED_IAM
```

After this update:

* `TX_SEND_KEY` is bound from SSM at task start.
* The task role has no `ses:SendEmail` IAM at all.
* The L2 sends via HTTP. Verify with the next invite-mint.

### Verify

Mint a test invite from the L2's admin shell to an external address
you control. Watch:

* The L2's task logs — `email_sender: HTTP transport error` would
  surface here if the central service is unreachable.
* The control-plane task logs —
  `transactional/send ok l2_id=<ent>/<grp> ...` lines per send.
* CloudWatch SES metrics in 124074140789 (Send count should tick up).
* The recipient inbox.

## Estimated time per L2

| Phase | Time |
|--|--|
| Backfill (single L2) | ~30 seconds |
| Image-bump update-stack | ~3 minutes (ECS rolling-restart) |
| Template-change update-stack | ~5 minutes (IAM change + new task-def revision) |
| Verification send | ~1 minute |
| **Total per L2** | **~10 minutes** |

For the 11 live L2s, plan ~2 hours of focused operator time. Bulk
backfill with `--from-file` reduces that to ~1.5 hours since the
backfill phase becomes one command.

## L2s in scope

(Refresh this list before running — these are the L2s known as of
2026-05-20.)

* eng (engineering / 8th-layer)
* sga (sga / 8th-layer)
* mvp-s1 … mvp-s4
* team-dw
* Carmen's L2 (the trigger case)

Plus any further L2s deployed between 2026-05-20 and the cut-over.

## Rollback

If a per-L2 backfill goes wrong: re-run with `--rotate --force` to
mint a fresh key.

If the central service is degraded and the L2 needs to bypass it
quickly: set `CQ_EMAIL_LEGACY_SES=1` in the task definition (or via
the CFN parameter once a follow-up adds it). The L2 will detect the
missing TX_SEND_KEY (if you've already pulled it via the template
update) and fall back to legacy SES with a clear deprecation log
line. This is the migration-window safety net; reset to clean state
once central is healthy.
