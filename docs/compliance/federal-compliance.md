# Federal Compliance

This document covers every compliance level above `All` in [Release Scope by Compliance Level](release-scope.md#release-scope-by-compliance-level): `Federal`, `CMMC`, `FedRAMP High` and `DoD IL4/5`. The Marketplace listing meets the `All` level, and the architecture documents describe it. Each level here adds the requirements of another group of customers. For each level, this document lists what it requires, the design decisions still open, the work to reach it and the questions for GraphAware. The GovCloud rules that apply to every deployment are in [GovCloud Requirements](gov-cloud.md).

Related documents:

1. [Architecture Overview](../architecture/architecture-overview.md)
2. [GovCloud Requirements](gov-cloud.md)
3. [Detailed Architecture](../architecture/detailed-architecture.md)
4. [Neo4j Enterprise Edition Compliance Findings](ee-compliance.md)
5. [Acronyms](../reference/acronyms.md)

## Compliance Levels

These documents tag each requirement and decision with the level that drives it. The tags also mark release stages. See [Release Scope by Compliance Level](release-scope.md#release-scope-by-compliance-level) for which tags belong to the Marketplace listing and which to the federal-ready stage.

1. **`All`:** This level covers every GovCloud customer, federal or not. The Marketplace listing meets it, and its rules are in [GovCloud Requirements](gov-cloud.md).
2. **`Federal`:** This level covers every federal agency system, civilian or DoD, at any FedRAMP baseline. FISMA and the OMB memos bind these systems, including systems a contractor runs for an agency. The design targets FedRAMP Moderate, and this level is the federal-ready release stage. See [Federal Level](#federal-level).
3. **`FedRAMP High`:** This level covers agency systems rated High. Hume must limit concurrent sessions per user and record who changed each item. Signed images and a recovery plan with backups in the second GovCloud Region also come into play. When availability is rated High, RDS runs Multi-AZ and each service runs more than one copy. See [FedRAMP High](#fedramp-high).
4. **`DoD IL4/5`:** This level covers DoD mission owners at Impact Level 4 or 5. Users log in with a CAC through DoD PKI. The ALB carries a DoD PKI certificate. The AMI is hardened to the DISA STIG, and the logs feed the mission owner's Cybersecurity Service Provider. See [DoD Impact Levels 4 and 5](#dod-impact-levels-4-and-5).
5. **`CMMC`:** This level covers defense contractors that hold CUI. They need FIPS-validated cryptography, and OMB M-22-09 does not bind them.

Most FedRAMP High and DoD requirements fall on the customer's processes. A few change Hume, and each section below names them.

Non-federal GovCloud customers are mainly ITAR companies, defense contractors, state and local governments, which may follow GovRAMP, formerly StateRAMP, and universities holding CUI. FISMA and M-22-09 do not bind them. Many adopt the same controls by choice or by contract.

| Level | Who | What it adds to the Hume design | Source |
|---|---|---|---|
| `All` | Every GovCloud customer | Partition-aware ARNs. AMI listing. No CloudFront. KMS encryption at rest. TLS and WAF on the ALB. Single sign-on to the customer's identity provider. | AWS Marketplace policies, GovCloud service availability |
| `Federal` | Civilian agencies and DoD | FIPS-validated cryptography in every layer. Single sign-on to the agency identity provider with PIV or phishing-resistant MFA. HTTPS inside the VPC. Logged access to encrypted data. Logs kept searchable for 6 months and retrievable for 1 year, and available to the agency's security operations center. | FISMA, FedRAMP SC-13 and SC-8, OMB M-22-09, OMB M-26-14 |
| `FedRAMP High`, `DoD IL4/5` | Agency systems rated High, and DoD mission owners at IL4 or IL5 | When availability is rated High, the system needs Multi-AZ RDS and more than one copy of each service. DoD adds the private shape, CAC login, and a STIG-hardened host. | NIST SP 800-53B High baseline, FedRAMP Rev5 High baseline, DoD Cloud Computing SRG |
| `CMMC` | Defense contractors holding CUI | FIPS-validated cryptography. M-22-09 does not apply. | NIST SP 800-171 Rev 2 control 3.13.11, 32 CFR 170.2 |
| ITAR only | Companies holding export-controlled data | US-person-only admin access. Data kept in the Region. FIPS stays optional, because the encryption carve-out also accepts other cryptography at AES-128 strength or better. | ITAR, 22 CFR 120.54(a)(5) |

## Federal Zero Trust Rules: OMB M-22-09

Applies to: `Federal`. ITAR, CMMC, and state and local customers are not bound by it, though many adopt the same rules.

[OMB M-22-09](https://www.whitehouse.gov/wp-content/uploads/2022/01/M-22-09.pdf) is the Federal Zero Trust Strategy. Hume must meet its rules to be approved by a federal customer.

1. **Who issued it:** The Office of Management and Budget issued it in January 2022.
2. **What it says:** It tells federal agencies to stop trusting any network by default and to check every user, device, and request. It sets goals in five areas: identity, devices, networks, applications, and data.
3. **Status:** The goals were due by the end of fiscal year 2024, and the memo is still in force.
4. **Who it binds:** The memo binds agencies, not vendors. Agencies pass its rules on to vendors through their security reviews before they approve a system for use.

The rules that apply to Hume are these.

1. **Single sign-on through the agency's identity provider:** The memo requires agencies to use central identity systems that plug into their applications. Hume must hand logins to the agency's identity provider. GraphAware chose direct OIDC, with no Keycloak. Hume's built-in accounts are separate from that system, so agencies are likely to reject them.
2. **Phishing-resistant MFA:** Agency staff, contractors, and partners must use MFA that resists phishing. The memo names PIV smart cards and FIDO2 or WebAuthn security keys. It tells agencies to turn off SMS and voice codes, one-time codes, and push approvals for routine access. Ordinary two-factor login does not qualify.
3. **PIV and CAC smart cards:** OMB M-19-17 makes the PIV card the primary way to log in to federal systems. DoD uses its own version, the CAC card. M-22-09 allows FIDO2 keys where PIV is not practical. Hume does not need to read smart cards itself. The agency's identity provider checks the card and sends Hume a signed token.
4. **MFA at the application, not the network:** The memo requires MFA at the application layer. A VPN, a private deployment, or a WAF IP allowlist does not count as MFA. These remain extra layers only.
5. **Device checks:** Agencies must consider at least one signal about the user's device when they grant access. The agency's identity provider usually performs this check. This is another reason to use the agency's identity provider instead of a separate user directory.
6. **No forced password complexity or rotation:** Systems must not require special characters or regular password changes. This matters only if Hume keeps any passwords, such as an emergency admin account.
7. **HTTPS inside the VPC:** Agencies must enforce authenticated HTTPS for all production HTTP traffic, including traffic that never leaves the internal network. Traffic from the ALB to the Hume tasks, the health checks, and calls between Hume services must use TLS. This may need Hume changes. The hop-by-hop table is in [Encryption in Transit](../architecture/detailed-architecture.md#encryption-in-transit), and the options are below.
8. **Treat the app as internet-facing:** The memo says every application should be treated as internet-accessible from a security perspective. Agencies cannot rely on the network perimeter to keep attackers out. The private deployment therefore needs the same login, MFA, TLS, and patching as the public deployment.
9. **Security testing and vulnerability reports:** Agencies must run application security testing and use independent third-party testers. They must also accept vulnerability reports from the public. Agencies will ask GraphAware for recent penetration test results and a vulnerability disclosure policy.
10. **Logged access to encrypted data:** When data is encrypted at rest in the cloud, a separate system must log every attempt to decrypt it. Customer-managed KMS keys do this, because CloudTrail logs each use of the key.

## Open Decisions for GraphAware: Federal

These are the design choices still open that apply to federal customers and to defense contractors that hold CUI. Each one names its options. The technical questions that feed them sit under each decision and in [Open Questions for GraphAware](#open-questions-for-graphaware).

### HTTPS Inside the VPC

Applies to: `Federal`.

Two hops inside the VPC are in the clear today: the ALB to each task, and service to service. Security groups limit each hop to one caller, but neither is encrypted. [OMB M-22-09](#federal-zero-trust-rules-omb-m-22-09) requires agencies to encrypt all HTTP traffic, including traffic that never leaves their own network. NIST SP 800-53 control SC-8 carries the same rule into the FedRAMP Moderate and High baselines.

There are three options.

1. **Hume serves HTTPS:** Each service receives a certificate and key at task start and trusts the deployment's CA. This meets M-22-09 as written.
2. **TLS sidecar in each task:** A proxy container beside each Hume container terminates TLS and forwards to Hume over `localhost`. Hume does not change. The sidecar covers inbound calls only, and the proxy image must itself be a FIPS build.
3. **Documented deviation:** Plain HTTP inside the VPC for the first release, declared as a deviation that the customer's authorizing official must accept. This is an interim position, not a destination.

The first two options need a certificate source. One path is one private CA generated at deploy time and stored in Secrets Manager, with each task issuing its own certificate at start. The hop-by-hop table is in [Encryption in Transit](../architecture/detailed-architecture.md#encryption-in-transit).

**Question for GraphAware, HTTPS inside the VPC:** Can each service serve HTTPS with a supplied certificate and trust a supplied CA?

- **Current design:** The stack ends TLS at the load balancer. The target groups use plain HTTP.
- **GraphAware answer:** Yes. Each service can be given private certificates.

### FIPS-Validated Cryptography

Applies to: `Federal`, `CMMC`.

The question for Hume is which validated provider its images can run on, and whether every algorithm Hume uses is an approved one.

1. **The rule:** M-22-09 does not mention FIPS. The requirement for [FIPS 140-3](https://csrc.nist.gov/pubs/fips/140-3/final) validated encryption comes from [FISMA](https://www.cisa.gov/topics/cyber-threats-and-advisories/federal-information-security-modernization-act) and [FedRAMP](https://www.fedramp.gov/). FISMA requires FIPS 140 validated crypto, and FedRAMP's SC-13 setting requires FIPS-validated or NSA-approved crypto at Low, Moderate and High. This covers civilian agencies at FedRAMP Moderate. The DoD SRG builds on the FedRAMP baseline, so DoD customers carry the same rule at every impact level. Defense contractors that hold CUI carry it too, under NIST SP 800-171 control 3.13.11, which CMMC Level 2 assesses.
2. **ITAR alone does not require it:** A commercial customer that uses GovCloud only to hold ITAR data has no blanket FIPS requirement. ITAR's encryption carve-out accepts FIPS 140 modules, or other cryptography at least as strong as AES-128.
3. **Java brings its own cryptography:** Java applications carry their own cryptography in Java Cryptography Extension providers, so turning on FIPS mode in the operating system does not make a Java service compliant on its own.

The cryptography sits in three layers.

1. **The instance:** Amazon Linux 2023 has a FIPS mode that switches the kernel and the system OpenSSL to validated modules. It does not reach the libraries inside the containers. Agencies may require it on the ECS instances, so Hume must be tested on an instance with FIPS mode turned on.
2. **Java inside the Hume images:** The Java crypto provider in each image decides compliance, and Hume must use only approved algorithms. The candidate providers and the algorithm rules are under [Java Providers in the Hume Images](#java-providers-in-the-hume-images).
3. **AWS services:** The ALB listener uses a FIPS TLS security policy, such as `ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04`, which needs testing with Hume's clients. See [Security policies for your Application Load Balancer](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/describe-ssl-policies.html). AWS APIs are reached at FIPS endpoints where GovCloud offers them, and KMS, Secrets Manager, RDS, and S3 encrypt with validated modules already. See [AWS FIPS 140-3 compliance](https://aws.amazon.com/compliance/fips/) for the AWS FIPS endpoints.

The GovCloud test account verifies the result end to end: Hume on instances with FIPS mode on, behind an ALB with a FIPS policy, with a FIPS provider in the images.

**Question for GraphAware, FIPS:** Can the Hume images run with a FIPS-validated Java crypto provider?

- **Details:** Which JDK and base image do the images use? Has Hume run on a FIPS provider before? Which algorithms does it use for JWT signing, keystore protection and password hashing? Can its TLS stack be limited to approved cipher suites?
- **Current design:** Not tried. The stack does not turn on FIPS mode.
- **GraphAware answer:** Today the main way to run on a FIPS-validated provider is to run Hume on Red Hat OpenJDK in FIPS mode. On RHEL that JDK sends crypto to the RHEL NSS module. AFP runs Hume this way. To support FIPS mode in the Docker images, GraphAware would have to switch to a Red Hat OpenJDK 21 base image. The design plans for that path in the federal stage. The first listing does not depend on it.
    - **JWT signing:** Hume signs tokens with HMAC-SHA256 or HMAC-SHA512, which FIPS approves.
    - **Licence keystore:** Every current release uses RC2-40 and HMAC-SHA1, which fail with FIPS enabled. Upcoming patches and Hume 3.2.0 and later use PBES2 with AES-256 and RSA-3072.
    - **Password hashing:** Hume uses BCrypt, which FIPS does not approve. It does not fail today with FIPS enabled, because Hume's implementation skips NSS. GraphAware knows about this and tracks it.
- **Still open:** Can Hume's TLS stack be limited to approved cipher suites? Red Hat validates its OpenJDK FIPS mode on a RHEL host, and the AMI is built on Amazon Linux 2023. Does a Red Hat OpenJDK 21 container on an Amazon Linux 2023 host count as validated, or must the AMI host run RHEL?

#### Java Providers in the Hume Images

Each provider changes the base image or the JDK configuration.

1. **Bouncy Castle FIPS:** It holds an active FIPS 140-3 certificate for its 2.0 release. Its 2.1 certificate is also active, but it carries an interim validation caveat, so a reviewer may ask for the 2.0 release until the caveat is lifted. Its 140-2 certificates became historical in September 2026. It is the only option that keeps the cryptography API, TLS, and the keystore inside one validated module, and it requires a BCFKS keystore rather than JKS or PKCS12.
2. **Amazon Corretto Crypto Provider, FIPS build:** It is not validated itself. It wraps the validated AWS-LC FIPS module and offers no TLS and no keystore, so those would still run on an unvalidated provider.
3. **Red Hat OpenJDK in FIPS mode:** It is validated only on a RHEL host, so it does not apply on Amazon Linux 2023.
4. **Stock Corretto on a FIPS host:** It is not validated. The host's FIPS mode does not change the JDK's provider.
5. **Approved algorithms only:** No MD5, no bcrypt, scrypt, or Argon2 for password hashing, RSA keys of 2048 bits or more, and approved TLS cipher suites. Hume's JWT signing, keystore format, password hashing if built-in accounts remain, and TLS client settings all need checking against that list.

## OMB M-26-14 Logging Maturity

Applies to: `Federal`.

[OMB M-26-14](https://www.whitehouse.gov/wp-content/uploads/2026/05/M-26-14-Ensuring-Effective-and-Efficient-Agency-Logging-and-Network-Visibility-to-Defend-Against-Evolving-Cyber-Threats.pdf) was issued on May 22, 2026, and rescinds M-21-31. It binds every federal agency system at every FedRAMP level. The minimum it sets for every federal system is below, and the rest of this section covers its maturity model.

1. **Availability:** Logs must be readily available to the agency's top-level security operations center.
2. **Time:** Timestamps must come from a traceable time source.
3. **Content:** The logs must show who acted, network sources and destinations, data that was read, changed, or deleted, and changes to privileges.

The template's logging is in [Logging, Audit, and Monitoring](../architecture/detailed-architecture.md#logging-audit-and-monitoring).

1. **Deadlines:** Agencies must reach Level 1 within 120 days, Level 2 within 180 days, and Level 3 within 320 days of CISA's [Logging Reference Architecture](https://www.cisa.gov/resources-tools/resources/logging-reference-architecture). Each level must hold across every element of the model.
2. **Updates:** When CISA updates the reference architecture, agencies must reach Level 2 within 60 days and Level 3 within 120 days.
3. **Scoring:** A system's overall level is the lowest level of any element.
4. **FedRAMP gap:** The FedRAMP High setting for AU-11 still cites M-21-31 for log retention.

Two elements of the model touch Hume's logs directly. The other three cover the agency's asset inventory, log coverage, and alerting.

| Element | Level 1 | Level 2 | Level 3 | Level 4 |
|---|---|---|---|---|
| Data Retention | Retrievable for 6 months | Retrievable for 12 months | Searchable for 3 months and retrievable for 12 months | Searchable for 6 months and retrievable for 12 months |
| Log Management | Stored | Stored and encrypted at rest | Encrypted in transit and at rest, and regularly hashed for veracity | Encrypted, access granted just in time, permissions reviewed, and retired logs removed with two-gate approval |

Hume's logs already go to CloudWatch Logs over TLS and are encrypted at rest by the service. Level 3 adds regular hashing to prove the logs were not changed. Who adds the hashing is question 1 in [Open Questions for GraphAware](#open-questions-for-graphaware).

## Federal Level

This section covers what it takes to reach the `Federal` level. That level applies to every federal agency system, civilian or DoD, at any FedRAMP baseline. It is the federal-ready release stage and it builds on the Marketplace listing stage. The rules are in [Federal Zero Trust Rules: OMB M-22-09](#federal-zero-trust-rules-omb-m-22-09), and the product decisions are in [Open Decisions for GraphAware: Federal](#open-decisions-for-graphaware-federal).

### Start Here

- **Finish the general list:** OMB M-22-09 tells agencies to treat every application as internet-facing. The private deployment needs the same login, MFA, TLS and patching as the public one. Every item under Template Changes in the general plan must be done first.
- **Pick the two product decisions:** [HTTPS Inside the VPC](#https-inside-the-vpc) and [FIPS-Validated Cryptography](#fips-validated-cryptography) both need GraphAware product work. The template design cannot be final until they are chosen.

### Product Work for GraphAware

- **Single sign-on:** Hume must hand every login to the agency identity provider, because M-22-09 rules out Hume's built-in accounts. GraphAware chose direct OIDC, with no Keycloak, and the template maps an identity provider group to the `ADMINISTRATOR` role. See item 1 of [Federal Zero Trust Rules](#federal-zero-trust-rules-omb-m-22-09) and [Direct OIDC Login](../architecture/identity.md#direct-oidc-login).
- **Phishing-resistant MFA:** The agency identity provider checks the PIV card or FIDO2 key and sends Hume a signed token. Hume does not read smart cards. See items 2 to 4 of [Federal Zero Trust Rules](#federal-zero-trust-rules-omb-m-22-09).
- **No shared administrator:** The `Native` login makes one shared administrator with a password that never rotates. The default `Oidc` login has no shared administrator. Federal customers need one account per person from their own identity provider.
- **HTTPS inside the VPC:** The load balancer to task hop and the service to service calls use plain HTTP today. GraphAware picks one of the three options in [HTTPS Inside the VPC](#https-inside-the-vpc).
- **FIPS-validated cryptography:** FIPS mode on the host does not reach the Java inside the containers. GraphAware answers the provider and algorithm questions in [FIPS-Validated Cryptography](#fips-validated-cryptography).

### Template and AMI Work

- **FIPS on the infrastructure:** Add a template parameter that turns on FIPS mode on the instances. Use a FIPS TLS policy on the load balancer listener, such as `ELBSecurityPolicy-TLS13-1-2-FIPS-2023-04`. Reach AWS APIs at FIPS endpoints where GovCloud has them. The ECS agent may need internet access to reach `ecs-fips`. That is an open question.
- **Certificate source:** HTTPS inside the VPC needs a private CA and a certificate for every task. One path is one CA made at deploy time and kept in Secrets Manager, with each task issuing its own certificate at start.
- **Logs kept long enough:** Log groups keep 7 days today. Federal needs logs searchable for 6 months and retrievable for 1 year. OMB M-26-14 Level 3 also needs regular hashing to prove the logs did not change. Who adds the hashing is question 1 in [Open Questions for GraphAware](#open-questions-for-graphaware).
- **Logs turned on:** Load balancer access logs, WAF logs and alarms are on. Give the agency a way to feed the logs to its security operations center.
- **Logged access to encrypted data:** Agencies must log every attempt to decrypt data at rest. The stack secrets and the database use the customer-managed key `DataKey`, so CloudTrail records each use.
- **Test in GovCloud:** Build every ARN with `${AWS::Partition}` and test the template in both `us-gov-west-1` and `us-gov-east-1`. Run one end to end test with instance FIPS mode on, a FIPS load balancer policy and a FIPS provider in the images.

### Evidence for the Agency ATO

- **Control descriptions:** Describe how Hume meets each control it owns, and say which controls the customer owns.
- **Customer responsibility matrix:** Show which controls AWS, GraphAware and the customer each carry.
- **SBOM:** Publish a software bill of materials for the AMI and every container image.
- **Penetration test:** Agencies must use independent third-party testers and will ask for recent results. The current review did not probe live endpoints or test the sandbox escape.
- **Vulnerability disclosure policy:** Agencies must accept vulnerability reports from the public. They will ask GraphAware for its policy.

### Not Needed at This Level

- **FedRAMP High and DoD IL4/5:** Session limits, signed images, a second-Region recovery plan, Multi-AZ, CAC login, a private load balancer and a STIG-hardened AMI belong to the stages in the general plan's Later Releases. Federal customers at FedRAMP Moderate do not need them.

## FedRAMP High

Applies to: `FedRAMP High`.

### When a System Is Rated High

An agency rates each system for confidentiality, integrity, and availability under [FIPS 199](https://csrc.nist.gov/pubs/fips/199/final). Each rating is Low, Moderate, or High. A High rating means a failure could cause severe or catastrophic harm. If any one of the three ratings is High, the whole system uses the High baseline. [FIPS 200](https://csrc.nist.gov/pubs/fips/200/final) calls this the high-water mark. FedRAMP names law enforcement, emergency services, financial, and health systems as typical High systems.

### What High Adds

The FedRAMP Rev5 High baseline has 410 controls and enhancements. Moderate has 323. High keeps every Moderate control and adds 87. Most of the 87 are agency processes, such as incident response and vulnerability scanning. The table lists the ones that change Hume, the template, or the customer's setup.

| Control | What it requires | What it means for Hume |
|---|---|---|
| AC-10 Concurrent Session Control | Each user may hold at most three sessions for admin access and two for other access. | Hume must limit sessions per user. See question 2. |
| AU-10 Non-repudiation | The system proves which user added, changed, deleted, approved, sent, or received data. | Hume's audit log must tie each data change to the user identity from the login token. See question 3. |
| AU-9(2) Store on Separate Systems | Audit records are copied to a separate system at least weekly. | CloudWatch Logs already sits outside the Hume instances. The customer may still want a copy in a separate log account. |
| AU-9(3) Cryptographic Protection | Cryptography protects the integrity of audit records. | KMS encryption protects the logs from being read. Integrity also needs hashing or signing, which matches [M-26-14 Level 3](#omb-m-26-14-logging-maturity). |
| AU-12(1) Time-correlated Audit Trail | All components feed one audit trail with timestamps from one time source. | The instances sync their clocks to the Amazon Time Sync Service. The customer correlates the logs from every Hume service. |
| CM-4(1) Separate Test Environments | Changes are tested in a separate environment before production. | Customers deploy a second Hume stack to test each upgrade. The template should allow more than one stack per account. |
| CM-14 Signed Components | Only components with a recognized signature are installed. | GraphAware may need to sign the container images and publish how to verify them. |
| SI-7(15) Code Authentication | Code signatures are checked before software is installed. | This is the same work as CM-14. |
| SA-4(5) System Configurations | The developer delivers the product with a secure configuration in place. | Hume ships hardened by default, and GraphAware documents each security setting. |
| SA-17 Developer Security Architecture | The developer describes the product's security architecture and design. | GraphAware supplies a security design document as evidence for the agency's ATO. These documents are a start. |
| CP-6(2), CP-9(3), CP-9(5), CP-10(4) Recovery | The agency sets recovery point and recovery time targets, stores backups separately, copies them to an alternate site, and restores within the target time. | Backups are copied to the other GovCloud Region. The targets are still to be set. See [Backup and Recovery](../architecture/detailed-architecture.md#backup-and-recovery). |
| CP-4(2), CP-7(4) Alternate Processing Site | The agency prepares and tests an alternate site. | The customer can rebuild Hume in the other GovCloud Region from the template and the copied backups. |
| SC-12(1) Key Availability | Data stays available if a key is lost. | The master key secret and the KMS keys must be recoverable in the other Region if the agency's recovery plan rebuilds Hume there. |
| SC-7(10) Prevent Exfiltration | The system blocks unauthorized data from leaving. | The private shape, VPC endpoint policies, and the absence of a NAT gateway support this. The customer adds its own egress controls. |
| SC-24 Fail in Known State | A failed component falls back to a secure state. | Each Hume service must refuse access when the identity provider or the database fails. |
| IA-5(13) Expiration of Cached Authenticators | Cached credentials expire after a set time. | Token and session lifetimes in Hume must be configurable. |

FedRAMP High also tightens some Moderate settings.

| Control | Moderate | High | What it means for Hume |
|---|---|---|---|
| AC-2(2) Temporary and emergency accounts | Disabled after 96 hours | Disabled after 24 hours | Applies to any break-glass account in Hume. |
| AC-2(3) Inactive accounts | Disabled after 90 days | Disabled after 35 days | The identity provider handles most accounts. Any local account in Hume must follow the same rule. |
| AC-2(5) Inactivity logout | End of the work period | 15 minutes of expected inactivity | Hume needs an idle timeout the customer can set. |
| CP-9(1) Backup testing | Yearly | Monthly | The customer tests a restore of RDS, the media files, and the master key secret every month. |

The other High-only controls fall on the agency's processes and security tools. GraphAware supplies evidence for them, and they do not change the design. They are AU-5(1), AU-5(2), AU-6(4), AU-6(5), AU-6(7), AU-12(3), AC-2(11), AC-4(4), AC-6(3), AC-6(8), CM-3(1), CM-3(6), CM-6(2), CM-8(2), CP-2(2), CP-2(5), CP-9(2), IA-5(8), IR-4(2), IR-4(4), IR-5(1), RA-5(4), RA-5(8), SC-3, SC-7(20), SC-7(21), SI-4(10), SI-4(11), SI-4(12), SI-4(20), SI-4(22), SI-5(1), SI-7(2), SI-7(5), and SR-9.

These counts come from the [FedRAMP Rev5 baseline workbook](https://www.fedramp.gov/) dated November 18, 2025. FedRAMP RFCs [0027 to 0029](https://www.fedramp.gov/rfcs/0027/) propose baseline changes, so the counts may change.

### When Availability Is Rated High

Most of the design change comes when the agency rates availability High.

1. **Database:** RDS must run Multi-AZ. The design runs it Single-AZ today. See [RDS for Postgres](../architecture/detailed-architecture.md#rds-for-postgres).
2. **Copies:** Each Hume service must run more than one copy. GraphAware says every service except Orchestra can. A second Orchestra copy needs additional licensing. Without it, Orchestra stays a single point of failure.
3. **Backups:** Backups must be copied to the other GovCloud Region.

## DoD Impact Levels 4 and 5

Applies to: `DoD IL4/5`.

### Background

DoD sorts cloud data into impact levels in its Cloud Computing Security Requirements Guide. IL4 covers Controlled Unclassified Information. IL5 covers more sensitive CUI and unclassified national security systems. GovCloud holds IL2, IL4, and IL5 authorizations. The DoD mission owner deploys Hume and gets its own ATO under DoDI 8510.01.

### Requirements for the Mission Owner

1. **Private shape only:** IL4 and IL5 systems reach users through the Boundary Cloud Access Point. The DoD Cloud Computing SRG says, "No direct Impact Level 4/5 traffic is permitted to/from the internet except via the NIPRNet IAPs and DMZ capabilities provided by the Mission Owner, a DOD component, or DISA." DoD users reach these systems from the DoD network, so these customers use the internal ALB. See the [DoD Cloud Security Playbook, Volume 1](https://dodcio.defense.gov/Portals/0/Documents/Library/CloudSecurityPlaybookVol1.pdf).
2. **CAC login:** The [Mission Owner SRG](https://cyber.trackr.live/stig/Cloud_Computing_Mission_Owner_Network_Security_Requirements_Guide/1/2/companion.pdf?file=U_Cloud_Computing_Mission_Owner_Overview.pdf), section 3.3.2, requires users to connect with a DoD CAC through DoD PKI. The DoD Security/Cybersecurity Authorization Working Group can approve another method. The [DoD CIO MFA memo](https://dowcio.war.gov/Portals/0/Documents/Library/MFA-U-S-DoDNetworks.pdf) calls PKI the preferred method. It allows other MFA at AAL2 or higher only with the authorizing official's approval. See question 4.
3. **DoD PKI server certificate:** [DoDI 8520.02](https://www.esd.whs.mil/portals/54/documents/dd/issuances/dodi/852002p.pdf), paragraph 3.1.a.(3)(a), requires a DoD-approved PKI certificate on every DoD web server. Commercial certificates are allowed only for public-facing sites. The ALB therefore uses a DoD PKI certificate imported into ACM.
4. **CSSP log feed:** DoDI 8530.01 requires every DoD system to align with a Cybersecurity Service Provider. The [DoD Cloud Security Playbook](https://dodcio.defense.gov/Portals/0/Documents/Library/CloudSecurityPlaybookVol1.pdf), Play 12, requires all logged data to be available to the CSSP. The mission owner chooses how to export Hume's logs from CloudWatch Logs.
5. **STIG hardening:** DoDI 8500.01 requires DoD systems to follow the applicable STIGs. DISA released a [STIG for Amazon Linux 2023](https://aws.amazon.com/blogs/compute/disa-stig-for-amazon-linux-2023-is-now-available/) in September 2025 and has updated it several times since. EC2 Image Builder and Systems Manager can apply it. See question 5.
6. **Key control:** The Mission Owner SRG, section 3.3.4, requires data at rest to be encrypted with keys under the mission owner's exclusive control. The crypto modules must be FIPS 140-2 or 140-3 validated and run in FIPS mode.

### What IL5 Adds

IL5 changes little in the Hume design. AWS carries the main IL5 additions, which are physical separation from non-federal tenants and deeper background checks for its staff. See the [CSP SRG](https://cyber.trackr.live/stig/Cloud_Computing_Mission_Owner_Network_Security_Requirements_Guide/1/1/companion.pdf?file=U_Cloud_Service_Provider_V1R1_SRG.pdf). The mission owner adds a few controls:

1. **AC-12(1) Logout:** Users need a logout they can start, and the system shows a clear logout message. With direct OIDC, the logout belongs to Hume. Hume must end its own session and may need to send the user to the identity provider's logout endpoint.
2. **AU-5(1) Storage capacity warning:** The customer adds an alarm before log storage fills.
3. **SC-18(3) and SC-18(4) Mobile code:** The system blocks unapproved mobile code and does not run code automatically. GraphAware confirms that the Hume web app meets this.
4. **MA-5(5) Maintenance staff:** This is a personnel rule for the mission owner.
5. **National security systems:** CNSSI 1253 overlays apply if the mission owner designates Hume as part of a national security system.

## Open Questions for GraphAware

These questions block the federal-ready stage and the stages after it. Each one carries the level that needs it.

1. `Federal` **Logging maturity:** Which OMB M-26-14 maturity level should Hume's logs reach? If Level 3, does Hume, the template, or the customer add the regular hashing that proves the logs were not changed?
    - **Background:** Federal agencies must reach Level 3 within 320 days of CISA's Logging Reference Architecture. CloudWatch Logs already covers encryption in transit and at rest. Hashing is the remaining gap, and it also meets FedRAMP High control AU-9(3). See [OMB M-26-14 Logging Maturity](#omb-m-26-14-logging-maturity).
    - **GraphAware answer:** Hume uses log4j2, which has no built-in log hashing or signing. A custom appender could add it, but it would be fragile. Async and multi-threaded logging, restarts, and the CloudWatch agent's batching all break the hash chain, and it would cover only Hume's logs. GraphAware suggests the template handle integrity instead. CloudWatch Logs sends each log group through a subscription filter and Firehose to S3 with Object Lock. A scheduled job writes SHA-256 digests signed with a KMS key. This covers every service the same way and meets AU-9(3).
2. `FedRAMP High` **AC-10 session limits:** Can Hume limit how many sessions one user holds at once?
    - **Why it matters:** FedRAMP High allows three sessions for admin access and two for other access. A shared or stolen login then cannot run many sessions at the same time. The customer's identity provider issues the login, but it cannot see how many Hume sessions a token opens. Hume has to count them.
3. `FedRAMP High` **AU-10 non-repudiation:** Does Hume's audit log record which user added, changed, deleted, or approved each item? Does it use the user identity from the login token? Can the records be protected from change?
    - **Why it matters:** Non-repudiation means a user cannot deny an action later. The audit log has to prove who did what, so it must hold the verified identity and be tamper-evident. The API writes its audit log to the EFS file system under `/audit-logs`, and Orchestra's audit log is not on the file system. Moving both to stdout is planned. The protection would then come from CloudWatch Logs and hashing.
4. `DoD IL4/5` **CAC through the identity provider:** Can the customer's identity provider check the CAC and pass Hume a token, with no certificate check in Hume?
    - **Why it matters:** DoD requires CAC login for IL4 and IL5 users. The design relies on the identity provider to check the card.
5. `DoD IL4/5` **STIG on the AMI:** Should the Hume AMI ship hardened to the DISA Amazon Linux 2023 STIG, or should a separate STIG-hardened AMI be offered? Which STIG rules does Hume or the ECS agent break?
    - **Why it matters:** DoD systems must follow the applicable STIGs. Customers patch by moving to a new AMI version, as described in [Upgrades and Rollback](../architecture/detailed-architecture.md#upgrades-and-rollback) item 6. A mission owner who hardens the instances in place loses that work at every upgrade. Only GraphAware can build the hardening into the AMI.

### For the AWS Marketplace Seller Team

6. `Federal` `CMMC` **ECS and FIPS without internet access:** Do the `ecs`, `ecs-agent`, and `ecs-telemetry` interface endpoints in us-gov-west-1 and us-gov-east-1 use FIPS 140-3 validated modules for TLS? If not, how should ECS container instances with no internet access reach `ecs-fips` and the FIPS versions of the agent's other two endpoints? Does AWS support the ECS agent with `AWS_USE_FIPS_ENDPOINT=true` on the ECS-optimized Amazon Linux 2023 AMI in GovCloud?
    - **Why it matters:** The GovCloud User Guide tells customers who need FIPS 140-3 to use the FIPS endpoints, and the only FIPS endpoint it lists for ECS is `ecs-fips`. That endpoint has no PrivateLink service, and an AWS roadmap request for one has been open since 2020. Without an answer, federal customers cannot show that the agent's traffic to ECS uses FIPS-validated crypto.
7. `Federal` `CMMC` **ALB FIPS TLS:** The ALB documentation says that all FIPS security policies use the AWS-LC FIPS validated module. The GovCloud User Guide says that customers who need FIPS 140-3 validated modules for TLS termination on ALB HTTPS listeners should have their account team contact the Elastic Load Balancing team. Is a FIPS security policy on the listener enough to meet FIPS 140-3 in us-gov-west-1 and us-gov-east-1? If not, what does the Elastic Load Balancing team enable, and does each customer account need to request it?

### For Red Hat

These questions apply only if Hume moves to a Red Hat Enterprise Linux AMI. GraphAware says Hume runs in FIPS mode on Red Hat OpenJDK on RHEL, which sends cryptography to NSS. The Docker images would need a Red Hat OpenJDK 21 base image.

8. **RHEL licensing:** Who pays for the RHEL license when a Marketplace AMI is built on RHEL, the buyer through hourly pricing or GraphAware? Is a RHEL-based AMI allowed in the GovCloud Marketplace, and does it work with a license-based Hume listing that has no per-instance-hour charge?
9. **ECS on RHEL:** Does Red Hat support running the Amazon ECS agent and a container runtime on RHEL 9? The ECS-optimized AMI exists only for Amazon Linux, so GraphAware would install and patch the agent.
10. `Federal` `CMMC` **Validated modules:** Which RHEL 9 cryptographic modules and OpenJDK builds hold current FIPS 140-3 validation, and does the validation cover the AWS instance types that ECS uses in GovCloud?

## To Verify in a GovCloud Test Account

These checks need a GovCloud account. Nothing here has a result yet.

1. `Federal` `CMMC` **FIPS:** Run Hume on instances with Amazon Linux 2023 FIPS mode on, behind an ALB with a FIPS security policy.
2. `Federal` `CMMC` **ECS agent on a FIPS host:** By default, the ECS agent sends its control-plane calls to the standard ECS hostname, even on an Amazon Linux 2023 FIPS instance. Host FIPS mode changes only its S3 calls and Service Connect. The agent is a standard Go build, so no public source shows that its own TLS uses a FIPS-validated module. According to the agent's code, `AWS_USE_FIPS_ENDPOINT=true` in `/etc/ecs/ecs.config` would send its ECS calls to `ecs-fips`. AWS has said it has not tested that setting, and `ecs-fips` needs internet access. Test the agent with and without the setting, record which hostnames it calls, and confirm with the customer whether the result meets their FIPS requirement. See question 6.
3. `Federal` `CMMC` **FIPS endpoint services:** In each GovCloud Region, list the endpoint services with `aws ec2 describe-vpc-endpoint-services` and record which `-fips` services exist. Check `ec2-fips`, `s3-fips`, `secretsmanager-fips`, `logs-fips`, `ecr-fips.api`, and `ecr-fips.dkr` in particular, because the GovCloud endpoint table does not list them.
