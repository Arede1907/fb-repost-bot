# FB Repost Bot — YouTube Shorts → Facebook Automation

Monitors source Facebook pages and automatically reposts new content to target pages. Downloads YouTube Shorts and uploads them as Facebook Reels.

## Features

- **Repost Bot** — Checks source FB page every 3 minutes, auto-reposts new posts to target pages
- **YouTube → Facebook** — Downloads YouTube Shorts and uploads as Facebook Reels
- **Web UI** — Flask-based management panel (login, bot management, settings)
- **Quiet Hours** — Automatically pauses all operations during specified hours
- **Scheduled Sharing** — Schedule video posts for specific times
- **Auto Like** — Likes source post as target page before reposting
- **Persistence** — Bot states are saved to disk and restored on restart

## Installation

### Requirements

- Python 3.10+
- ffmpeg (for video re-encoding)
- Node.js (for yt-dlp)

### Steps

1. **Clone the repo**
```bash
git clone https://github.com/Arede1907/fb-repost-bot.git
cd fb-repost-bot
```

2. **Create virtual environment**
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows
```

3. **Install dependencies**
```bash
pip install -r requirements.txt
```

4. **Create `.env` file**
```bash
cp .env.example .env
```
Edit `.env` and fill in your credentials:
- `FB_USER_ACCESS_TOKEN` — Get from Facebook Graph API Explorer
- `FB_PAGES` — Page IDs and tokens (`PAGE_ID:TOKEN,PAGE_ID:TOKEN`)
- `YOUTUBE_CHANNEL_ID` — YouTube channel ID
- `UI_USERNAME` / `UI_PASSWORD` — Web UI login credentials

5. **Run**
```bash
python onapp.py
```
Web UI: `http://localhost:5000`

### Server Setup (systemd)

```bash
cp fbbot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable fbbot
systemctl start fbbot
```

### SSL (Optional)

Place `cert.pem` and `key.pem` in the `ssl/` directory to enable HTTPS automatically.

## Configuration

All settings are configured via the `.env` file. See `.env.example` for details.

| Variable | Description |
|---|---|
| `FB_USER_ACCESS_TOKEN` | Facebook User Access Token |
| `FB_PAGES` | Page IDs and tokens |
| `YOUTUBE_CHANNEL_ID` | YouTube channel ID |
| `QUIET_HOURS_START/END` | Quiet hours schedule (HH:MM) |
| `UI_USERNAME/PASSWORD` | Web UI login credentials |
| `PROXY_URL` | Residential proxy (optional) |

## License

MIT
