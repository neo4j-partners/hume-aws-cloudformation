# Operations manual

This manual covers the work on a stack after it exists. To create a stack, follow [the run book](run-book.md). To remove one, follow [Tear down](teardown.md).

- **Stack:** One CloudFormation stack built from `stack/hume-ecs.template.yaml`. Its name defaults to `hume-ecs-<epoch seconds>`.
- **Record file:** The file `stack/.deploy/<stack name>.txt` that each stack writes. The scripts use it to find the stack.
- **Licence secret:** The secret `hume/licence-key`. Every stack in the account shares it.
- **Staged template:** A copy of the template in the bucket `hume-ecs-templates-<account>-<region>`. The template is larger than the 51,200 bytes CloudFormation accepts in a direct upload, so `deploy.py` stages it in this private S3 bucket and creates the stack from its URL.

## Contents

1. [Sign in](#sign-in)
2. [Change the allowed addresses](#change-the-allowed-addresses)
3. [Change a template parameter](#parameters-that-deploypy-does-not-set)
4. [Rotate the Neo4j app password](#rotate-the-neo4j-app-password)
5. [Rotate the Neo4j certificate](#rotate-the-neo4j-certificate)
6. [Turn on the Spine demo](#turn-on-the-spine-demo)
7. [Operate the Neo4j cluster](#operate-the-neo4j-cluster)
8. [Keep credentials in Secrets Manager](#keep-credentials-in-secrets-manager)
9. [What `up` does](#what-up-does)
10. [Record files](#record-files)

## Sign in

A stack signs users in through OIDC by default. You sign in with an account from the identity provider, and the stack creates no administrator password. [Check that it works](run-book.md#check-that-it-works) gives the steps with the Cognito test pool. Set `HUME_AUTH_PROVIDER=Native` to use one generated administrator password instead.

The steps below apply only to a stack deployed with `HUME_AUTH_PROVIDER=Native`. Such a stack generates a long random administrator password. It stores the password in Secrets Manager as `hume/<stack name>/admin-password`.

The commands below are for macOS, because `pbcopy` is the macOS clipboard tool. They put the password on the clipboard, so it never appears in the terminal, in the shell history or in a log.

- **Stack name:** The stack name is the short name of the stack, such as `hume-ecs-<epoch seconds>`. The commands below need the name.
- **Stack ID:** The stack ID is the full ARN that CloudFormation gives the stack. It differs from the name, and the commands below do not use it.

1. Set the profile, the region and the stack name for this terminal.

   ```
   export AWS_PROFILE=<profile>
   export AWS_DEFAULT_REGION=us-east-1
   ```

   Find the stack name in one of three ways:

   - **From `up`:** `deploy.py up` prints the name when it starts, in the line `Creating <stack name> in <region>.`
   - **From CloudFormation:** This command lists the Hume stacks that finished, with the time each was created.

     ```
     aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE \
       --query "StackSummaries[?starts_with(StackName,'hume-')].[StackName,CreationTime]" \
       --output text
     ```

   - **From the record files:** Each record file is named `<stack name>.txt`. Run `ls stack/.deploy/*.txt` from the repository root to list them.

   Then set the name:

   ```
   export HUME_STACK=<stack name>
   ```

   This optional command prints the stack ID: `aws cloudformation describe-stacks --stack-name "$HUME_STACK" --query "Stacks[0].StackId" --output text`

2. Read the user name and the address. Neither is secret.

   ```
   aws cloudformation describe-stacks \
     --stack-name "$HUME_STACK" \
     --query "Stacks[0].Outputs[?OutputKey=='AdminUsername' || OutputKey=='WebUrl'].[OutputKey,OutputValue]" \
     --output text
   ```

3. Copy the password to the clipboard. Run this in your own terminal, not in a shared or recorded one. Run it as one command. If you type it on several lines, end every line except the last with a backslash and put nothing after the backslash. If the shell splits the command before `--query`, `aws` prints the whole secret to the terminal and `pbcopy` never runs.

   ```
   aws secretsmanager get-secret-value \
     --secret-id "hume/$HUME_STACK/admin-password" \
     --query SecretString --output text | pbcopy
   ```

4. Open the web address from step 2 in a browser on an allowed address. Enter the user name, then paste the password.

5. Clear the clipboard.

   ```
   pbcopy < /dev/null
   ```

- **Keep the password private:** Never paste the password into a chat, an issue or a log.
- **When to sign in:** The API creates the administrator at startup, so sign in only after the `api` service is running.
- **Without `pbcopy`:** Use `xclip -selection clipboard` on Linux or `clip.exe` in WSL in its place.

To check the stack from outside and run the proofs, see [Check and prove a stack](testing.md).

## Change the allowed addresses

Use the script to change the addresses, not the AWS console. The deployed stack is the source of truth.

```
./stack/allowlist.py list
./stack/allowlist.py add 198.51.100.4
./stack/allowlist.py remove 198.51.100.4
```

- **Options:** `--stack-name`, `--profile` and `--region` go before the command. The stack name defaults to the only record file. The profile and region default to the record.
- **Change set check:** Each change builds a change set first. The script refuses to run it if anything other than a load balancer security group rule would change.
- **Refusals:** The script refuses a range wider than one address. It refuses to remove the last address.
- **Write-back:** After a successful change, the script writes the new list back to the record file and to `ALLOWED_CIDRS` in `.env`. A later `up` then keeps the new list.
- **Template version:** The script works only on a stack built from the current template.

An add creates exactly two rules, one per port. A remove deletes exactly two. Removing the first address moves the last address into the first slot and clears the last slot in one change set. The moved address has no access for the length of that change.

## Parameters that `deploy.py` does not set

These template parameters have no `deploy.py` flag, so a new stack uses the default. Change one with a stack update, or edit the default in the template.

| Parameter | Default | What it sets |
|---|---|---|
| `DbBackupRetentionDays` | 7 | Days of automated database backups. It takes 1 to 35. |
| `AccessLogExpirationDays` | 90 | Days before load balancer access logs expire. It takes 1 to 3650. |
| `AlarmEmail` | empty | An address for alarm mail. Empty creates the topic with no subscription. |
| `EventstoreCopies`, `ApiCopies`, `MediaCopies`, `WebCopies` | 1 | Copies of each service. Each takes 1 to 4. |

- **No copy count for Orchestra:** Orchestra always runs one copy, and no parameter can raise it.
- **Alarm mail:** After a stack gets an `AlarmEmail`, AWS sends a confirmation mail. The address receives nothing until someone follows its link.
- **Updating a parameter:** This command changes one parameter and keeps the others. Add one `ParameterKey=<name>,UsePreviousValue=true` entry for every other parameter in the stack.

  ```
  aws cloudformation update-stack --stack-name "$HUME_STACK" --use-previous-template \
    --capabilities CAPABILITY_IAM \
    --parameters ParameterKey=AlarmEmail,ParameterValue=you@example.com <the other parameters, each with UsePreviousValue=true>
  ```

## Rotate the Neo4j app password

The hume database keeps only a reference to the app password. The Spine resource `spine-neo4j-sa` holds a vault reference to the secret, and the API and Orchestra read the password through it with their task role. Orchestra also stores the resolved value in its own database for each workflow that uses the resource. A stack without `SpineConfigSecretArn` has no resource that uses the password, so it needs no Hume step.

1. Rotate the app password with the Neo4j EE run book `neo4j-ee/docs/run-books/app-password-rotation.md`. The secret keeps its ARN and its KMS key, so the Hume stack needs no change. The task role of the API and Orchestra decrypts the new value through Secrets Manager with its `kms:Decrypt` grant on `kms-key-arn`.
2. Restart `orchestra`, so that it opens a new Neo4j driver with the new password. The `reimport` command does this. Type `y` at the prompt.

   ```
   ./stack/deploy.py reimport "$HUME_STACK"
   ```

   - **What it does:** The command runs the configuration import task once and waits for it to exit 0. The import carries no password, so this run changes nothing. It then restarts `orchestra`, which holds the open Neo4j driver, and waits for it to be stable.
   - **No cache:** Hume reads the secret on every resolve. The API uses a changed secret on its next call. Orchestra resolves at the start of a workflow, so a running workflow keeps its old value until it restarts. A local test showed this result, and the rebuild test confirms it on AWS.
   - **Downtime:** `orchestra` is down for about a minute.
   - **Which stacks:** The command needs the stack output `ConfigCliTaskDefinitionArn`.
   - **Failure:** If the import exits with an error, the command stops before the restart. Read the log group `/hume/<hume stack name>/config-cli`.

## Rotate the Neo4j certificate

The Neo4j nodes publish the trust anchor at boot. A changed certificate, such as a self-signed one re-imported into ACM, needs the anchor published again and the truststore built again. ACM managed renewal keeps the chain, so it needs none of these steps.

1. Rotate the certificate with the Neo4j EE steps in `neo4j-ee/docs/topologies/private/architecture.md#certificate-rotation`. Those steps publish the new chain to `ca-pem`. Retire the old certificate only after step 4 below passes.

2. Read the Neo4j values again, join the new anchor with the identity provider roots, and build the truststore again. The secret keeps its ARN, so `.env` needs no change. The run book step [Build the truststore](run-book.md#build-the-truststore) downloads the roots file if it is missing.

   ```
   uv run stack/neo4j_env.py "$NEO4J_STACK"
   cat stack/.deploy/neo4j-ca.pem stack/truststore/amazon-trust-services-roots.pem > stack/.deploy/truststore-ca.pem
   (cd stack && ./deploy.py truststore --ca-file .deploy/truststore-ca.pem)
   ```

3. Write the new truststore to the Hume instance and restart `orchestra` and `api`. Type `y` at the prompt.

   ```
   ./stack/deploy.py refresh-truststore "$HUME_STACK"
   ```

   - **What it does:** The command writes the file from the secret the same way the instance does at boot. It then restarts `orchestra` and `api`, which each read the file and the matching password when they start, and waits for both to be stable.
   - **Downtime:** `orchestra` and `api` are down for about a minute. Sign-in and every API call fail while `api` restarts.
   - **Why not a plain restart:** Each build has a new password. A restart alone gives `orchestra` the new password with the old file.
   - **Why not a new instance:** Replacing the instance loses no data, because the media files, the Orchestra files and the API audit logs are on EFS. It would still restart every service, and the command restarts only `orchestra` and `api`.

4. Check the connection again.

   ```
   ./stack/deploy.py validate "$HUME_STACK"
   ```

## Turn on the Spine demo

The Spine demo is an update to a running stack. Its steps are in the [Spine demo guide](spine-demo.md).

## Operate the Neo4j cluster

The Neo4j EE project owns these procedures. Each path is in that project's repository.

- **Access:** `neo4j-ee/docs/topologies/private/operator-guide.md` covers the bastion, the tunnels, the password and Cypher queries.
- **Logs and metrics:** `neo4j-ee/docs/topologies/private/operator-guide.md#observability` covers both.
- **Problems:** `neo4j-ee/docs/topologies/private/local-testing.md#troubleshooting` lists known failures.
- **Failover:** `neo4j-ee/docs/topologies/private/local-testing.md#failover-suite` tests the loss of a node.
- **Resilience:** `neo4j-ee/docs/topologies/private/local-testing.md#resilience-suite` runs the longer tests.
- **Backups:** `neo4j-ee/docs/guides/ebs-snapshots.md` covers disk snapshots.
- **Release check:** `neo4j-ee/validate-private/README.md#release-gate` gives the checks for a release.
- **New AMI:** `neo4j-ee/marketplace/README.md#updating-the-ami` gives the steps.
- **Tear down:** `neo4j-ee/docs/run-books/oidc.md#tear-down` gives the normal steps. `neo4j-ee/docs/guides/local-testing.md#tear-down` covers a failed create.

## Keep credentials in Secrets Manager

Keep credentials such as database passwords in AWS Secrets Manager. Point a Hume resource at the secret with a `vault:aws` reference. Hume reads the value when it needs it. The hume database keeps only the reference. Orchestra stores the resolved value in its own database for each workflow that uses the resource, so the secret is as protected as the orchestra database and its snapshots.

## What `up` does

`up` runs these steps. The run book uses `deploy`, which skips step 4.

1. It reads the AMI, the addresses and the zone from `.env`.
2. It checks that the base domain name equals the zone or sits inside it, and that the Neo4j VPC, subnets and security groups exist and fit. Each private subnet must have an active default route, which the Neo4j stack provides with `EnableInternetEgress` true. The route check reads the VPC route tables, so the deployer needs `ec2:DescribeRouteTables`. If a check fails, `up` stops before it creates anything.
3. It prints the account, profile, region, AMI, instance type, addresses, host names and hosted zone, then asks for confirmation. Pass `--yes` to skip the question.
4. It writes the licence key to the secret `hume/licence-key` and replaces any value already there. Every stack shares this secret, so everyone who deploys into the same account must use the same licence key.
5. If the template is larger than the direct upload limit, it first uploads the template to the bucket `hume-ecs-templates-<account>-<region>`. It creates that bucket with all public access blocked.
6. It creates the stack.

The flags are in the [command reference](../reference/commands.md#deploypy). Run `uv run stack/check_prereqs.py` to confirm the AMI, the zone, the three secrets and the Cognito pool in one read-only run. See [check_prereqs.py](../reference/commands.md#check_prereqspy).

## Record files

Each stack writes a record file `stack/.deploy/<stack name>.txt`.

- **Contents:** The file holds IDs, names, the allowed addresses, the stack outputs, the certificate validation record and the name of the staged template. It never holds a licence key or a registry login.
- **When it is written:** `up` writes it before it creates the stack, so teardown works after a failed create.
- **When it is deleted:** A successful teardown deletes it.

`teardown` always needs the stack name. When you give no stack name, `smoke`, `allowlist.py`, `blackbox.py` and the `prove.py` commands that take a stack name use the only record file. If several record files exist, they stop, list each stack with the age of its record, and ask for the name. Pass the stack name when more than one stack exists.
