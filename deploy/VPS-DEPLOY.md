# VPS deployment — `deploy.wrupup.com` + SSH + Cloudflare

Bu rehber tek bir Ubuntu/Debian VPS üzerinde DeployForge’u **SSH anahtarı ile** yönetip Cloudflare üzerinden subdomain ile yayınlamayı anlatır.

**Sunucu:** `root@217.60.98.139`  
**Alan adı:** `deploy.wrupup.com` → bu IP’ye yönlenecek.

---

## 1. Bilgisayarından SSH anahtarı hazırlama

```bash
ssh-keygen -t ed25519 -C "deployforge-wrupup" -f ~/.ssh/deployforge_wrupup
ssh-copy-id -i ~/.ssh/deployforge_wrupup.pub root@217.60.98.139
```

Sonrasında:

```bash
ssh -i ~/.ssh/deployforge_wrupup root@217.60.98.139
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
   - **IPv4:** `217.60.98.139`
   - **Proxy:** başlangıçta sorun yaşarsan **DNS only** (gri bulut); sonra **Proxied** (turuncu) yapabilirsin.
3. **SSL/TLS** → öneri:
   - Origin’de **Let’s Encrypt** (Caddy otomatik alır) kullanıyorsan: **Full** veya **Full (strict)**.
   - İlk sertifika alımında sorun olursa kaydı geçici olarak **DNS only** yap, sertifika oluşunca tekrar **Proxied** aç.

Webhook URL’leri için dış adres: `https://deploy.wrupup.com`

---

## 3. Sunucuda Docker kurulumu

```bash
ssh root@217.60.98.139

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

## 4. Tek komut (yerel makineden rsync + kurulum)

SSH anahtarın sunucuda tanımlıysa, **bilgisayarından** (repo kökü):

```bash
chmod +x deploy/push-and-boot.sh
./deploy/push-and-boot.sh
```

- Kod `/opt/deployforges` altına rsync edilir (`--delete`: sunucudaki fazla dosyalar silinir).
- Yerelde `deployforges/.env` varsa sunucuya da kopyalanır; yoksa sunucuda şablondan `.env` üretilir (`DB_PASSWORD` / `DF_ADMIN_API_KEY` rastgele).
- Yerel `.env` göndermek istemezsen: `SKIP_LOCAL_ENV=1 ./deploy/push-and-boot.sh`

---

## 5. Repo’yu sunucuya alma (alternatif: manuel git)

Depo kökünde `Dockerfile` bulunur (`deployforges/` veya senin klasör adın).

```bash
cd /opt
git clone https://github.com/YOUR_ORG/deployforges.git
cd deployforges
```

Monorepo kullanıyorsan (`detected_stack` gibi), sunucuda ilgili alt klasöre gir:

```bash
cd /opt/detected_stack/deployforges
```

SSH ile git kullanıyorsan deploy key ekle veya private repo için PAT.

---

## 6. Ortam değişkenleri

`push-and-boot.sh` kullanıyorsan ilk kurulumda çoğu adım otomatik; elle yapıyorsan:

```bash
cd /opt/deployforges   # veya monorepo içindeki kök
cp deploy/.env.deploy.example .env
nano .env
```

Mutlaka doldur:

- `DB_PASSWORD` — güçlü parola  
- `DF_GEMINI_API_KEY`  
- `DF_ADMIN_API_KEY` — rastgele uzun string  
- `DF_GCP_PROJECT_ID` / region — Cloud Run ile **deploy doğrulaması** veya `DF_BUILD_BACKEND=kaniko` için  
- `DF_BUILD_BACKEND` — `local_docker` (varsayılan, VPS’te host Docker), `kaniko` (GCP Cloud Run Job + Kaniko; imajda gcloud gerekir), `skip` (imaj build yok; GCP yoksa deploy adımı da atlanır)  

**VPS’te `local_docker`:** `celery-worker` servisi host’taki `docker.sock` ile eşlenir. Bu soket **host root ile eşdeğer yetki** verir; sadece build worker’a mount et, `DOCKER_GID` ile host’taki `docker` grubunun GID’sini ver (`getent group docker | cut -d: -f3`). Varsayılan `999` her sunucuda doğru olmayabilir.

**Kaniko:** `DF_BUILD_BACKEND=kaniko` için `DF_GCP_PROJECT_ID` zorunludur; worker imajını `INSTALL_GCLOUD=1` ile yeniden build et (`deploy/docker-compose.vps.yml` içindeki `celery-worker` build arg). GCS bucket `gs://<project>-build-contexts` ve `gcloud` izinleri için `scripts/setup_gcp.sh` ve aşağıdaki API/bucket adımlarına bak.

HTTPS için kullanılan hostname `deploy/Caddyfile` içindedir (varsayılan `deploy.wrupup.com`). Farklı bir subdomain kullanıyorsan bu dosyayı düzenle ve yeniden `docker compose up -d` çalıştır.

---

## 7. İlk migration ve seed

**Sıra:** Önce şema (`alembic upgrade head`), sonra isteğe bağlı seed. Seed’i migration’dan önce çalıştırırsanız `relation "users" does not exist` alırsınız.

`push-and-boot.sh` kullanıyorsanız bu adımlar genelde otomatik. Elle yapıyorsanız:

```bash
cd /opt/deployforges   # repo kökü
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api alembic upgrade head
docker compose -f deploy/docker-compose.vps.yml --env-file .env run --rm api python scripts/seed_dev.py
```

---

## 8. Stack’i başlatma

```bash
cd /opt/deployforges
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```

Durum:

```bash
docker compose -f deploy/docker-compose.vps.yml ps
curl -s https://deploy.wrupup.com/api/v1/health
```

---

## 9. Güncelleme (deploy)

Tekrar yerelden:

```bash
./deploy/push-and-boot.sh
```

Sunucuda git kullanıyorsan:

```bash
ssh root@217.60.98.139
cd /opt/deployforges
git pull
docker compose -f deploy/docker-compose.vps.yml --env-file .env up -d --build
```

---

## 10. Güvenlik özeti

| Öğe | Öneri |
|-----|--------|
| SSH | Ed25519 anahtar, root yerine `deploy` kullanıcısı + sudo |
| Firewall | `ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable` |
| Postgres | Sadece Docker iç ağı; dışarıya port publish yok |
| Redis | Sadece iç ağ |
| Docker socket | `celery-worker` için: host `docker.sock` mount **yalnızca** bu serviste; GID eşlemesi için `DOCKER_GID` |
| API anahtarları | `POST /api/v1/auth/register` ile üret |

---

## Sorun giderme

- **502 Bad Gateway:** `docker compose logs api` — API healthcheck geçene kadar Caddy beklemeli.
- **TLS hatası:** DNS’nin IP’ye işlediğini doğrula; Cloudflare’da geçici DNS-only dene.
- **`relation "users" does not exist` (seed / API):** Migration çalışmadı. Önce `alembic upgrade head` (bkz. bölüm 7), sonra seed veya API kullanın.
- **Docker Hub imaj çekilemiyor (timeout / R2):** Sunucuda `/etc/docker/daemon.json` örneği — DNS ve isteğe bağlı mirror:

```json
{
  "registry-mirrors": ["https://mirror.gcr.io"],
  "dns": ["8.8.8.8", "1.1.1.1"]
}
```

Ardından `systemctl restart docker` ve tekrar `docker pull` / `compose up`.
