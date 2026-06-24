#!/usr/bin/env bash
# Auto-retry launcher for an Oracle "Always Free" instance.
# Run inside OCI Cloud Shell (already authenticated — no API keys needed).
# It auto-discovers your compartment, the Ubuntu image, and your public subnet,
# generates an SSH key, then loops the launch across every AD until capacity
# is available. Leave it running; it grabs the instance the moment a slot opens.
set -uo pipefail

# ---- settings (edit if you want) ----
SHAPE="VM.Standard.A1.Flex"     # fallback: "VM.Standard.E2.1.Micro" (then OCPUS/MEM ignored)
OCPUS=1                          # A1.Flex only
MEM=6                            # A1.Flex only (GB)
DISPLAY_NAME="trading-bot"
RETRY_SECONDS=30
# -------------------------------------

echo "== discovering account resources =="
COMPARTMENT="${OCI_TENANCY:-}"
[ -z "$COMPARTMENT" ] && COMPARTMENT=$(grep -m1 '^tenancy' ~/.oci/config 2>/dev/null | cut -d'=' -f2 | tr -d ' ')
[ -z "$COMPARTMENT" ] && { echo "!! could not find tenancy OCID — set COMPARTMENT=ocid1.tenancy... manually"; exit 1; }

ADS=$(oci iam availability-domain list -c "$COMPARTMENT" \
      | python3 -c "import sys,json;[print(a['name']) for a in json.load(sys.stdin)['data']]")
IMAGE=$(oci compute image list -c "$COMPARTMENT" --shape "$SHAPE" \
        --operating-system "Canonical Ubuntu" --sort-by TIMECREATED --sort-order DESC \
        | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(d[0]['id'] if d else '')")
SUBNET=$(oci network subnet list -c "$COMPARTMENT" --all \
         | python3 -c "import sys,json;ds=json.load(sys.stdin)['data'];p=[s for s in ds if not s.get('prohibit-public-ip-on-vnic')];print(p[0]['id'] if p else '')")

[ -z "$IMAGE" ]  && { echo "!! no Ubuntu image found for $SHAPE"; exit 1; }
[ -z "$SUBNET" ] && { echo "!! no PUBLIC subnet found — create the VCN with the wizard first"; exit 1; }

KEY=~/.ssh/trading_bot
[ -f "$KEY" ] || ssh-keygen -t rsa -b 2048 -f "$KEY" -N "" -q
PUBKEY=$(cat "${KEY}.pub")

SHAPE_ARGS=()
[[ "$SHAPE" == *Flex* ]] && SHAPE_ARGS=(--shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM}")

echo "  shape      : $SHAPE"
echo "  image      : $IMAGE"
echo "  subnet     : $SUBNET"
echo "  ADs        : $(echo "$ADS" | tr '\n' ' ')"
echo "  ssh key    : $KEY (.pub used for the instance)"
echo "== launching (retry every ${RETRY_SECONDS}s until capacity) — Ctrl-C to stop =="

while true; do
  for AD in $ADS; do
    ts=$(date +%H:%M:%S)
    if OUT=$(oci compute instance launch \
          --availability-domain "$AD" --compartment-id "$COMPARTMENT" \
          --shape "$SHAPE" "${SHAPE_ARGS[@]}" \
          --image-id "$IMAGE" --subnet-id "$SUBNET" --assign-public-ip true \
          --display-name "$DISPLAY_NAME" \
          --metadata "{\"ssh_authorized_keys\":\"$PUBKEY\"}" 2>&1); then
      ID=$(echo "$OUT" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])" 2>/dev/null)
      echo "[$ts] LAUNCHED in $AD"
      echo "  instance: $ID"
      echo "  waiting ~40s for the network, then fetching the public IP..."
      sleep 40
      IP=$(oci compute instance list-vnics --instance-id "$ID" \
           --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
      echo "  PUBLIC IP: ${IP:-<check console: Compute > Instances > $DISPLAY_NAME>}"
      echo "  SSH from here:  ssh -i $KEY ubuntu@${IP}"
      exit 0
    fi
    reason=$(echo "$OUT" | grep -io 'out of host capacity\|limitexceeded\|too many requests\|[A-Za-z]*error' | head -1)
    echo "[$ts] $AD: ${reason:-retry}"
    sleep "$RETRY_SECONDS"
  done
done
