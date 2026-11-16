#!/bin/bash
# Hume AMI build, run as root by cloud-init on the build instance.
# create_ami.py fills in the double-at-sign placeholders before launch. No secret value appears
# here: the registry login and the licence key are read from Secrets Manager through the
# instance role. Never run this script with set -x.
set -Eeuo pipefail

export AWS_DEFAULT_REGION=@@REGION@@

imds() {
  local token
  token=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
  curl -fsS -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/$1"
}

INSTANCE_ID=$(imds instance-id)

tag() {
  aws ec2 create-tags --resources "${INSTANCE_ID}" --tags "Key=$1,Value=$2"
}

# On any failure, record it and stop, so create_ami.py sees the instance stop
# and reads BuildStatus instead of waiting for a build that will never finish.
trap 'echo "=== BUILD FAILED ==="; tag BuildStatus failed || true; shutdown -h now' ERR

echo "=== Hume AMI build ==="

if command -v aws >/dev/null 2>&1; then
  AWS_CLI_PREINSTALLED=yes
else
  AWS_CLI_PREINSTALLED=no
  dnf install -y awscli-2
fi
tag BuildAwsCliPreinstalled "${AWS_CLI_PREINSTALLED}"
tag BuildEcsInit "$(rpm -q ecs-init)"
tag BuildDocker "$(rpm -q docker)"
# ECS needs amazon-efs-utils to mount EFS task volumes. Record it, do not install it.
if rpm -q amazon-efs-utils >/dev/null 2>&1; then
  tag BuildEfsUtils "$(rpm -q amazon-efs-utils)"
else
  tag BuildEfsUtils missing
fi

# --- Trust store fix for the eMudhra intermediate CA compromise. ---
# Amazon Linux 2023 locks dnf to the base AMI's release, so a plain upgrade can
# report nothing to do. --releasever=latest reaches the current package.
dnf upgrade -y --releasever=latest ca-certificates

for _ in $(seq 60); do
  docker info >/dev/null 2>&1 && break
  sleep 5
done
docker info >/dev/null

# --- Pull images. The login lives in a throwaway Docker config directory. ---
export DOCKER_CONFIG
DOCKER_CONFIG=$(mktemp -d)

SECRET_JSON=$(aws secretsmanager get-secret-value \
  --secret-id "@@SECRET_ID@@" --query SecretString --output text)
REGISTRY_USER=$(printf '%s' "${SECRET_JSON}" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["username"])')
printf '%s' "${SECRET_JSON}" |
  python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["password"])' |
  docker login @@REGISTRY@@ --username "${REGISTRY_USER}" --password-stdin
unset SECRET_JSON REGISTRY_USER

pull_with_retry() {
  local attempt
  for attempt in 1 2 3; do
    docker pull "$1" && return 0
    echo "Pull of $1 failed (attempt ${attempt} of 3)"
    sleep $((attempt * 10))
  done
  return 1
}

IMAGES="@@IMAGES@@"
for image in ${IMAGES}; do
  pull_with_retry "${image}"
done
for image in ${IMAGES}; do
  docker image inspect "${image}" >/dev/null
done

docker logout @@REGISTRY@@
rm -rf "${DOCKER_CONFIG}" /root/.docker /home/*/.docker
unset DOCKER_CONFIG

# --- Static Hume files. The containers run as uid 1001 and must read them. ---
install -d -m 755 /opt/hume /opt/hume/config
echo "@@FILES_B64@@" | base64 -d | tar xz --no-same-owner -C /opt/hume/config
# The RDS CA bundle is too big for UserData, so the build downloads it. The
# tasks bind mount it read-only. The chown and chmod below cover it.
RDS_CA_PATH="/opt/hume/config/@@RDS_CA_FILE@@"
install -d "$(dirname "${RDS_CA_PATH}")"
curl -fsS --proto '=https' --retry 3 -o "${RDS_CA_PATH}" "@@RDS_CA_URL@@"
echo "@@RDS_CA_SHA256@@  ${RDS_CA_PATH}" | sha256sum -c -
chown -R 1001:1001 /opt/hume/config
find /opt/hume/config -type d -exec chmod 755 {} +
find /opt/hume/config -type f -exec chmod 644 {} +

# --- Hume licence key. Written after the chmod above, so it stays 0400. ---
# Only the API task mounts this folder. The value goes from the secret straight to
# the file and is never echoed. The file has no trailing newline.
LICENCE_PATH="/opt/hume/config/@@LICENCE_FILE@@"
install -d -m 700 -o 1001 -g 1001 "$(dirname "${LICENCE_PATH}")"
(
  umask 377
  aws secretsmanager get-secret-value \
    --secret-id "@@SECRET_ID@@" --query SecretString --output text |
    python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["licence_key"])' \
      >"${LICENCE_PATH}"
)
chown 1001:1001 "${LICENCE_PATH}"
chmod 400 "${LICENCE_PATH}"
[ -s "${LICENCE_PATH}" ]

printf '%s\n' ${IMAGES} >/opt/hume/images.txt

# --- ECS agent settings. The launch template appends ECS_CLUSTER at boot. ---
touch /etc/ecs/ecs.config
echo "--- ecs.config shipped with the base AMI ---"
cat /etc/ecs/ecs.config
echo "--- end ---"
cat >/tmp/hume-ecs.config <<'ECSCONFIG'
@@ECS_CONFIG@@
ECSCONFIG
# Drop any shipped line for a key we set, keep the other defaults, then append ours.
while IFS='=' read -r key _; do
  if [ -n "${key}" ]; then
    sed -i "/^${key}=/d" /etc/ecs/ecs.config
  fi
done </tmp/hume-ecs.config
if [ -s /etc/ecs/ecs.config ] && [ -n "$(tail -c1 /etc/ecs/ecs.config)" ]; then
  echo >>/etc/ecs/ecs.config
fi
cat /tmp/hume-ecs.config >>/etc/ecs/ecs.config

df -h /
docker images

# --- Clean up before imaging ---
systemctl stop ecs || true
systemctl stop amazon-ssm-agent || true
rm -rf /var/lib/ecs/data/* /var/log/ecs/* /var/log/amazon/ssm/* /root/.aws /home/*/.aws
shred -u /etc/ssh/ssh_host_* 2>/dev/null || true
rm -f /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys
shred -u /root/.bash_history /home/*/.bash_history 2>/dev/null || true
dnf clean all
rm -rf /var/cache/dnf /tmp/* /var/tmp/*
CLOUD_INIT_CLEAN_HELP=$(cloud-init clean --help)
if grep -q -- --machine-id <<<"${CLOUD_INIT_CLEAN_HELP}"; then
  cloud-init clean --logs --machine-id
else
  echo "cloud-init clean has no --machine-id here; /etc/machine-id is not reset"
  cloud-init clean --logs
fi

AUTHORIZED_KEYS_WITH_CONTENT=$(find /root/.ssh /home/*/.ssh -name authorized_keys -size +0 2>/dev/null || true)
if [ -n "${AUTHORIZED_KEYS_WITH_CONTENT}" ]; then
  echo "authorized_keys has content, refusing to image: ${AUTHORIZED_KEYS_WITH_CONTENT}"
  false
fi

SSM_LOGS_LEFT=$(ls -A /var/log/amazon/ssm 2>/dev/null || true)
if [ -n "${SSM_LOGS_LEFT}" ]; then
  echo "SSM agent logs are not empty, refusing to image: ${SSM_LOGS_LEFT}"
  false
fi

tag BuildStatus success
echo "=== Build complete. Shutting down. ==="
shutdown -h now
