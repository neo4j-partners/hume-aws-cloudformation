# Acronyms

These acronyms appear across the Hume deployment documents.

1. **AAL2:** Authenticator Assurance Level 2 is the NIST login assurance level that requires two authentication factors. The DoD accepts MFA methods at AAL2 or higher only with the authorizing official's approval.
2. **ACL:** A web access control list holds the rules that AWS WAF applies to each request. The Hume web ACL uses AWS managed rule groups and attaches to the ALB.
3. **ACM:** AWS Certificate Manager issues and renews the TLS certificates that the ALB uses for HTTPS. It can also hold certificates imported from a customer's own CA.
4. **AL2023:** Amazon Linux 2023 is the AWS Linux distribution. The ECS-optimized AL2023 AMI is the base of the Hume AMI.
5. **ALB:** An Application Load Balancer routes HTTP and HTTPS traffic at layer 7. It ends TLS connections and sends requests to the Hume tasks. It is the only AWS load balancer that WAF can protect.
6. **AMI:** An Amazon Machine Image is a saved copy of a server's disk. EC2 starts new servers from it. The Hume AMI carries the Hume container images, and the GovCloud Marketplace listing ships Hume as an AMI.
7. **ARN:** An Amazon Resource Name is the unique ID that AWS gives each resource, such as a KMS key or an IAM role. GovCloud ARNs start with `arn:aws-us-gov`.
8. **ATO:** An Authority to Operate is the formal approval an agency's authorizing official signs before a system can run with agency data. The agency, not the vendor, holds the ATO for the system that includes Hume.
9. **AZ:** An Availability Zone is one or more separate data centers inside an AWS Region. Spreading Hume across two AZs keeps it running if one fails.
10. **BCFKS:** Bouncy Castle FIPS Keystore is the keystore file format that Bouncy Castle FIPS requires in approved mode. It replaces JKS or PKCS12 when a Java service runs on Bouncy Castle FIPS.
11. **CA:** A certificate authority issues and signs TLS certificates. A private CA signs certificates that are trusted only inside the customer's network.
12. **CAC:** The Common Access Card is the DoD smart card for staff and contractors. It is the DoD version of the PIV card.
13. **CFR:** The Code of Federal Regulations is the published body of US federal rules. Title 32 holds the CMMC rules. Title 22 holds ITAR.
14. **CIDR:** CIDR notation describes a range of IP addresses, such as `10.0.0.0/16`.
15. **CIO:** A chief information officer runs an organization's information technology. The DoD CIO issues DoD policy on networks and login, including its MFA rules.
16. **CISA:** The Cybersecurity and Infrastructure Security Agency leads cyber defense for federal civilian agencies. It publishes the Logging Reference Architecture that starts the OMB M-26-14 deadlines.
17. **CMMC:** The Cybersecurity Maturity Model Certification is the DoD program that checks how defense contractors protect controlled unclassified information.
18. **CNAME:** A CNAME record is a DNS record that points one name at another name. It cannot sit at the top of a domain.
19. **CNSSI:** Committee on National Security Systems Instructions set security rules for national security systems. CNSSI 1253 adds control overlays on top of NIST SP 800-53.
20. **CRL:** A certificate revocation list is a signed list of certificates a CA has cancelled. Clients download it to check that a certificate is still valid.
21. **CSP:** A cloud service provider runs the cloud that a customer's system uses. AWS is the CSP in this design.
22. **CSSP:** A Cybersecurity Service Provider is the DoD organization that monitors a mission owner's systems for attacks. IL4 and IL5 deployments must make their logs available to it.
23. **CUI:** Controlled Unclassified Information is federal information that is not classified but must be protected. Defense contractors that hold it must use FIPS 140 validated cryptography under NIST SP 800-171.
24. **DDoS:** A distributed denial-of-service attack floods a service with traffic from many sources at once.
25. **DISA:** The Defense Information Systems Agency runs DoD networks and publishes the DoD Cloud Computing SRG.
26. **DMZ:** A demilitarized zone is a network segment between an internal network and the internet. It holds the systems that accept outside traffic.
27. **DoD:** The US Department of Defense.
28. **EBS:** Amazon Elastic Block Store provides disks that attach to a single EC2 instance. Each ECS instance's root disk is an EBS volume that holds the preloaded Hume images.
29. **EC2:** Amazon Elastic Compute Cloud runs virtual servers. In this design, EC2 instances launched from the Hume AMI form the ECS cluster.
30. **ECR:** Amazon Elastic Container Registry stores container images. GraphAware uses it at build time. Customer deployments do not pull from it.
31. **ECS:** Amazon Elastic Container Service runs containers. This design uses the EC2 launch type, where ECS places containers on EC2 instances in the customer's account.
32. **EE:** Neo4j EE is Neo4j Enterprise Edition, the licensed edition of the Neo4j graph database.
33. **ENI:** An elastic network interface is a virtual network card in a VPC. In `awsvpc` mode, each ECS task gets its own ENI and its own security group.
34. **FedRAMP:** The Federal Risk and Authorization Management Program sets the security baseline that cloud services must meet before federal agencies use them. Its levels are Low, Moderate, and High.
35. **FIDO2:** FIDO2 is the open standard for phishing-resistant login with security keys and passkeys. WebAuthn is the browser part of FIDO2.
36. **FIPS:** Federal Information Processing Standards are US government computing standards that NIST publishes. FIPS 140-3 sets the rules for encryption modules. Federal systems must use encryption modules that NIST has tested and validated against it.
37. **FISMA:** The Federal Information Security Modernization Act is the law that requires each federal agency to secure its systems and approve each one before use.
38. **GovRAMP:** GovRAMP, formerly StateRAMP, is the authorization program that state and local governments use to approve cloud products. It follows the FedRAMP model.
39. **HMAC:** Hash-based Message Authentication Code is a keyed hash that checks a message's integrity and origin. Hume signs its tokens with HMAC-SHA256 or HMAC-SHA512, which FIPS approves.
40. **IAM:** AWS Identity and Access Management controls which people and services can call AWS APIs.
41. **IAP:** An Internet Access Point is a DISA gateway between NIPRNet and the internet. DoD traffic to and from the internet must pass through one.
42. **IL4 and IL5:** Impact Levels are DoD security categories from the Cloud Computing SRG. IL4 covers controlled unclassified information. IL5 covers more sensitive controlled unclassified information and unclassified national security systems. GovCloud is authorized up to IL5.
43. **IMDS:** The Instance Metadata Service gives an EC2 instance its settings and the temporary credentials of its IAM role. IMDSv2 is the version that requires a session token.
44. **ITAR:** The International Traffic in Arms Regulations control defense-related data. Sharing ITAR data with anyone who is not a US person requires an export license.
45. **JDBC:** Java Database Connectivity is the Java interface for database connections. The Hume services reach RDS through JDBC URLs.
46. **JDK:** Java Development Kit is the Java runtime and tools that the Hume images run on. GraphAware would switch the images to a Red Hat OpenJDK 21 base to support FIPS mode.
47. **JKS:** Java KeyStore is Java's older keystore file format. Bouncy Castle FIPS in approved mode does not accept it.
48. **JVM:** The Java Virtual Machine runs Java programs, including the Hume services. Its DNS cache settings decide how long it keeps a resolved address.
49. **JWT:** A JSON Web Token is a signed token that carries a user's identity after login.
50. **KMS:** AWS Key Management Service creates and controls encryption keys. CloudTrail logs every use of a KMS key.
51. **MFA:** Multi-factor authentication requires a second proof of identity on top of a password.
52. **MVP:** The minimum viable product is the first release, with the smallest useful set of features.
53. **NAT:** A NAT gateway lets servers in private subnets reach the internet without accepting inbound connections.
54. **NIPRNet:** The Non-classified Internet Protocol Router Network is the DoD network for unclassified traffic.
55. **NIST:** The National Institute of Standards and Technology publishes US federal security standards. These include FIPS 140-3, SP 800-53, and SP 800-171.
56. **NLB:** A Network Load Balancer routes TCP and UDP traffic at layer 4. It cannot inspect HTTP requests. WAF cannot protect it. Neo4j EE sits behind an internal NLB.
57. **NSA:** The National Security Agency approves cryptography for national security systems. FedRAMP accepts NSA-approved cryptography as an alternative to FIPS 140 validation.
58. **OCSP:** The Online Certificate Status Protocol lets a client ask a CA whether one certificate has been revoked, instead of downloading a full CRL.
59. **OIDC:** OpenID Connect is a login standard built on OAuth 2.0. Hume uses it to hand logins to an identity provider.
60. **OMB:** The Office of Management and Budget is the White House office that issues policy memos, such as M-22-09, that federal agencies must follow.
61. **PBKDF2:** Password-Based Key Derivation Function 2 is the approved password hashing function under FIPS. Hume uses BCrypt today, which FIPS does not approve.
62. **PIV:** Personal Identity Verification cards are the smart cards that federal staff and contractors use to log in.
63. **PKCS12:** Public-Key Cryptography Standards #12 is the common cross-platform keystore file format. Bouncy Castle FIPS in approved mode does not accept it.
64. **PKI:** Public key infrastructure is the system of certificate authorities and certificates that proves identities. DoD PKI issues the certificates on CAC cards and on DoD web servers.
65. **RDS:** Amazon Relational Database Service runs managed databases such as PostgreSQL.
66. **RHEL:** Red Hat Enterprise Linux is Red Hat's Linux distribution. Red Hat's OpenJDK FIPS mode is validated only on a RHEL host.
67. **S3:** Amazon Simple Storage Service stores objects such as ALB access logs.
68. **SBOM:** A software bill of materials lists every component and library in a software release. Agencies use it to check a release for known vulnerabilities.
69. **SMS:** Short Message Service is the text messaging system on mobile phones. Federal guidance tells agencies to turn off SMS login codes because they are not phishing-resistant.
70. **SMTP:** Simple Mail Transfer Protocol sends email between servers. Hume's optional alerting module sends notifications through an SMTP server. The first listing ships without alerting.
71. **SRG:** The DoD Cloud Computing Security Requirements Guide sets the rules for DoD workloads in commercial clouds, including the Impact Levels.
72. **SSM:** AWS Systems Manager manages servers. Its Session Manager gives shell and port-forwarding access without SSH. Its Parameter Store holds settings.
73. **SSO:** Single sign-on lets users log in once with their organization's identity provider and reach many applications.
74. **STIG:** A Security Technical Implementation Guide is a DISA hardening checklist for one product. DISA publishes one for Amazon Linux 2023.
75. **TIC:** A Trusted Internet Connection is the federal program that routes agency internet traffic through approved, monitored gateways.
76. **TLS:** Transport Layer Security encrypts network traffic. HTTPS is HTTP sent over TLS.
77. **TTL:** A DNS record's time to live sets how long a client may cache it. Hume services need a short TTL so they find peer tasks after their addresses change.
78. **UBI:** Universal Base Image is Red Hat's container base image built from RHEL.
79. **VPC:** A Virtual Private Cloud is a private network inside AWS where Hume runs.
80. **VPN:** A virtual private network is an encrypted tunnel that connects users or offices to the VPC.
81. **WAF:** AWS WAF is a web application firewall that works only at layer 7, the HTTP layer. It inspects each web request and blocks requests that match its rules, such as SQL injection or known bad IP addresses. It does not stop network-layer floods, such as SYN or UDP floods. WAF attaches to the ALB, not to the NLB.
