# Hume on AWS Marketplace: Detailed Architecture

This document describes what the current template (`stack/hume-ecs.template.yaml`) and AMI (`ami/`) build. It states only what is implemented. The design is in the [Architecture Overview](architecture-overview.md). The release stages are in [Release Scope by Compliance Level](../compliance/release-scope.md). The open questions for federal customers are in the [Federal Compliance](../compliance/federal-compliance.md) document. The checks that prove the behaviour are in [Check and prove a stack](../guides/testing.md).

## The Hume AMI

The AMI carries everything an instance needs to run tasks with no registry access. It carries no customer settings.

1. **Base image:** The latest ECS-optimized Amazon Linux 2023 AMI, which includes Docker, the ECS agent and the SSM agent.
2. **Images on disk:** Seven images: `hume-web`, `hume-api`, `hume-orchestra`, `hume-eventstore`, `hume-media`, `hume-config-cli`, and `postgres:17-alpine`, which the `db-init` task uses for `psql`. Neo4j and the Postgres server are not in the AMI. Neo4j EE is a separate stack and RDS replaces the Postgres containers.
3. **RDS CA bundle:** The AMI carries the RDS certificate bundle, and the tasks that open database connections mount it read-only.
4. **Root volume:** 40 GiB gp3 in the image. The launch template sets the size from `RootVolumeSize`, 40 GiB by default.

### ECS Agent Settings

The AMI writes five lines to `/etc/ecs/ecs.config`.

1. **`ECS_IMAGE_PULL_BEHAVIOR=prefer-cached`:** ECS uses the image on disk and pulls only when it is missing.
2. **`ECS_DISABLE_IMAGE_CLEANUP=true`:** Automated image cleanup is off, so the preloaded images stay on disk.
3. **`ECS_AWSVPC_BLOCK_IMDS=true`:** Tasks in `awsvpc` mode cannot reach the instance metadata service.
4. **`ECS_AVAILABLE_LOGGING_DRIVERS=["json-file","awslogs"]`:** Whether the `awslogs` driver needs this line is not proven.
5. **`ECS_INSTANCE_ATTRIBUTES={"hume.version":"<version>"}`:** The instance carries the Hume version on its disk. Task definitions require it, so a task lands only on an instance that holds its images.

The user data appends `ECS_CLUSTER=<cluster>` at launch, so the AMI holds no cluster name.

### Hardening

1. **No credentials:** The AMI holds no passwords, SSH keys or AWS credentials, and the launch template sets no key pair. Administrators connect through Session Manager.
2. **IMDSv2 required:** The launch template sets `HttpTokens: required`, and `ECS_AWSVPC_BLOCK_IMDS` blocks tasks from the metadata service.
3. **Build credentials removed:** The build pulls from the registry with a temporary login and deletes the Docker credential file before it creates the image. `verify/prove.py credentials` checks the result on a running instance.
4. **Digests kept:** The build pulls images instead of loading tar files, so each image keeps its registry digest.
5. **State cleared:** The build stops the ECS agent and removes `/var/lib/ecs/data`, deletes the SSH host keys and removes every shell history file.
6. **Host trust store updated:** `ami/build.sh` upgrades `ca-certificates` with `--releasever=latest` before it pulls the images, so the host rejects the chains from the eMudhra CA compromise.
7. **Tested:** `ami/test_ami.py` boots the AMI and checks the images, the agent settings, the file owners and modes, the eMudhra rejection, and that no Java key store file or customer settings are present.

## ECS Cluster and Capacity

1. **Launch template:** It names the Hume AMI, requires IMDSv2, sets no key pair and attaches the instance profile. It encrypts the root volume with `Encrypted: true` and names no key, so the volume uses the account's default EBS key. The user data appends `ECS_CLUSTER`, writes the truststore file from Secrets Manager, and turns on ENI trunking.
2. **Instances:** The Auto Scaling group is fixed at 1 / 1 / 1 in the first private subnet. The instance type is `m6i.large` or `m6i.xlarge`.
3. **Capacity provider:** Managed scaling and managed termination protection are both disabled, so the group never adds or removes instances by itself.
4. **Placement:** Each service has one `memberOf` constraint on the `hume.version` attribute. There is no placement strategy.
5. **Network mode:** Tasks use `awsvpc` mode, so each task has its own interface and security group. ENI trunking raises the interface limit. The instance role turns it on from user data with no principal ARN, because ECS rejects a custom resource that sets it for the role.
6. **Instance replacement:** `verify/prove.py replace-instance` showed every service back at its desired count and every target healthy about 10 minutes after the only instance was terminated. A replacement keeps the media files and the API's audit log, because they are on EFS. See [Uploaded Files](#uploaded-files).

## Hume Services

| Service | Ports | Copies | Container health check | State |
|---|---|---|---|---|
| `web` | 8081 | 1 | None | None |
| `api` | 8080, 9080, 7001 | 1 | HTTP liveness on 7001 | `/conf` on the instance, audit logs on EFS |
| `orchestra` | 8100, 8666, 7001 | 1, A second copy needs another licence. | HTTP liveness on 7001 | `/conf` and `/security` on the instance, `/data` on EFS |
| `eventstore` | 8080, 9090, 7001 | 1 | HTTP liveness on 7001 | Postgres only |
| `media` | 8008, 7001 | 1 | HTTP liveness on 7001 | Uploaded files on EFS |
| `db-init` | none | Runs once per deploy | None | None |

Each copy parameter defaults to 1 and takes up to 4 copies, except Orchestra, which always runs one.

### Task Definitions

1. **Images by tag:** Each task definition names the tag that the AMI holds.
2. **No digest lookups:** Every task definition sets `versionConsistency: disabled`, because there is no registry to contact.
3. **Version placement:** Each service requires `attribute:hume.version == <version>`.
4. **Secrets:** Passwords and keys reach the containers through the `secrets` field, which reads Secrets Manager at task start.
5. **Logs:** Every container uses the `awslogs` driver and writes to its own log group.
6. **Load balancer checks:** The ALB target groups use HTTP and count only a 200. The `web` group checks `/` on the traffic port. The `api` and `media` groups check `/actuator/health` on port 7001, and the load balancer never forwards that port.
7. **Container checks:** `api`, `orchestra`, `eventstore` and `media` move the Spring actuator to port 7001 with `management.server.port`. Each one runs `curl` against `http://localhost:7001/actuator/health/liveness` and needs a 200. This probe tracks the state of the Java process and ignores RabbitMQ and the database. The aggregate `/actuator/health` returns 503 for `orchestra` and `eventstore`, so the container checks do not use it. A frozen JVM fails the check, which a TCP check would miss. `web` has no container check, because it has no health endpoint. GraphAware confirmed this.
8. **Containers:** There is no read-only root file system. One shared task execution role serves every service. The API and Orchestra also run under a task role that reads the `neo4j_app` secret for the Hume vault, and the other services have no task role.

### Start Order

ECS has no ordering across services. GraphAware says the Hume services do not retry their connections at start. ECS restarts a task whose dependency is not ready only if the process exits. The container checks use liveness, so a service that stays running without its dependency is never replaced. A test on the live stack will show which case applies.

1. **Creation order:** The template creates the services in dependency order: `eventstore`, `orchestra`, `api`, `media`, then `web`.
2. **One-time:** The order controls only when each service is created. A task that restarts later relies on ECS.
3. **Deploys:** Every service deploys with minimum healthy 0 and maximum 100, so ECS stops the old task and then starts the new one. The deployment circuit breaker rolls a service back if its new tasks keep failing.

### One-Time Tasks

A Lambda-backed custom resource, `Custom::EcsRunTask`, calls `ecs:RunTask`, waits for the task to stop and reports its exit code to CloudFormation. The Lambda code is inline in the template. Its role can run and describe only this stack's one-time task definitions and pass only their roles.

1. **`db-init`:** It connects as the RDS master user and creates the `hume`, `orchestra`, `eventstore` and `media` databases, each with its own login whose password comes from Secrets Manager. It is safe to run again on a stack update.
2. **Config CLI:** It runs only when `SpineConfigSecretArn` is set, to import the demo configuration.

## Data

### RDS for Postgres

1. **One instance, four databases:** A single-AZ instance holds `hume`, `orchestra`, `eventstore` and `media`. Each service logs in with its own user, which reaches only its own database. The parameter group sets `rds.logical_replication=1`.
2. **Master password:** RDS manages the master password in Secrets Manager. Only `db-init` reads it.
3. **TLS verified:** The connection strings use `sslmode=verify-full` with the RDS CA bundle from the AMI.
4. **Encryption and backup:** Storage is encrypted with the deployment's KMS key. Automated backups run for `DbBackupRetentionDays`. Deletion protection is on.
5. **Single-AZ:** If the RDS zone fails, Hume is down until RDS recovers or the customer restores a backup. See [When Availability Is Rated High](../compliance/federal-compliance.md#when-availability-is-rated-high).

### Uploaded Files

Media files, Orchestra's `/data` folder and the API's audit log live on one Amazon EFS file system. Replacing the instance loses none of them. The stack keeps the file system when it is deleted. The deploy guide's [Where your data lives](../deploy-guide.md#where-your-data-lives) lists each location.

1. **One file system per stack:** The file system is encrypted with the stack KMS key. AWS Backup takes a daily backup and keeps each one for 35 days.
2. **Three access points:** The access points `/media`, `/orchestra` and `/audit-logs` each run as uid 1001. The stack creates each directory with mode 0700 only when it is missing, so a kept file system keeps its data.
3. **TLS mounts:** The tasks mount the file system with TLS. The `media` container mounts its access point at `/hume-media`, because the image's `/media` folder belongs to root.
4. **Mount targets:** The stack creates mount targets in the first two private subnets. The security group admits NFS on port 2049 from the instance security group only.
5. **Adopting an old file system:** A new stack can take over a kept file system through the `ExistingFileSystemId` parameter. See "Restore into a new stack" in the deploy guide.

Moving media to S3 is planned.

## Secrets and Keys

| Secret | Created by | Read by |
|---|---|---|
| RDS master password | RDS | `db-init` |
| Four database passwords | Template | `db-init` and the matching service |
| Event store password | Template | `api`, `media` |
| JWT signing key | Template | `api`, `media` |
| Admin API key (Spine demo only) | Template | `api`, `config-cli` |
| Media download key | Template | `api`, `media` |
| API-to-media key | Template | `api`, `media` |
| Media-to-API key | Template | `api`, `media` |
| Symmetric key | Template | `api`, `media` |
| Maintenance key | Template | `media` |
| Admin password (Native login only) | Template | `api` |
| Hume licence key | The user, passed as `HumeLicenceSecretArn` | `api` |
| Neo4j app password | The Neo4j EE stack | `api`, `orchestra` |
| Truststore | `deploy.py` or the user, passed as `Neo4jTruststoreSecretArn` | `api`, `orchestra` |

1. **Generated secrets:** The template generates its secrets with `GenerateSecretString`. No fixed value from the Compose file appears in the AMI or the template.
2. **Service keys:** The API and media share four keys: the download key, the API-to-media key, the media-to-API key and the symmetric key. Media also holds a maintenance key that no Hume service sends. Every stack generates its own value for each, so no stack runs the defaults built into the images. Each key is its own secret, because media looks a key up by value and two equal keys would mix up their roles. ECS injects the keys as secrets, and Orchestra and the event store get none of them.
3. **Truststore:** One JKS truststore holds the Neo4j cluster certificate and the identity provider roots. Its secret also carries `java_tool_options`, which holds the truststore flags and `-Dsun.net.inetaddr.ttl=10`. Orchestra and the API read that value as `JAVA_TOOL_OPTIONS`, so both cache DNS answers for 10 seconds. `eventstore`, `media` and `web` keep the JVM default. `deploy.py refresh-truststore` updates the secret and restarts both services.
4. **Customer-managed key:** The template creates one KMS key, called the DataKey, with rotation on and retention after stack delete. It encrypts the secrets and RDS.
5. **GraphAware answers not built yet:** GraphAware says the licence key will ship in the AMI, and the template does not do that. It still takes the key as `HumeLicenceSecretArn`. Which licence key to use and how Hume checks it are open questions for GraphAware.

## IAM

Every ARN is built with `${AWS::Partition}`. No role has a managed policy on the instance.

1. **Instance role:** Four policies. `EcsAgent` serves the ECS agent. `SsmAgent` serves Run Command and Session Manager. `EniTrunking` turns on ENI trunking. `HumeBoot` signals the stack and reads the truststore secret. It has no data access.
2. **Task execution role:** One role for every task. It reads this stack's secrets, the licence secret and the truststore secret, decrypts the stack's secrets with this stack's key, and writes to this stack's log groups. It does not read the Neo4j app password. Only the task role of the API and Orchestra reads that secret, and it may decrypt with the Neo4j stack's key only through Secrets Manager. The execution role has no ECR permissions, because nothing is pulled.
3. **Custom resource role:** See [One-Time Tasks](#one-time-tasks).
4. **Neo4j lookup role:** The `Neo4jEeLookup` custom resource reads the Neo4j EE values with it. It may call `ssm:GetParameters` only on `/neo4j-ee/<EeStackName>/*` and write only to its own log group. See [Connection to Neo4j EE](network-architecture.md#connection-to-neo4j-ee).

## Encryption

### Encryption at Rest

| Data | Key |
|---|---|
| Secrets Manager, RDS | The deployment's DataKey |
| The EFS file system | The deployment's DataKey, or `ExistingDataKeyArn` when it is set |
| CloudWatch log groups | The CloudWatch Logs service default. No log group names the DataKey. |
| Root volume | The account's default EBS key |
| ALB access log bucket | AES256 |

The launch template sets `Encrypted: true` on the root device with no `KmsKeyId`, so the account default EBS key encrypts the volume. The root volume holds the images, container writable layers, and ECS agent state. Naming the DataKey on the volume would need the key policy to grant the Auto Scaling service-linked role. That is not built.

### Encryption in Transit

| Hop | State |
|---|---|
| Browser to ALB | HTTPS, policy `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`. Port 80 only redirects. |
| ALB to tasks | HTTP |
| Service to service | HTTP |
| Services to RDS | TLS, `verify-full` |
| API and Orchestra to Neo4j | `neo4j+s`, with the shared truststore |
| Tasks to AWS APIs | HTTPS through the NAT gateways of the Neo4j EE VPC. The template creates no VPC endpoints. |

The two HTTP hops are limited to one caller each by security groups. [HTTPS Inside the VPC](../compliance/federal-compliance.md#https-inside-the-vpc) holds the options for encrypting them.

## Identity and Login

Hume connects directly to the customer's identity provider over OIDC. The decision is in [Direct OIDC Login](identity.md#direct-oidc-login).

1. **Generic OIDC:** `AuthProvider=Oidc` is the default. The API and the media service set `hume.security.provider=oidc` with the issuer and client id. `Native` stays as a debugging fallback and is removed before the listing.
2. **Parameters:** The template takes the issuer, the client id, `OidcJwkSetUri`, `OidcScope`, and the admin group. The defaults fit Cognito. Okta and Microsoft Entra ID set the other two parameters.
3. **Users and roles:** Hume creates each user on first login. `hume.security.oidc.roles-claim-paths` and `roles-mappings` map the admin group to `ADMINISTRATOR`. A live test on 2026-11-05 showed that the API creates no local administrator under OIDC.
4. **Provider outside the stack:** The Hume and Neo4j EE templates create no user pool. A Cognito test pool stands in for the customer's provider.

### Outbound Access to the Identity Provider

The API and media tasks must reach the provider's token and signing key endpoints. They do this through the Neo4j EE stack's NAT gateways, and the template adds no NAT gateway. An agency that routes egress through a proxy needs an allowlist entry for these endpoints. Entra ID Government uses `login.microsoftonline.us`.

### Hume to Neo4j Over SSO

The three flows and the Neo4j roles are in [Hume to Neo4j Over SSO](identity.md#hume-to-neo4j-over-sso). The proof is in [Check Hume reaches Neo4j over SSO](../guides/testing.md#check-hume-reaches-neo4j-over-sso). These notes cover what [Identity Architecture](identity.md) leaves out.

1. **The token is forwarded unchanged:** Hume puts the access token into the credentials as the password of the user name `__keycloak_token`, and the EE provider is not switched for it. Neo4j validates the signature and audience.
2. **The `aud` trigger is outside the template:** Cognito does not add the audience claim, so a pre token trigger in the Cognito test pool tooling adds it. The Hume template stays provider neutral.
3. **`sub` is the principal:** Cognito puts `username` only on the access token and `cognito:username` only on the ID token. Neo4j's security log shows the user id, not a name, and the id changes if the pool is recreated. Every role therefore comes through the group mapping, and nothing is granted to a principal.
4. **Explicit group mapping:** The admin mapping already sets an explicit mapping, so Neo4j no longer maps a group to a role of the same name. The EE stack takes one general mapping parameter. `hume-admins` maps to Hume `ADMINISTRATOR` and `neo4j-admins` maps to Neo4j `admin`, so the two stay separate.
5. **One truststore for two services:** The API opens its own Neo4j driver, so it mounts the same truststore as Orchestra. The truststore replaces the JVM roots, so it also holds the identity provider's roots. For Cognito these are the five Amazon Trust Services roots. The refresh restarts Orchestra and the API, and sign-in pauses for about a minute.
6. **Realm unset:** `hume.driver.neo4j.security.realm` defaults to null and sends no realm. A cluster rejected a login that carried a realm. An empty value is not the same as unset.
7. **Basic scheme:** Neo4j accepted the JWT as the password of `__keycloak_token` for both token types. GraphAware is asked to confirm this is supported.
8. **Orchestra has no automatic token path:** A writer step without the `#Orchestra.Neo4j.AuthToken` header fails, and readers and change data capture use a driver with no credentials. Workflows with no user run on a service account resource.
9. **Token lifetime:** The access token lives 1 hour and the refresh token 30 days. A user disabled at the provider keeps access until the next renewal fails, up to one hour.
10. **The `aud` comparison is unknown:** Whether Hume compares `aud` with its client id cannot be shown on Cognito. See [Other OIDC providers](../reference/infrastructure-setup-details.md#other-oidc-providers).

## WAF Rules

The web ACL sits on the ALB and holds two rules.

1. **Rate rule:** 10000 requests per 5 minutes per client IP address.
2. **Common rule set:** `AWSManagedRulesCommonRuleSet` blocks, except `SizeRestrictions_BODY`, which runs in Count mode. WAF inspects only the first 8 KB of a body, and uploads reach 20 GB.
3. **Logging:** WAF logs to its own log group and redacts the authorization and cookie headers.
4. **Limits:** A rate-based rule needs time to count requests, and an attack spread across many addresses can stay under the per-IP limit. The other layers and the agency's share of SC-5 are in [GovCloud Requirements](../compliance/gov-cloud.md#denial-of-service-protection-nist-sc-5).

### Listener Rules

1. **Hosts:** Port 443 routes by host name to `hume.<base>`, `hume-api.<base>` and `hume-media.<base>`.
2. **Prometheus block:** A rule at priority 10 returns 403 on `hume-api.<base>` for `/actuator/prometheus*`. None of 29 path variants returned metrics to an anonymous caller.
3. **Media paths:** The media host forwards only `/v1/download*` and `/v1/upload*`.

## Logging, Audit, and Monitoring

1. **Container logs:** Every service writes to its own CloudWatch log group. Retention is `LogRetentionDays`, 7 days by default.
2. **Audit logs:** The API writes its audit log to the EFS file system, under `/audit-logs`. Orchestra's audit log is not on the file system. The plan is to move them to stdout.
3. **ALB access logs:** They go to an S3 bucket with AES256 encryption.
4. **Alarms:** Eight alarms publish to an SNS topic: three unhealthy-target alarms, ALB target 5xx, ELB 5xx, RDS CPU, RDS free storage and the EC2 status check.
5. **Federal minimum:** [OMB M-26-14 Logging Maturity](../compliance/federal-compliance.md#omb-m-26-14-logging-maturity) holds the federal minimum.

### Customer Responsibility

The customer's security team runs the account-wide services, so the template does not create them. GuardDuty, Security Hub and AWS Config allow only one per Region.

- **CloudTrail:** It records every API call, including each use of the DataKey.
- **AWS Config, GuardDuty, Security Hub and Inspector:** The customer runs them.
- **VPC Flow Logs:** The owner of the Neo4j VPC turns them on.
- **AWS Backup:** The stack already backs up the EFS file system every day. A customer who needs anything beyond the RDS automated backups and the EFS backups adds it to their own plan.
- **Identity provider:** The customer runs their own OIDC identity provider and configures Hume and Neo4j EE to use it.
- **Neo4j EE:** The customer runs it, including Neo4j monitoring and backups.

## Backup and Recovery

1. **RDS:** Automated backups cover every Hume database on the instance.
2. **EFS:** AWS Backup takes a daily backup of the file system, which holds the media files, Orchestra `/data` and the API audit log, and keeps each backup for 35 days. See [Uploaded Files](#uploaded-files).
3. **Neo4j:** The customer's Neo4j EE deployment handles its own backups.

## Upgrades and Rollback

1. **Stack update:** The customer updates the stack with the new Hume version. The new version brings a new AMI ID and new task definitions.
2. **Instance refresh:** The launch template points at the new AMI. New tasks require the new `hume.version` label, so they start only on an instance built for it.
3. **Tasks move:** Each service stops its old task and then starts the new one, see [Start Order](#start-order).
4. **Database changes:** GraphAware says that every version can bring schema migrations to the Postgres databases. Each service migrates its own database at startup, and version 3.2.1 brings none. A local test found that an interrupted migration does not always run again, so the upgrade steps follow three rules.
    - **Snapshot:** Take a database snapshot before a version change.
    - **Copies:** Run one API copy and one media copy during the change.
    - **Interruption:** If a migration is interrupted, restore the snapshot and retry.
5. **Rollback:** The customer updates the stack back to the previous version. Rolling back across a schema migration needs a database restore, so the upgrade steps take a snapshot before every version change.
6. **Patching:** Customers patch by moving to a new AMI version, not by patching in place.
