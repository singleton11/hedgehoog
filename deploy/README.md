# EC2 release deployment

The `Release to EC2` workflow runs on every published GitHub release (including
prereleases). It builds x86 Docker images in Actions, transfers them using a
forced-command deployment SSH key, waits for Compose healthchecks, and tests
HTTP from the runner. Deployments are serialized. Images are identified by the
release commit SHA; no mutable registry credentials are required.

## Current infrastructure

- Region: eu-central-1; instance: i-092fd2cde12b17931 (t3a.medium, 4 GiB).
- URL: http://ec2-52-29-101-224.eu-central-1.compute.amazonaws.com
- Disk: encrypted 24 GiB gp3; database and queue use persistent Docker volumes.
- MongoDB, RabbitMQ and the API have no published host ports.
- SSH allows the administrator source IPv4 only. Actions uses temporary OIDC
  credentials scoped to changing this instance's security group, opens its own
  runner IPv4 /32, and revokes it even on deployment failure.
- `EC2_DEPLOY_KEY` and `EC2_KNOWN_HOSTS` are GitHub Actions secrets;
  `EC2_HOST` is a repository variable. No AWS access keys are stored in GitHub.
- Host-key pinning was verified against the AWS instance console fingerprint.

## HTTP warning

This AWS-owned hostname has **no trusted TLS**. Login credentials, session
cookies and financial data are unencrypted in transit. Production HTTP is an
explicit `ALLOW_INSECURE_HTTP=true` exception, not the default. For real sensitive
use, configure a custom domain and TLS, then set `COOKIE_SECURE=true`, remove
the exception, and update allowed origins. Avoid sending sensitive data before
this change. The public IPv4/DNS can change on stop/start; update configuration,
repository variable and host-key entry if replacing the instance.

## Host operations

Runtime configuration is `/opt/hedgehoog/.env.production` (root-only, mode 0600).
The initial admin password is there; account seeding runs only once, so editing
this variable does not rotate an existing account. Never print secrets to logs.
The deployment SSH key is restricted to `/usr/local/sbin/hedgehoog-deploy` with
no port forwarding or interactive shell. This is privileged code deployment;
protect release publishing and repository secrets accordingly.

Use root on the instance:

```sh
cd /opt/hedgehoog
docker compose --env-file .env.production --env-file .release.env ps
docker compose --env-file .env.production --env-file .release.env logs --tail 100
cat deployed-sha
```

Do not use `down -v`. Release deployments preserve MongoDB and RabbitMQ volumes.
Back up MongoDB and the secrets file securely before upgrades. This small
single-instance deployment has no high availability or automated off-host backup.
A failed rollout can leave a partially upgraded stack; investigate health and
redeploy a release containing the last known-good commit. Old images are retained
for at least seven days. EC2 stop does not stop EBS charges; terminating deletes
the root disk and all current application data. There is no ALB, NAT or RDS.

Changes are proposed in a PR; debug prereleases can deploy the branch without
merging or bypassing main-branch review/check protections. After approval and
merge, publish subsequent releases from main.
