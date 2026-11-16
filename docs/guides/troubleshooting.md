# Troubleshooting

This guide lists the problems seen so far and what to do about each one. Each entry names the symptom, the cause and the fix. Where another guide holds the full text, the entry links to it.

- **Symptom:** What you see, such as an error message or a failed check.
- **Cause:** Why it happens.
- **Fix:** What to do next.

## Deploy and sign in

### The browser address differs from the terminal address

- **Symptom:** The load balancer refuses the browser, but the address from `curl https://checkip.amazonaws.com` is on the allowlist.
- **Cause:** A VPN or a proxy such as WARP can send your browser out through a different address.
- **Fix:** Check the address from the browser, not from the terminal. See [Create the Hume stack](run-book.md#create-the-hume-stack).

### `describe-secret` still returns a deleted secret

- **Symptom:** After the delete, `describe-secret` still returns the name.
- **Cause:** The delete takes a few seconds to show.
- **Fix:** Wait a few seconds and check again.

### `deploy.py` fails with `TokenRetrievalError`

- **Symptom:** `deploy.py` fails with the botocore error `TokenRetrievalError`. The error says the SSO token expired and the refresh failed. The `aws` CLI still works.
- **Cause:** The SSO token expired. Any `deploy.py` command, such as `smoke`, can stop this way.
- **Fix:** Run `aws sso login --profile <profile>`, then run the command again.

### MissingDependencyException asks for botocore[crt]

- **Symptom:** A script fails at sign-in with MissingDependencyException, and the message says to install `botocore[crt]`.
- **Cause:** The profile was signed in with `aws login`. boto3 reads such a profile only with the CRT extra. A checkout older than this change installs boto3 without it.
- **Fix:** Pull the current scripts, which declare `boto3[crt]`. Or sign in with `aws sso login` and use that profile, or use a profile with static keys.

## OIDC sign-in

### Hume sign-in fails for every user

- **Symptom:** The provider's login page works, but Hume refuses every user after it.
- **Cause:** The api and media tasks fetch the provider's signing keys from `<issuer>/.well-known/jwks.json`. That call needs a route from the private subnets to the internet. The NAT gateways of the Neo4j VPC provide it.
- **Fix:** Check that the route tables of the private subnets send `0.0.0.0/0` to a NAT gateway. Then look in the api log for an error on the call to the issuer address. See [How TLS is set up](../reference/infrastructure-setup-details.md#how-tls-is-set-up).

### check says Auth flow not enabled for this client

- **Symptom:** `uv run verify/cognito.py check` fails with `InvalidParameterException` from `AdminInitiateAuth`. The message reads `Auth flow not enabled for this client`.
- **Cause:** A `cognito.py up` from before commit 72ce3f2 created the `hume-web` client, and that version still set `ALLOW_USER_PASSWORD_AUTH`. `check` now signs in with `ADMIN_USER_PASSWORD_AUTH`. Only `cognito.py env` applies that flow to an existing client. A pool kept from an earlier checkout keeps the old flow. So does a run book pass that skips `env` because the pool already exists.
- **Fix:** Run `uv run verify/cognito.py env --advertised-dns <Neo4j advertised name>` once. It sets the flows on both clients and refreshes the redirect addresses of the `neo4j` client. Then run `check` again. See [Write the OIDC settings](run-book.md#write-the-oidc-settings).

## Checks and teardown

### The prometheus check fails

- **Symptom:** The `blackbox.py` check on `GET /actuator/prometheus` fails.
- **Cause:** The stack was built before the template blocked that path.
- **Fix:** Build the stack from the current template. See [Check the stack from outside](testing.md#check-the-stack-from-outside).

### Teardown exits 1 and keeps the record file

- **Symptom:** `./stack/deploy.py teardown` exits 1, and the record file stays.
- **Cause:** Teardown found a resource still tagged with the stack. The Resource Groups Tagging API can list a resource for a while after it is gone. The index lagged more than 7 hours in one test, and teardown reads the listing as live resources.
- **Fix:** Read the list it prints, and check each resource with a direct call. See [Tear down a Hume stack](teardown.md#tear-down-a-hume-stack).

### Teardown says it could not switch off deletion protection

- **Symptom:** `./stack/deploy.py teardown` exits 1 before it deletes anything and prints `Could not switch off deletion protection`.
- **Cause:** The template turns on deletion protection for the database, and teardown must switch it off first. The change failed, for example because the database was in a state that does not accept changes.
- **Fix:** Wait until the database is `available`, or switch the protection off by hand, then run teardown again.

  ```
  aws rds modify-db-instance --db-instance-identifier <database id> --no-deletion-protection --apply-immediately
  ```

### Teardown does nothing and exits 1

- **Symptom:** `./stack/deploy.py teardown <stack name>` prints `The name does not match. Nothing was deleted.`
- **Cause:** The answer was not exactly the stack name, or the command had no terminal to read the answer from.
- **Fix:** Type the stack name, or pass `--yes` in a script. Leaving out the stack name is an argument error.

## Neo4j EE

### The Neo4j create fails on the bastion

- **Symptom:** The Neo4j create ends in `CREATE_FAILED` on the bastion, which never sends its signal.
- **Cause:** A `t4g.nano` bastion has about 470 MB of memory. The Linux out-of-memory killer ends the `dnf install` in its startup script.
- **Fix:** Use a Neo4j checkout whose bastion `InstanceType` is `t4g.small` in `networking-private.yaml` and `networking-existing-vpc.yaml`, built with `python3 build.py`. Rollback is off, so delete the failed stack before you create it again.

### A failed Neo4j create leaves no record file

- **Symptom:** `teardown.sh` stops at once and deletes nothing.
- **Cause:** A failed create leaves no `.deploy/<stack-name>.txt`, and `teardown.sh` needs that file.
- **Fix:** Write a small file at `.deploy/<stack-name>.txt` by hand. Put in the stack name, the region, the deployment mode, the SSM parameter path and the ARN of the auto-imported certificate. Put in no secrets. Then run `./teardown.sh <stack-name>`. Check the result with the list in [Tear down the Neo4j EE setup](teardown.md#tear-down-the-neo4j-ee-setup).

### The Hume instance never joins the cluster

- **Symptom:** The Hume stack create waits on the services and fails. The instance is running, but the ECS cluster shows no registered container instance. On the current `deploy.py`, `up` stops before the create instead, with `Private subnet <id> has no active default route` and the `EnableInternetEgress` hint.
- **Cause:** The Neo4j stack was built with `EnableInternetEgress` false, so its private subnets have no default route and the ECS agent cannot reach the ECS control plane.
- **Fix:** Update the Neo4j stack with `EnableInternetEgress` true. Delete the failed Hume stack as in [Tear down](run-book.md#tear-down), then create it again. See [What Hume needs from the Neo4j stack](../reference/infrastructure-setup-details.md#what-hume-needs-from-the-neo4j-stack).

## Spine demo

### The config task fails on an SSM Parameter Store error

- **Symptom:** `ConfigCliRun` fails and the stack rolls back. The task never starts. The error reads `Fetching secret data from SSM Parameter Store ... not authorized to perform ssm:GetParameters`.
- **Cause:** The `render-spine` container named the Secrets Manager secret by its bare name. ECS reads a bare name as an SSM parameter name. A Secrets Manager secret needs its full ARN.
- **Fix:** Use the current template, which takes the full ARN in the parameter `SpineConfigSecretArn`. `spine_demo.py` now runs the `spine-config` step and sets the parameter to the ARN.

### A second spine_demo.py run stops with UPDATE_IN_PROGRESS

- **Symptom:** The change set step of `spine_demo.py` fails with `UPDATE_IN_PROGRESS`.
- **Cause:** A second run started while the first one was still updating the stack.
- **Fix:** Run the script once and wait for it to finish. The refused run changes nothing.

## Covered in other guides

- **The password appears in the terminal:** See step 3 of [Sign in](operations.md#sign-in).
- **The login page shows `redirect_mismatch`:** See [Redirect addresses](../reference/infrastructure-setup-details.md#redirect-addresses).
- **A single node has too few zones:** See [Use three nodes](../reference/infrastructure-setup-details.md#use-three-nodes).
- **Neo4j fails to start after the seed provider edit:** See [Seed provider](spine-demo.md#seed-provider).
- **Teardown lists the key or the snapshot as left:** See [Tear down a Hume stack](teardown.md#tear-down-a-hume-stack).
