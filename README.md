# fbbot — YouTube Shorts → Facebook Repost Botu

Kaynak Facebook sayfasını izler, yeni postları hedef sayfalara otomatik repost eder. YouTube Shorts videolarını indirip Facebook Reels olarak paylaşır.

## Özellikler

- **Repost Bot** — Kaynak FB sayfasını 3 dakikada bir kontrol eder, yeni postları hedef sayfalara otomatik repost eder
- **YouTube → Facebook** — YouTube Shorts videolarını indirip Facebook Reels olarak yükler
- **Web UI** — Flask tabanlı yönetim paneli (login, bot yönetimi, ayarlar)
- **Gece Modu** — Belirlenen saatler arasında tüm işlemler otomatik duraklar
- **Zamanlanmış Paylaşım** — Videoları belirli saatte paylaşma
- **Otomatik Like** — Repost öncesi kaynak postu hedef sayfa olarak beğenir
- **Persistence** — Bot durumları diske kaydedilir, restart'ta geri yüklenir

## Kurulum

### Gereksinimler

- Python 3.10+
- ffmpeg (video re-encode için)
- Node.js (yt-dlp için)

### Adımlar

1. **Repoyu klonla**
```bash
git clone https://github.com/Arede1907/fbbot.git
cd fbbot
```

2. **Virtual environment oluştur**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. **Bağımlılıkları kur**
```bash
pip install -r requirements.txt
```

4. **`.env` dosyasını oluştur**
```bash
cp .env.example .env
```
`.env` dosyasını düzenleyip kendi bilgilerini gir:
- `FB_USER_ACCESS_TOKEN` — Facebook Graph API Explorer'dan al
- `FB_PAGES` — Sayfa ID'leri ve token'ları (`PAGE_ID:TOKEN,PAGE_ID:TOKEN`)
- `YOUTUBE_CHANNEL_ID` — YouTube kanal ID'si
- `UI_USERNAME` / `UI_PASSWORD` — Web UI giriş bilgileri

5. **Çalıştır**
```bash
python onapp.py
```
Web UI: `http://localhost:5000`

### Sunucu Kurulumu (systemd)

```bash
cp fbbot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable fbbot
systemctl start fbbot
```

### SSL (Opsiyonel)

`ssl/` klasörüne `cert.pem` ve `key.pem` koyarsan otomatik HTTPS'e geçer.

## Yapılandırma

Tüm ayarlar `.env` dosyasından yapılır. Detaylar için `.env.example` dosyasına bak.

| Değişken | Açıklama |
|---|---|
| `FB_USER_ACCESS_TOKEN` | Facebook User Access Token |
| `FB_PAGES` | Sayfa ID ve token'ları |
| `YOUTUBE_CHANNEL_ID` | YouTube kanal ID |
| `QUIET_HOURS_START/END` | Gece modu saatleri (HH:MM) |
| `UI_USERNAME/PASSWORD` | Web UI giriş bilgileri |
| `PROXY_URL` | Residential proxy (opsiyonel) |

## Lisans

MIT
