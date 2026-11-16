# GovCloud Requirements for Hume

This document collects the GovCloud rules that apply to every deployment, at the `All` level. The architecture documents refer back to it instead of repeating these rules. The rules for the federal levels are in [Federal Compliance](federal-compliance.md).

Related documents:

1. [Architecture Overview](../architecture/architecture-overview.md)
2. [Network Design](../architecture/network-design.md)
3. [Detailed Architecture](../architecture/detailed-architecture.md)
4. [Federal Compliance](federal-compliance.md)
5. [Acronyms](../reference/acronyms.md)

## Constraints That Set the Delivery Model

Applies to: `All`.

1. **AWS Marketplace is required:** Hume must be sold and delivered through AWS Marketplace.
2. **Only GovCloud services:** The design uses only services and features that are available in the GovCloud Regions.
3. **No container listings in GovCloud:** The GovCloud Marketplace offers AMI-based and SaaS products only. Container products are not available. See [AWS Marketplace in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-marketplace.html) and [Publishing your product in AWS GovCloud (US)](https://docs.aws.amazon.com/marketplace/latest/userguide/govcloud-publishing.html).
4. **Customer-deployed:** Hume runs in the customer's GovCloud account. A SaaS listing would mean GraphAware runs Hume, so it does not fit.

These four constraints leave an AMI listing as the only delivery path. Hume keeps its containers inside that path by running them on Amazon ECS on EC2, with the container images preloaded on the Hume AMI. The [Architecture Overview](../architecture/architecture-overview.md) describes that design.

## GovCloud Marketplace Restrictions

Applies to: `All`.

1. **One catalog for both partitions:** GovCloud does not have its own Marketplace catalog. It mirrors the commercial one. A GovCloud listing is a commercial listing with the GovCloud Regions turned on. Only AMI-based and EC2 Image Builder component listings let the seller choose Regions. SaaS products reach GovCloud through a support request.
2. **No launch from the Marketplace website:** A GovCloud account cannot launch products from the AWS Marketplace website. Launching from the website needs a commercial AWS account. The GovCloud runbook must give the exact subscribe and launch steps for GovCloud customers.
3. **No Service Catalog integration:** GovCloud Marketplace products cannot be added to AWS Service Catalog. Agencies that hand out approved software through Service Catalog must deploy Hume another way.
4. **Seller requirements:** AWS sets extra requirements for any seller that publishes in GovCloud. See [Publishing your product in AWS GovCloud (US)](https://docs.aws.amazon.com/marketplace/latest/userguide/govcloud-publishing.html).
   1. The seller must be a US company, incorporated and based in the US.
   2. The person publishing must be a US citizen or green card holder.
   3. The seller must be able to handle ITAR export-controlled data.
   4. The seller must be a direct AWS customer with an active GovCloud account.
   5. Publishing into GovCloud starts with a one-time access request.
   6. GraphAware must confirm which company publishes the GovCloud listing and that it meets these requirements.

## Services and Features Not Available in GovCloud

Applies to: `All`.

These services and features are missing or limited in GovCloud. The design must not depend on them.

1. **Amazon CloudFront:** CloudFront runs only in commercial Regions. GovCloud deployments get no edge caching and no edge DDoS protection. The ALB with WAF is the only entry point.
2. **AWS Shield:** Shield Advanced is not supported in GovCloud, and Shield Standard coverage is unconfirmed. See [AWS Firewall Manager in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-fms.html). WAF covers layer 7 attacks. See [Denial-of-Service Protection: NIST SC-5](#denial-of-service-protection-nist-sc-5).
3. **ALB login with Amazon Cognito:** The ALB's `authenticate-cognito` action is not available in GovCloud. The plain `authenticate-oidc` action is available. See [Elastic Load Balancing in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-elb.html).
4. **Custom domains for Cognito user pools:** GovCloud does not allow custom domains for Cognito user pool login pages, so a Cognito login page would sit on an AWS-owned `amazoncognito.com` address. The Hume design does not use Cognito. See [Amazon Cognito in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-cog.html).
5. **Bottlerocket ECS AMI:** The Bottlerocket ECS-optimized AMI is not available in GovCloud. This is the only ECS difference AWS lists for GovCloud. The Hume AMI is built on the ECS-optimized Amazon Linux 2023 AMI instead. See [Amazon ECS in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-ecs.html).
6. **Cloud Map public DNS namespaces:** AWS Cloud Map runs in both GovCloud Regions, but public DNS namespaces are not available. Hume needs only a private namespace. See [AWS Cloud Map in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-cm.html).

## The GovCloud Partition

Applies to: `All`.

1. **Partition-aware ARNs:** GovCloud ARNs start with `arn:aws-us-gov`. The template builds every ARN with `${AWS::Partition}`, because a hardcoded `arn:aws` ARN fails in GovCloud.
2. **Service endpoints:** VPC endpoint service names use the GovCloud Region, such as `com.amazonaws.us-gov-west-1.ecs`. The Hume template builds no endpoints. Its [Network Design](../architecture/network-design.md#aws-service-access) says which endpoints the Neo4j EE stack supplies and which are a next step.
3. **Every enabled Region must work:** AWS Marketplace requires the template to launch successfully in every Region enabled for the product. Both GovCloud Regions, `us-gov-west-1` and `us-gov-east-1`, must be tested. See [Add CloudFormation templates to your product](https://docs.aws.amazon.com/marketplace/latest/userguide/cloudformation.html).

## Authorization Boundary and Impact Levels

1. **Frameworks agencies ask for:** GovCloud customers typically need FedRAMP High, DoD IL4 or IL5, ITAR, or CMMC. [Compliance Levels](federal-compliance.md#compliance-levels) lists what each one adds to the design.
2. `All` **Commercial CloudFront in front of a GovCloud ALB:** AWS documents a pattern where CloudFront in a commercial account uses a GovCloud ALB as its origin. See [Setting Up Amazon CloudFront with Your AWS GovCloud (US) Resources](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/setting-up-cloudfront.html). The Hume design does not use it, for these reasons.
   1. **Outside the boundary:** CloudFront sits outside the GovCloud boundary, and TLS ends at edge locations outside GovCloud. AWS states that CloudFront is not within the ITAR boundary. See [Tips for Setting Up CloudFront](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/setting-up-cloudfront-tips.html).
   2. **FedRAMP Moderate only:** CloudFront is authorized at FedRAMP Moderate in US East/West, not in GovCloud. Customers with FedRAMP High requirements would need their authorizing official to accept the pattern. See [AWS Services in Scope for FedRAMP](https://aws.amazon.com/compliance/services-in-scope/FedRAMP/).
   3. **Not authorized above IL2:** AWS lists CloudFront at DoD IL2 in commercial Regions only. IL4 and IL5 data must stay inside an authorized boundary. See [AWS Services in Scope for DoD SRG](https://aws.amazon.com/compliance/services-in-scope/DoD/).
   4. **No path to a private ALB:** Commercial CloudFront connects to its origin over the internet, so it cannot reach the internal ALB that IL4 and IL5 deployments use.
3. `All` **Data stays in the Region:** Every Hume data store sits in the customer's GovCloud account and Region. Calls to AWS services go to endpoints in the same Region. The login redirect happens in the user's browser. The server-side calls to the agency's identity provider leave the VPC, and some customer setups add other outbound connections. The [Network Design](../architecture/network-design.md#optional-nat-gateway) lists them. The listing must disclose each one.

## Denial-of-Service Protection: NIST SC-5

GovCloud offers no CloudFront, no Shield Advanced, and unconfirmed Shield Standard coverage, so DDoS protection is thinner than in commercial Regions.

`Federal` NIST SP 800-53 control SC-5, Denial-of-Service Protection, lets the agency decide which denial-of-service events it protects against and which controls it uses. See [NIST SP 800-53 Rev. 5](https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final).

1. **Its examples:** Its guidance gives boundary filtering, extra capacity, and redundancy as examples.
2. **What assessors expect:** In practice, assessors expect the agency to address both network floods and application-layer attacks.
3. **Inherited protection:** Some of that protection is inherited from AWS and from the agency's own network.
4. **The runbook:** It must state what the Hume template provides and what the agency must provide.

The design provides the following.

1. `All` **The ALB is the front door:** In the public shape, all user HTTP traffic enters through the ALB. This includes the web UI, the API, and media. Bolt traffic to Neo4j stays on the internal NLB and bypasses the ALB.
2. `All` **WAF covers layer 7:** AWS WAF runs in both GovCloud Regions. It handles rate limits, IP reputation, and malformed or malicious requests. GovCloud offers only the AWS managed rule groups. Third-party rule groups from AWS Marketplace are excluded. See [AWS WAF in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-waf.html). The [Detailed Architecture](../architecture/detailed-architecture.md#waf-rules) document lists the rules in the template.
3. `All` **The ALB and security groups cover layers 3 and 4:** The ALB accepts only well-formed TCP connections on its listener ports. SYN floods hit the ALB fleet, and UDP floods stop at the ALB. Security groups allow only port 443 and the port 80 redirect to the ALB. The tasks, RDS, and Neo4j accept traffic only from inside the VPC. Agencies that need more add their own defenses in front, such as their TIC or a FedRAMP-authorized DDoS service. A third-party service in front ends TLS outside GovCloud, which raises the same boundary concern as commercial CloudFront.
4. `All` **GovCloud is isolated for compliance only:** GovCloud is a separate partition with separate accounts and US-person staff. Its public endpoints sit on the internet like those of any commercial Region. A public ALB in GovCloud is as reachable from the internet as one in a commercial Region.
5. `DoD IL4/5` **The private shape moves the risk upstream:** The internal ALB is reachable only from the customer's network. The DoD boundary, through a Boundary Cloud Access Point, or the agency network handles DDoS upstream. Most of the DDoS risk moves to the customer's network.

## Other Considerations

### Route 53

Applies to: `All`.

Today the Hume template creates three alias records in a Route 53 hosted zone in the same account. The target design has no Route 53 resources in the template. The customer then points the Hume hostname at the ALB with a CNAME record in their own DNS. The change is one of the next steps to support a customer. In GovCloud, Route 53 alias records can point only at resources inside the GovCloud Regions. See [Amazon Route 53 in AWS GovCloud (US)](https://docs.aws.amazon.com/govcloud-us/latest/UserGuide/govcloud-r53.html).
