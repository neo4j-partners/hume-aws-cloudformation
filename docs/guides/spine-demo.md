# Spine demo

This guide adds the Spine demo to a running Hume stack, loads its data and checks it. The demo creates a `spine` database on the Neo4j Enterprise cluster that Hume reads. Hume then shows it as the SPINE knowledge graph. The file list and a local Docker run are in the [Spine demo README](../../demo/spine/README.md).

- **Spine database:** The `spine` database is a Neo4j database of 3.0 GiB. It holds 2,596,708 nodes, and 193,038 of them are Person nodes.
- **SPINE knowledge graph:** The SPINE knowledge graph is the Hume view of the `spine` database.
- **Seed node:** The seed node is the one cluster node that holds the backup in its seeds directory. The other nodes copy the backup from it.
- **Server id:** A server id names one Neo4j server, as `SHOW SERVERS` lists it. It has the form `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` in lowercase hex.

## How it works

Two uv scripts run the setup. `demo/spine/seed_nodes.py` puts the backup on one Neo4j node. `demo/spine/spine_demo.py` runs the rest on the Hume stack.

- **What `seed_nodes.py` does:** The script finds the cluster nodes and copies the backup to one of them.
- **What `spine_demo.py` does:** The script stores the Spine config secret and copies the backup to the seed node. It loads the database on that node, runs the Hume config import and checks the counts. It sets `SpineConfigSecretArn` after the load, so the import always finds the `spine` database. The same change set adds the Spine pairs to `OidcRolesMappings` when a Spine role has no pair yet. The provider groups `spine-analysts` and `spine-officers` then grant the Hume roles `SPINE_ANALYST` and `SPINE_OFFICER`, which are the roles that see the SPINE graph. On a stack that uses the Cognito test pool, the script ends by signing in as `spine-analyst` and checks that Hume lists `SPINE_ANALYST`.
- **What belongs to the demo:** The demo alone adds the configuration import task, the admin API key with its secret `hume/<stack name>/api-key`, the API key settings on the API, the Spine config secret and the Spine pairs in `OidcRolesMappings`. A stack without `SpineConfigSecretArn`, such as the Marketplace deployment, has none of them. Its API runs with API keys off. The import hands Hume no credential. It writes only the Neo4j host and user name, and the password stays behind a vault reference.
- **Where the load runs:** The load runs `cypher-shell` on the seed node over SSM, and it connects to `neo4j+ssc://localhost:7687`. The load signs in as the `neo4j` superuser. The node reads the superuser password from Secrets Manager itself. The password is therefore never in the command text.
- **Run again:** Each script skips a step that is done. The database load runs again but creates nothing that exists, and prints the counts again. After a failure, fix the cause and run the same command again.

What you need before you start:

- **Hume stack:** The Hume stack must be running, as built in the [run book](run-book.md).
- **Superuser secret:** The load reads the ARN of the superuser password secret from the SSM parameter `/neo4j-ee/<neo4j stack name>/password-secret-arn`. Hume itself signs in as the app user and never reads this secret.
- **Neo4j EE stack:** The Neo4j EE stack is the one that the Hume stack reads. Each node must be online in SSM and reach the backup address over https. By default that address is a copy in the template bucket of the same region, read through the S3 gateway endpoint. Each node must also list `ServerSeedProvider` in its seed providers. See [Seed provider](#seed-provider).
- **Seed node:** The load runs `/usr/bin/cypher-shell` and `/usr/local/bin/aws` on the seed node. The Neo4j EE project promises both paths in its node tool contract, in `docs/architecture/template-architecture.md` under **Node tool contract**, and its AMI test checks them. Its instance role must allow `secretsmanager:GetSecretValue` on the superuser password secret.
- **uv:** Install uv. Both scripts start with `uv run --script`, so `./demo/spine/spine_demo.py` runs the script. uv installs its Python packages.
- **Sign-in:** Set the profile and the region as in [Set up the shell](run-book.md#set-up-the-shell). Set `HUME_STACK` to the stack name.
- **Settings:** Set `HUME_SPINE_CONFIG_DIR=demo/spine/hume-config` in `.env` or in the environment.
- **Cognito test pool:** The sign-in check in step 6 reads the record `stack/.deploy/hume-test-idp.cognito` and signs in as `spine-analyst`. `cognito.py up` and `sync` create that user and its group `spine-analysts`. A stack that uses another provider skips the step.

## Add the demo to a Hume stack

1. **Find the nodes.** This command changes nothing on the nodes. Without `--url` it first stages the backup in the template bucket. It probes each instance of the Neo4j EE stack over SSM and downloads nothing to the node:

   ```
   ./demo/spine/seed_nodes.py discover <neo4j stack name>
   ```

   Expect one block per instance. For a Neo4j node, the block shows the service state, the Neo4j home, the seeds directory and the seed providers. It also shows the free disk, the server id, the bytes of the backup already on the node and whether outbound https works. The output ends with the copy, load and `spine_demo.py` commands for the first node. If the seeds directory does not exist yet, `copy` creates it.

2. **Copy the backup to the seed node.** Use the instance id of the node from step 1:

   ```
   ./demo/spine/seed_nodes.py copy <neo4j stack name> --instance-id <instance id>
   ```

   The command prints the node, the target file and the server id, then asks before it changes anything. It downloads the backup, about 586 MiB, into the seeds directory of that node. It ends with `Copied <bytes> bytes. Server id: <server id>`. A second run skips a complete file. `spine_demo.py` also runs this copy, so this step is optional.

3. **Run the setup.**

   ```
   ./demo/spine/spine_demo.py "$HUME_STACK" --neo4j-stack <neo4j stack name> --instance-id <instance id>
   ```

   The first line is `Confirm setting up demo in stack <stack name>`. The script then checks the stack, the node and the backup. The plan and a `Continue? [y/N]` prompt follow. Then the script runs six steps:

   - **Step 1:** The script creates the secret `hume/<stack name>/spine-config` with the Spine config files.
   - **Step 2:** The script copies the backup to the seed node. It skips the copy when the file there is complete.
   - **Step 3:** The script runs the load on the seed node over SSM. The load checks that `SHOW SERVERS` lists the server id of the node. It then creates the `spine` database on the cluster from the seed node, with one primary on each server. It waits up to 900 seconds, then creates the roles and prints the counts. When the `spine` database exists, the load creates nothing and prints the counts again.
   - **Step 4:** The script updates the stack with `SpineConfigSecretArn`. The update adds the config import and runs it. On an OIDC stack, the same change set adds `spine-analysts=SPINE_ANALYST` and `spine-officers=SPINE_OFFICER` to `OidcRolesMappings` when no pair grants that role yet. A pair that grants the role to another group is kept. The api and media services restart for the new value. Neo4j does not restart, because it is not in the Hume stack. On a stack that already has this ARN and the roles, the script skips this step.
   - **Step 5:** The script checks the counts. See [Test it](#test-it).
   - **Step 6:** The script signs in as `spine-analyst` through the Cognito test pool and reads `/users/me`. It passes when the roles list `SPINE_ANALYST`. Hume grants a mapped role from the second request of a new user, so the script asks twice. On a native stack, without a Cognito record, or when the stack's issuer is not the pool, it prints `Skip` and the reason.

   The last line is `The demo is set up in stack <stack name>. Sign in to Hume as a user in a Spine group, such as spine-analyst, to see the SPINE graph.`

   Run the script once and wait for it to finish.

## See it in Hume

1. Sign in as `spine-analyst`. The steps are in [Sign in as each user](#sign-in-as-each-user), with the secret `hume/hume-test-idp/cognito/spine-analyst`.
2. Open **Knowledge Graphs**, then **SPINE**. A plain Hume user sees the tabs **Overview**, **Action Boards** and **Visualisations**. Only an administrator, such as `hume-admin`, gets the **Schema** tab.
3. On the **Visualisations** tab, click **Create Visualisation**. Enter a name and keep **Analyst Perspective**, the default. Click **Create**.
4. In the **Actions** side panel, under **Global**, click **Absconders and Escapes**.

The action returns 381 rows. **Birth Date** is `null` in every row, because the `spine_analyst` role in Neo4j cannot read `birthDate`.

To see the SPINE schema, sign in as `hume-admin` and open **SPINE**, then **Schema**. It shows classes such as Person, PhoneNumber, EmailAddress, Call, Message, Contact, DeviceExtraction and LocationEvent.

`spine-analyst` is a plain Hume user. It holds `USER` and `SPINE_ANALYST`, and the SPINE config grants the graph to that role. `hume-user` holds only `USER`, so Hume hides the SPINE graph from it. `hume-admin` sees the graph as an administrator, and it is in `spine-officers`, so an action on the Analyst perspective runs for it as `spine_officer` and reads every property. See [Roles](#roles).

## Sign in as each user

The Cognito test pool has one user for each role in the demo. Sign in as each one to see what that role gets. The steps are those of [Check that it works](run-book.md#check-that-it-works), with the user name in the secret id.

| User | Cognito groups | Hume roles | What it sees |
| --- | --- | --- | --- |
| `hume-admin` | `hume-admins`, `spine-officers` | `USER`, `ADMINISTRATOR`, `SPINE_OFFICER` | Every knowledge graph, SPINE included. In Neo4j the user holds `spine_officer`, which reads all of Spine, and no superuser role. |
| `hume-user` | `hume-users` | `USER` | No SPINE graph. Hume hides it from a user with no Spine role. |
| `spine-analyst` | `hume-users`, `spine-analysts` | `USER`, `SPINE_ANALYST` | The SPINE graph. In Neo4j the user holds `spine_analyst`, which cannot read `birthDate` on Person nodes. |
| `spine-officer` | `hume-users`, `spine-officers` | `USER`, `SPINE_OFFICER` | The SPINE graph. In Neo4j the user holds `spine_officer`, which reads `birthDate` too. |

`neo4j-admin` is for Neo4j Browser only. It is in no group that maps to a Hume role, so Hume rejects its token.

1. Check the roles of the user from the terminal. Put the user name after `--user`.

   ```
   uv run verify/cognito.py check --stack-name "$HUME_STACK" --user spine-officer
   ```

   The `GET /users/me` line lists the roles. Hume grants a mapped role from the second request of a new user, so the first check of a user that has never signed in shows only `USER`. Run the check again and expect the Spine role.

2. Copy the password of that user to the clipboard. Put the user name at the end of the secret id. Run it as one command.

   ```
   aws secretsmanager get-secret-value --secret-id hume/hume-test-idp/cognito/spine-officer \
     --query SecretString --output text | pbcopy
   ```

3. Open `https://hume.<HUME_BASE_DOMAIN>` in a new private window. Sign in with the user name and paste the password. Then clear the clipboard.

   ```
   pbcopy < /dev/null
   ```

4. Open **Knowledge Graphs**. Compare what you see with the table above. For a Spine user, open **SPINE** and run the action in [See it in Hume](#see-it-in-hume).

5. To switch to the next user, close the private window and open a new one. A private window starts with no site data, so the next sign-in is a fresh one. If Hume shows `Request failed with status code 403` and `Jwt token contains no application roles`, the browser kept a sign-in from before Hume granted the role. Run the check in step 1 again, then clear the Hume site data as in step 5 of [Check that it works](run-book.md#check-that-it-works), and sign in again.

## Test it

- **Counts:** Step 5 prints `PASS: 2,596,708 nodes, 193,038 Person nodes, 1,552,236 relationships` when the counts match. A difference prints `WARNING` and does not fail the run. The run fails if the database is not online or a count is missing.
- **Load alone:** `./demo/spine/seed_nodes.py load <neo4j stack name> --instance-id <instance id>` runs only the load, then prints the status and the counts. It reads the superuser password secret ARN from `--password-secret-arn`, or else from the SSM parameter `/neo4j-ee/<neo4j stack name>/password-secret-arn`. It passes only if the command on the node exits 0, the `spine` database is online and the node count is above 0. A second run changes nothing.
- **Timeout:** If the create times out, the load can still be going on. Check `SHOW DATABASE spine` again later.
- **Import:** Read the log group `/hume/<stack name>/config-cli` in CloudWatch Logs. Expect 0 WARN lines and 0 ERROR lines.
- **Protobuf warning:** During the first import, the `api` log can show one more WARN line, `Vulnerable protobuf generated type in use`. It appears once, on the first use of the eventstore stream. It has no known effect.
- **API warning:** The `api` log shows three WARN lines at the perspective imports: `ActionQueue - The batch containing 253 statements could not be sorted. This might indicate a circular entity relationship`. The import still completes. The warning has no known effect.
- **Roles:** Step 6 prints `PASS: spine-analyst has the roles USER, SPINE_ANALYST`. The same check is `uv run verify/cognito.py check --stack-name "$HUME_STACK" --user spine-analyst`. A test of a Neo4j role needs a user with that role. See [Roles](#roles).

## Remove the demo or tear down

- **Remove the demo only:** Drop the `spine` database in Neo4j, then set `SpineConfigSecretArn` back to empty. The update removes the config import. Clearing the ARN alone keeps the database.
- **Seed file:** The backup stays in the seeds directory of the seed node. Neither script deletes it.
- **Tear down the stack:** Follow [Tear down a Hume stack](teardown.md#tear-down-a-hume-stack). Teardown also deletes the log groups under `/hume/<stack name>/`, which include the config import log group.
- **Spine config secret:** Teardown deletes every secret under `hume/<stack name>/`, so the `spine-config` secret goes too.
- **Template copy:** The script stages the template at the stack's own key, which teardown deletes.

## Details

### What the script checks

- **Plan first:** `spine_demo.py` prints the profile, the region, the account, the stack and what each step will do. Apart from staging the backup in the template bucket, nothing changes until you answer `y`, or pass `--yes`.
- **Seed node check:** `spine_demo.py` stops unless `--instance-id` names a running Neo4j node of the `--neo4j-stack` stack that is online in SSM. It also stops if the SSM parameter `password-secret-arn` of the Neo4j stack is missing or is not a full secret ARN.
- **Server id check:** The scripts read the server id from the file `data/server_id` on the node. They refuse a value that is not in the server id form, so a bad value never reaches the database command. The load also stops unless `SHOW SERVERS` lists the id.
- **Change sets:** Each change set keeps every other parameter on its previous value. The script lists the entries and stops before it executes a change set that holds anything else.
- **Template:** The script stages the template at the stack's own key in the template bucket.
- **GovCloud:** The server-side copy cannot cross partitions. The scripts stop and ask you to upload the backup to `spine/spine-latest.backup` in the template bucket yourself, or to pass `--url`.
- **Safe:** The scripts delete nothing and touch no other stack. They never print a secret value.

### Seed provider

Neo4j reads `dbms.databases.seed_from_uri_providers` only at start. Each node must list `ServerSeedProvider` in `dbms.databases.seed_from_uri_providers` in `neo4j.conf`. The load seeds the database from `server://<server id>/spine-latest.backup`. The base config of the new cluster build already lists `CloudSeedProvider,ServerSeedProvider`. `discover` prints the current value for each node.

- **Older cluster:** A cluster built without that value needs the key in `/etc/neo4j/neo4j.conf` on every node, then a restart of each node, one at a time. Delete the existing line before adding it, because a duplicate key stops Neo4j from starting.
- **Lost on replace:** An edit by hand is lost when the instance is replaced.

### The Spine knowledge graph files

Hume imports the SPINE knowledge graph from a set of configuration files. The stack reads them from a secret, because they are too large for the template.

- **Files:** The files are in `demo/spine/hume-config`. They are copied unchanged from `config/hume/spine` in the hume-quickstart repo, with the same layout.
- **Quickstart `.env`:** Never copy the quickstart `.env` into this repository. It holds a real licence key.
- **`HUME_SPINE_CONFIG_DIR`:** This setting names the directory with the files. Set it to `demo/spine/hume-config` in `.env` or in the environment. `deploy.py` reads a relative path from the repository root, so the command works from any directory. An absolute path or a path that starts with `~` also works.
- **Secret:** `deploy.py` packs the files into the secret `hume/<stack name>/spine-config`. The value is base64 text of a bzip2 tar.
- **Size:** The secret holds 47,056 bytes. `deploy.py` stops if the value is over 60,000 bytes. A secret holds at most 65,536 bytes, so the limit leaves room for the files to grow.
- **Check:** `deploy.py` stops if `spine-kg/knowledge-graph.yml` is missing from the directory.
- **New stack:** A create does not store the secret. It leaves `SpineConfigSecretArn` empty, so the stack runs no config import.
- **Store the secret:** `spine_demo.py` stores the secret in its step 1. To store it without the script, run the command below. The command needs the stack name and a record file for that stack.

```
HUME_SPINE_CONFIG_DIR=demo/spine/hume-config ./stack/deploy.py spine-config "$HUME_STACK"
```

The command prints `Created secret hume/<stack name>/spine-config` and `The Spine config secret holds 47056 bytes.` It then prints `SpineConfigSecretArn=<arn>`. The import update needs that ARN, which is not secret. The command never prints the value. This command checks that the secret exists. It reads only the metadata, not the value:

```
aws secretsmanager describe-secret --secret-id "hume/$HUME_STACK/spine-config" \
  --query Name --output text
```

`deploy.py` leaves out six files and every hidden file. The copy therefore holds files that the stack does not use:

- **`resources/webhook-server.yml` and `resources/spine-http-api.yml`:** These resources need the webhook server and the Spine HTTP API. The stack runs neither.
- **`spine-kg/workflows/ccr-records-import.yml`:** This workflow needs the webhook server or the HTTP API resource.
- **`spine-kg/actions/get-ccr-records-graph.yml` and `spine-kg/actions/expand-info-about-ccr-imported-phone-numbers.yml`:** These two actions use the HTTP API.
- **`resources/spine-analytic-neo4j-sa.yml`:** This resource needs the `spine.uk.analytic` database. The backup does not hold it.

### Roles

`demo/spine/roles.cypher` creates three roles on the `spine` database. The load creates them on the seed node after the database. The file is safe to run again.

- **`spine_neo4j_sa_rw`:** This role has all database and graph privileges on `spine`.
- **`spine_analyst`:** This role can read every node and relationship, but cannot read `birthDate` on Person nodes.
- **`spine_officer`:** This role can read every node and relationship, including `birthDate`.

No Neo4j user holds these roles through a password login. A user in a Spine group gets `spine_analyst` or `spine_officer` over SSO through the EE stack's group-to-role mapping, as the `spine-analyst` sign-in on 2026-11-05 showed. No user holds `spine_neo4j_sa_rw`. `neo4j_app` holds `architect`, and [Neo4j Roles](../architecture/identity.md#neo4j-roles) says why and lists every principal on the cluster.

Hume has roles of its own for the SPINE knowledge graph, `SPINE_ANALYST` and `SPINE_OFFICER`, which the imported config defines. The Hume stack grants them from the provider groups through `OidcRolesMappings`, so a Spine user needs the mapping on the Hume side as well as the Neo4j group-to-role mapping on the EE stack. `spine_demo.py` adds the pairs for the Cognito groups in its step 4 when no pair grants the role yet. On another provider, set the pairs for its groups before `deploy.py up`, or change the parameter on a running stack. With the Cognito groups the value is:

```
HUME_OIDC_ROLES_MAPPINGS=hume-admins=ADMINISTRATOR,hume-users=USER,spine-analysts=SPINE_ANALYST,spine-officers=SPINE_OFFICER
```

A user in no Spine group gets `Neo4jInsufficientUserPrivileges` from the `spine-neo4j-sso` resource. Neo4j signs that user in with the `PUBLIC` role only, and `PUBLIC` has no access to the `spine` database. The `spine-neo4j-sa` resource still works for that user, because it signs in as `neo4j_app`.
