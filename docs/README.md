# Documentation

Start with [the run book](guides/run-book.md). The [README](../README.md) describes the project.

## Customer deploy guide

This guide is for a customer who subscribes to Hume in AWS Marketplace. The Marketplace listing links to it, so its path must stay the same.

- **[Deploy Hume from AWS Marketplace](deploy-guide.md):** This guide covers the deploy, sign-in, daily operation and teardown with the AWS console, the AWS CLI and a few standard tools.

## Guides

These guides are for the team that builds and runs a stack with the scripts in this repository.

- **[Run book](guides/run-book.md):** The run book builds a Hume stack against a private Neo4j EE cluster with OIDC sign-in, with copy-paste commands for each step.
- **[Check and prove a stack](guides/testing.md):** This guide covers the black box check, the proofs, the browser checks and the hardening checks.
- **[Spine demo](guides/spine-demo.md):** This guide turns on the Spine demo, loads the Spine database into the Neo4j EE cluster and tests it. The scripts are listed in [demo/spine/README.md](../demo/spine/README.md).
- **[Operations manual](guides/operations.md):** This manual covers sign-in, the allowed addresses, template parameters, rotation and the Neo4j operator links. It also says what a deploy does and what record files hold.
- **[Troubleshooting](guides/troubleshooting.md):** This page lists known problems, with the cause and the fix of each.
- **[Tear down](guides/teardown.md):** This guide covers removing a stack, the template bucket, and a Hume plus Neo4j EE setup.

## Reference

- **[Command reference](reference/commands.md):** This page lists every script, command and flag.
- **[Infrastructure setup details](reference/infrastructure-setup-details.md):** This page explains what each run book step does and what Hume needs from the Neo4j EE stack.
- **[Acronyms](reference/acronyms.md):** This page defines the acronyms used across the documents.

## Architecture

These documents describe the design as built.

- **[Architecture overview](architecture/architecture-overview.md):** This document gives a short overview of the design and lists the other documents.
- **[Network architecture](architecture/network-architecture.md):** This document covers the network rules, the entry point, the private connection to Neo4j EE and the egress path.
- **[Network design](architecture/network-design.md):** This document covers the subnet layout, host routing, security groups, VPC endpoints and a traffic walkthrough.
- **[Identity architecture](architecture/identity.md):** This document covers the OIDC login, the Neo4j sign-in flows and the Neo4j roles.
- **[Detailed architecture](architecture/detailed-architecture.md):** This document describes what the current template and AMI build: each component, identity, encryption, logging, and upgrades.
- **[Diagram script](architecture/make_svg.py):** This script generates the ECS pre-baked AMI diagram for the architecture overview. The diagrams live in `docs/images/`.

## Compliance

These documents hold the requirements and the review findings.

- **[Release scope by compliance level](compliance/release-scope.md):** This document says what this release covers and what the roadmap adds.
- **[GovCloud requirements](compliance/gov-cloud.md):** This document collects the GovCloud rules that apply to every deployment.
- **[Federal compliance](compliance/federal-compliance.md):** This document covers every compliance level above `All`: the Federal level work, and what CMMC, FedRAMP High and DoD Impact Levels 4 and 5 add.
- **[Neo4j EE compliance findings](compliance/ee-compliance.md):** This document records how Neo4j Enterprise Edition measures against the federal requirements.
