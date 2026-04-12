# YouTube → Facebook Bot — Kurulum Rehberi

## 1. Python Bağımlılıklarını Yükle

```bash
pip install -r requirements.txt
```

---

## 2. .env Dosyasını Oluştur

```bash
cp .env.example .env
```

`.env` dosyasını bir metin editörüyle aç ve doldur.

---

## 3. YouTube Kanal ID Bulma

1. Takip etmek istediğin kanalın sayfasına git
2. URL'ye bak:
   - `youtube.com/channel/UCxxxxxx` → `UCxxxxxx` kısmı senin Channel ID'n
   - `youtube.com/@KanalAdi` formatındaysa: sayfada sağ tık → "Sayfa Kaynağını Görüntüle" → `channelId` ara
3. `.env` içinde `YOUTUBE_CHANNEL_ID=UCxxxxxx` olarak yaz

---

## 4. Facebook API Kurulumu

### 4.1 — Developer App Oluştur

1. https://developers.facebook.com adresine git
2. "My Apps" → "Create App"
3. Tip: **Business** seç
4. Uygulamayı oluştur

### 4.2 — Graph API Explorer ile Token Al

1. https://developers.facebook.com/tools/explorer adresine git
2. Uygulamanı seç (sağ üst)
3. "Generate Access Token" → **User Token** oluştur
4. Şu izinleri ekle:
   - `pages_show_list`
   - `pages_manage_posts`
   - `pages_read_engagement`
   - `publish_video`
5. Token'ı oluştur ve Facebook hesabınla giriş yap

### 4.3 — Page Access Token Al

User token ile şunu çalıştır:

```
GET https://graph.facebook.com/v19.0/me/accounts?access_token=USER_TOKEN
```

Her sayfa için `access_token` ve `id` alanları gelir. Bunları `.env`'e yaz.

### 4.4 — Long-lived Token (Önerilen)

User token 1 saat geçerlidir. Uzun süreli token için:

```
GET https://graph.facebook.com/v19.0/oauth/access_token
  ?grant_type=fb_exchange_token
  &client_id=APP_ID
  &client_secret=APP_SECRET
  &fb_exchange_token=SHORT_LIVED_TOKEN
```

Page token'lar zaten uzun sürelidir (60 gün veya kalıcı).

---

## 5. .env Örneği (Doldurulmuş)

```env
YOUTUBE_CHANNEL_ID=UCxxxxxxxxxxxxxxxxxxxxxxxx
CHECK_INTERVAL_MINUTES=30
FB_PAGES=111222333444:EAAxxxxxx,555666777888:EAAyyyyyy
FB_POST_MODE=instant
FB_SCHEDULE_DELAY_MINUTES=60
FB_DESCRIPTION_TEMPLATE={title}\n\n{url}
DOWNLOAD_DIR=./downloads
LOG_LEVEL=INFO
```

---

## 6. Çalıştırma

**Sürekli çalışma (önerilen):**
```bash
python main.py
```

**Tek seferlik kontrol:**
```bash
python main.py --once
```

**Arka planda çalıştırma (Windows):**
```bash
start /B pythonw main.py
```

---

## 7. Notlar

- İndirilen videolar `./downloads/` klasörüne kaydedilir, paylaşım sonrası otomatik silinir
- İşlenen video ID'leri `state.json`'da tutulur — aynı video iki kez paylaşılmaz
- Loglar hem ekrana hem `bot.log` dosyasına yazılır
- `FB_POST_MODE=scheduled` yapılırsa videolar `FB_SCHEDULE_DELAY_MINUTES` dakika sonraya planlanır
