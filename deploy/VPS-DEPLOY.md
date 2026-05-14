# VPS deployment — `deploy.wrupup.com` + SSH + Cloudflare

**Server:** `root@IP`  
**Domain:** `api.[domain].com` 

---

## 1. Preparing SSH key locally

```bash
ssh-keygen -t ed25519 -C "deployforge-wrupup" -f ~/.ssh/deployforge_wrupup
ssh-copy-id -i ~/.ssh/deployforge_wrupup.pub root@IP
```

After:

```bash
ssh -i ~/.ssh/deployforge_wrupup root@IP
```

block logging by password the server (Optinal, recomended):

```bash
# Server:
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload sshd
```

---

## 2. Cloudflare DNS

1. Cloudflare or other provider DNS recording settings → **DNS** → **Records**.
2. **Add record:**
   - **Type:** `A`
   - **Name:** `api`
   - **IPv4:** `SERVER-IP`
   - **Proxy:** firstly **DNS only** (proxied off); then **Proxied** (orange).
3. **SSL/TLS** → suggestion:
   - if you use the **Let’s Encrypt** on the origin (Caddy provide it automaticly): **Full** or **Full (strict)**.
   - Make sure that **DNS only** getting first certification on record, then after getting certification successfully, you can turn on **Proxied**.

hotlink address for Webhook URLs: `https://api.[domain].com`

---

## 3. Composing up the Docker on the server

```bash
ssh root@IP

apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# Debian:
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker --now
```

---

## 4. Master Command (locally rsync + installation)

if your local SSH key defined on the server. **on localhost** (repo root):

```bash
chmod +x deploy/push-and-boot.sh
./deploy/push-and-boot.sh
```

## 5. manuel installation with git (alternatives)

`Dockerfile` (`deployforges/`).

```bash
cd /opt
git clone https://github.com/mrkkyatilla/deployforges.git
cd deployforges
```

```bash
cd /opt/deployforges
```

---

## 6. ENV

if you use `push-and-boot.sh` installing automaticly first installation.

```bash
cd /opt/deployforges   
cp deploy/.env.deploy.example .env
nano .env
```

Requirements:

- `DB_PASSWORD` — strong password
- `DF_GEMINI_API_KEY`  api key
- `DF_ADMIN_API_KEY` — random strong string  
- `DF_REQUIRE_EMAIL_VERIFICATION` — `false` for self-host (instant API key). Set `true` only after email verification is implemented.
- `DF_AI_DEBUG_IO` / `DF_AI_PERSIST_IO_EXCERPTS` — Gemini IO logging / DB excerpts (see README env table). Admin: `GET /api/v1/admin/projects/{id}/ai-interactions` with `X-Admin-Key`.
- `DF_GCP_PROJECT_ID` / region — Cloud Run  **deploy correction** or `DF_BUILD_BACKEND=kaniko`. 
- `DF_BUILD_BACKEND` — `local_docker`, `kaniko` (GCP Cloud Run Job + Kaniko), `skip`   
 

**Kaniko:** it has to be `DF_GCP_PROJECT_ID` on your .env file for running `DF_BUILD_BACKEND=kaniko` 
 
---

## 7. first migration and seed

```bash
cd /opt/deployforges   # repo kökü
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api alembic upgrade head
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api python scripts/seed_dev.py
```

---

## 8. Runnig the Stack

```bash
cd /opt/deployforges
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```

Control the Docker's healty:

```bash
docker compose -f deploy/docker-compose.vps.yml ps
curl -s https://api.[domain].com/api/v1/health
```

---

## 9. Upgrade (deploy)


Using by git:

```bash
ssh root@IP
cd /opt/deployforges
git pull
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```


THAT'S ALL

if your network is blocking you downloading the docker try it:

`/etc/docker/daemon.json` 

```json
{
  "registry-mirrors": ["https://mirror.gcr.io"],
  "dns": ["8.8.8.8", "1.1.1.1"]
}
```

then `systemctl restart docker` and  `docker pull` / `compose up`.
