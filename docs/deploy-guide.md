# Deploy Hume from AWS Marketplace

> **Version:** This guide covers Hume 3.2.1, the first version of this listing.

> **Prerequisite:** This guide needs a Neo4j Enterprise Edition stack deployed from the Private template of the Neo4j EE listing in AWS Marketplace. A Neo4j EE stack from an older template lacks the Private settings and the Parameter Store values this guide reads.

This guide is for an AWS customer who subscribes to Hume in AWS Marketplace. It covers the deploy, sign-in, daily operation and teardown. It uses only the AWS console, the AWS CLI and a few standard tools.

The [run book](guides/run-book.md) and [Tear down](guides/teardown.md) describe the same stack for the team that builds it. They use the scripts in this repository, a test Amazon Cognito pool and a private AMI. This guide uses none of them.

Never paste a password, a licence key or a secret value into a chat, a ticket or a log.

## Contents

1. [What the stack builds](#what-the-stack-builds)
2. [Before you start](#before-you-start)
3. [Deploy Neo4j Enterprise Edition](#deploy-neo4j-enterprise-edition)
4. [Read the Neo4j values](#read-the-neo4j-values)
5. [Set up the identity provider](#set-up-the-identity-provider)
6. [Store the licence key](#store-the-licence-key)
7. [Build the truststore](#build-the-truststore)
8. [Create the Hume stack](#create-the-hume-stack)
9. [Sign in](#sign-in)
10. [Connect Hume to Neo4j](#connect-hume-to-neo4j)
11. [Monitor health](#monitor-health)
12. [Change the allowed addresses](#change-the-allowed-addresses)
13. [Rotate credentials and certificates](#rotate-credentials-and-certificates)
14. [Where your data lives](#where-your-data-lives)
15. [Encryption and keys](#encryption-and-keys)
16. [IAM roles](#iam-roles)
17. [Network](#network)
18. [Service quotas](#service-quotas)
19. [Costs](#costs)
20. [Upgrades](#upgrades)
21. [Tear down](#tear-down)
22. [Restore into a new stack](#restore-into-a-new-stack)

## What the stack builds

Hume extends Neo4j Enterprise Edition and has very limited use without it. You deploy Neo4j EE first. The Hume stack then joins the Neo4j VPC and reads the Neo4j cluster over TLS.

The Hume stack builds these resources:

- **Compute:** One Amazon ECS cluster runs on one EC2 instance from the Hume AMI. The AMI holds every Hume container image, so the instance contacts no registry.
- **Services:** ECS runs five Hume services: `eventstore`, `orchestra`, `api`, `media` and `web`.
- **Database:** One Amazon RDS for PostgreSQL instance holds four databases: `hume`, `orchestra`, `eventstore` and `media`.
- **Entry point:** One internet-facing Application Load Balancer serves three host names under your domain. An AWS WAF web ACL protects it.
- **Files:** One Amazon EFS file system holds the uploaded media files, the Orchestra files and the API audit log. AWS Backup backs it up every day.
- **Secrets:** AWS Secrets Manager holds every password and key that the stack generates.
- **Key:** One AWS KMS key encrypts the secrets, the database and the file system.
- **Monitoring:** CloudWatch log groups, eight CloudWatch alarms and one SNS topic report on the stack.

The stack builds no VPC, no NAT gateway and no bastion. It uses those of the Neo4j EE stack.

## Before you start

You need these things:

- **AWS account and Region:** Use one account and one Region for Neo4j EE and Hume.
- **AWS CLI:** Install AWS CLI version 2 and the Session Manager plugin. Sign in with a profile that can create CloudFormation stacks with IAM resources.
- **Tools:** Install `openssl`, `jq` and a Java runtime that provides `keytool`. They build the truststore.
- **Public hosted zone:** You need a Route 53 public hosted zone in the same account. The stack adds three records to it: `hume.<domain>`, `hume-api.<domain>` and `hume-media.<domain>`. It also adds the records that validate the certificate.
- **Identity provider:** You need an OpenID Connect identity provider that you operate. Hume signs every user in through it. It must issue access tokens that carry the user's groups.
- **Hume licence key:** GraphAware provides the licence key.
- **Your address:** Find the public IPv4 address that your browser uses. The load balancer admits only the addresses you list.

## Deploy Neo4j Enterprise Edition

Subscribe to the Neo4j Enterprise Edition listing in AWS Marketplace. Deploy its Private template with these settings:

- **`NumberOfServers`:** Set it to `3`. A single node stack has one Availability Zone, so it cannot host Hume.
- **`EnableInternetEgress`:** Set it to `true`. The Hume instance has no route of its own. It reaches the identity provider and Amazon ECS through the Neo4j NAT gateways.
- **`CreatePrivateDns`:** Set it to `true`, and let the stack create the private zone from `PrivateDnsZoneName`. Hume connects to the advertised name, which resolves only inside the VPC through that zone.
- **`AdvertisedDNS` and `CertificateArn`:** Follow the Neo4j EE instructions. The certificate must cover the advertised name, because Hume checks it.

Wait for the Neo4j stack to finish before you go on.

## Read the Neo4j values

The Neo4j EE stack publishes its values to Systems Manager Parameter Store under `/neo4j-ee/<neo4j stack name>/`. None of them is secret. Some are the ARNs of secrets.

The Hume stack reads these values itself. You give it only the Neo4j EE stack name, as `EeStackName`. A Lambda function in the Hume stack reads the VPC, the subnets, the security groups, the advertised name, the app user, its secret and the KMS key from that path. The create stops with a clear message if the Neo4j stack does not exist, has one server, or lacks a value.

To list the values, run these commands:

```
export AWS_REGION=<region>
export NEO4J_STACK=<neo4j stack name>
aws ssm get-parameters-by-path --path "/neo4j-ee/$NEO4J_STACK/" \
  --query "Parameters[].[Name,Value]" --output text
```

You need one value yourself. The `ca-pem` value is the Neo4j trust anchor, which goes into the truststore in a later step. Save it to a file. If the value reads `pending`, a Neo4j node has not finished its boot. Wait a few minutes and read it again.

```
aws ssm get-parameter --name "/neo4j-ee/$NEO4J_STACK/ca-pem" \
  --query Parameter.Value --output text > neo4j-ca.pem
```

## Set up the identity provider

Create a client for Hume in your identity provider:

- **Client type:** Use a public client with the authorization code flow. The client has no secret.
- **Redirect addresses:** Allow `https://hume.<domain>` and `https://hume.<domain>/`. Use the same addresses as sign-out addresses.
- **Groups:** Create a group for Hume administrators and a group for Hume users. The access token must carry the user's groups.

Note these values for the Hume stack:

| Value | Hume stack parameter |
|---|---|
| Issuer URL, with no trailing slash | `OidcIssuerUri` |
| Client ID | `OidcClientId` |
| Group names and the Hume roles they get | `OidcRolesMappings`, for example `hume-admins=ADMINISTRATOR,hume-users=USER` |
| Where the groups sit in the access token | `OidcRolesClaimPath`, for example `$['cognito:groups']` or `$.groups` |
| The claim that names the user | `OidcPrincipalClaim`. Cognito uses `username`. Okta and Microsoft Entra ID use `sub`. |
| The signing key address, if it is not `<issuer>/.well-known/jwks.json` | `OidcJwkSetUri`. Read it from `jwks_uri` in `<issuer>/.well-known/openid-configuration`. |
| The scopes Hume requests | `OidcScope`. The default is `openid profile email`. |

Hume has been proven only with Amazon Cognito. [Other OIDC providers](reference/infrastructure-setup-details.md#other-oidc-providers) gives the settings for Okta and Microsoft Entra ID, taken from their documentation. That section names the settings by their environment variable names. Each `HUME_OIDC_` variable maps to the `Oidc` parameter of the same name.

## Store the licence key

Store the licence key in Secrets Manager as a plain string. Use the default `aws/secretsmanager` key. The stack's roles can decrypt only with that key or the stack's own key.

1. Save the key in a file that only you can read, such as `licence.txt`.
2. Create the secret and note the ARN it prints.

   ```
   aws secretsmanager create-secret --name hume/licence-key \
     --secret-string file://licence.txt --query ARN --output text
   ```

3. Delete the file.

The ARN is the value of `HumeLicenceSecretArn`.

## Build the truststore

Hume reads Neo4j over `neo4j+s` and checks the certificate. The truststore holds the certificates that Orchestra and the API trust. It replaces the default roots of both Java runtimes. It must therefore hold every root that those two services need:

- **Neo4j:** The trust anchor in `neo4j-ca.pem`.
- **Identity provider:** The roots of your identity provider's certificate. The API fetches the signing keys over HTTPS. For Amazon Cognito, use the Amazon Trust Services roots from the [Amazon Trust Services repository](https://www.amazontrust.com/repository/).
- **Other endpoints:** The roots of any HTTPS service that your Orchestra workflows call.

The truststore holds only public certificates. Its password protects nothing else, and the Java runtime prints it to the service logs. Use a password made only for this file.

1. Join the certificates into one file, `truststore-ca.pem`. Put each certificate in PEM form, one after the other.

   ```
   cat neo4j-ca.pem identity-provider-roots.pem > truststore-ca.pem
   ```

2. Build a PKCS12 truststore from the file.

   ```
   mkdir certs
   awk '/-----BEGIN CERTIFICATE-----/ {n++} n {print > ("certs/ca" n ".pem")}' truststore-ca.pem
   export STOREPASS=$(openssl rand -hex 16)
   for f in certs/ca*.pem; do
     keytool -importcert -noprompt -alias "$(basename "$f" .pem)" -file "$f" \
       -keystore truststore.p12 -storetype PKCS12 -storepass:env STOREPASS
   done
   ```

3. Write the secret value. The JSON has two keys. `truststore` holds the file in base64. `java_tool_options` holds the Java options that point at the file. Keep the path `/opt/hume-truststore` as it is, because both services see the file there.

   ```
   jq -n \
     --arg truststore "$(base64 < truststore.p12 | tr -d '\n')" \
     --arg options "-Djavax.net.ssl.trustStore=/opt/hume-truststore -Djavax.net.ssl.trustStoreType=PKCS12 -Djavax.net.ssl.trustStorePassword=$STOREPASS -Dsun.net.inetaddr.ttl=10" \
     '{truststore: $truststore, java_tool_options: $options}' > truststore-secret.json
   ```

4. Create the secret with the default `aws/secretsmanager` key, and note the ARN it prints.

   ```
   aws secretsmanager create-secret --name hume/neo4j-truststore \
     --secret-string file://truststore-secret.json --query ARN --output text
   ```

5. Delete `truststore-secret.json`, `truststore.p12` and the `certs` folder.

The ARN is the value of `Neo4jTruststoreSecretArn`.

## Create the Hume stack

From the Hume listing in AWS Marketplace, choose the CloudFormation template and launch it in the Region of your Neo4j stack. Fill in the parameters below. Acknowledge that the stack creates IAM resources.

| Parameter | Value |
|---|---|
| `HumeAmiId` | Leave the value that Marketplace sets. It resolves to the Hume AMI for your Region. |
| `InstanceType` | `m6i.xlarge`, the default, or `m6i.large`. |
| `RootVolumeSize` | 40 GiB or more. The root volume holds the container images. |
| `AllowedCidr1` | Your address with `/32` after it, or an IPv4 range. `0.0.0.0/0` opens Hume to the internet and needs `AuthProvider` `Oidc`. |
| `AllowedCidr2` to `AllowedCidr5` | Up to four more addresses or ranges. Leave the rest empty. |
| `HumeLicenceSecretArn` | The ARN from [Store the licence key](#store-the-licence-key). |
| `DbInstanceClass`, `DbEngineVersion` | Leave the defaults, `db.t4g.medium` and `17`. |
| `DbBackupRetentionDays` | Days of automated RDS backups, from 1 to 35. The default is 7. |
| `LogRetentionDays`, `AccessLogExpirationDays` | Days to keep the service logs and the load balancer logs. |
| `AlarmEmail` | An address for the alarms. Leave it empty to subscribe no address. |
| `EventstoreCopies`, `ApiCopies`, `MediaCopies`, `WebCopies` | Leave the default, `1`. Each takes 1 to 4. |
| `HumeVersion` | Leave the default, `3.2.1`. |
| `BaseDomainName` | The domain for the three host names. It must be your hosted zone or sit inside it. |
| `HostedZoneId` | The ID of that hosted zone, which starts with `Z`. |
| `AuthProvider` | Leave the default, `Oidc`. |
| `OidcIssuerUri` to `OidcScope` | The values from [Set up the identity provider](#set-up-the-identity-provider). |
| `EeStackName` | The name of the Neo4j EE stack. See [Read the Neo4j values](#read-the-neo4j-values). |
| `Neo4jTruststoreSecretArn` | The ARN from [Build the truststore](#build-the-truststore). |
| `SpineConfigSecretArn` | Leave it empty. It is for a demo data set. |
| `DbSnapshotIdentifier`, `ExistingFileSystemId`, `ExistingDataKeyArn` | Leave them empty for a new install. To bring back the data of a deleted stack, see [Restore into a new stack](#restore-into-a-new-stack). |

The create takes about 20 minutes. When it ends with `CREATE_COMPLETE`, open the stack's **Outputs** tab. You need these outputs later:

- **`WebUrl`:** The output holds the Hume address, `https://hume.<domain>`.
- **`ClusterName`:** The output holds the name of the ECS cluster.
- **`AutoScalingGroupName`:** The output holds the name of the group that runs the instance.
- **`DatabaseEndpoint`:** The output holds the RDS address.
- **`Neo4jHost`:** The output holds the advertised name of the Neo4j cluster, which the stack read from the Neo4j EE stack.
- **`AccessLogBucketName`:** The output holds the bucket for the load balancer logs.
- **`LogGroupPrefix`:** The output holds `/hume/<stack name>/`, the prefix of the service log groups.
- **`SecretNamePrefix`:** The output holds `hume/<stack name>/`, the prefix of the generated secrets.
- **`FileSystemId`:** The output holds the ID of the EFS file system.
- **`DataKeyArn`:** The output holds the ARN of the stack's KMS key.

If you set `AlarmEmail`, confirm the subscription in the email that SNS sends.

## Sign in

1. Open the `WebUrl` address from an allowed address.
2. Sign in with an account from your identity provider. Hume creates each user at the first sign-in. The stack creates no administrator password.
3. Members of the group that `OidcRolesMappings` maps to `ADMINISTRATOR` are Hume administrators.

Hume adds the mapped role after a new user's first request. If Hume shows `Request failed with status code 403` and `Jwt token contains no application roles`, clear the site data for the Hume address and sign in again. A new private window also works.

## Connect Hume to Neo4j

Create a knowledge graph in Hume that connects to `neo4j+s://<Neo4jHost>:7687`. Sign in as the user in the `app-username` value of the Neo4j EE stack.

Do not type the Neo4j password into Hume. Set the password to a vault reference to the secret in the `app-password-secret-arn` value instead. The API and Orchestra then read the password from Secrets Manager. Their task role may read that one secret. [Keep credentials in Secrets Manager](guides/operations.md#keep-credentials-in-secrets-manager) explains what Hume stores. To use a vault reference to another secret, grant the stack's task role access to that secret.

## Monitor health

Check each layer in this order:

1. **Alarms:** Open CloudWatch alarms and filter on the stack name. Eight alarms publish to the stack's SNS topic. Three fire when a `web`, `api` or `media` target has been unhealthy for three minutes. Two fire on 5xx responses from the targets or from the load balancer. Two watch RDS CPU and free storage. One fires when the instance fails an EC2 status check.
2. **Services:** Check that each service runs its desired count.

   ```
   aws ecs describe-services --cluster <ClusterName> \
     --services eventstore orchestra api media web \
     --query "services[].[serviceName,runningCount,desiredCount]" --output table
   ```

3. **Targets:** Open the load balancer's target groups. The `web` target group checks `/`. The `api` and `media` target groups check `/actuator/health` on port 7001.
4. **Logs:** Each service writes to its own log group, `/hume/<stack name>/<service>`. The one-time tasks write to `/hume/<stack name>/db-init` and to `/aws/lambda/<stack name>-run-task`.
5. **Instance:** Connect to the instance with Session Manager. No SSH port is open.

   ```
   INSTANCE_ID=$(aws autoscaling describe-auto-scaling-groups \
     --auto-scaling-group-names <AutoScalingGroupName> \
     --query "AutoScalingGroups[0].Instances[0].InstanceId" --output text)
   aws ssm start-session --target "$INSTANCE_ID"
   ```

The web ACL limits each client address to 10,000 requests in five minutes. Its log group is the `WebAclLogGroupName` output.

## Change the allowed addresses

Update the stack in the CloudFormation console. Choose **Use existing template**, then change `AllowedCidr1` to `AllowedCidr5`. Each value is an address with `/32` or an IPv4 range. Review the change set. It must change only load balancer security group rules.

## Rotate credentials and certificates

| Material | Rotation |
|---|---|
| Stack KMS key | AWS rotates the key material every year. You do nothing. |
| Generated passwords and service keys | The template sets no rotation schedule on the secrets under `hume/<stack name>/`. |
| Hume licence key | Put the new key in the licence secret. Then force a new deployment of `api`, the only service that reads it. |
| Neo4j app password | Rotate it with the Neo4j EE procedure. Then force a new deployment of `orchestra`. |
| Neo4j certificate or identity provider roots | Build the truststore again, then refresh it on the instance. |

Force a new deployment of a service with this command:

```
aws ecs update-service --cluster <ClusterName> --service <service> --force-new-deployment
```

### Refresh the truststore

The instance writes the truststore file only at boot. After you change the truststore secret, write the file again and restart the two services that read it.

1. Build the truststore again as in [Build the truststore](#build-the-truststore). Put the new value in the existing secret, so that its ARN stays the same. After a Neo4j certificate change, the `ca-pem` value keeps the old chain until a Neo4j node is replaced. Follow the Neo4j [certificate rotation](https://github.com/neo4j-partners/neo4j-aws-cloudformation/blob/main/neo4j-ee/docs/topologies/private/architecture.md#certificate-rotation) steps first, then read `ca-pem` again.

   ```
   aws secretsmanager put-secret-value --secret-id <Neo4jTruststoreSecretArn> \
     --secret-string file://truststore-secret.json
   ```

2. Start a Session Manager session on the instance, as in [Monitor health](#monitor-health). Run these commands in it. They write the file the same way the boot script does.

   ```
   sudo -i
   export AWS_DEFAULT_REGION=<region>
   aws secretsmanager get-secret-value --secret-id <Neo4jTruststoreSecretArn> \
     --query SecretString --output text |
     python3 -c 'import base64, json, sys; s = json.load(sys.stdin); sys.stdout.buffer.write(base64.b64decode("".join(s["truststore"].split()), validate=True))' \
     > /opt/hume/truststore/hume-truststore.tmp
   chown 1001:1001 /opt/hume/truststore/hume-truststore.tmp
   chmod 0600 /opt/hume/truststore/hume-truststore.tmp
   mv -f /opt/hume/truststore/hume-truststore.tmp /opt/hume/truststore/hume-truststore
   ```

3. Force a new deployment of `orchestra` and of `api`. Each reads the file and the matching password when it starts. Sign-in and API calls fail while `api` restarts.

## Where your data lives

| Data | Location | Backup |
|---|---|---|
| Hume records | The four RDS databases | RDS automated backups for `DbBackupRetentionDays` days. The stack takes a final snapshot when it is deleted. |
| Workflow state, including resolved vault values | The `orchestra` database in RDS | As above. |
| Passwords and keys | Secrets Manager, under `hume/<stack name>/` | None needed. The stack generates them. |
| Hume licence key | The `hume` database in RDS, in plain text. The API stores it there when it first starts. | As above. |
| Uploaded media files | The EFS file system, under `/media` | AWS Backup takes a daily backup and keeps each one for 35 days. |
| Orchestra files | The EFS file system, under `/orchestra` | As above. |
| API audit log | The EFS file system, under `/audit-logs` | As above. |
| The graph | Your Neo4j EE cluster | Use the Neo4j EE backup procedures. |
| Load balancer logs | The `AccessLogBucketName` bucket | None. Objects expire after `AccessLogExpirationDays` days. |

The file system outlives the instance and the stack. Replacing the instance loses no data. The stack keeps the file system when you delete the stack.

RDS deletion protection is on. To recover the database, restore a snapshot or a point in time to a new DB instance with the RDS console. To recover files, restore an EFS recovery point with the AWS Backup console. The backups are in the default EFS backup vault, `aws/efs/automatic-backup-vault`.

## Encryption and keys

- **Stack key:** The stack creates one KMS key, `alias/hume/<stack name>`. It encrypts the generated secrets, the RDS storage, the RDS snapshots and the EFS file system. Key rotation is on. The key stays when you delete the stack, because the final snapshot and the file system need it.
- **File system traffic:** The tasks mount the file system with TLS.
- **Root volume:** The account's default EBS key encrypts the instance root volume.
- **Logs:** CloudWatch Logs encrypts the log groups with its service key. S3 encrypts the load balancer logs with SSE-S3.
- **Browser traffic:** Browsers reach the load balancer over HTTPS. The listener uses the TLS policy `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`. Port 80 only redirects to HTTPS. An ACM certificate covers the three host names.
- **Database traffic:** The services reach RDS over TLS and verify its certificate with `verify-full`.
- **Neo4j traffic:** Orchestra and the API reach Neo4j over `neo4j+s` and verify its certificate against the truststore.
- **Traffic inside the stack:** The load balancer reaches the tasks over HTTP, and the services reach each other over HTTP. Security groups limit both hops.

The stack generates these secrets under `hume/<stack name>/`:

| Secret | Purpose |
|---|---|
| `rds-master` | The RDS master login. Only the one-time database setup task uses it. |
| `db-api`, `db-orchestra`, `db-eventstore`, `db-media` | The login for each database. |
| `eventstore-password` | The password that the API uses with the event store. |
| `media-download-key` | The key the API sends when it downloads a file from media. |
| `api-to-media-key` | The key the API sends to media. |
| `media-to-api-key` | The key media sends to the API. |
| `symmetric-key` | The key the API uses to encrypt the caller's identity for media. |
| `maintenance-key` | The media maintenance key. No Hume service sends it. |

You supply two more secrets: the licence key and the truststore. The Neo4j EE stack owns the app password secret and its KMS key.

Hume stores a secret that a user creates inside Hume as base64 in the `hume` database. It has no other encryption. Keep credentials in Secrets Manager instead, as [Connect Hume to Neo4j](#connect-hume-to-neo4j) describes.

## IAM roles

The stack creates six roles. None of them has long-term access keys.

| Role | Purpose |
|---|---|
| Instance role | `EcsAgent` lets the ECS agent register the instance and run tasks in this cluster. `SsmAgent` lets the Systems Manager agent serve Session Manager. `EniTrunking` lets the boot script turn on ENI trunking for this role. `HumeBoot` lets the boot script read the truststore secret and signal CloudFormation. |
| Task execution role | ECS uses it to start the tasks. It reads the secrets under `hume/<stack name>/`, the licence secret and the truststore secret, and decrypts with the stack key. It writes the task logs. |
| Vault task role | The API and Orchestra run as this role. It reads only the Neo4j app password secret, and decrypts with the Neo4j key only through Secrets Manager for that secret. |
| Run task role | A Lambda function uses it to run the one-time database setup task during create and update. It can run only this stack's one-time task definitions in this cluster. |
| Database password reset role | A Lambda function uses it only on a stack that restores a database snapshot. It sets the restored database's master password to the value in this stack's secret. It can modify and describe only this stack's database, read only that secret, decrypt with the stack key and write its own log group. |
| Neo4j lookup role | A Lambda function uses it to read the Neo4j EE values during create and update. It can read only the parameters under `/neo4j-ee/<EeStackName>/` and write its own log group. |

Some statements use `Resource: '*'` because AWS offers no resource-level permissions for those actions. They are `ecs:DiscoverPollEndpoint`, `ec2:DescribeTags`, the Systems Manager message actions and the three ENI trunking actions.

## Network

The stack runs in the VPC of your Neo4j EE stack:

- **Load balancer:** The load balancer sits in the public subnets. It accepts ports 443 and 80 only from the `AllowedCidr` addresses.
- **Routing:** The load balancer routes by host name. `hume.<domain>` goes to `web`. `hume-api.<domain>` goes to `api`. `hume-media.<domain>` goes to `media`, but only for `/v1/download` and `/v1/upload`. The API's metrics path is blocked.
- **Instance and tasks:** The instance and every task sit in the first private subnet. Each task has its own network interface.
- **Database:** RDS sits in the private subnets and accepts port 5432 only from the instance security group.
- **File system:** The EFS mount targets sit in the first two private subnets. They accept NFS on port 2049 only from the instance security group.
- **Neo4j:** The stack adds a rule to the Neo4j load balancer security group for port 7687 from the Hume instance.
- **VPC endpoints:** The stack adds a rule to the Neo4j VPC endpoint security group for port 443 from the Hume instance.
- **Outbound through endpoints:** The Neo4j VPC has interface endpoints for Systems Manager, Secrets Manager, CloudWatch Logs and CloudFormation. The instance's calls to those services stay in the VPC.
- **Outbound through NAT:** Calls to Amazon ECS and to your identity provider leave through the Neo4j NAT gateways over HTTPS on port 443.
- **Administration:** Administrators use Session Manager. No SSH or RDP port is open.

The services find each other by private names in the `hume.local` AWS Cloud Map namespace.

### How the stack starts

1. The instance boots from the Hume AMI. Its boot script writes the truststore file and signals CloudFormation.
2. A Lambda function runs the one-time database setup task. The task creates the four databases and their logins.
3. ECS starts the five services from the images on the AMI.
4. The load balancer marks the targets healthy, and the stack completes.

## Service quotas

The stack needs one of each resource below. A default quota blocks it only in an account that is already near the limit.

- **EC2:** Running On-Demand Standard instances need room for 4 vCPUs on `m6i.xlarge` or 2 vCPUs on `m6i.large`. Read your account's value in Service Quotas.
- **Load balancing:** Application Load Balancers per Region default to 50.
- **AWS WAF:** Regional web ACLs per account default to 100.
- **RDS:** DB instances per Region default to 40.
- **Other services:** The KMS, ACM, Secrets Manager, ECS and Cloud Map defaults are far above what the stack uses.

The Neo4j EE stack adds one VPC, three NAT gateways and three Elastic IP addresses. VPCs per Region and Elastic IP addresses per Region each default to 5. An account that already holds three or more Elastic IP addresses may need an increase.

## Costs

The Hume stack adds these billed AWS resources:

- One EC2 instance of the chosen type, with its root volume.
- One single-AZ RDS instance of `db.t4g.medium` with 20 GiB of storage, plus its backups and the final snapshot.
- One Application Load Balancer and one AWS WAF web ACL.
- One KMS key and the Secrets Manager secrets.
- One EFS file system with its storage and its daily backups. The file system and its backups stay after you delete the stack.
- The CloudWatch log groups and alarms, and the load balancer log bucket.

The Neo4j EE stack and its software charges are billed separately. Use the [AWS Pricing Calculator](https://calculator.aws/) with your Region and instance types. The Hume software price is on the Marketplace listing.

## Upgrades

Hume 3.2.1 is the first version of this listing. A later version's release notes will give its upgrade steps.

## Tear down

Delete Hume before Neo4j EE. The Neo4j teardown removes the VPC, the NAT gateways and the endpoints that Hume uses.

If you may restore the data later, first copy the `DatabaseEndpoint`, `FileSystemId` and `DataKeyArn` outputs. The outputs are gone once the stack is deleted. With `AuthProvider` set to `Native`, also save the native administrator password from the `AdminPasswordSecretArn` secret in your password manager. The restored stack keeps that password.

1. Turn off RDS deletion protection. The DB instance identifier is the first part of the `DatabaseEndpoint` output.

   ```
   aws rds modify-db-instance --db-instance-identifier <identifier> \
     --no-deletion-protection --apply-immediately
   ```

2. Scale the Auto Scaling group to zero.

   ```
   aws autoscaling update-auto-scaling-group --auto-scaling-group-name <AutoScalingGroupName> \
     --min-size 0 --max-size 0 --desired-capacity 0
   ```

3. Empty the load balancer log bucket. CloudFormation cannot delete a bucket that holds objects.

   ```
   aws s3 rm "s3://<AccessLogBucketName>" --recursive
   ```

4. Delete the stack and wait for it.

   ```
   aws cloudformation delete-stack --stack-name <stack name>
   aws cloudformation wait stack-delete-complete --stack-name <stack name>
   ```

   If the delete fails, repeat steps 2 and 3, then delete the stack again.

5. Check for these leftovers and delete each one you no longer need:

   - **Final snapshot:** The database takes a snapshot before it is deleted. It costs storage until you delete it.
   - **File system:** The EFS file system in the `FileSystemId` output stays with the Hume files. It costs storage until you delete it.
   - **File system backups:** AWS Backup keeps each daily backup for 35 days in the vault `aws/efs/automatic-backup-vault`. The vault's access policy denies deleting them. Let them expire, or change the policy first.
   - **Stack key:** The key in the `DataKeyArn` output stays. Its alias is deleted with the stack. Schedule the key for deletion only after you delete the snapshot and the file system, because neither can be read without it.
   - **Validation records:** The certificate validation records stay in your hosted zone. Their names start with `_` and end in `hume.<domain>`, `hume-api.<domain>` and `hume-media.<domain>`.
   - **Secrets:** Look for secrets under `hume/<stack name>/`. Delete your licence and truststore secrets if no other stack uses them.
   - **Log groups:** Look for log groups under `/hume/<stack name>/` and the Lambda groups `/aws/lambda/<stack name>-neo4j-ee-lookup`, `/aws/lambda/<stack name>-run-task` and `/aws/lambda/<stack name>-db-master-reset`. The last one exists only on a stack that restored a snapshot.

6. Delete the Neo4j EE stack with its own instructions.

## Restore into a new stack

A new stack can take over the data of a deleted Hume stack. It needs three things that the old stack kept: the final database snapshot, the EFS file system and the KMS key. Restore in the same Region.

1. Delete the old stack as in [Tear down](#tear-down). Keep the final snapshot, the file system and the key. The file system must have no mount targets left, and deleting the stack removes them.

2. Find the final snapshot. The DB instance identifier is the first part of the old `DatabaseEndpoint` output.

   ```
   aws rds describe-db-snapshots --snapshot-type manual \
     --query "DBSnapshots[?DBInstanceIdentifier=='<identifier>'].[DBSnapshotIdentifier,SnapshotCreateTime]" \
     --output table
   ```

3. Create the Hume stack as in [Create the Hume stack](#create-the-hume-stack), with these three parameters set as well:

   | Parameter | Value |
   |---|---|
   | `DbSnapshotIdentifier` | The snapshot from step 2. |
   | `ExistingFileSystemId` | The old `FileSystemId` output. |
   | `ExistingDataKeyArn` | The old `DataKeyArn` output. |

   The stack refuses a snapshot or a file system without the key, because both stay encrypted with it.

4. Pass the same three values on every later update. A changed or cleared `DbSnapshotIdentifier` replaces the database.

5. Keep the snapshot until you have checked the restored stack.

The new stack generates new passwords and service keys. During the create, it sets the database master password and each database login to the new values. The services switch to the new keys. Users must sign in again.

The restored databases keep some old values:

- **Native administrator:** With `AuthProvider` set to `Native`, the administrator keeps the old stack's password. Hume creates the administrator only once, so the new `AdminPasswordSecretArn` secret does not apply. Sign in with the password you saved before the tear down. With `Oidc`, the default, there is no native administrator, and users sign in through the identity provider as before.
- **Licence key and Hume secrets:** The `hume` database still holds the old licence key and the secrets that users created in Hume.
- **Vault values:** The `orchestra` database still holds the resolved vault values.
- **Spine demo:** The Hume secret `ORCHESTRA_MEDIA_DOWNLOAD_KEY` keeps the old media download key. Set it to the new `media-download-key` value, or the `ccr-records-import` workflow fails with HTTP 401.
