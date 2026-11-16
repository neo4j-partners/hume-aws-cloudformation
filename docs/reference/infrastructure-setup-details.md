# Infrastructure setup details

This page explains what each step of the [run book](../guides/run-book.md) does and what Hume needs from the Neo4j stack. None of the values on this page is a secret.

## What gets built

- **Identity provider:** An Amazon Cognito user pool stands in for the customer's OIDC provider. It belongs to neither stack. Hume and Neo4j both sign users in through it.
- **Neo4j EE stack:** The Neo4j project builds a Private Neo4j Enterprise cluster with 3 nodes, in a new VPC with a network load balancer and an SSM bastion. Neo4j has no public address.
- **Hume stack:** This project builds a Hume stack that joins the Neo4j VPC. Its application load balancer sits in the VPC's public subnets. It connects to Neo4j over TLS.

Create them in this order: the identity provider, then Neo4j, then Hume. Delete them in the reverse order.

The Neo4j stack takes about 9 minutes to create. The Hume stack takes about 20 minutes. The identity provider takes a few seconds.

## The identity provider

`uv run verify/cognito.py up` creates these resources:

- **User pool:** The pool is named `hume-test-idp`. Only an administrator can add users to it.
- **Login domain:** The domain is `https://hume-test-idp.auth.us-east-1.amazoncognito.com`.
- **App clients:** The two clients are public and have no client secret. `hume-web` is for Hume. `neo4j` is for Neo4j Browser and Bloom. Both use the authorization code flow with the scopes `openid profile email`. Both allow `ALLOW_REFRESH_TOKEN_AUTH`. `hume-web` also allows `ALLOW_ADMIN_USER_PASSWORD_AUTH`, which `cognito.py check` uses. That flow needs AWS credentials, so a password alone cannot sign in through the API. The caller needs the IAM action `cognito-idp:AdminInitiateAuth`.
- **Groups:** `hume-admins` maps to the Hume role `ADMINISTRATOR`. `hume-users` maps to the Hume role `USER`. `neo4j-admins` maps to the Neo4j role `admin`. `spine-analysts` and `spine-officers` map to the Hume roles `SPINE_ANALYST` and `SPINE_OFFICER` and to the Neo4j roles `spine_analyst` and `spine_officer`.
- **Users:** `hume-admin` is in `hume-admins` and `spine-officers`, so it administers Hume and reads Spine as `spine_officer`. `hume-user` is in `hume-users`. `neo4j-admin` is in `neo4j-admins` and administers Neo4j. Hume rejects a token whose groups map to no role, so every Hume user needs a mapped group. A pool created before `hume-users` existed needs that group added by hand, because `cognito.py up` refuses an existing record.
- **Password secrets:** Each user has one secret named `hume/hume-test-idp/cognito/<user>`. The script never prints a password.
- **Record:** The file `stack/.deploy/hume-test-idp.cognito` holds ids and names only.

The `hume-web` client allows `https://hume.<HUME_BASE_DOMAIN>`, with and without a trailing slash. `up` reads `HUME_BASE_DOMAIN` from `.env` when it runs. The `neo4j` client starts with the placeholder `https://localhost:7473/browser/`, because Cognito needs a redirect address for the code flow. `cognito.py env` replaces it with the real Browser and Bloom addresses. With `up --neo4j-host <advertised-dns>`, the client gets the real addresses at once. `up` leaves the Bloom address off when `NO_BLOOM` is true.

### Profile, region and other pools

The script picks the AWS profile in this order:

1. The `--profile` flag.
2. The profile saved in the record.
3. `AWS_PROFILE` from the environment or `.env`.
4. The `deploy.py` default.

The region follows the same order, without the third step. Put `--profile`, `--region` and `--name` before the subcommand, as in `uv run verify/cognito.py --name <name> status`.

- **Domain taken:** Cognito may refuse the domain prefix because another account holds it. Run `uv run verify/cognito.py teardown`, then `uv run verify/cognito.py up --domain-prefix <other prefix>`.
- **Another pool:** `--name <name>` gives a separate pool, record and set of secrets.

### Find the issuer and the client ids

`up` prints the issuer and both client ids when it ends. To read them again, use one of these ways.

- **From the record:** `uv run verify/cognito.py status` prints `Issuer`, `HumeClientId` and `Neo4jClientId` with the other ids.
- **From a customer's provider:** Ask the provider's administrator for the issuer URL and the client id of the app registration for Hume. That client must be public and use the authorization code flow. The default scopes are `openid profile email`. [Other OIDC providers](#other-oidc-providers) covers Okta and Entra ID. The claim defaults suit Cognito. [Sign-in settings](#sign-in-settings) lists the settings that change them.

The template accepts an issuer only with `https://` and without a trailing slash. The discovery document at `<issuer>/.well-known/openid-configuration` must name the same issuer.

## OIDC client ids and claims

- **Hume client id:** `HUME_OIDC_CLIENT_ID` takes the `hume-web` id. The `neo4j` id goes only to the Neo4j stack.
- **Neo4j audience:** The Neo4j setting `OIDC_CLIENT_ID` holds both ids, separated by a comma with no space. Hume sends tokens issued to `hume-web`, so that id must be in the list.
- **Browser client:** `OIDC_BROWSER_CLIENT_ID` names the client that Browser and Bloom sign in with. It is the `neo4j` id, because only the `neo4j` client allows the Browser redirect. The id must also be in `OIDC_CLIENT_ID`.
- **Order of the ids:** The order matters only when `OIDC_BROWSER_CLIENT_ID` is left out. Browser then uses the first id in the list. The audience check accepts a token from any listed id.
- **User name claim:** Hume reads the user name from `username`, which only the access token holds. Neo4j reads it from `sub`, which both tokens hold, because Hume forwards the access token and Browser presents the ID token. Cognito access tokens carry no `aud` claim, so the test pool's pre token generation trigger adds one. Neo4j shows the `sub` value, a Cognito user id, as the user name.
- **Groups claim:** Both read groups from `cognito:groups`.
- **Admin groups:** The admin groups are separate on purpose. `hume-admins` grants no Neo4j role, and `neo4j-admins` grants no Hume role. No Hume user needs the Neo4j role `admin`. The Hume mappings come from `OidcRolesMappings`.
- **Native login:** `HUME_AUTH_PROVIDER=Native` deploys Hume with a generated administrator password instead of OIDC. Neither OIDC setting is then needed.
- **Missing setting:** On an OIDC stack, `deploy.py` stops before any AWS call if an OIDC setting is missing. Its message names the setting.

## Sign-in settings

The template takes these parameters. `deploy.py` reads each one from the setting next to it, first from the environment and then from `.env`. A setting left unset leaves the parameter at its template default.

| Template parameter | Default | `deploy.py` setting | Meaning |
|---|---|---|---|
| `AuthProvider` | `Oidc` | `HUME_AUTH_PROVIDER` | `Oidc` or `Native`. `Native` is the debugging fallback. |
| `OidcIssuerUri` | empty | `HUME_OIDC_ISSUER_URI` | The issuer, with https and no trailing slash. It is required when `AuthProvider` is `Oidc`. |
| `OidcClientId` | empty | `HUME_OIDC_CLIENT_ID` | The `hume-web` client id. It is required when `AuthProvider` is `Oidc`. |
| `OidcRolesMappings` | `hume-admins=ADMINISTRATOR,hume-users=USER` | `HUME_OIDC_ROLES_MAPPINGS` | The `group=ROLE` pairs, separated by commas, that map provider groups to Hume roles. |
| `OidcRolesClaimPath` | `$['cognito:groups']` | `HUME_OIDC_ROLES_CLAIM_PATH` | The JsonPath to the groups claim. |
| `OidcPrincipalClaim` | `username` | `HUME_OIDC_PRINCIPAL_CLAIM` | The claim that Hume uses as the user name. |
| `OidcJwkSetUri` | empty | `HUME_OIDC_JWK_SET_URI` | The address of the provider's signing keys. |
| `OidcScope` | `openid profile email` | `HUME_OIDC_SCOPE` | The scopes that hume-web requests at sign-in. The value must include `openid`. |

- **Required values:** A template rule rejects an `Oidc` deploy without the issuer or the client id. `deploy.py` stops before any AWS call when either setting is missing.
- **Key address:** An empty `OidcJwkSetUri` builds the address as `<issuer>/.well-known/jwks.json`. That path is the Cognito one. Set the parameter for any other provider.
- **No mapped role:** A user whose groups map to no role cannot sign in.
- **Native resources:** The native admin password secret, the native JWT key secret and the `AdminUsername` and `AdminPasswordSecretArn` outputs exist only when `AuthProvider` is `Native`.
- **Record file:** The stack's `stack/.deploy/<stack>.txt` record stores `AuthProvider`.

## Other OIDC providers

These steps come from the Neo4j, Okta and Microsoft documentation. None of them has been tried on a tenant, and Hume's sign-in has been proven only with Cognito. An item marked **Unverified** is an inference from that documentation.

Both providers put `aud` in the access token themselves, so neither needs the pre token generation trigger that Cognito uses. Neo4j needs the same three things from any provider's access token: a signature it can verify through the issuer's discovery document, an `aud` value listed in the EE provider's audience setting, and a groups claim named in its groups claim setting. The audience setting is a comma-separated list, so one entry can accept the Browser ID token's audience and Hume's access token audience together. On the Hume side, two values were fixed to Cognito's layout until 2026-11-05 and are now parameters with defaults that keep the Cognito behaviour: `OidcJwkSetUri`, because Okta and Entra publish their keys at other paths, and `OidcScope`, because Entra needs the exposed API scope requested at sign-in. `OidcPrincipalClaim` defaults to `username`, a Cognito claim, so both providers set it to `sub`.

The API fetches the provider's signing keys over HTTPS, so add the provider's root certificates to the truststore CA file. See [The truststore](#the-truststore).

### Okta

- **Authorization server:** Use a custom authorization server. Okta's org authorization server issues access tokens that are not meant for validation by other apps, and it cannot add groups to an access token.
- **Licence:** A custom authorization server needs API Access Management, which is an optional add-on in production orgs.
- **Issuer:** Set `HUME_OIDC_ISSUER_URI` to `https://<okta domain>/oauth2/<server id>`. The default server's id is `default`.
- **Key address:** Set `HUME_OIDC_JWK_SET_URI` to the `jwks_uri` value in `<issuer>/.well-known/openid-configuration`.
- **Scope:** Leave `HUME_OIDC_SCOPE` unset.
- **Groups claim:** On the server's Claims tab, add a claim named `groups`. Choose Access Token as the token type and Groups as the value type. Use a filter regex that matches only the groups Hume and Neo4j need, because `.*` sends every group.
- **Group settings:** Set `HUME_OIDC_ROLES_CLAIM_PATH` to `$.groups`, and set `HUME_OIDC_ROLES_MAPPINGS` to the Okta group names. The claim holds names, so the Neo4j role mapping uses names too.
- **Administrator group:** Hume administrators see nothing on a `Neo4jSSO` knowledge graph until you map one of their groups to a Neo4j role. Choose the role yourself. A Neo4j role with fewer rights than `admin` keeps a Hume administrator from being a Neo4j superuser.
- **Neo4j audience:** The access token's `aud` is the server's audience, `api://default` on the default server. The ID token's `aud` is the client id. Hume forwards the access token and Browser presents the ID token, so the Neo4j setting `OIDC_CLIENT_ID` lists both, separated by a comma. **Unverified:** The Neo4j EE stack has not been tried with a value such as `api://default` in that list.
- **Principal claim:** Set `HUME_OIDC_PRINCIPAL_CLAIM` to `sub`. Okta tokens have no `username` claim in its samples.
- **Unverified, ID token groups:** Browser reads groups from the ID token, so it probably needs a second `groups` claim with ID Token as the token type.
- **Unverified, Hume audience check:** GraphAware says that Hume does not check `aud` when it uses `jwt.issuer-uri`. Our stack sets `hume.security.oidc.issuer-uri`, and whether that setting behaves the same is an open follow-up. Okta's `aud` is not the client id, so test this first.
- **Unverified, user names:** Okta's samples show a login as the access token's `sub` and a user id as the ID token's `sub`. One person would then appear under two names in Neo4j, one from Hume and one from Browser. Roles still match through groups, but audit records do not.

### Microsoft Entra ID

- **App registration:** Use one registration for sign-in and for the API. A v2.0 access token's `aud` is the client id of the API, so one registration gives ID tokens and access tokens the same `aud`.
- **Exposed scope:** Under Expose an API, set the Application ID URI to `api://<client id>` and add a scope such as `access-token`.
- **Requested scope:** Set `HUME_OIDC_SCOPE` to `openid profile email api://<client id>/access-token`. Without the API scope, the access token is for Microsoft Graph, and Microsoft says such tokens cannot be validated.
- **Issuer:** Set `HUME_OIDC_ISSUER_URI` to `https://login.microsoftonline.com/<tenant id>/v2.0`. Use the tenant id, because the tenant-independent address returns a templated issuer.
- **Key address:** Set `HUME_OIDC_JWK_SET_URI` to the `jwks_uri` value in `<issuer>/.well-known/openid-configuration`.
- **Token version:** Set `requestedAccessTokenVersion` to `2` in the app manifest. The default gives v1.0 tokens, which must be validated against the v1.0 discovery document. The Neo4j tutorial does not mention this setting, but its v2.0 issuer and client id audience depend on it.
- **Neo4j audience:** Set the Neo4j setting `OIDC_CLIENT_ID` to the client id.
- **Groups claim:** Under Token configuration, add a groups claim and choose groups assigned to the application. The claim holds group object ids, so `HUME_OIDC_ROLES_MAPPINGS` and the Neo4j role mapping both use ids. Set `HUME_OIDC_ROLES_CLAIM_PATH` to `$.groups`.
- **Administrator group:** Hume administrators see nothing on a `Neo4jSSO` knowledge graph until you map one of their groups to a Neo4j role. Choose the role yourself. A Neo4j role with fewer rights than `admin` keeps a Hume administrator from being a Neo4j superuser.
- **Group limit:** Entra leaves the groups claim out of a JWT when a user is in more than 200 groups, counting nested groups. Neo4j has no fallback in its documentation, so **unverified:** that user would sign in with no groups and no role.
- **App Roles:** App Roles avoid the limit, because a token carries only the roles for this application. Set `HUME_OIDC_ROLES_CLAIM_PATH` to `$.roles` and the Neo4j setting `OIDC_GROUPS_CLAIM` to `roles`.
- **Principal claim:** Set `HUME_OIDC_PRINCIPAL_CLAIM` to `sub`. The value differs per application, so two registrations would give one person two names.
- **Unverified, scope in Hume:** No source confirms that hume-web sends `HUME_OIDC_SCOPE` unchanged in its sign-in request. Check the first access token's `aud` and `iss`.

The sources are the [Neo4j SSO tutorial](https://neo4j.com/docs/operations-manual/current/tutorial/tutorial-sso-configuration/), [Okta authorization servers](https://developer.okta.com/docs/concepts/auth-servers/), [Okta groups claims](https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/), [Microsoft access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens) and the [Microsoft claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference).

## What cognito.py env writes

`uv run verify/cognito.py env` reads the pool record and checks that the issuer's discovery document names the same issuer. It writes `HUME_OIDC_ISSUER_URI` and `HUME_OIDC_CLIENT_ID` to the root `.env` and generates `stack/.deploy/.env.neo4jee`. It then sets the redirect and logout addresses of the `neo4j` client. If the issuer check fails or the AWS credentials are wrong, it stops and writes nothing.

The advertised name comes from `NEO4J_ADVERTISED_DNS` in the environment or `.env`. The `--advertised-dns` flag overrides it. The command stops if neither is set.

Bloom is on by default. `NO_BLOOM=true` in the environment or `.env` turns it off, and so does the `--no-bloom` flag. This is the same switch the Neo4j EE deploy reads.

The `.env.neo4jee` file holds these settings:

| Key | Value |
|---|---|
| `OIDC_ISSUER_URI` | The pool issuer |
| `OIDC_CLIENT_ID` | `<hume-web id>,<neo4j id>` |
| `OIDC_BROWSER_CLIENT_ID` | The `neo4j` id |
| `OIDC_PRINCIPAL_CLAIM` | `sub` |
| `OIDC_GROUPS_CLAIM` | `cognito:groups` |
| `OIDC_ADMIN_GROUP` | `neo4j-admins` |
| `OIDC_GROUP_ROLE_MAPPING` | `spine-analysts=spine_analyst;spine-officers=spine_officer` |
| `ADVERTISED_DNS` | `NEO4J_ADVERTISED_DNS`, or the `--advertised-dns` value |
| `CREATE_PRIVATE_DNS` | `true` |
| `PRIVATE_DNS_ZONE` | The advertised name without its first label |
| `NO_BLOOM` | `true`, only when Bloom is off |

- **Zone:** `neo4j-oidc.neo4j.local` gives the zone `neo4j.local`. Pass `--private-dns-zone <zone>` to choose another parent. The advertised name must sit inside it.
- **Reader group:** The file has no `OIDC_READER_GROUP`, because the pool has no reader group.
- **Roles:** [Neo4j Roles](../architecture/identity.md#neo4j-roles) lists every principal that signs in to the cluster, the role each holds and why `neo4j_app` holds `architect`.
- **Deployment settings:** The file holds no mode, region or node count. The Neo4j EE run book adds them after the copy.
- **Neo4j addresses:** The `neo4j` client gets exactly the addresses in the table below. They replace the client's whole list. This drops the `up` placeholder, addresses for an earlier advertised name, and any address added with `callbacks --add`. The command prints each removed address with `-`.

| Address | When |
|---|---|
| `https://<advertised-dns>:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri` | Always |
| `https://<advertised-dns>:7473/browser/` | Always |
| `https://<advertised-dns>:7473/bloom/` | Unless Bloom is off |

## Private DNS

Hume dials the advertised name, such as `neo4j-oidc.neo4j.local`. That name resolves only inside the VPC, and only when the Neo4j stack has `CREATE_PRIVATE_DNS=true`. The setting creates a private zone on the stack's VPC, with an alias from the advertised name to the load balancer. Without it, the Hume instance cannot resolve the name.

Route 53 caches a missing name for up to 900 seconds. A query sent before the record exists can keep failing for minutes after the record is in sync. Wait, or query the name only after the record exists.

## Redirect addresses

Cognito matches a redirect address exactly, including its query string. Neo4j Browser sends `https://<advertised-dns>:7473/browser/?idp_id=sso&auth_flow_step=redirect_uri`. `sso` is the provider id that the Neo4j stack configures. `cognito.py env` sets these addresses on the `neo4j` client. It leaves out the `bloom` address when Bloom is off.

If a login page shows `redirect_mismatch`, the app asked for an address that its client does not allow. URL-decode the `redirect_uri` value in the address bar. Then allow it on the right client:

```
uv run verify/cognito.py callbacks --client <hume or neo4j> --add "<decoded redirect_uri>"
```

`--client hume` picks the `hume-web` client, and it is the default. `callbacks` refuses an address that is not https. It keeps the client's other settings, and it sets the logout addresses to the same list. Run it without `--add` to list the allowed addresses.

`callbacks --add` only adds addresses, so it cannot remove an old address from a pool you keep. `uv run verify/cognito.py callbacks --reset-hume` makes `https://hume.<HUME_BASE_DOMAIN>`, with and without a trailing slash, the only addresses on `hume-web`. It works only on the `hume` client.

The next `cognito.py env` run drops any address added to the `neo4j` client with `callbacks --add`. Add it again after that run.

## What Hume needs from the Neo4j stack

The Neo4j stack belongs to the Neo4j EE project, and Hume never creates or changes anything in it. The deploy steps are in `neo4j-ee/docs/run-books/oidc.md`.

- **Account and region:** Use one AWS account and region for both stacks. Hume attaches to the Neo4j VPC, so the two stacks must share them. The examples use `us-east-1`.
- **Profile:** You need an AWS profile that can create CloudFormation, EC2, VPC, ELB, IAM, Secrets Manager, SSM, ACM and S3 resources.
- **Licence terms:** The Neo4j template sets `NEO4J_ACCEPT_LICENSE_AGREEMENT=yes`. Whoever runs the Neo4j deploy accepts those terms for the account. Do this yourself, or with the account owner's written approval.
- **Neo4j template:** Use the Neo4j EE Private template. Only it publishes the public subnets that Hume needs, so a Public or ExistingVpc stack cannot be used. Build it with `--enable-internet-egress true`. Hume's instance has no route of its own and uses the Neo4j NAT gateways for the ECS agent and the identity provider.
- **Account limits:** A Neo4j stack with 3 nodes needs one VPC, three NAT gateways and three Elastic IPs. Check the quotas before you start.
- **Private zone:** Use a private zone that the stack creates. With a zone passed by id, a Private stack advertises the load balancer name for Bolt. The certificate does not cover that name.
- **Failed create:** The Neo4j deploy turns rollback off, so a failed create leaves its resources running, and they keep costing money. The imported certificate cannot be deleted while the stack uses it. See [A failed Neo4j create leaves no record file](../guides/troubleshooting.md#a-failed-neo4j-create-leaves-no-record-file).
- **Neo4j password:** Never print a Neo4j password. Hume reads the app user password from Secrets Manager through the ARN in `app-password-secret-arn`.

The Neo4j EE project covers the rest, in `neo4j-ee/docs/guides/local-testing.md`:

- **Prerequisites:** See `#prerequisites`.
- **Region and rollback:** See `#how-deploypy-behaves`.
- **Neo4j AMI:** See `#choose-an-ami`.
- **Certificates and DNS:** See `#tls-and-dns`.

### Use three nodes

A Hume stack needs a 3 node Neo4j Private stack. A single node stack cannot host it.

- **Single node:** A single node stack builds a VPC with two subnets in one zone. The private subnet holds the node and the bastion. The public subnet holds the NAT gateway. There is no second zone, so the stack publishes no `public-subnet-2-id`, `public-subnet-3-id` or `private-subnet-2-id`. The Hume stack and `neo4j_env.py` both refuse it with a message that asks for a three-server stack.
- **Three nodes:** A 3 node Private stack builds 3 public subnets, `10.0.1.0/24` to `10.0.3.0/24`. It builds 3 private subnets, `10.0.10.0/24` to `10.0.12.0/24`. They span 3 zones, with a NAT gateway in each public subnet.
- **What to do:** Use `--number-of-servers 3` for a Hume test.
- **Private subnets in SSM:** Only the first two private subnets are published in SSM, as `private-subnet-1-id` and `private-subnet-2-id`. The third private subnet, for the third node, is not published.
- **New stack, new values:** A new stack means a new certificate, load balancer name, security groups and subnet ids. Run `neo4j_env.py` and build the truststore again.

## What neo4j_env.py reads

`uv run stack/neo4j_env.py <neo4j stack name>` checks the values that Hume needs in the SSM parameters under `/neo4j-ee/<stack>/`. The template reads the same values itself, through the `Neo4jEeLookup` custom resource, so `.env` holds only the stack name. The script checks these values:

- **Network:** It reads `advertised-dns`, `vpc-id`, `external-sg-id` and `vpc-endpoint-sg-id`.
- **Subnets:** It reads `private-subnet-1-id`, `private-subnet-2-id`, `public-subnet-1-id`, `public-subnet-2-id` and `public-subnet-3-id`.
- **App user:** It reads `app-username` and `app-password-secret-arn`.
- **Secrets key:** It reads `kms-key-arn`, the single-Region KMS key that encrypts the Neo4j secrets. The task role of the API and Orchestra gets `kms:Decrypt` on that exact key ARN, only through Secrets Manager and only for the app password secret.
- **CA:** It reads `ca-pem`, the chain that issued the load balancer certificate or, for a self-signed certificate, the certificate itself.

The script stops when a value is missing or fails its check, and the message names each missing value. It prints the subject, issuer and end date of each certificate in `ca-pem`, then every setting.

With `--dry-run`, the script stops after printing. Without it, the script saves the trust anchor to `stack/.deploy/neo4j-ca.pem` and writes these settings to `.env`:

| Setting | Source |
|---|---|
| `HUME_NEO4J_STACK_NAME` | The Neo4j EE stack name. `deploy.py` passes it as `EeStackName`. |
| `HUME_NEO4J_CA_FILE` | `stack/.deploy/neo4j-ca.pem` |
| `HUME_BASE_DOMAIN` | The name already in `.env` |

- **How `.env` changes:** The script replaces each setting that `.env` already holds on its own line. It appends a missing one at the end. Other lines stay as they are. It uses the same `set_key` call as `allowlist.py`.
- **What it leaves out:** The script writes no OIDC setting and no truststore ARN. It never reads `password-secret-arn`, the ARN of the `neo4j` superuser. Only the Spine demo load reads that one.
- **What it changes in AWS:** It changes nothing. It only reads SSM parameters.
- **Stack shape:** The stack must be Private with 3 nodes. A one-node stack has no `private-subnet-2-id`, `public-subnet-2-id` or `public-subnet-3-id`. It must also have `EnableInternetEgress` true, so its private subnets have a default route.
- **Host name:** The host name must appear in the certificate and must resolve inside the VPC. The script does not check the host name against the certificate, because `ca-pem` is the trust anchor and need not be the server certificate. `deploy.py validate` checks the TLS connection from the Hume instance.
- **Profile and region:** The script takes `--profile` and `--region`. Otherwise it uses `AWS_PROFILE` from the environment or `.env`, then the `deploy.py` defaults.
- **Older stacks:** A Neo4j stack built before these values existed fails at `neo4j_env.py`, and the message names each missing value. Rebuild that stack with the current Neo4j EE project.
- **Secrets key:** `deploy.py` accepts only a single-Region key id, never `mrk-`.
- **Public subnets:** `public-subnet-2-id` and `public-subnet-3-id` exist only on a 3 node stack. The Hume load balancer needs two public subnets in different zones.
- **Private subnets:** The Hume instance runs in the first private subnet. The Hume database uses both, so they must be in different zones.
- **Pending CA:** A fresh or just-updated Neo4j stack can still hold the placeholder `pending` in `ca-pem` until a node boots. `neo4j_env.py` stops on it.
- **Wrong account:** The read fails when `AWS_PROFILE` names another account, because the SSM values are not there.

## The truststore

`./deploy.py truststore --ca-file .deploy/truststore-ca.pem` builds a PKCS12 truststore from every certificate in the file and stores it as the secret `hume/neo4j-truststore`. It needs `keytool` from a JDK, or docker. Orchestra and the API both mount the truststore and read the same `JAVA_TOOL_OPTIONS` value from the secret, because each opens its own Neo4j driver.

- **Identity provider roots:** The truststore replaces the Java default trust roots of both JVMs, and the API fetches the identity provider's signing keys over HTTPS. So the file must hold the provider's root certificates beside the Neo4j CA. The run book downloads the five Amazon Trust Services roots that Cognito chains to into `stack/truststore/amazon-trust-services-roots.pem`. It then joins the Neo4j CA and that file into `.deploy/truststore-ca.pem`. Another provider needs its own roots in the same file.
- **New Neo4j stack:** Build the truststore again for each new Neo4j stack, because each stack has its own certificate. `neo4j_env.py` rewrites `.deploy/neo4j-ca.pem`, so join the files again before the build.
- **Running Hume stack:** The Hume instance writes the truststore file only at boot. After a new build, `./deploy.py refresh-truststore` writes the file again and restarts `orchestra` and `api`. See [Rotate the Neo4j certificate](../guides/operations.md#rotate-the-neo4j-certificate).
- **Password:** The truststore password is public by design, because it protects a trust list, not a key.
- **Default roots:** Any other public HTTPS call from Orchestra or the API needs its roots in the file too. The RDS connections are unaffected, because each JDBC URL names its own CA bundle.

## The Hume host names

You choose `HUME_BASE_DOMAIN`. No stack publishes it. The stack derives three host names from it: `hume.<base>` for the web UI, `hume-api.<base>` for the API and `hume-media.<base>` for media.

- **Zone:** The base name must equal the hosted zone `HUME_HOSTED_ZONE_ID` or sit inside it.
- **Unique:** No running Hume stack may use the same base name.
- **Redirect:** `hume.<base>` must match a redirect address of the `hume-web` client. `cognito.py up` allowed the name built from the base that `.env` held when it ran. Run `uv run verify/cognito.py callbacks` to list the addresses.
- **Another name:** Allow it first with `uv run verify/cognito.py callbacks --add "https://hume.<base>/"`. Add it again without the trailing slash.

## How Hume connects to Neo4j

Hume connects to `neo4j+s://<advertised-dns>:7687`. It checks the Neo4j certificate against the truststore and checks the host name. The Neo4j stack's own `bolt-url` parameter says `neo4j+ssc://`, which skips the certificate check. Hume does not use that parameter.

Hume uses `neo4j+s://` because `bolt+s://` failed writes on a 3 node cluster through a direct connection pool. `neo4j-ee/docs/topologies/private/architecture.md#nlb-routing` says what the routing table returns.

- **Hume write:** Hume reads the `spine` database over `neo4j+s`. A write through Hume's Java driver is untested.
- **Python driver 6.4.1:** With a private CA, use `bolt://` or `neo4j://` with `encrypted=True` and `trusted_certificates=TrustCustomCAs(<pem file>)`. The driver does not accept `trusted_certificates` together with a `bolt+s://` or `neo4j+s://` address.

## How TLS is set up

| Connection | Setup |
|---|---|
| Browser to the load balancer | The stack requests one ACM certificate that covers the three host names, validates each name by DNS in your Route 53 zone, and creates three alias records. The load balancer has one HTTPS listener on port 443, with the policy `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`. Host rules send `hume.<base>` to web, `hume-api.<base>` to the API and `hume-media.<base>` to media. Any other host gets a fixed 403. On the media host, any path other than `/v1/download` and `/v1/upload` gets a fixed 403. The listener on port 80 only sends a 301 to the same host on port 443. Teardown removes the validation records, the alias records and the certificate. |
| Hume to RDS | The database requires TLS, and every connection verifies the server certificate. The four JDBC URLs, for the event store, Orchestra, the API and media, end in `?sslmode=verify-full&sslrootcert=/opt/hume-rds-ca.pem`. The db-init task sets `PGSSLMODE=verify-full` and `PGSSLROOTCERT=/opt/hume-rds-ca.pem`. The AMI build downloads the RDS global CA bundle, checks it against a SHA-256 pin, and stores it at `/opt/hume/config/rds/global-bundle.pem`. All five tasks mount that file read-only at `/opt/hume-rds-ca.pem`, so a stack needs an AMI built with the bundle. The bundle for GovCloud differs from the commercial one, so a GovCloud build needs its own URL and pin. |
| Browser to the identity provider | The browser opens the provider's login page over HTTPS. The `hume-web` client is public and uses the authorization code flow. |
| Hume to the identity provider | The api and media tasks call the issuer over HTTPS on port 443 to fetch its signing keys. The instance security group allows port 443 out. The task subnets need a route to the internet for this call. The NAT gateways of the Neo4j VPC provide it. |
| Hume to Neo4j | Orchestra and the API each connect with `neo4j+s` and check the certificate against one PKCS12 truststore. The API opens its own driver for each action. `deploy.py truststore` builds the truststore from a CA PEM and stores it as one secret. That PEM joins the Neo4j CA with the identity provider's roots. The instance writes the file to the host. Both tasks mount it read-only and read `JAVA_TOOL_OPTIONS` from the secret. The JVM prints that variable to its log, so the truststore password is random per build and treated as public. See [How Hume connects to Neo4j](#how-hume-connects-to-neo4j). |

## The TLS check

`./stack/deploy.py validate "$HUME_STACK"` runs `openssl s_client` on the Hume instance over SSM Run Command, because the Neo4j name resolves only inside the VPC. It copies the public CA file to `/tmp` on the instance, checks the certificate and the host name, and removes the file. It sends no password and no query.

- **Neo4j name:** The command reads the name from the stack's `Neo4jHost` parameter.
- **CA file:** The command reads the file from `--ca-file`, then `HUME_NEO4J_CA_FILE`. The `.env` value is relative to the repository root, so run the command from there.
- **Instance:** The command runs the check on the in-service instance of the stack's Auto Scaling group.
- **Result:** The command prints the `openssl` lines. It passes only when they hold `Verify return code: 0 (ok)`, and it exits 1 otherwise.

A pass shows three things:

- The name resolves from the instance.
- Port 7687 is open.
- The certificate passes the host name check.

A pass does not show that Hume uses the truststore secret, that a Bolt login works, or that a write works.

If the check prints only `connect:errno=0`, the instance cannot resolve the name or reach the port. Run `getent hosts <Neo4j name>` on the instance over SSM. An empty answer means the private zone is missing. See [Private DNS](#private-dns).

## Why teardown goes Hume first

The Neo4j teardown force-deletes the VPC, the NAT gateways and the load balancers. The Hume stack still uses all of them. Delete Hume first and wait for `DELETE_COMPLETE`. Delete the identity provider last, because both stacks sign users in through it. [Tear down](../guides/teardown.md) lists what each teardown keeps.
