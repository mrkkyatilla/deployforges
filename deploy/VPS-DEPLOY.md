# VPS deployment — `deploy.wrupup.com` + SSH + Cloudflare

Bu rehber tek bir Ubuntu/Debian VPS üzerinde DeployForge’u **SSH anahtarı ile** yönetip Cloudflare üzerinden subdomain ile yayınlamayı anlatır.

**Sunucu:** `root@188.132.197.229`  
**Alan adı:** `deploy.wrupup.com` → bu IP’ye yönlenecek.

---

## 1. Bilgisayarından SSH anahtarı hazırlama

```bash
ssh-keygen -t ed25519 -C "deployforge-wrupup" -f ~/.ssh/deployforge_wrupup
ssh-copy-id -i ~/.ssh/deployforge_wrupup.pub root@188.132.197.229
```

Sonrasında:

```bash
ssh -i ~/.ssh/deployforge_wrupup root@188.132.197.229
```

Sunucuda parola girişini kapatmak (isteğe bağlı, önerilir):

```bash
# Sunucuda:
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload sshd
```

---

## 2. Cloudflare DNS

1. Cloudflare’da `wrupup.com` seç → **DNS** → **Records**.
2. **Add record:**
   - **Type:** `A`
   - **Name:** `deploy`
   - **IPv4:** `188.132.197.229`
   - **Proxy:** başlangıçta sorun yaşarsan **DNS only** (gri bulut); sonra **Proxied** (turuncu) yapabilirsin.
3. **SSL/TLS** → öneri:
   - Origin’de **Let’s Encrypt** (Caddy otomatik alır) kullanıyorsan: **Full** veya **Full (strict)**.
   - İlk sertifika alımında sorun olursa kaydı geçici olarak **DNS only** yap, sertifika oluşunca tekrar **Proxied** aç.

Webhook URL’leri için dış adres: `https://deploy.wrupup.com`

---

## 3. Sunucuda Docker kurulumu

```bash
ssh root@188.132.197.229

apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# Debian kullanıyorsan satırı Debian için değiştir:
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable docker --now
```

---

## 4. Repo’yu sunucuya alma

DeployForge deposunun kökünde `Dockerfile` bulunur (`deployforge/` klasörü).

```bash
cd /opt
git clone https://github.com/YOUR_ORG/deployforge.git
cd deployforge
```

Monorepo kullanıyorsan (`detected_stack` gibi), sunucuda ilgili alt klasöre gir:

```bash
cd /opt/detected_stack/deployforge
```

SSH ile git kullanıyorsan deploy key ekle veya private repo için PAT.

---

## 5. Ortam değişkenleri

```bash
cd /opt/deployforge   # veya monorepo içindeki deployforge kökü
cp deploy/.env.deploy.example .env
nano .env
```

Mutlaka doldur:

- `DB_PASSWORD` — güçlü parola  
- `DF_GEMINI_API_KEY`  
- `DF_ADMIN_API_KEY` — rastgele uzun string  
- `DF_GCP_PROJECT_ID` / region — build Cloud Run kullanılacaksa  

HTTPS için kullanılan hostname `deploy/Caddyfile` içindedir (varsayılan `deploy.wrupup.com`). Farklı bir subdomain kullanıyorsan bu dosyayı düzenle ve yeniden `docker compose up -d` çalıştır.

---

## 6. İlk migration ve seed

```bash
cd /opt/deployforge   # repo kökü
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api alembic upgrade head
# seed opsiyonel:
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api python scripts/seed_dev.py
```

---

## 7. Stack’i başlatma

```bash
cd /opt/deployforge
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```

Durum:

```bash
docker compose -f deploy/docker-compose.vps.yml ps
curl -s https://deploy.wrupup.com/api/v1/health
```

---

## 8. Güncelleme (deploy)

```bash
ssh root@188.132.197.229
cd /opt/deployforge
git pull
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```

---

## 9. Güvenlik özeti

| Öğe | Öneri |
|-----|--------|
| SSH | Ed25519 anahtar, root yerine `deploy` kullanıcısı + sudo |
| Firewall | `ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable` |
| Postgres | Sadece Docker iç ağı; dışarıya port publish yok |
| Redis | Sadece iç ağ |
| API anahtarları | `POST /api/v1/auth/register` ile üret |

---

## Sorun giderme

- **502 Bad Gateway:** `docker compose logs api` — API healthcheck geçene kadar Caddy beklemeli.
- **TLS hatası:** DNS’nin IP’ye işlediğini doğrula; Cloudflare’da geçici DNS-only dene.
- **Alembic tablo yok:** İlk kez `alembic upgrade head` çalıştırılmalı (üstteki `run --rm api` komutu).
