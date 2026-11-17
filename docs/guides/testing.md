# Check and prove a stack

This guide checks a running stack from outside and runs the proofs. It also lists the browser checks for sign-in. The [hardening checks](#hardening-checks) are a table below. Create a stack first with [the run book](run-book.md).

- **Black box check:** `verify/blackbox.py`. It tests the stack over HTTPS from an allowed address.
- **Proof:** A command of `verify/prove.py`. Each one shows one claim about the stack, such as no registry credential on the instance.
- **Test double:** A fake server inside the offline tests. A check that has run only against test doubles has not run against AWS.

## Check the stack from outside

Run this from an address on the allowlist. It prints one PASS, FAIL or SKIP line per check. Every request uses HTTPS, except the requests of the `http-redirect` and `old-ports-closed` checks.

```
uv run verify/blackbox.py --stack <stack name>
```

- **Sign-in:** The script reads `AuthProvider` from the record file. On a `Native` stack, it signs in with `POST /login` as the native administrator. On an `Oidc` stack, it gets a Cognito access token for `hume-admin` from the test pool.
- **AWS calls:** The script reads one password from Secrets Manager. On an `Oidc` stack, it also asks Cognito for the token. That call is `cognito-idp:AdminInitiateAuth`, so the profile needs that permission. The password and the token stay in memory.
- **Media upload:** The `media-upload` check needs an upload configuration named `DEFAULT`. On our stacks only the configuration import creates one, and that task runs only when `SpineConfigSecretArn` is set. On a stack without it, `uploadConfigs` returns 200 with no `DEFAULT` entry and the check fails.
- **Exit code:** The script exits 1 if any check fails.
- **Defaults:** `--stack` defaults to the only record file. `--profile` and `--region` default to the values in the record file, and then to `marketplace` and `us-east-1`.
- **API health:** No check calls a health path on `hume-api.<base>`. The Hume API has no `/api/health`, and its actuator answers on the management port 7001, which the load balancer checks and never forwards. The `api-unauthenticated` check proves the API answers through the load balancer: `GET /users/me` without a token returns 401.
- **Media routing check:** The `media-routing` check sends `GET /v1/download` to `hume-media.<base>` without a token and expects 401, the Bearer challenge from media. The load balancer's fixed 403 means no listener rule sent the request to media.
- **Prometheus check:** One check sends `GET /actuator/prometheus` to `hume-api.<base>` with no token and expects 403. It reads only the status code and discards the body. It fails on a stack built before the template blocked that path.
- **Unknown host check:** The `alb-unknown-host` check sends a request with an unknown `Host` header and expects the load balancer's fixed 403.
- **Redirect check:** The `http-redirect` check sends a request to port 80 on each of the three host names. It expects a 301 to the same host on port 443. The request carries a `User-Agent` header, because the WAF common rule set answers 403 to a request without one before the port 80 listener can redirect it.
- **Old ports check:** The `old-ports-closed` check expects ports 8080, 8081 and 8009 to refuse the connection or time out.
- **Upload check:** The media upload check first lists the upload configurations on the API, picks the one named `DEFAULT`, and sends its id with the file. It prints no id.
- **Delete check:** After the download, the script deletes the test file with `DELETE /media/management/files/<id>` on `hume-api.<base>` and `hardDelete` set to true. It expects 204. It then downloads the file again and expects an error status.

## Run the proofs

Run `prove.py` with `uv run --script verify/prove.py <command>`. Commands that change anything ask first. `--yes` skips the question.

The commands are `egress`, `credentials`, `replace-instance`, `soak`, `teardown-check` and `delete-ami`. The [command reference](../reference/commands.md#provepy) says what each one does and lists its flags.

## Hardening checks

These checks prove the hardening on a live stack built from the current template. Every row passed on 2026-11-05 except the GovCloud deploy. Run them from an address on the allowlist with the AWS CLI and `curl`, after setting these once:

```
export AWS_PROFILE=<profile>
export AWS_DEFAULT_REGION=us-east-1
export HUME_STACK=<stack name>
rid() { aws cloudformation describe-stack-resource --stack-name "$HUME_STACK" \
  --logical-resource-id "$1" --query StackResourceDetail.PhysicalResourceId --output text; }
WEB_URL=$(aws cloudformation describe-stacks --stack-name "$HUME_STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text)
```

| Check | Command | Expected | Passed |
|---|---|---|---|
| Health check counts only 200 | `for TG in WebTargetGroup ApiTargetGroup MediaTargetGroup; do aws elbv2 describe-target-groups --target-group-arns "$(rid $TG)" --query 'TargetGroups[0].[HealthCheckPath,HealthCheckPort,Matcher.HttpCode]' --output text; done` | Web prints `/`, `traffic-port`, `200`. API and media print `/actuator/health`, `7001`, `200`. | 2026-11-05 |
| Every target healthy | `for TG in WebTargetGroup ApiTargetGroup MediaTargetGroup; do aws elbv2 describe-target-health --target-group-arn "$(rid $TG)" --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output text; done` | Each line ends in `healthy`. An unhealthy API or media target on a fresh stack points at the management port. | 2026-11-05 |
| Invalid headers dropped | `aws elbv2 describe-load-balancer-attributes --load-balancer-arn "$(rid LoadBalancer)" --query "Attributes[?Key=='routing.http.drop_invalid_header_fields.enabled'].Value" --output text` | `true` | 2026-11-05 |
| WAF blocks a script tag | `curl -s -o /dev/null -w '%{http_code}\n' "$WEB_URL/?q=%3Cscript%3Ealert(1)%3C/script%3E"` | `403`. In the earlier count mode it was 200. | 2026-11-05 |
| WAF logs the block | `aws logs filter-log-events --log-group-name "aws-waf-logs-$HUME_STACK" --filter-pattern '{ $.action = "BLOCK" }' --start-time $(( ($(date +%s) - 900) * 1000 )) --query 'events[].message' --output text` | One JSON entry a minute later with `"action":"BLOCK"`, the terminating rule `aws-common-rules`, a cross-site scripting rule in the rule group list, and `REDACTED` for the authorization and cookie headers. | 2026-11-05 |
| WAF leaves normal use alone | `uv run verify/blackbox.py --stack "$HUME_STACK"` | Every check passes. Upload one file over 8 KB by hand as well, because the body size rule only counts. A 403 points at a managed rule, so read the WAF log. Without the Spine import the `media-upload` check fails for want of `DEFAULT`, which is not a WAF block. | 2026-11-05 |
| Access logs reach S3 | `BUCKET=$(rid AccessLogBucket); curl -s -o /dev/null "$WEB_URL/"; sleep 300; aws s3 ls "s3://$BUCKET/alb/AWSLogs/" --recursive \| tail -5` | Objects under `alb/AWSLogs/<account>/elasticloadbalancing/<region>/`. The first is the test file from when logging was turned on, the rest end in `.log.gz`. | 2026-11-05 |
| Access log bucket settings | `aws s3api get-bucket-encryption --bucket "$BUCKET" --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text; aws s3api get-public-access-block --bucket "$BUCKET" --query 'PublicAccessBlockConfiguration' --output text; aws s3api get-bucket-lifecycle-configuration --bucket "$BUCKET" --query 'Rules[0].Expiration.Days' --output text; aws s3api get-bucket-policy --bucket "$BUCKET" --query Policy --output text` | `AES256`, four `True` values, `90` unless `AccessLogExpirationDays` changed, and a policy with one deny for requests without TLS and one allow for `logdelivery.elasticloadbalancing.amazonaws.com` under `alb/AWSLogs/<account>/*`. | 2026-11-05 |
| KMS key use in CloudTrail | `KEY=$(rid DataKey); KEY_ARN=$(aws kms describe-key --key-id "$KEY" --query KeyMetadata.Arn --output text); aws cloudtrail lookup-events --lookup-attributes AttributeKey=EventName,AttributeValue=Decrypt --start-time $(( $(date +%s) - 604800 )) --max-results 50 --query "Events[?contains(CloudTrailEvent, '$KEY_ARN')].[EventTime,EventName,Username]" --output text` | `Decrypt` events for the key by the task execution role, up to 15 minutes after the call. A task reads its secrets only at start, so an empty list means `--start-time` must move back before the last task start. | 2026-11-05 |
| KMS rotation and alias | `KEY=$(rid DataKey); aws kms get-key-rotation-status --key-id "$KEY" --query KeyRotationEnabled --output text; aws kms list-aliases --key-id "$KEY" --query 'Aliases[].AliasName' --output text` | `True`, then `alias/hume/<stack name>`. | 2026-11-05 |
| Partition lint | `for R in us-east-1 us-gov-west-1 us-gov-east-1; do cfn-lint --regions $R -- stack/hume-ecs.template.yaml \| grep -c '^E'; done` | Three lines of `0`, and no warnings. Use cfn-lint 1.60 or later, because older versions do not know that load balancer listeners and listener rules take tags. | 2026-11-16 |
| No fixed partition | `grep -n -e 'arn:aws' -e 'amazonaws.com' stack/hume-ecs.template.yaml` | Only the six `AllowedPattern` lines, which accept `aws-us-gov`, plus service principals and links in comments. No resource builds an ARN with a literal `arn:aws`. | 2026-11-05 |
| Offline tests | `uv run --with pyyaml --with boto3 --with python-dotenv python -m unittest discover -s tests -t .` | All tests pass. One PRM tag test skips without cfn-lint. Add `--with cfn-lint` to run it. | 2026-11-16 |
| GovCloud deploy | A deploy to GovCloud needs a GovCloud account. | The stack builds and the black box check passes. | Not run |

## Check the sign-in in a browser

Run these on an OIDC stack, after `uv run verify/cognito.py check` passes. The [run book](run-book.md#check-that-it-works) shows how to sign in as `hume-admin`.

| Check | How | Expected |
|---|---|---|
| Login | Sign in as `hume-admin`. | Hume shows the administrator pages. The role appears from the second request for a new user. |
| First login | Sign in as `hume-user`. | Hume creates the user without the administrator role. |
| Logout | Sign out, then sign in again. | Note whether the provider asks for the password again. |
| Session restore | Reload the page after 10 minutes. | You stay signed in. |
| API key | On a stack with the Spine demo, run a script that uses `admin-api-key`. A stack without the demo has no API keys. | It still works. |
| Neo4j | Open the Spine demo with the resource `spine-neo4j-sa`. | It loads data. |

- **`spine-neo4j-sso`:** This resource works for a user in a Spine group since 2026-11-05. A user in no Spine group gets `Neo4jInsufficientUserPrivileges`. See [Roles](spine-demo.md#roles) and [Hume to Neo4j Over SSO](../architecture/identity.md#hume-to-neo4j-over-sso).

## Check Hume reaches Neo4j over SSO

Run these on a stack with the Spine demo. The design is in [Hume to Neo4j Over SSO](../architecture/identity.md#hume-to-neo4j-over-sso). The security log is `/var/log/neo4j/security.log` on each cluster node, read over SSM.

| Check | How | Expected |
|---|---|---|
| Roles mapping | `uv run verify/cognito.py check --stack-name <stack> --user spine-analyst`. Step 6 of `spine_demo.py` runs the same check. | `/users/me` lists `USER` and `SPINE_ANALYST`. |
| SSO query | As `spine-analyst`, run the `absconders-and-escapes` action on the Analyst perspective. | Rows appear and `Birth Date` is empty. |
| Security log | Read the log on the node that served the query. | A `logged in` line with the user's `sub`, and none with `neo4j_app` for that query. |
| PUBLIC only | Hume refuses the SPINE graph to `hume-user` with 403 `AccessDeniedException` before any Neo4j call. Connect straight to Neo4j with the user's ID token over an SSM port forward. Run `SHOW CURRENT USER` and a count of `Person` on `spine`. | The roles are `[PUBLIC]`, the count fails with `Neo.ClientError.Security.Forbidden`, and the log shows `42NFF` with `roles [PUBLIC]`. |
| Service account | As `hume-admin`, run the action on the Main perspective. | Rows with `Birth Date`, and the log shows `neo4j_app`. |
| Logs | Search `/hume/<stack>/api` and `/hume/<stack>/orchestra` for `PKIX` and `eyJ`. | No matches. |
