# Reconcile two sales exports

Import `workflow.json` into n8n and select **Execute workflow**. The two **sales export** nodes hold fictional source and destination records. Each export contains ten rows. **Reconcile by Sale ID** groups the records by their exact ID and compares the amount in integer minor units, currency, and consent. Select an output node to inspect the original rows for any ID.

| Output | Expected result |
| --- | --- |
| Clean sales | Three sales match, including a free sale and an opted-out sale. The opted-out sale keeps `follow_up_allowed: false`. |
| Exceptions for review | Seven IDs need review. The examples cover a different amount, missing sale, unexpected sale, duplicate on either side, conflicting consent, and different currency. Every exception blocks follow-up. |

A duplicate keeps all the rows from both exports so the reviewer can resolve the ambiguity. All workflow nodes operate in memory. The two output nodes display results, and the workflow ends there.

## Verify in an isolated container

Run these commands from the repository root. Docker pulls the official n8n image. The workflow runs with networking disabled and a temporary SQLite database. Only the fictional workflow file is mounted.

```sh
result_file=$(mktemp)
docker run --rm --network none \
  --tmpfs /home/node/.n8n:uid=1000,gid=1000,mode=0700 \
  -e N8N_DIAGNOSTICS_ENABLED=false \
  -e N8N_VERSION_NOTIFICATIONS_ENABLED=false \
  -e N8N_TEMPLATES_ENABLED=false \
  -v "$PWD/n8n/workflow.json:/demo/workflow.json:ro" \
  --entrypoint /bin/sh \
  n8nio/n8n:2.48.5 \
  -ec 'n8n import:workflow --input=/demo/workflow.json >&2
       n8n execute --id=SalesReconcileDemo01 --rawOutput' > "$result_file"
python3 n8n/verify.py "$result_file"
```

The verifier checks the actual records at both output nodes and the follow-up flag for every sale.
