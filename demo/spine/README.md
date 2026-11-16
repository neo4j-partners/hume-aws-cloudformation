# Spine demo

[The Spine demo guide](../../docs/guides/spine-demo.md) is the run book. The scripts `spine_demo.py` and `seed_nodes.py` in this directory run every step. Run them from the repository root as `./demo/spine/spine_demo.py` and `./demo/spine/seed_nodes.py`.

These files load the Spine demo database into the Neo4j EE cluster that Hume reads. The load creates a `spine` database and three roles on it. A normal deploy never downloads or installs the demo. It runs only when you run `spine_demo.py`.

- **Backup:** The backup is the public file `spine-latest.backup`. Neo4j seeds the `spine` database from it.
- **Seed node:** `seed_nodes.py copy` puts the backup in the seeds directory of one cluster node. The other nodes copy it from that node.
- **Seed provider:** The seed provider setting says which kinds of seed address Neo4j accepts. The load seeds from the seed node with a `server://` address, which needs `ServerSeedProvider`. See [Seed provider](../../docs/guides/spine-demo.md#seed-provider).
- **Load:** `seed_nodes.py load` runs `cypher-shell` on the seed node over SSM. It creates the database, then runs `roles.cypher`.

## Files

Some files are copied from the hume-quickstart repo. The copied Cypher, CCR data and Hume config are unchanged.

| File | Source |
|---|---|
| `roles.cypher` | hume-quickstart `SPINE.md`, step 3, unchanged. It creates the roles `spine_neo4j_sa_rw`, `spine_analyst` and `spine_officer` and their privileges on `spine`. `seed_nodes.py` sends it to the node with the load. |
| `ccr/Spine MONTES CCR.csv` | hume-quickstart `assets/spine/ccr/Spine MONTES CCR.csv`, unchanged. It is the CCR file for the demo playbook. |
| `hume-config/` | hume-quickstart `config/hume/spine`, all 113 files, unchanged and in the same layout. It holds the SPINE knowledge graph files that `stack/deploy.py` stores in the `spine-config` secret. `deploy.py` skips 6 of them, so the copy holds files the stack does not use. The list is in the [Spine demo guide](../../docs/guides/spine-demo.md#the-spine-knowledge-graph-files). |

## What a local Docker test showed

These results come from a local test with Docker, not from the AWS cluster.

- **File seed time:** The file seed took 12 seconds, and the `spine` database was online after 18 seconds. The result had 2,596,708 nodes.
- **`https` seed time:** The `https` seed took 293 seconds, mostly the download of 615 MB.
- **Sizes:** The `spine` database is 3.0 GiB. The Neo4j data was 3.5 GB after the `https` seed and 4.1 GB with the backup file kept. The backup file can be deleted after the load.

## Running twice

- **Database:** The load uses `CREATE DATABASE spine IF NOT EXISTS`. A second run does nothing and does not reseed.
- **Roles:** The role statements use `IF NOT EXISTS`, and a second grant changes nothing.

## Roles

`roles.cypher` holds several statements separated by `;`. The load runs it with `cypher-shell -f`.

No Neo4j user holds these roles through a password login. A user in the group `spine-analysts` or `spine-officers` of the test pool gets the role over SSO. A test of a role through a password login needs a Neo4j user with that role. The user's password must come from Secrets Manager, never from this repo.

Hume has its own roles `SPINE_ANALYST` and `SPINE_OFFICER` for the SPINE knowledge graph. `spine_demo.py` maps the two groups to them on the Hume stack. Sign in as `spine-analyst` to see the SPINE graph as a plain user.

## Remove the demo

Drop the `spine` database in Neo4j. Then clear `SpineConfigSecretArn` on the Hume stack to remove the config import. Clearing the ARN alone keeps the database.
