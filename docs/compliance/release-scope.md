# Release Scope by Compliance Level

Applies to: `All`.

This release meets the `All` level. The other levels are the roadmap, and each adds the requirements of another group of customers. The levels above `All` are in [Federal Compliance](federal-compliance.md).

Each requirement is tagged with the compliance level that drives it. The levels are defined in [Compliance Levels](federal-compliance.md#compliance-levels).

| Stage | Status | Includes | Who it serves | Described in |
|---|---|---|---|---|
| Marketplace listing | Implemented in this release. Some next steps remain before a customer deploys it. | Every item tagged `All` | Commercial customers and non-federal GovCloud customers, such as ITAR companies and state and local governments | These documents |
| Federal-ready | Roadmap | Every item tagged `Federal` or `CMMC` | Federal agencies at FedRAMP Moderate, and defense contractors that hold CUI | [Federal Compliance](federal-compliance.md#federal-level) |
| Beyond Moderate | Roadmap | Every item tagged `FedRAMP High` or `DoD IL4/5` | Agency systems rated High, and DoD mission owners | [Federal Compliance](federal-compliance.md) |

Later stages should change configuration, not the design, so the roadmap keeps hooks such as a template parameter for instance FIPS mode. The template has no FIPS parameter yet. Whether the ECS agent can meet FIPS without internet access is AWS question 6 in [Open Questions for GraphAware](federal-compliance.md#open-questions-for-graphaware).
