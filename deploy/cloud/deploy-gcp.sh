#!/usr/bin/env bash
# ─── RootOps — Deploy to Google Cloud (GCE VM) ──────────────────
# Creates a GCE VM and deploys the full RootOps stack with
# Caddy reverse proxy + automatic HTTPS via Duck DNS.
#
# Prerequisites:
#   1. Google Cloud CLI installed and authenticated
#        https://cloud.google.com/sdk/docs/install
#        gcloud auth login
#   2. A GCP project with billing enabled
#   3. A Duck DNS account with a subdomain + token
#        https://www.duckdns.org
#
# Usage:
#   # Set required variables in .env or export them:
#   export GCP_PROJECT_ID=my-project
#   export DUCKDNS_SUBDOMAIN=rootops
#   export DUCKDNS_TOKEN=abc123...
#
#   # Deploy:
#   ./deploy/cloud/deploy-gcp.sh
#
#   # Or via Make:
#   make cloud-deploy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Load .env if present ──────────────────────────────────────
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_ROOT/.env"
    set +a
fi

# ── Colors ─────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}▸${NC} $*"; }
ok()      { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}$*${NC}"; }

# ── Required variables ─────────────────────────────────────────
missing=()
[ -z "${GCP_PROJECT_ID:-}" ]    && missing+=("GCP_PROJECT_ID")
[ -z "${DUCKDNS_SUBDOMAIN:-}" ] && missing+=("DUCKDNS_SUBDOMAIN")
[ -z "${DUCKDNS_TOKEN:-}" ]     && missing+=("DUCKDNS_TOKEN")

if [ ${#missing[@]} -gt 0 ]; then
    error "Missing required environment variables:"
    for var in "${missing[@]}"; do
        echo "  - $var"
    done
    echo ""
    echo "Set them in .env or export them. See: deploy/cloud/.env.cloud.example"
    exit 1
fi

# ── Configurable defaults ─────────────────────────────────────
ZONE="${GCP_ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-rootops}"
MACHINE_TYPE="${GCP_MACHINE_TYPE:-e2-standard-4}"
DISK_SIZE="${GCP_DISK_SIZE:-50}"
REPO_URL="${REPO_URL:-https://github.com/Intelligent-IDP/rootops.git}"
DOMAIN="${DUCKDNS_SUBDOMAIN}.duckdns.org"

# LLM config
LLM_BACKEND="${LLM_BACKEND:-ollama}"
OPENAI_API_KEY="${OPENAI_API_KEY:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3}"
OPENAI_MODEL="${OPENAI_MODEL:-gpt-4o}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-4-6}"

# GitHub (optional)
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
GITHUB_DEFAULT_REPO="${GITHUB_DEFAULT_REPO:-}"

# SSH helper
SSH_CMD="gcloud compute ssh $VM_NAME --zone=$ZONE --project=$GCP_PROJECT_ID --quiet --command"

# ── Banner ─────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        RootOps — Google Cloud Deployment         ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Project:    $GCP_PROJECT_ID"
echo "  Zone:       $ZONE"
echo "  VM:         $VM_NAME ($MACHINE_TYPE, ${DISK_SIZE}GB)"
echo "  Domain:     https://$DOMAIN"
echo "  LLM:        $LLM_BACKEND"
echo ""
read -p "  Proceed? [Y/n] " -n 1 -r
echo
if [[ ${REPLY:-Y} =~ ^[Nn]$ ]]; then
    echo "Aborted."
    exit 0
fi

# ══════════════════════════════════════════════════════════════
# Step 1: Prerequisites
# ══════════════════════════════════════════════════════════════
header "Step 1/8 — Checking prerequisites"

if ! command -v gcloud &>/dev/null; then
    error "gcloud CLI not found."
    echo "  Install: https://cloud.google.com/sdk/docs/install"
    echo "  Then:    gcloud auth login"
    exit 1
fi

gcloud config set project "$GCP_PROJECT_ID" --quiet 2>/dev/null
ok "gcloud CLI configured (project: $GCP_PROJECT_ID)"

# ══════════════════════════════════════════════════════════════
# Step 2: Firewall rules
# ══════════════════════════════════════════════════════════════
header "Step 2/8 — Firewall rules"

gcloud compute firewall-rules create rootops-allow-https \
    --project="$GCP_PROJECT_ID" \
    --allow=tcp:80,tcp:443 \
    --target-tags=rootops-server \
    --description="RootOps: Allow HTTP and HTTPS" \
    --quiet 2>/dev/null && ok "Created firewall rule: rootops-allow-https" \
    || ok "Firewall rule already exists"

# ══════════════════════════════════════════════════════════════
# Step 3: Create VM
# ══════════════════════════════════════════════════════════════
header "Step 3/8 — Creating VM"

if gcloud compute instances describe "$VM_NAME" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" &>/dev/null; then
    warn "VM '$VM_NAME' already exists"
    read -p "  Delete and recreate? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        info "Deleting existing VM..."
        gcloud compute instances delete "$VM_NAME" \
            --zone="$ZONE" --project="$GCP_PROJECT_ID" --quiet
    fi
fi

if ! gcloud compute instances describe "$VM_NAME" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" &>/dev/null 2>&1; then
    gcloud compute instances create "$VM_NAME" \
        --project="$GCP_PROJECT_ID" \
        --zone="$ZONE" \
        --machine-type="$MACHINE_TYPE" \
        --boot-disk-size="${DISK_SIZE}GB" \
        --boot-disk-type=pd-balanced \
        --image-family=ubuntu-2404-lts-amd64 \
        --image-project=ubuntu-os-cloud \
        --tags=rootops-server \
        --quiet
    ok "VM created: $VM_NAME"
else
    ok "Using existing VM: $VM_NAME"
fi

# ══════════════════════════════════════════════════════════════
# Step 4: Get external IP + update Duck DNS
# ══════════════════════════════════════════════════════════════
header "Step 4/8 — Configuring DNS"

EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" \
    --format='get(networkInterfaces[0].accessConfigs[0].natIP)')

ok "External IP: $EXTERNAL_IP"

info "Updating Duck DNS: $DOMAIN → $EXTERNAL_IP"
DUCK_RESPONSE=$(curl -s "https://www.duckdns.org/update?domains=${DUCKDNS_SUBDOMAIN}&token=${DUCKDNS_TOKEN}&ip=${EXTERNAL_IP}")

if [ "$DUCK_RESPONSE" = "OK" ]; then
    ok "Duck DNS updated successfully"
else
    error "Duck DNS update failed (response: $DUCK_RESPONSE)"
    echo "  Check your DUCKDNS_SUBDOMAIN and DUCKDNS_TOKEN"
    exit 1
fi

# ══════════════════════════════════════════════════════════════
# Step 5: Wait for SSH
# ══════════════════════════════════════════════════════════════
header "Step 5/8 — Waiting for SSH"

info "Waiting for VM to accept SSH connections..."
for i in $(seq 1 30); do
    if gcloud compute ssh "$VM_NAME" \
        --zone="$ZONE" --project="$GCP_PROJECT_ID" \
        --command="echo ok" --quiet 2>/dev/null; then
        ok "SSH connected"
        break
    fi
    if [ "$i" -eq 30 ]; then
        error "SSH connection timed out after 150s"
        exit 1
    fi
    sleep 5
done

# ══════════════════════════════════════════════════════════════
# Step 6: Install Docker
# ══════════════════════════════════════════════════════════════
header "Step 6/8 — Installing Docker"

$SSH_CMD "
    set -e
    if command -v docker &>/dev/null; then
        echo 'Docker already installed'
        exit 0
    fi
    echo 'Installing Docker...'
    export DEBIAN_FRONTEND=noninteractive
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker \$USER
    echo 'Docker installed'
"
ok "Docker ready"

# ══════════════════════════════════════════════════════════════
# Step 7: Clone repo + configure
# ══════════════════════════════════════════════════════════════
header "Step 7/8 — Deploying RootOps"

# Generate .env file locally, then SCP it to the VM
ENV_TMP=$(mktemp)
cat > "$ENV_TMP" <<ENVFILE
# ─── RootOps Cloud .env (auto-generated) ─────────────────────
# Generated by deploy-gcp.sh on $(date -u +"%Y-%m-%d %H:%M UTC")

# Duck DNS
DUCKDNS_SUBDOMAIN=${DUCKDNS_SUBDOMAIN}
DUCKDNS_TOKEN=${DUCKDNS_TOKEN}

# LLM
LLM_BACKEND=${LLM_BACKEND}
OLLAMA_MODEL=${OLLAMA_MODEL}
OPENAI_API_KEY=${OPENAI_API_KEY}
OPENAI_MODEL=${OPENAI_MODEL}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
ANTHROPIC_MODEL=${ANTHROPIC_MODEL}

# CORS (includes the cloud domain)
CORS_ALLOWED_ORIGINS=https://${DOMAIN},http://localhost:3000,http://web:3000

# Log Ingestion
OTEL_LOGS_RECEIVER_ENABLED=true
LOG_MIN_SEVERITY=${LOG_MIN_SEVERITY:-INFO}

# GitHub
GITHUB_TOKEN=${GITHUB_TOKEN}
GITHUB_DEFAULT_REPO=${GITHUB_DEFAULT_REPO}

# Database (bundled)
POSTGRES_USER=rootops
POSTGRES_PASSWORD=rootops
POSTGRES_DB=rootops
ENVFILE

info "Uploading configuration..."
gcloud compute scp "$ENV_TMP" "$VM_NAME:/tmp/rootops.env" \
    --zone="$ZONE" --project="$GCP_PROJECT_ID" --quiet
rm -f "$ENV_TMP"

info "Cloning repository and starting services..."
$SSH_CMD "
    set -e

    # Clone or update
    if [ -d /opt/rootops ]; then
        cd /opt/rootops
        sudo git fetch --all
        sudo git reset --hard origin/main 2>/dev/null || sudo git reset --hard origin/master
    else
        sudo git clone ${REPO_URL} /opt/rootops
    fi

    sudo chown -R \$USER:\$USER /opt/rootops
    cd /opt/rootops

    # Place .env
    cp /tmp/rootops.env .env
    rm -f /tmp/rootops.env

    echo 'Repository ready'
"

ok "Repository deployed and configured"

# ══════════════════════════════════════════════════════════════
# Step 8: Start services
# ══════════════════════════════════════════════════════════════
header "Step 8/8 — Starting services"

# Determine compose command — need sg docker for group membership
# in the same SSH session (avoids logout/login requirement)
COMPOSE_CMD="docker compose -f docker-compose.yml -f deploy/cloud/docker-compose.cloud.yml"

info "Pulling images and building Caddy..."
$SSH_CMD "
    cd /opt/rootops
    sg docker -c '${COMPOSE_CMD} pull api web db ollama 2>/dev/null || true'
    sg docker -c '${COMPOSE_CMD} up -d --build'
"

ok "Services starting"

# ── Wait for health ───────────────────────────────────────────
info "Waiting for services to become healthy (this may take 2-5 minutes)..."
info "  Caddy needs to obtain a TLS certificate via Duck DNS..."

HEALTHY=false
for i in $(seq 1 60); do
    STATUS=$(curl -sk -o /dev/null -w "%{http_code}" "https://${DOMAIN}/health" 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        HEALTHY=true
        break
    fi
    # Show progress every 30s
    if [ $((i % 3)) -eq 0 ]; then
        info "  Still waiting... (attempt $i/60, last status: $STATUS)"
    fi
    sleep 10
done

echo ""
if [ "$HEALTHY" = true ]; then
    ok "RootOps is live and healthy!"
else
    warn "Health check did not return 200 yet (last: $STATUS)"
    warn "Services may still be starting. Check logs with:"
    echo "  make cloud-logs"
    echo "  # or:"
    echo "  gcloud compute ssh $VM_NAME --zone=$ZONE -- 'cd /opt/rootops && $COMPOSE_CMD logs -f'"
fi

# ── Pull Ollama model if needed ───────────────────────────────
if [ "$LLM_BACKEND" = "ollama" ]; then
    info "Pulling Ollama model: $OLLAMA_MODEL (this may take several minutes)..."
    $SSH_CMD "
        cd /opt/rootops
        sg docker -c '${COMPOSE_CMD} exec -T ollama ollama pull ${OLLAMA_MODEL}'
    " && ok "Ollama model pulled" || warn "Ollama model pull failed — you can retry with: make cloud-pull-model"
fi

# ══════════════════════════════════════════════════════════════
# Done!
# ══════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Deployment Complete! 🚀                ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${NC}"
echo ""
echo "  🌐 Dashboard:  https://${DOMAIN}"
echo "  📡 API:        https://${DOMAIN}/api"
echo "  📖 API Docs:   https://${DOMAIN}/docs"
echo "  💻 VM IP:      ${EXTERNAL_IP}"
echo ""
echo "  Add these to your .env for 'make cloud-*' commands:"
echo "    DEPLOY_HOST=$(whoami)@${EXTERNAL_IP}"
echo "    DEPLOY_URL=https://${DOMAIN}"
echo ""
echo "  Useful commands:"
echo "    make cloud-test       # Run endpoint tests"
echo "    make cloud-logs       # Stream all service logs"
echo "    make cloud-ssh        # SSH into the VM"
echo "    make cloud-teardown   # Destroy everything"
echo ""
echo "  Test now:"
echo "    ./deploy/cloud/test-deployment.sh https://${DOMAIN}"
echo ""
