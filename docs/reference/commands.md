# Command reference

This page lists every script, its commands and its flags. The names and flag text match each script's `--help`. The topic guides explain when to run each command.

- **Global flag:** A flag that goes before the command, such as `--profile` and `--region`.
- **Stack name:** The optional `stack_name` argument. Where it is optional, it defaults to the only record file in `stack/.deploy/`. If several record files exist, the script stops and asks for the name.
- **`--yes`:** A flag that skips the confirmation prompt on a command that changes something.
- **Precedence:** A command-line flag overrides the process environment. The environment overrides `.env`.

## deploy.py

Run it as `./stack/deploy.py [--profile P] [--region R] <command>`. The guides are [the run book](../guides/run-book.md), [the operations manual](../guides/operations.md) and [Tear down](../guides/teardown.md).

| Command | What it does |
|---|---|
| `up` | Creates or updates the licence secret, then deploys. It takes the same flags as `deploy`. |
| `deploy` | Creates the stack and waits for it. It does not write the licence secret. |
| `licence` | Creates or updates the `hume/licence-key` secret. |
| `truststore` | Creates or updates the `hume/neo4j-truststore` secret from a CA file. The file holds the Neo4j CA and the roots of the identity provider. |
| `truststore-options` | Rewrites only the `JAVA_TOOL_OPTIONS` value of the truststore secret. The store and its password stay in place, so the file on each instance stays valid. Restart `orchestra` and `api` to apply it. |
| `smoke [stack_name]` | Checks the instance, then runs the test task. |
| `validate [--ca-file FILE] [stack_name]` | Checks from the Hume instance that Neo4j answers on port 7687 with a certificate that the CA file verifies. It runs `openssl s_client` over SSM and passes only on `Verify return code: 0 (ok)`. See [The TLS check](infrastructure-setup-details.md#the-tls-check). |
| `reimport [--yes] [stack_name]` | Runs the configuration import task again, then restarts `orchestra`. Run it after the Neo4j app password is rotated, so that `orchestra` opens a new driver. It asks first, and `--yes` skips the question. The stack needs the output `ConfigCliTaskDefinitionArn`, which exists only when `SpineConfigSecretArn` is set. See [Rotate the Neo4j app password](../guides/operations.md#rotate-the-neo4j-app-password). |
| `refresh-truststore [--yes] [stack_name]` | Writes the truststore file on the Hume instance from the secret `hume/neo4j-truststore`, the same way the instance does at boot, then restarts `orchestra` and `api`. Run it after `truststore` on a running stack. It asks first, and `--yes` skips the question. See [Rotate the Neo4j certificate](../guides/operations.md#rotate-the-neo4j-certificate). |
| `spine-config <stack_name>` | Stores the Spine knowledge graph files in the stack's `spine-config` secret. They come from the directory in `HUME_SPINE_CONFIG_DIR`. It prints `SpineConfigSecretArn=<arn>`. The import update sets the `SpineConfigSecretArn` parameter to that ARN. `spine_demo.py` stores the secret itself, so run this command only for the manual steps. |
| `teardown <stack_name> [--yes]` | Deletes the stack and what it leaves. The stack name is required. It asks you to type the stack name, and `--yes` skips the question. It switches off database deletion protection and empties the access log bucket first. It keeps the encryption key and the final database snapshot. |
| `delete-template-bucket` | Deletes the empty template bucket, after the last stack is gone. |

Flags for `up` and `deploy`:

- **`--ami-id`:** The Hume AMI id. It defaults to `HUME_AMI_ID`, then `ami-id.txt`.
- **`--allowed-cidr`:** The comma-separated IPv4 ranges for the ALB. A bare address becomes `/32`. It defaults to `ALLOWED_CIDRS`.
- **`--base-domain`:** The base of the three Hume host names: `hume.<base>`, `hume-api.<base>` and `hume-media.<base>`. It defaults to `HUME_BASE_DOMAIN`. The command exits when neither is set.
- **`--hosted-zone-id`:** The Route 53 zone that holds the host names. It defaults to `HUME_HOSTED_ZONE_ID`.
- **`--yes`:** This flag skips the confirmation prompt.
- **`--instance-type`:** The instance type. It takes `m6i.large` or `m6i.xlarge`. It defaults to `m6i.xlarge`.
- **`--stack-name`:** The stack name. It defaults to `hume-ecs-<epoch seconds>`.

Flags for `truststore`:

- **`--ca-file`:** The PEM file with the CA certificate of Neo4j and the roots of the identity provider. It defaults to `HUME_NEO4J_CA_FILE`. See [The truststore](infrastructure-setup-details.md#the-truststore).
- **`--secret-name`:** The secret to write. It defaults to `hume/neo4j-truststore`.

Flags for `truststore-options`:

- **`--secret-name`:** The secret to update. It defaults to `hume/neo4j-truststore`.

Flags for `smoke`:

- **`--expect-attribute NAME=VALUE`:** The instance attribute that the AMI must set, such as the Hume version. The command fails unless the instance has that value.

Flags for `validate`:

- **`--ca-file`:** The PEM file with the CA certificate of Neo4j. It defaults to `HUME_NEO4J_CA_FILE`. A relative path is read from the current directory, so run the command from the repository root when it uses the `.env` value.

Setting for `spine-config`:

- **`HUME_SPINE_CONFIG_DIR`:** The directory with the Spine knowledge graph files. Set it to `demo/spine/hume-config`, the unchanged copy of `config/hume/spine` from the hume-quickstart repo. It is read from the environment or `.env`, and the command has no flag for it. A relative path is read from the repository root, so the command works from any directory. An absolute path or a path that starts with `~` also works. `deploy.py` skips 6 of the files. See [the Spine demo guide](../guides/spine-demo.md#the-spine-knowledge-graph-files).

## prove.py

Run it as `uv run --script verify/prove.py [--profile P] [--region R] <command>`. Commands that change anything ask first. The guide is [Check and prove a stack](../guides/testing.md#run-the-proofs).

| Command | What it does |
|---|---|
| `egress [stack_name]` | Blocks the registry and replaces a task. It blocks `docker.graphaware.com`, replaces a task, and shows that the replacement starts from the cached image. |
| `credentials [stack_name]` | Checks for credential files on the instance. It checks that no registry credential file is on the instance. |
| `replace-instance [stack_name]` | Terminates the instance and times the recovery. It also records what data is lost. |
| `soak [stack_name]` | Records task restarts, memory and CPU over a period. |
| `teardown-check [stack_name]` | Lists what is left after teardown. With a stack name it checks only this stack. Without one it checks the whole account. |
| `delete-ami <ami_id>` | Deregisters the AMI and deletes its snapshots. |

Flags:

- **`--yes`:** `egress`, `credentials`, `replace-instance`, `soak` and `delete-ami` take this flag. It skips the prompt.
- **`--service`:** `egress` takes this flag. It picks the service to replace. It defaults to `web`.
- **`--timeout` on `egress`:** This flag sets the wait in seconds. It defaults to 600.
- **`--timeout` on `replace-instance`:** This flag sets the wait in minutes. It defaults to 40.
- **`--minutes`:** `soak` takes this flag. It sets the length. It defaults to 30.

## check_prereqs.py

Run it as `uv run stack/check_prereqs.py [--profile P] [--region R] [--ami-id A]`. It checks that an account holds what `deploy.py up` needs. It changes nothing, so run it before `up`. It reads its settings from the process environment, then from `.env`.

It prints the account id first, then one line per check.

| Check | What passes |
|---|---|
| `ami` | The Hume AMI exists and is `available`. |
| `hosted zone` | The zone in `HUME_HOSTED_ZONE_ID` exists. |
| `secret` | `hume/licence-key` and `hume/neo4j-truststore` each exist and are not scheduled for deletion. The script only describes each secret. It never reads or prints a value. |
| `cognito pool` | The Cognito pool behind `HUME_OIDC_ISSUER_URI` exists. The check is skipped when the issuer is not a Cognito address. An unset issuer passes only when `HUME_AUTH_PROVIDER` is `Native`. |

- **Early stop:** The script stops after the account line when the credentials fail. It also stops there when it finds no AMI id.
- **Exit code:** The script exits 1 when any check fails or a required value is unset. Otherwise it exits 0.
- **`--profile`:** The AWS profile. It defaults to `AWS_PROFILE`, then the `deploy.py` default.
- **`--region`:** The AWS region. It defaults to `us-east-1`.
- **`--ami-id`:** The Hume AMI id. It defaults to `HUME_AMI_ID`, then the ID in `ami/ami-id.txt`.

## allowlist.py

Run it as `./stack/allowlist.py [--stack-name S] [--profile P] [--region R] <command>`. The guide is [Change the allowed addresses](../guides/operations.md#change-the-allowed-addresses).

| Command | What it does |
|---|---|
| `list` | Shows the addresses the stack allows. |
| `add <address>` | Allows an address or range. The value is an IPv4 address or range. |
| `remove <address>` | Stops allowing one address. |

- **`--stack-name`:** The stack. It defaults to the only record file.
- **`--profile` and `--region`:** The AWS profile and region. They default to the record.

## vpn_ip_ranges.py

Run it as `uv run stack/vpn_ip_ranges.py [--region R] [--region-wide] <host>`. It lists the AWS address ranges that a VPN must allow so that users can reach a Hume host name. The ALB has no fixed addresses, so the script reads the published AWS ranges. It needs no AWS credentials and changes nothing. uv runs it and it installs no packages.

It downloads `https://ip-ranges.amazonaws.com/ip-ranges.json` and keeps the IPv4 prefixes with the service `EC2` in one Region. It writes one range per line to standard output. The resolved addresses and any warnings go to standard error.

- **`host`:** The host name to resolve, such as `hume.example.com`. It is required. The script prints only the published prefixes that contain the addresses the name resolves to now.
- **`--region`:** The Region whose prefixes the script reads. It defaults to `us-east-1`.
- **`--region-wide`:** This flag prints every `EC2` prefix in the Region instead of only the matching ones. The list is longer, but it stays valid when AWS replaces ALB nodes.
- **Exit code:** The script exits 1 when the host name does not resolve, or when a resolved address is in no `EC2` prefix of the Region. Otherwise it exits 0.
- **Other host names:** The stack serves `hume.<base>`, `hume-api.<base>` and `hume-media.<base>`. Run the script for each name if the VPN rule is per host.

## blackbox.py

Run it as `uv run verify/blackbox.py [--stack S] [--profile P] [--region R] [--idp-name N] [--user U]`. The guide is [Check the stack from outside](../guides/testing.md#check-the-stack-from-outside).

- **`--stack`:** The stack name. It defaults to the only record.
- **`--profile` and `--region`:** The AWS profile and region. A flag wins. Without a flag, the value in the record file is used, and then `marketplace` and `us-east-1`.
- **`--idp-name`:** The Cognito test pool that an `Oidc` stack signs in through. It defaults to `hume-test-idp`.
- **`--user`:** The Cognito user that an `Oidc` stack signs in as. It defaults to `hume-admin`.

## spine_demo.py

Run it as `./demo/spine/spine_demo.py [--profile P] [--region R] --neo4j-stack S --instance-id I [--url U] [--yes] [stack_name]`. It sets up the Spine demo on a Hume stack in six steps. It skips each step that is done, and repeats the load without changing anything. uv runs it and installs its Python packages. The guide is [the Spine demo guide](../guides/spine-demo.md#how-it-works).

- **`stack_name`:** The Hume stack. It defaults to the only record file.
- **`--profile`:** The AWS profile. It defaults to the profile in the record, then `marketplace`.
- **`--region`:** The AWS region. It defaults to the region in the record, then `us-east-1`.
- **`--neo4j-stack`:** The Neo4j EE stack that the Hume stack reads. The script requires it.
- **`--instance-id`:** The instance id of the seed node, as `seed_nodes.py discover` lists it. The script requires it.
- **`--url`:** The https address of the backup. Without it, the script stages the public backup once into the template bucket of the region and uses a presigned address for it, good for one hour.
- **`--yes`:** This flag skips the confirmation prompt.
- **Settings:** The script needs `HUME_SPINE_CONFIG_DIR` in the environment or `.env`. It reads the superuser password ARN from the SSM parameter `/neo4j-ee/<neo4j stack>/password-secret-arn`.
- **Roles:** Step 4 adds `spine-analysts=SPINE_ANALYST` and `spine-officers=SPINE_OFFICER` to `OidcRolesMappings` when no pair grants that role. Step 6 signs in as `spine-analyst` through the Cognito test pool and checks that `/users/me` lists `SPINE_ANALYST`. It skips the check on a stack that uses another provider.

## seed_nodes.py

Run it as `./demo/spine/seed_nodes.py [--profile P] [--region R] <command> <neo4j_stack>`. It finds the Neo4j cluster nodes, puts the Spine backup on one of them and loads the `spine` database from it. uv runs it and installs its Python packages. The guide is [Add the demo to a Hume stack](../guides/spine-demo.md#add-the-demo-to-a-hume-stack).

| Command | What it does |
|---|---|
| `discover <neo4j_stack>` | Lists the running instances of the Neo4j EE stack and probes each one over SSM. It changes nothing on the nodes and downloads nothing to them. Without `--url` it stages the backup in the template bucket first. |
| `copy <neo4j_stack> --instance-id <id>` | Downloads the backup into the seeds directory of one node. It asks first and deletes nothing. |
| `load <neo4j_stack> --instance-id <id>` | Creates the `spine` database from the backup on that node, then its roles, and prints the counts. `cypher-shell` runs on the node over SSM. The node reads the `neo4j` superuser password from Secrets Manager. It asks first and deletes nothing. |

- **`--profile`:** The AWS profile. It defaults to `AWS_PROFILE`, then `marketplace`.
- **`--region`:** The AWS region. It defaults to `us-east-1`.
- **`--url`:** All three commands take this flag. It gives the https address of the backup. Without it, each command stages the public backup once into the template bucket of the region and uses a presigned address for it, good for one hour. Use the same `--url` for `copy` and `load`. The address must start with `https://`.
- **`--instance-id`:** `discover` takes this flag to probe only one instance. `copy` and `load` require it.
- **`--password-secret-arn`:** `load` takes this flag. It gives the full ARN of the `neo4j` superuser password secret. It defaults to the SSM parameter `/neo4j-ee/<neo4j_stack>/password-secret-arn`.
- **`--seeds-dir`:** `copy` takes this flag. It defaults to the seeds directory that the probe found.
- **`--yes`:** `copy` and `load` take this flag. It skips the confirmation prompt.

## cognito.py

Run it as `uv run verify/cognito.py [--profile P] [--region R] [--name N] <command>`. It builds a test Cognito pool that stands in for the customer's identity provider. The pool belongs to no stack. The guides are [the run book](../guides/run-book.md#create-the-identity-provider) and [Infrastructure setup details](infrastructure-setup-details.md#the-identity-provider).

| Command | What it does |
|---|---|
| `up [--domain-prefix P] [--neo4j-host <name>]` | Creates the pool, the login domain, both app clients, the five groups, the five users, their password secrets and the pre token generation trigger. It needs no Hume stack. |
| `sync` | Adds the groups, users, password secrets and the pre token generation trigger that a pool from an older `up` lacks. Existing passwords stay. Run `env` afterwards for the client settings. |
| `env [--advertised-dns <name>] [--private-dns-zone Z] [--no-bloom]` | Checks the issuer against its discovery document. Writes `HUME_OIDC_ISSUER_URI` and `HUME_OIDC_CLIENT_ID` to `.env`, and the Neo4j EE settings to `stack/.deploy/.env.neo4jee`. Then sets the Neo4j Browser and Bloom redirect addresses on the `neo4j` client. |
| `check [--user U] [--stack-name S]` | Signs in with a password through the Cognito admin API and calls the stack's api and media with each token. It needs AWS credentials. |
| `callbacks [--client hume\|neo4j] [--add URL \| --reset-hume]` | Adds a redirect address to a client. Without `--add` it lists the addresses. |
| `status` | Prints the record. |
| `teardown` | Deletes the domain, the pool, the password secrets and the record. |

- **`--profile` and `--region`:** The AWS profile and region. They default to the record, then `AWS_PROFILE`, then the `deploy.py` defaults.
- **`--name`:** The pool name. It defaults to `hume-test-idp`. Each name has its own pool, record and secrets.
- **`--domain-prefix`:** `up` takes this flag. It sets the prefix of the Cognito login domain and defaults to the pool name.
- **`--neo4j-host`:** `up` takes this flag. It gives the name that the Neo4j stack advertises. `up` then puts the Browser and Bloom addresses on the `neo4j` client in place of the placeholder. It leaves the Bloom address off when `NO_BLOOM` is true.
- **`--advertised-dns`:** `env` takes this flag. It gives the name that the Neo4j stack advertises, for example `neo4j-oidc.neo4j.local`. It defaults to `NEO4J_ADVERTISED_DNS`.
- **`--private-dns-zone`:** `env` takes this flag. It names the zone that holds the advertised name. It defaults to the advertised name without its first label.
- **`--no-bloom`:** `env` takes this flag. It leaves the Bloom address off the `neo4j` client and writes `NO_BLOOM=true` to `.env.neo4jee`. It defaults to `NO_BLOOM`.
- **`--user`:** `check` takes this flag. It takes `hume-admin`, `hume-user` or `neo4j-admin`, and defaults to `hume-admin`.
- **`--stack-name`:** `check` takes this flag. It defaults to the only record file.
- **`--client`:** `callbacks` takes this flag. `hume` picks the `hume-web` client and is the default. `neo4j` picks the `neo4j` client.
- **`--add`:** `callbacks` takes this flag. It allows one https redirect address.
- **`--reset-hume`:** `callbacks` takes this flag. It makes the two addresses built from the base domain the only `hume-web` redirect and sign-out addresses. It refuses the `neo4j` client.

## neo4j_env.py

Run it as `uv run stack/neo4j_env.py [--profile P] [--region R] [--dry-run] <neo4j_stack>`. It checks the Neo4j EE values that Hume needs in SSM and writes the Neo4j EE stack name to `.env` as `HUME_NEO4J_STACK_NAME`. The template reads the other values itself. It also saves the Neo4j trust anchor from `ca-pem` to `stack/.deploy/neo4j-ca.pem`. The guides are [the run book](../guides/run-book.md#read-the-neo4j-values) and [What neo4j_env.py reads](infrastructure-setup-details.md#what-neo4j_envpy-reads).

- **`neo4j_stack`:** The name of the Neo4j EE stack. It is required.
- **`--profile`:** The AWS profile. It defaults to `AWS_PROFILE`, then the `deploy.py` default.
- **`--region`:** The AWS region. It defaults to `us-east-1`.
- **`--dry-run`:** This flag prints the values and writes neither `.env` nor the PEM file.

## AMI scripts

The guide is [Build the AMI](../guides/run-book.md#build-the-ami).

| Command | What it does |
|---|---|
| `./ami/create_ami.py` | Builds the AMI and writes its ID to `ami/ami-id.txt`. |
| `./ami/test_ami.py` | Launches the AMI for a few minutes and prints one PASS or FAIL line per check. |

- **`--profile`:** Both scripts take this flag. It defaults to `AWS_PROFILE`.
- **`--yes`:** Both scripts take this flag. It skips the confirmation prompt.
- **`--ami-id`:** `test_ami.py` takes this flag. It defaults to the ID in `ami/ami-id.txt`.

## Spine demo scripts

The files are in `demo/spine/`. The steps are in the [Spine demo guide](../guides/spine-demo.md). The full file list is in the [Spine demo README](../../demo/spine/README.md).

| File | How it runs |
|---|---|
| `roles.cypher` | `seed_nodes.py load` runs it on the seed node with `cypher-shell`. |
