# Tear down

This guide removes a Hume stack and the things it leaves behind. It also gives the order for removing a Hume stack, a Neo4j EE stack and the identity provider together. A running stack costs money, so tear it down when you finish testing.

- **Teardown:** The command `./stack/deploy.py teardown <stack name>`. It deletes one Hume stack and its stack-owned leftovers.
- **Shared secrets:** The secret `hume/licence-key`. It lives outside every stack, so teardown keeps it.
- **Template bucket:** The bucket `hume-ecs-templates-<account>-<region>` that holds staged templates. Every stack in the region uses it.

## Tear down a Hume stack

```
./stack/deploy.py teardown <stack name>
```

The stack name is required. Teardown prints what it will delete and asks you to type the stack name. Any other answer, or no answer, deletes nothing and exits 1. Pass `--yes` to skip the question in a script.

What teardown does:

1. It switches off deletion protection on the database. The template turns it on, and CloudFormation cannot delete a protected database. If this fails, teardown stops before it deletes anything and says how to switch the protection off by hand.
2. It sets the Auto Scaling group to zero.
3. It empties the access log bucket, because CloudFormation cannot delete a bucket that holds objects.
4. It deletes the stack. It retries a failed delete up to three times. Each retry scales the group in and empties the bucket again.
5. It force deletes the secrets under `hume/<stack name>/`.
6. It deletes the log groups `/hume/<stack name>/` and `/aws/lambda/<stack name>-run-task`.
7. It deletes the staged template object.
8. It deletes the three certificate validation records that the stack left in the zone.
9. It lists any resource still tagged with the stack. If one remains, it exits 1 and keeps the record file. The encryption key and the final database snapshot are not counted, because the template keeps them on purpose.

What teardown keeps:

- **Final database snapshot:** The database has `DeletionPolicy: Snapshot`, so CloudFormation takes a snapshot before it deletes the database. The snapshot costs storage until you delete it. List it with `aws rds describe-db-snapshots --snapshot-type manual --query "DBSnapshots[?contains(DBSnapshotIdentifier, '<stack name>')].[DBSnapshotIdentifier,SnapshotCreateTime]" --output text`.
- **Encryption key:** The key `alias/hume/<stack name>` is retained, because the snapshot and the secrets are encrypted with it. Teardown prints the key id and the command to remove it. Scheduling the key for deletion makes the snapshot unrestorable, so do it only after you delete the snapshot.
- **Shared secrets:** Teardown keeps the secret `hume/licence-key`, because it lives outside the stack. Delete it by hand if you no longer need it.
- **AMI:** Teardown keeps the AMI. To remove the AMI, run `uv run --script verify/prove.py delete-ami <ami id>`.

Delete the snapshot, then schedule the key for deletion, when you no longer need the data:

```
aws rds delete-db-snapshot --db-snapshot-identifier <snapshot identifier>
aws kms schedule-key-deletion --key-id <key id> --pending-window-in-days 30
```

To check that nothing is left, run `uv run --script verify/prove.py teardown-check <stack name>`. Without a stack name it checks the whole account. That command does not know about the retained key and snapshot, so it can list them as left. On a stack torn down on 2026-11-01 it listed the snapshot as left and exited 1, and it did not list the key.

## Delete the template bucket

When you no longer need any stack, `./stack/deploy.py delete-template-bucket` deletes the template bucket. It refuses if the bucket still holds objects.

## Tear down the Neo4j EE setup

Delete Hume first, then Neo4j EE, then the identity provider. The Neo4j teardown force-deletes the VPC, load balancers, NAT gateways and endpoints, which Hume would still be using. [Why teardown goes Hume first](../reference/infrastructure-setup-details.md#why-teardown-goes-hume-first) explains the order. The setup itself is in [the run book](run-book.md).

1. Delete the Hume stack. Wait for `DELETE_COMPLETE`.

   ```
   ./stack/deploy.py teardown <hume-stack-name>
   ./stack/deploy.py delete-template-bucket        # only if you no longer need any Hume stack
   ```

2. Delete the Neo4j EE stack. Follow the section `#tear-down` of the Neo4j EE run book `neo4j-ee/docs/run-books/oidc.md`, in the Neo4j project.

3. Delete the identity provider. The command deletes the login domain, the pool, the user password secrets and the record.

   ```
   uv run verify/cognito.py teardown
   ```

- **Wait for Hume:** Wait for `DELETE_COMPLETE` on the Hume stack before you start the Neo4j teardown.
- **`--delete-volumes`:** The Neo4j EE `teardown.sh` takes this flag. Without it, the data disks, both password secrets, the load balancer log bucket and the stack KMS key are kept, and they keep costing money. With it, teardown deletes them and the data is gone for good. It schedules the KMS key for deletion with a 7 day wait. Use it only for a throwaway test.
- **Neo4j parameters:** The Neo4j SSM parameters are stack resources, so they go with the stack.
- **Hume secrets:** Hume keeps the secrets `hume/licence-key` and `hume/neo4j-truststore` after teardown. Delete the truststore secret yourself when you are done if you do not want it kept.
- **Imported certificate:** The Neo4j teardown deletes the self-signed certificate it imported into ACM, after the stack is gone. If that step prints a warning, delete the certificate by hand.
- **Temporary bucket:** A create that fails removes the temporary S3 bucket for its template. After a create that succeeds, look for a leftover bucket.
- **Final check:** Finish by listing what is left. Run the project's `teardown-check` for the Hume side. For the Neo4j side, look for a `test-ee-` stack, NAT gateways, load balancers and unattached volumes.
- **Failed Neo4j create:** A failed create leaves no record file, so `teardown.sh` stops at once. See [A failed Neo4j create leaves no record file](troubleshooting.md#a-failed-neo4j-create-leaves-no-record-file).
