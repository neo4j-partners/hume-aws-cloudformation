# Neo4j Enterprise Edition Compliance Findings

This document records how Neo4j Enterprise Edition measures against the federal requirements in [Federal Compliance](federal-compliance.md). The findings come from two sources. The first is a review of the neo4j-aws-cloudformation repository, folder `neo4j-ee/`. The second is Neo4j's official documentation, checked on 2026-10-20.

## Summary

Neo4j EE as packaged in the neo4j-aws-cloudformation repository does not meet the `Federal` level out of the box. The product supports more than the template enables. Most gaps are template and image work. Two items depend on Neo4j itself: FIPS validation wording and an SBOM.

| Requirement | Level | Status | Where the gap sits |
|---|---|---|---|
| FIPS 140-3 cryptography | `Federal`, `CMMC` | Partial | Product supports it. Template, AMI, and auth setup do not enable it. |
| TLS everywhere | `Federal` | Partial | Client traffic is covered. Intra-cluster ports are plaintext in the template. |
| SSO with PIV, CAC, or phishing-resistant MFA | `Federal` | Gap | Product supports OIDC and LDAP on Enterprise Edition. Template configures neither. |
| Logging for the security operations center | `Federal` | Partial | Template retention and forwarding. |
| Encryption at rest | `All` | Met | EBS encryption with an optional customer-managed KMS key. |
| Multi-AZ high availability | `FedRAMP High` | Met | Not checked against FedRAMP High criteria. |
| STIG-hardened host | `DoD IL4/5` | Gap | Template image. |
| Pen test, disclosure policy, SBOM | `Federal` | Partial | Neo4j publishes pen test and disclosure material. No SBOM found. |
| GovCloud partition readiness | `All` | Gap | Four hard blockers in the templates and tooling. Three need fixes and one needs confirmation from AWS or Neo4j. |

## How to Read the Evidence

Each finding below carries one of two marks.

1. **Verified in repo:** A reviewer read the repository source.
2. **Verified in docs:** The claim comes from a Neo4j documentation page cited in the finding.

Anything marked **Unverified** is an assumption.

## FIPS 140-3

Applies to: `Federal`, `CMMC`.

**Status:** Partial.

Neo4j documents a [FIPS 140-3 SSL configuration](https://neo4j.com/docs/operations-manual/current/security/ssl-fips-compatibility/). It is marked Enterprise Edition. All 2025.x and 2026.x versions support it, and only Linux is supported.

1. **How it works:** Neo4j passes crypto to a FIPS-certified OpenSSL through the dynamically linked `netty-tcnative` library. The setting `dbms.netty.ssl.provider=OPENSSL` turns this on. The default BoringSSL variant is not FIPS-certified, so the BoringSSL jars must be removed.
2. **What the customer supplies:** The documentation leaves the build and install of a FIPS OpenSSL out of scope. The Hume AMI would need to provide it.
3. **Scope of coverage:** The documentation covers TLS for Bolt, HTTPS, intra-cluster traffic, and backup. It does not state that the whole JVM runs a validated crypto provider.
4. **Authentication prerequisite:** The documentation requires a non-native authentication provider, such as LDAP or SSO. The repository's native admin password does not satisfy this. The 2026-10-31 decision keeps native login for neo4j_app and service accounts, so this prerequisite stays unmet by design.
5. **Cluster requirement:** Intra-cluster encryption must be on, with `dbms.ssl.policy.cluster.client_auth=REQUIRED`.
6. **Key and cipher rules:** Private keys must have a password that is read from an encrypted file. Ciphers are limited to AES-GCM suites on TLS 1.2 and 1.3.
7. **Verified in repo:** The repository configures no FIPS provider, FIPS kernel mode, or FIPS AMI. The AMI is stock Amazon Linux 2023, and Neo4j comes from the `neo4j-enterprise` yum package.
8. **Verified in repo:** The NLB TLS policy is `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09`. This is a post-quantum hybrid policy and not a FIPS policy. The AWS FIPS ELB policies would cover only the client-to-NLB leg.
9. **Unverified:** The documentation describes FIPS compatibility. It does not say that a given Neo4j version holds its own FIPS 140-3 certificate. Neo4j product or security must confirm what wording an assessor accepts.

## TLS Everywhere

Applies to: `Federal`. This maps to OMB M-22-09, HTTPS inside the VPC.

**Status:** Partial.

1. **Verified in repo:** The Neo4j EE `Private` and `ExistingVpc` templates require TLS. TLS ends at the NLB with an ACM certificate. The NLB then re-encrypts to a per-instance self-signed Neo4j certificate on Bolt port 7687 and HTTPS port 7473, with `client_auth` set to NONE. See `neo4j-ee/docs/architecture/template-architecture.md` in the neo4j-aws-cloudformation repository, decision AD-4.
2. **Verified in repo:** The Neo4j EE `Public` template uses plain TCP unless TLS is enabled.
3. **Verified in repo:** Intra-cluster ports 5000, 6000, 7000, and 7688 are plaintext. Only a security group protects them. `neo4j-ee/docs/topologies/private/architecture.md` in that repository records this under "Residual risk".
4. **Verified in docs:** Neo4j supports intra-cluster encryption. Setting `dbms.ssl.policy.cluster.enabled=true` with a key and certificate on every server covers these ports. See [SSL framework](https://neo4j.com/docs/operations-manual/current/security/ssl-framework/) and [Intra-cluster encryption](https://neo4j.com/docs/operations-manual/current/clustering/setup/encryption/). The gap is in the template and not in the product.

## Single Sign-On

Applies to: `Federal`. This maps to OMB M-22-09, items 1 to 3.

**Status:** Gap in the template. The product supports OIDC and LDAP.

1. **Verified in repo:** The repository configures no OIDC or LDAP. Authentication uses the native Neo4j admin password, which is read from Secrets Manager.
2. **Verified in docs:** The [SSO integration](https://neo4j.com/docs/operations-manual/current/authentication-authorization/sso-integration/), [LDAP integration](https://neo4j.com/docs/operations-manual/current/authentication-authorization/ldap-integration/), and [auth providers](https://neo4j.com/docs/operations-manual/current/authentication-authorization/auth-providers/) pages all carry the Enterprise Edition label. Browser SSO is also Enterprise only.
3. **Verified in docs:** Neo4j supports OpenID Connect. The page names Okta, Entra ID, and Google as example identity providers. Several OIDC providers can run at the same time, each with its own provider prefix.
4. **Verified in docs:** Neo4j supports LDAP, including Active Directory and OpenLDAP, over LDAPS or StartTLS. With LDAP alone, native user and role administration is turned off. The same page also shows a mixed `ldap,native` mode.
5. **Verified in docs:** There is no single `auth_providers` setting. Two settings control this: `dbms.security.authentication_providers` and `dbms.security.authorization_providers`. Values are comma-separated and tried in the declared order. Examples are `oidc-<provider>,native` and `ldap,native`.
6. **Verified in docs:** OIDC settings sit under `dbms.security.oidc.<provider>`. They include `well_known_discovery_uri`, `issuer`, `audience`, `auth_flow`, `claims.username`, `claims.groups`, and `authorization.group_to_role_mapping`. LDAP settings include `dbms.security.ldap.host`, `authentication.user_dn_template`, and `authorization.group_to_role_mapping`.
7. **Verified in docs:** Auth providers is also the name of a separate per-user feature. It uses `dbms.security.require_local_user=true` and the Cypher form `CREATE USER ... SET AUTH 'oidc-<provider>'`. It is a different mechanism from the two settings above.
8. **Verified in docs:** Group-to-role mapping works for both OIDC and LDAP. The format is `group=role1,role2;group2=role3`. It can target the built-in roles `admin`, `architect`, `publisher`, `editor`, and `reader`, or custom roles. For OIDC, identity provider groups named like Neo4j roles map automatically until any explicit mapping is set. LDAP also supports nested groups.
9. **Verified in docs:** [Neo4j Browser](https://neo4j.com/docs/browser/operations/browser-sso/) supports OIDC with PKCE or implicit flows. The SSO page marks the implicit flow as deprecated from 2026.06 and says to use PKCE. HTTPS is required, because mixed HTTP and HTTPS flows are not supported.
10. **Partly verified:** Bolt access with OIDC bearer tokens works in practice. The [Neo4j MCP authentication page](https://neo4j.com/docs/mcp/current/authentication/) and the [Enterprise Studio security page](https://neo4j.com/docs/enterprise-studio/current/security/) both show it. No driver manual page states it, so treat it as likely and not confirmed.
11. **Unverified:** Minimum versions are mostly unknown. The Browser page gives Neo4j 4.4 for self-hosted OIDC. No page read gave an introduction version for server OIDC, LDAP, or the per-user feature. The release notes should settle this.
12. **Verified in docs:** The SSO page says Neo4j needs HTTPS egress to the identity provider for the discovery URI and the JSON Web Key Set. Proxies that require credentials are not supported. The private subnets therefore need a NAT gateway or a proxy route. See [Network Design](../architecture/network-design.md#optional-nat-gateway).
13. **Verified in docs:** The FIPS guide names LDAP and SSO as the non-native providers it expects.
14. **Scope:** PIV, CAC, and MFA enforcement happens at the agency's identity provider and not in Neo4j.

## Audit and Security Logging

Applies to: `Federal`. The target is logs searchable for 6 months, retained for 1 year, and available to the agency's security operations center.

**Status:** Partial.

1. **Verified in repo:** The CloudWatch agent ships `security.log`, `debug.log`, and `cloud-init-output.log` to the log group `/neo4j/<stack>/application`. Retention is hardcoded to 90 days.
2. **Verified in repo:** VPC flow logs go to CloudWatch with 90-day retention. A CloudWatch alarm covers failed authentication. A check confirms a multi-region CloudTrail trail exists.
3. **Verified in repo:** The repository has no query logging setup with `db.logs.query`. It has no SIEM or security operations center forwarding.
4. **Unverified:** EE is believed to support query logging. It is not enabled in the template.

## Encryption at Rest

Applies to: `All`.

**Status:** Met for storage. Partial for logged access to encrypted data.

1. **Verified in repo:** EBS data volumes and root volumes are encrypted. The optional `DataVolumeKmsKeyId` parameter accepts a customer-managed KMS key. See `ebs-volumes.yaml`. The SNS alert topic uses its own KMS key.
2. **Verified in repo:** The CloudTrail check confirms only that a trail exists. It does not configure KMS data-key logging. KMS events would still appear in CloudTrail.
3. **Unverified:** Neo4j is believed to have no native at-rest encryption and to rely on EBS.

## High Availability

Applies to: `FedRAMP High`.

**Status:** Met for the AWS design. Not checked against FedRAMP High criteria.

1. **Verified in repo:** The cluster has 3 nodes. Each node runs in its own Auto Scaling group and its own Availability Zone, with its own encrypted EBS volume. An internal NLB spans 3 private subnets with cross-zone load balancing. The Auto Scaling groups replace failed nodes, and the data volumes are retained.

## STIG-Hardened Host

Applies to: `DoD IL4/5`.

**Status:** Gap.

1. **Verified in repo:** The AMI has OS patches, SSH hardening, and IMDSv2 required. It is not STIG-hardened or CIS-hardened, and its base image is not STIG-hardened.
2. **Verified in repo:** The AMI is built in commercial regions. The repository has no GovCloud AMI build and no GovCloud Marketplace availability.
3. **Next step:** A GovCloud deployment needs a separate AMI build in GovCloud, or a STIG-hardened Amazon Linux 2023 base.

## Pen Test, Disclosure Policy, and SBOM

Applies to: `Federal`. This maps to OMB M-22-09, item 9, and to the agency's ATO package.

**Status:** Partial.

1. **Verified in docs:** The [Neo4j Trust Center](https://trust.neo4j.com/) lists a Core DB pentest report, a SOC 2 Type 2 report, an information security policy, and a security whitepaper. Access to the reports probably needs a request through the Trust Center.
2. **Verified in docs:** Neo4j publishes a [responsible disclosure policy](https://neo4j.com/responsible-disclosure/) and [security advisories](https://neo4j.com/security/advisories/).
3. **Verified in docs:** Neo4j publishes a [Security Benchmark](https://neo4j.com/neoassets/support/Neo4j+Security+Benchmark_5.pdf) for Neo4j 5 LTS configuration.
4. **Verified in repo:** The repository holds no SBOM generation, no pen test evidence, and no disclosure policy. The file `docs/architecture/patch-CVE-2026-31431.md` shows ad hoc CVE patching only.
5. **Unverified:** The searches found no published Neo4j SBOM. Neo4j security or the Marketplace team must confirm whether one exists.

## GovCloud Partition Readiness

Applies to: `All`.

**Status:** Gap. The templates do not launch in GovCloud as they stand.

The audit covered `neo4j-ee/`. The sources are under `templates/src`, and `build.py` inlines them into three generated templates, so each finding also appears in the generated output. Nothing was tested in GovCloud. Statements about how a GovCloud launch behaves are expectations from how partitions work. See [The GovCloud Partition](gov-cloud.md#the-govcloud-partition) for the rules the audit applies.

### Hard blockers

1. **Hardcoded commercial partition on managed policy ARNs:** The instance role at `templates/src/iam.yaml:19`, the bastion role at `iam.yaml:90`, and `templates/src/iam-public.yaml:19` use `arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore`. In GovCloud the ARN must start with `arn:aws-us-gov` or use `${AWS::Partition}`. The IAM role would fail to create, which fails the stack.
2. **Marketplace AMI parameter:** `templates/src/parameters-common.yaml:1-3` sets `ImageId` as an SSM parameter type with the default `/aws/service/marketplace/prod-vwigx4rbvvb5s/2026.03.1`. The stack fails at parameter resolution unless the Marketplace product is published to GovCloud under that path. AWS documents that the full Marketplace catalog is not available in GovCloud. See [AWS Marketplace in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-marketplace.html) and [Publishing your product in AWS GovCloud (US)](https://docs.aws.amazon.com/marketplace/latest/userguide/govcloud-publishing.html). Whether the Neo4j listing is published there is unverified. AMIs also cannot be shared across partitions, so GovCloud needs its own AMI build in a GovCloud account.
3. **AMI build and deploy tooling assumes commercial regions:** `marketplace/create-ami.sh:26` and `marketplace/test-ami.sh:27` set `REGION="us-east-1"`. `deploy.py:36` sets `SOURCE_REGION="us-east-1"`, and `deploy.py:37-40` lists seven commercial regions and no GovCloud region. `deploy.py` copies the AMI from the source region at lines 511 and 628, and a copy cannot cross partitions. This is tooling and not template code, so the CloudFormation templates are unaffected by it.
4. **Instance type default may not exist in GovCloud:** `templates/src/parameters-common.yaml:20-32` defaults `InstanceType` to `r8i.xlarge`. The allowed values are `t3.medium` and `r8i` sizes. Regional availability of `r8i` in GovCloud is unverified. If it is missing, the deploy fails unless the user picks `t3.medium`, whose availability is also unverified.

### Silent problem

1. **Hardcoded partition in the CloudWatch agent log policy:** `templates/src/iam.yaml:59` and `templates/src/iam-public.yaml:59` use a hardcoded `arn:aws:logs:${AWS::Region}:...` resource. In GovCloud the policy would not match. The stack would create, but the CloudWatch agent could not write to `/neo4j/<stack>/application`. That would silently drop `security.log` shipping, which also undermines the logging finding above.

### Needs checking

All of these are unverified for GovCloud.

1. **Service principals:** `ec2.amazonaws.com` at `iam.yaml:15,87` and `iam-public.yaml:15`. `vpc-flow-logs.amazonaws.com` at `observability.yaml:15` and `observability-existing-vpc.yaml:15`. `cloudwatch.amazonaws.com` at `observability.yaml:67` and `observability-existing-vpc.yaml:67`. They are probably the same in GovCloud.
2. **VPC endpoint names:** `com.amazonaws.${AWS::Region}.{ssm,ssmmessages,logs,secretsmanager,s3}` at `networking-private.yaml:552,568,584,600,616` and `networking-existing-vpc.yaml:58,75,92,109`. They are templated by region. GovCloud availability of each endpoint service is unchecked.
3. **Bastion AMI and type:** The public Amazon Linux 2023 arm64 SSM parameter at `networking-private.yaml:503` and `networking-existing-vpc.yaml:8`, with `t4g.nano` at `:504` and `:9`. The parameter path and `t4g` availability in GovCloud are unchecked. The AMI build script uses the x86_64 path at `create-ami.sh:154`.
4. **NLB TLS policy:** `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` at `networking-private.yaml:387,400` and `networking-existing-vpc.yaml:172,185`, and in `networking-public.yaml` through an `If`. A launch fails if GovCloud does not offer this policy. AWS notes that ELB TLS is not FIPS 140-3 by default. See [Elastic Load Balancing in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-elb.html).
5. **Parameter Store names:** Systems Manager works in GovCloud, so Session Manager and Parameter Store are fine in principle. The [GovCloud SSM page](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-ssm.html) says parameter names must not contain export-controlled data. The templates create parameters under `/neo4j-ee/<stack>/...`, so stack names must avoid such content.
6. **Build-time internet access:** `marketplace/create-ami.sh:193` downloads the AWS CLI. Lines 199 and 203 reach `debian.neo4j.com` and `yum.neo4j.com`. These are not partition problems. The build needs outbound access. Bootstrap installs `neo4j-enterprise` from the baked repository at `templates/src/partials/install-neo4j.sh:4`. The audit found no other hardcoded URLs in `userdata.sh` or the bootstrap partials beyond the instance metadata address.

### Already partition-safe

1. `arn:${AWS::Partition}` is used at `iam.yaml:43,50,106`, `iam-public.yaml:43,50`, `observability.yaml:61,76`, and `observability-existing-vpc.yaml:61,76`.
2. The templates create no `AWS::CloudTrail::Trail`. A preflight check looks for an existing trail, so this assumes an existing account setup.
3. The audit saw no Cognito, Lambda, or other obviously GovCloud-limited resource types. The resources in use are EC2, Auto Scaling, NLB, SSM, Secrets Manager, KMS, SNS, Logs, CloudWatch, a Route 53 private zone, and flow logs.

### Not checked

The audit did not check `arn:aws` literals inside `validate-private`, which uses the AWS CLI with region-templated hostnames. It also did not check `tests/`, which holds only `arn:aws:acm` fixtures.

### Outcome

With fixes 1 to 3, a GovCloud AMI, and a valid instance type, the template is plausibly launchable. The AMI and Marketplace item and the instance type need confirmation from AWS or Neo4j.

## Open Questions for Neo4j

1. Does a Neo4j version hold its own FIPS 140-3 validation, or is the documented configuration the full claim?
2. Does Neo4j publish an SBOM that can go into an agency ATO package?
3. How does an agency request the Core DB pentest report and SOC 2 Type 2 report?
4. Which Neo4j versions introduced server OIDC, LDAP, and the per-user auth providers feature?
5. Is the Neo4j Marketplace listing published to GovCloud, and are `r8i` and `t4g` instances offered in `us-gov-west-1` and `us-gov-east-1`?
