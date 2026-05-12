#!/bin/bash
set -e

echo "=== DeployForge: GCP Setup ==="
echo ""

if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI not found."
    echo "Install: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
if [ -z "$PROJECT_ID" ]; then
    echo "ERROR: No GCP project set."
    echo "Run: gcloud config set project YOUR_PROJECT_ID"
    exit 1
fi

echo "GCP Project: $PROJECT_ID"
echo "Region: europe-west1 (configurable via DF_GCP_REGION)"
echo ""

echo "1. Enabling required APIs..."
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    storage.googleapis.com \
    containerregistry.googleapis.com \
    artifactregistry.googleapis.com \
    --quiet

echo ""
echo "2. Creating GCS bucket for build contexts..."
BUCKET_NAME="${PROJECT_ID}-deployforge-builds"
if gsutil ls -b "gs://${BUCKET_NAME}" &>/dev/null; then
    echo "   Bucket already exists: gs://${BUCKET_NAME}"
else
    gsutil mb -l europe-west1 "gs://${BUCKET_NAME}"
    gsutil lifecycle set /dev/stdin "gs://${BUCKET_NAME}" <<'EOF'
{
  "rule": [{"action": {"type": "Delete"}, "condition": {"age": 1}}]
}
EOF
    echo "   Created bucket: gs://${BUCKET_NAME} (auto-delete after 1 day)"
fi

echo ""
echo "3. Creating service account..."
SA_NAME="deployforge-builder"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" &>/dev/null; then
    echo "   Service account already exists: $SA_EMAIL"
else
    gcloud iam service-accounts create "$SA_NAME" \
        --display-name="DeployForge Builder"
    echo "   Created: $SA_EMAIL"
fi

echo ""
echo "4. Granting permissions..."
for ROLE in roles/run.admin roles/storage.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:$SA_EMAIL" \
        --role="$ROLE" \
        --quiet &>/dev/null
    echo "   Granted: $ROLE"
done

echo ""
echo "5. Creating service account key..."
KEY_PATH="./gcp-service-account.json"
if [ -f "$KEY_PATH" ]; then
    echo "   Key file already exists: $KEY_PATH"
else
    gcloud iam service-accounts keys create "$KEY_PATH" \
        --iam-account="$SA_EMAIL"
    echo "   Key saved to: $KEY_PATH"
    echo "   WARNING: Add gcp-service-account.json to .gitignore!"
fi

echo ""
echo "══════════════════════════════════════════════"
echo "✅ GCP Setup Complete!"
echo ""
echo "Add these to your .env:"
echo ""
echo "  DF_GCP_PROJECT_ID=$PROJECT_ID"
echo "  DF_GCP_REGION=europe-west1"
echo "  GOOGLE_APPLICATION_CREDENTIALS=$KEY_PATH"
echo ""
echo "══════════════════════════════════════════════"
