# Hume on AWS Marketplace: Network Design

This document covers the network details behind [Network Architecture](network-architecture.md): the subnet layout, host routing, security groups, AWS service access, and a traffic walkthrough. The [Detailed Architecture](detailed-architecture.md) document covers what runs inside each task.

## Subnet Layout

The template creates no VPC, subnets, route tables, NAT gateways, or VPC endpoints. The Neo4j EE stack supplies them. The template takes the Neo4j EE stack name as `EeStackName`. The `Neo4jEeLookup` custom resource reads the VPC and the subnets from Parameter Store under `/neo4j-ee/<EeStackName>/`.

```
 VPC from the Neo4j EE stack, two Availability Zones
 +---------------------------------------------------------------+
 |  Public subnets, one per zone (public-subnet-1-id to 3-id)    |
 |    Internet-facing ALB nodes                                  |
 |                                                               |
 |  Private subnets (private-subnet-1-id, private-subnet-2-id)   |
 |    First subnet:  ECS instance and every task interface       |
 |    All subnets:   RDS subnet group, Neo4j EE NLB and servers  |
 +---------------------------------------------------------------+
```

1. **Two Availability Zones:** The template names no zone, because AWS Marketplace forbids templates that depend on specific zones. The ALB needs public subnets in two zones, and the RDS subnet group needs private subnets in two zones.
2. **Public subnets:** Only the internet-facing ALB uses them.
3. **First private subnet:** The Auto Scaling group launches its one instance in `private-subnet-1-id`, and every ECS service places its task network interface there. In `awsvpc` mode each task takes an IP address from that subnet, so it needs room for one address per task on top of the instance.
4. **RDS:** The database runs single-AZ. Its subnet group takes `private-subnet-1-id` and `private-subnet-2-id`.


## Entry Point

[Network Architecture](network-architecture.md#network-entry-point) covers the public shape this release builds, its HTTPS listeners, WAF at the edge, and the hostname and certificate. The private shape is a roadmap item there. It would use the same listener rules and target groups below.

1. **WAF rules:** The web ACL's rules are in [WAF Rules](detailed-architecture.md#waf-rules).
2. **Target groups:** Tasks run in `awsvpc` mode, so every target group uses the `ip` target type and registers task IP addresses directly.
3. **Static IPs:** ALB IP addresses change over time. Customers who need fixed IPs for firewall allowlists would need an NLB in front of the ALB. This is deferred until a customer asks.
4. **Allowlist size:** The ALB security group admits up to five IPv4 ranges, one per `AllowedCidr` parameter. A range can be a single `/32` address or `0.0.0.0/0`. Each range gets one rule for port 443 and one for port 80, so five ranges make ten rules.
5. **Open to the internet:** A slot set to `0.0.0.0/0` opens the load balancer to the internet. The template then refuses Native sign-in, so every user signs in through OIDC.

### Host Routing

The browser talks to three Hume services today. Docker Compose publishes each on its own port, and nginx sits in front of `media` to allow only the upload and download paths. The ALB replaces both with host rules on one HTTPS listener on port 443. Each service the browser calls gets its own hostname. A port 80 listener has no rules and only sends a 301 to the same host and path on port 443.

| Priority | Host | Path | Target | Container port | Health check |
|---|---|---|---|---|---|
| 10 | `hume-api.<base>` | `/actuator/prometheus*` | Fixed 403 | | |
| 20 | `hume-api.<base>` | any | `api` | 8080 | `/actuator/health` on 7001 |
| 30 | `hume-media.<base>` | `/v1/upload*`, `/v1/download*` | `media` | 8008 | `/actuator/health` on 7001 |
| 40 | `hume.<base>` | any | `web` | 8081 | `/` on 8081. GraphAware confirmed `web` has no health endpoint, and testing found that `/` returns 200. |
| Default | any other host or path | any | Fixed 403 | | |

1. **No nginx media proxy:** The nginx proxy allows `/v1/download` and `/v1/upload` and returns 403 for everything else. The media host rule matches only those two paths, which gives the same result, so the `media-reverse-proxy` service is dropped. Any other media path matches no rule and gets the listener's fixed 403, never `media` and never `web`. A request to the raw load balancer name or to a stale record gets the same 403.
2. **One hostname per service:** Each service the browser calls gets its own hostname, such as `hume.<domain>`, `hume-api.<domain>`, and `hume-media.<domain>`. GraphAware prefers this shape. The rules need no list of API paths. The template takes one `BaseDomainName` and derives the three names from it. The certificate covers the three names, and the template writes one alias record per name into the hosted zone. The template sets `HUME_API_URL` and `HUME_MEDIA_API_URL` on `web` and `hume.media.uri-fe` on `api` to those names. The team chose this layout on 2026-11-05, with a fixed 403 as the default in place of `web`.
3. **Not routed:** Orchestra and the event store get no listener rule. Orchestra webhooks stay internal, because the API is the usual caller.

## Service-to-Service Traffic and Security Groups

Docker Compose resolves container names such as `orchestra` and `media`. On ECS, each service registers in a private AWS Cloud Map DNS namespace, `hume.local`, and Hume's settings point at names such as `orchestra.hume.local`.

The template builds three security groups. The ECS instance and every `awsvpc` task share `InstanceSecurityGroup`, so service-to-service traffic is governed by rules from that group to itself and not by one group per service.

| Group | Direction | Port | Peer | Purpose |
|---|---|---|---|---|
| `AlbSecurityGroup` | In | 443, 80 | `AllowedCidr1` to `AllowedCidr5`, each an IPv4 range. Port 80 only redirects to 443. | User traffic |
| `AlbSecurityGroup` | Out | 8080 to 8081, 8008, 7001 | `InstanceSecurityGroup` | Targets and health checks |
| `InstanceSecurityGroup` | In | 8080 to 8081, 8008, 7001 | `AlbSecurityGroup` | Web, API, media, and the health checks |
| `InstanceSecurityGroup` | In | 8008, 8080 to 8081, 8100, 8666, 9080, 9090 | `InstanceSecurityGroup` | Service to service |
| `InstanceSecurityGroup` | Out | All | `InstanceSecurityGroup` | Service to service |
| `InstanceSecurityGroup` | Out | 443 | `0.0.0.0/0` | AWS APIs and the identity provider |
| `InstanceSecurityGroup` | Out | 5432 | `DatabaseSecurityGroup` | Databases |
| `InstanceSecurityGroup` | Out | 7687 | The Neo4j `external-sg-id` group | Bolt |
| `DatabaseSecurityGroup` | In | 5432 | `InstanceSecurityGroup` | Databases |

Two rules are added to security groups that belong to the Neo4j EE stack. Each is its own resource, so deleting the Hume stack removes it. `Neo4jNlbIngressFromHume` allows port 7687 from `InstanceSecurityGroup` to the Neo4j NLB group, and `VpcEndpointIngressFromHume` allows port 443 from `InstanceSecurityGroup` to the endpoint group.

1. **Ports inside the group:** The ingress rules name the ports the containers listen on: 8080 for the API and event store, 8081 for `web`, 8008 for `media`, 8100 and 8666 for Orchestra, 9080 for the API security server, and 9090 for the event store. The group is open to itself on egress, so the ingress rules set the real limits. Any task can reach any of those ports on any other task, because the rules do not separate one service from another.
2. **No inbound rules from outside:** The ECS instance accepts nothing except the ALB ports above and traffic from its own group. Administrators connect through Session Manager.
3. **Records:** Cloud Map registers an A record per task with the task's IP address. When a task moves, the record changes.
4. **DNS caching:** A restarted service gets a new IP address. Orchestra and the API set a 10-second JVM DNS cache with `java_tool_options` in the truststore secret, so they follow the new address quickly.
5. **Start order:** ECS starts services in any order. GraphAware says the services do not retry their connections. ECS restarts a task only when its process exits, and the container checks ignore dependencies, so a service that stays running without its dependency is never replaced. A test on the live stack will show which case applies. See [Start Order](detailed-architecture.md#start-order).
6. **Identity provider egress:** `api` and `media` need outbound 443 to the customer's identity provider. Both set the OIDC issuer and signing key settings. The 443 rule to `0.0.0.0/0` covers it, and the traffic leaves through the Neo4j EE stack's NAT gateways. [Outbound Access to the Identity Provider](detailed-architecture.md#outbound-access-to-the-identity-provider) covers how it reaches an identity provider on the internet.
7. **Health checks:** ALB health checks come from the ALB security group, so the ALB rules above already cover them.

## AWS Service Access

The images live on the AMI, so no task pulls from a registry. The Hume template builds no NAT gateway, S3 gateway endpoint, or interface endpoint. The ECS instance and tasks use what the Neo4j EE stack provides.

1. **Neo4j NAT gateways:** The ECS agent, CloudWatch Logs, and the identity provider are reached through the Neo4j stack's NAT gateways. The `InstanceSecurityGroup` allows outbound 443 to any address, which is what lets that traffic leave. Without internet egress on the Neo4j stack, the ECS agent never registers and the Hume stack fails to create.
2. **Neo4j VPC endpoints:** Systems Manager and Secrets Manager are reached through the Neo4j stack's interface endpoints. `VpcEndpointIngressFromHume` opens port 443 on the endpoint security group to `InstanceSecurityGroup`.
3. **No ECR access needed:** With `ECS_IMAGE_PULL_BEHAVIOR=prefer-cached`, the agent resolves the image digest from the copy on disk and makes no registry call, as long as the image name in the task definition matches the name the image was pulled under when the AMI was built, or `versionConsistency` is disabled. The task definitions set `versionConsistency` to disabled.
4. **No `ec2` or `ec2messages` endpoint:** ECS attaches task network interfaces through its service-linked role, so the instance never calls the EC2 API. AWS still lists `ec2messages` among the Session Manager endpoints, but SSM Agent 3.3.40.0 and later uses `ssmmessages` whenever it is available, and Amazon Linux 2023 ships a newer agent.
5. `Federal` `CMMC` **FIPS endpoints:** KMS has a separate `-fips` endpoint service in GovCloud, and a `-fips` endpoint's private DNS resolves the FIPS hostname. The service docs also list `ec2-fips`, `s3-fips`, `secretsmanager-fips`, `logs-fips`, `ecr-fips.api`, and `ecr-fips.dkr` endpoint services. The GovCloud endpoint table does not list them, so their availability in GovCloud needs a live check. AWS lists CloudWatch Logs and SSM as FIPS at their standard hostnames in GovCloud. `ssmmessages` does not appear on the AWS FIPS list. ECS is different. Its only FIPS hostname is `ecs-fips.<region>.amazonaws.com`, and no `ecs-fips` endpoint service exists. By default the ECS agent does not use that hostname, so agent traffic stays on the standard `ecs`, `ecs-agent`, and `ecs-telemetry` endpoints. See AWS question 6 in [Open Questions for GraphAware](../compliance/federal-compliance.md#open-questions-for-graphaware) and test items 2 and 3 in [To Verify in a GovCloud Test Account](../compliance/federal-compliance.md#to-verify-in-a-govcloud-test-account).
6. **Not built, endpoints for every AWS API:** The design calls for interface endpoints for `ecs`, `ecs-agent`, `ecs-telemetry`, `ssm`, `ssmmessages`, `secretsmanager`, and `logs`, plus an S3 gateway endpoint, so that no AWS call needs a NAT gateway. AWS says that if any of the three ECS endpoints is missing, ECS traffic goes over the public endpoints instead. See [Amazon ECS interface VPC endpoints](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/vpc-endpoints.html). This is a next step, and a customer VPC that already has these endpoints would need the template to reuse them, because a second interface endpoint for the same service with private DNS turned on fails.

### Optional NAT Gateway

[Network Architecture](network-architecture.md#egress-and-aws-service-access) covers the egress the Neo4j EE stack supplies today. The template has no NAT gateway parameter. A NAT gateway that is off by default is a next step, so that blocking all outbound traffic other than the endpoints shrinks what a compromised task can reach. Time sync uses the Amazon Time Sync Service local to each instance. Hume makes no metering calls to AWS Marketplace.

Some customer setups need outbound access beyond the VPC.

1. **Identity provider:** `api` and `media` call the identity provider server to server. It fetches signing keys and exchanges login codes for tokens. A cloud identity provider such as Okta or Microsoft Entra ID sits on the internet.
2. **Orchestra workflows:** A workflow may call a REST API, message broker, or data source outside the VPC.
3. **Email:** The optional alerting module sends notifications through an SMTP server. That server may sit outside the VPC. The first listing ships the minimum Hume capability, without alerting.
4. **Certificate revocation:** Hume may check certificate revocation lists or OCSP responders for the identity provider's certificate. The CA may publish these on the internet.

## Traffic Walkthrough

One upload shows how the pieces hand off. A user uploads a file from the Hume web UI.

1. The browser resolves `hume-media.<base>`, which is an alias record to the internet-facing ALB. The request arrives from one of the five allowed ranges on port 443, so the ALB security group admits it.
2. WAF checks the request. The upload path matches the common rule set, whose `SizeRestrictions_BODY` rule is set to Count, so a large body is counted and not blocked.
3. The listener matches `/v1/upload*` and forwards to the `media` task's IP address on port 8008.
4. The `media` task asks `api.hume.local` on port 9080 whether the user may upload. It writes the file to the `/media` access point of the EFS file system, which the container sees at `/hume-media`, records it in the `media` database on RDS, and publishes an event to `eventstore.hume.local` on port 9090.
5. Nothing in this path touched a container registry. The request reached the instance through the ALB and stayed inside the VPC afterward.
