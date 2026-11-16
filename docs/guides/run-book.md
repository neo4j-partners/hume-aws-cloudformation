# Run book: Hume and Neo4j Enterprise with OIDC sign-in

This run book builds a Hume stack that reads a private Neo4j Enterprise cluster and signs users in through an OIDC identity provider. An Amazon Cognito user pool stands in for the customer's provider. [Infrastructure setup details](../reference/infrastructure-setup-details.md) explains what each step does and how to do it by hand.

The Neo4j EE stack is built from the Neo4j project, with its own run book at `neo4j-ee/docs/run-books/oidc.md`. This run book sends you there for that stack and tells you where to come back.

Run the steps in this order:

1. [Before you start](#before-you-start)
2. [Set up the shell](#set-up-the-shell)
3. [Build the AMI](#build-the-ami)
4. [Create the identity provider](#create-the-identity-provider)
5. [Write the OIDC settings](#write-the-oidc-settings)
6. [Create the Neo4j EE stack](#create-the-neo4j-ee-stack)
7. [Read the Neo4j values](#read-the-neo4j-values)
8. [Build the truststore](#build-the-truststore)
9. [Create the Hume stack](#create-the-hume-stack)
10. [Check that it works](#check-that-it-works)
11. [Tear down](#tear-down)

Never paste a password, a licence key or a secret value into a chat, a ticket or a log.

## Before you start

You need these before the first step:

- **Tools:** Install the AWS CLI v2, the Session Manager plugin, `uv`, Python 3.11 or newer and `openssl`. The truststore step needs a JDK or Docker. Install `gitleaks` if you plan to commit.
- **AWS login:** You need a profile for the account that owns both stacks. If the profile uses SSO, run `aws sso login --profile <profile>` before each session.
- **Route 53 zone:** You need a public hosted zone in that account. The stack writes three certificate validation records and three alias records into it.
- **Registry login:** You need a login for `docker.graphaware.com`. Only the AMI build uses it.
- **Licence:** You need a Hume licence key.
- **Neo4j project:** You need a checkout of the Neo4j project, with its `neo4j-ee` folder.

Copy `.env.example` to `.env` and fill in the settings below. The file is gitignored, so never commit it. Later steps write the other settings. A command line flag beats the environment, and the environment beats `.env`.

- **`REGISTRY_USER` and `REGISTRY_PASSWORD`:** These settings hold the registry login.
- **`HUME_LICENCE_KEY`:** This setting holds the licence key.
- **`HUME_HOSTED_ZONE_ID`:** This setting holds the id of the zone, without `/hostedzone/`.
- **`HUME_BASE_DOMAIN`:** Set a base name now, because the identity provider builds the Hume sign-in address from it. The base name can be the zone name or a name inside the zone. Hume serves `hume.<base>`, `hume-api.<base>` and `hume-media.<base>`. Give every stack that runs at the same time its own base name.
- **`ALLOWED_CIDRS`:** List the one to five addresses or ranges that may reach the load balancer. A bare address becomes `/32`. `0.0.0.0/0` opens Hume to the internet and needs OIDC sign-in.
- **`NEO4J_ADVERTISED_DNS`:** Set the name the Neo4j stack will advertise, such as `neo4j-oidc.neo4j.local`.
- **`NO_BLOOM`:** Set it to `true` if the Neo4j stack will not install Bloom.

## Set up the shell

Run every command from the root of this repository.

```
export AWS_PROFILE=<profile of the account that owns both stacks>
export AWS_DEFAULT_REGION=us-east-1
aws sts get-caller-identity --query Account --output text
```

The last command must print your account number. Stop if it does not.

The `aws` commands read `AWS_DEFAULT_REGION`. The scripts in `stack/`, `verify/` and `demo/spine/` ignore it. They use `--region`, or `us-east-1` without it.

## Build the AMI

Skip this step if the account already has a Hume AMI. Put its id in `HUME_AMI_ID`.

```
./ami/create_ami.py
./ami/test_ami.py
```

- **`create_ami.py`:** This script builds the image on a temporary instance. It deletes the temporary registry secret, role and instance when it ends, and it writes the AMI id to `ami/ami-id.txt`. It asks first.
- **`test_ami.py`:** This script launches the AMI for a few minutes and prints one PASS or FAIL line per check.
- **AMI id:** `deploy.py` reads `HUME_AMI_ID` first, then `ami/ami-id.txt`. You can leave the setting empty.

Keep the AMI private. It holds proprietary Hume images, so never share it or make it public.

## Create the identity provider

```
uv run verify/cognito.py up
```

[The identity provider](../reference/infrastructure-setup-details.md#the-identity-provider) lists what `up` creates and what to do if Cognito refuses the domain prefix.

If you already know the name the Neo4j stack advertises, run `uv run verify/cognito.py up --neo4j-host <advertised-dns>` instead. The `neo4j` client then gets the Browser and Bloom addresses at once, in place of a placeholder.

## Write the OIDC settings

```
uv run verify/cognito.py env
```

- **AWS credentials:** The command reads and updates the `neo4j` app client, so it needs the shell's AWS profile.
- **Issuer check:** The command fetches the issuer's discovery document. It stops before it writes anything if the document names another issuer.
- **Root `.env`:** The command writes `HUME_OIDC_ISSUER_URI` and `HUME_OIDC_CLIENT_ID` to `.env`. The client id is the `hume-web` id.
- **Provider settings:** The command does not write `HUME_OIDC_JWK_SET_URI` or `HUME_OIDC_SCOPE`. Cognito needs neither. [Other OIDC providers](../reference/infrastructure-setup-details.md#other-oidc-providers) covers Okta and Entra ID.
- **Neo4j EE file:** The command writes `stack/.deploy/.env.neo4jee` for the name in `NEO4J_ADVERTISED_DNS`. [What cognito.py env writes](../reference/infrastructure-setup-details.md#what-cognitopy-env-writes) lists its settings.
- **Neo4j sign-in addresses:** The command sets the Neo4j Browser and Bloom addresses on the `neo4j` client for the same name. It leaves out Bloom when `NO_BLOOM=true` is set. It prints each address and marks new ones with `+` and removed ones with `-`. To sign in to Neo4j Browser, see `#sign-in-to-neo4j-browser` in the Neo4j EE run book.

## Create the Neo4j EE stack

Follow the Neo4j EE run book `neo4j-ee/docs/run-books/oidc.md` in the Neo4j project. Start at its section `#create-the-env-file`. Stop at the end of `#check-the-values-hume-reads`, which sends you back here. That run book's section `#enable-internet-egress` covers the `EnableInternetEgress` setting Hume requires. A default `deploy.py` Private or ExistingVpc stack with no `--advertised-dns` creates a private hosted zone named `neo4j-<stack>.neo4j.local`, with the alias record at the zone apex and one zone per stack. The advertised name then resolves inside the VPC with no manual DNS step. Pass `--no-create-private-dns` or `--advertised-dns` to opt out.

## Read the Neo4j values

Set the Neo4j stack name that the Neo4j EE deploy printed:

```
export NEO4J_STACK=<neo4j stack name>
```

Then read the values:

```
uv run stack/neo4j_env.py "$NEO4J_STACK" --dry-run
uv run stack/neo4j_env.py "$NEO4J_STACK"
```

The second command saves the Neo4j trust anchor to `stack/.deploy/neo4j-ca.pem` and writes `HUME_NEO4J_STACK_NAME` and `HUME_NEO4J_CA_FILE` to `.env`. The Hume stack reads the other Neo4j values itself from Parameter Store. [What neo4j_env.py reads](../reference/infrastructure-setup-details.md#what-neo4j_envpy-reads) lists them.

Hume signs in to Neo4j as the app user in `app-username`, not as the `neo4j` superuser. The command stops and names each missing value if the Neo4j stack does not publish the app user, the secrets key, the public subnets or the CA. An older Neo4j stack without `kms-key-arn` fails here.

## Build the truststore

Download the five Amazon Trust Services root certificates. Cognito's certificate chains to one of them.

```
mkdir -p stack/truststore
for name in AmazonRootCA1 AmazonRootCA2 AmazonRootCA3 AmazonRootCA4 SFSRootCAG2; do
  curl -fsS "https://www.amazontrust.com/repository/$name.pem"
done > stack/truststore/amazon-trust-services-roots.pem
```

Check each fingerprint against the list at https://www.amazontrust.com/repository/.

```
cat stack/.deploy/neo4j-ca.pem stack/truststore/amazon-trust-services-roots.pem > stack/.deploy/truststore-ca.pem
(cd stack && ./deploy.py truststore --ca-file .deploy/truststore-ca.pem)
```

The `cat` command joins the Neo4j trust anchor with the Amazon Trust Services roots. The truststore replaces the JVM default roots, and the API needs those roots to fetch Cognito's signing keys. Another identity provider needs its own roots in that file. See [The truststore](../reference/infrastructure-setup-details.md#the-truststore).

The second command prints `Set HUME_NEO4J_TRUSTSTORE_SECRET_ARN=<arn>`. Put that line in `.env`, with `HUME_NEO4J_CA_FILE=stack/.deploy/truststore-ca.pem`.

## Create the Hume stack

1. Check that the account holds what the stack needs. The command reads the AMI, the hosted zone, the three shared secrets and the Cognito pool. It changes nothing.

   ```
   uv run stack/check_prereqs.py
   ```

   The command exits 1 when a check fails or a required value is unset. Fix that item before you go on. If only `hume/licence-key` is missing, go on and use `up` in place of `deploy` in step 3.

2. Check that the address this command prints is in `ALLOWED_CIDRS`, or inside a range there.

   ```
   curl -s https://checkip.amazonaws.com
   ```

3. Create the stack. Choose a stack name that no other stack uses.

   ```
   export HUME_STACK=<hume stack name>
   ./stack/deploy.py deploy --stack-name "$HUME_STACK"
   ```

4. Check the summary, then answer `y`.

   - **`Neo4j`:** The line shows the advertised name of the Neo4j stack.
   - **`Host names`:** The line shows `hume.<base>`, `hume-api.<base>` and `hume-media.<base>`.
   - **`Sign-in` and `Client id`:** The lines show the issuer and the `hume-web` client id.

5. Wait for the create. It takes about 20 minutes. It ends with `CREATE_COMPLETE. Record: stack/.deploy/<hume stack name>.txt`. If the create fails, delete the stack as in [Tear down](#tear-down) and create it again.

6. Check the web address. Expect `HTTP/2 200`.

   ```
   WEB_URL=$(aws cloudformation describe-stacks --stack-name "$HUME_STACK" \
     --query "Stacks[0].Outputs[?OutputKey=='WebUrl'].OutputValue" --output text)
   curl -sI "$WEB_URL"
   ```

## Check that it works

1. Run the smoke check.

   ```
   ./stack/deploy.py smoke "$HUME_STACK"
   ```

2. Check the sign-in from the terminal.

   ```
   uv run verify/cognito.py check --stack-name "$HUME_STACK"
   ```

   The check signs in through the Cognito admin API, so it needs the shell's AWS profile.

3. Copy the `hume-admin` password to the clipboard. Run it as one command.

   ```
   aws secretsmanager get-secret-value --secret-id hume/hume-test-idp/cognito/hume-admin \
     --query SecretString --output text | pbcopy
   ```

4. Open `https://hume.<HUME_BASE_DOMAIN>` in a private window. Sign in as `hume-admin` and paste the password. Then clear the clipboard.

   ```
   pbcopy < /dev/null
   ```

5. If Hume shows `Request failed with status code 403` and `Jwt token contains no application roles`, the browser kept a sign-in that had only the role `USER`. Hume adds the mapped role after the first request for a new user. Run the check in step 2 again. Go on when `/users/me` shows `USER` and `ADMINISTRATOR`. Then clear the Hume site data and sign in again:

   1. Open `https://hume.<HUME_BASE_DOMAIN>` in the same window.
   2. Open DevTools with Cmd+Option+I. Go to **Application**, then **Storage**.
   3. Click **Clear site data**.
   4. Reload the page and sign in as `hume-admin`.

   A new private window also starts with no site data.

6. Check that Hume reaches Neo4j over TLS.

   ```
   ./stack/deploy.py validate "$HUME_STACK"
   ```

   Expect `CONNECTED`, `Verification: OK` and `Verify return code: 0 (ok)`, then a line that starts with `PASS`. [The TLS check](../reference/infrastructure-setup-details.md#the-tls-check) says what a pass shows.

For the black box check and the proofs, see [Check and prove a stack](testing.md). The hardening checks are a table on that page. For rotation and other work on a running stack, see [the operations manual](operations.md).

## Tear down

Delete Hume first, then Neo4j EE, then the identity provider. Each step deletes resources.

1. Delete the Hume stack. Type the stack name at the prompt.

   ```
   ./stack/deploy.py teardown "$HUME_STACK"
   ```

   Wait for `DELETE_COMPLETE`. [Tear down a Hume stack](teardown.md#tear-down-a-hume-stack) lists what the command keeps.

2. Delete the Neo4j EE stack with `#tear-down` in the Neo4j EE run book.

3. Delete the identity provider.

   ```
   uv run verify/cognito.py teardown
   aws cognito-idp list-user-pools --max-results 20 --query 'UserPools[].Name' --output text
   ```

   The list must not show `hume-test-idp`.
