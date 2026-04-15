# Plex VOD Bridge

Turn your Plex library into an IPTV service! Watch your Plex movies and TV shows in any IPTV player like TiviMate, IPTV Smarters, and more.

![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## 🎯 What Does This Do?

This bridge transforms your Plex Media Server into an Xtream Codes API service, allowing you to:
- ✅ Access your Plex movies and TV shows through any IPTV player
- ✅ Get high-quality movie posters from TMDb (optional)
- ✅ Browse your entire library with beautiful artwork
- ✅ Stream directly from Plex to your IPTV app

## 🚀 Quick Start (Portainer)

**The easiest way to run this is with Docker/Portainer:**
```yaml
docker pull ghcr.io/kohnorw/plex-vod:latest
```
### Step 1: Deploy in Portainer

1. Open Portainer → **Stacks** → **+ Add stack**
2. Name it: `plex-vod`
3. Paste this:

```yaml
version: '3.8'

services:
  plex-vod:
    image: ghcr.io/kohnorw/plex-vod:latest
    container_name: plex-vod
    restart: unless-stopped
    
    ports:
      - "8080:8080"
    
    volumes:
      - plex-vod-data:/app/data
    
    environment:
      - BRIDGE_HOST=0.0.0.0
      - BRIDGE_PORT=8080

volumes:
  plex-vod-data:
```

4. Click **Deploy the stack**

### Step 2: Configure

Visit: `http://YOUR_SERVER_IP:8080/admin`

**Login with:**
- Username: `admin`
- Password: `admin123` (you'll change this on first login)

**Add your Plex info:**
1. **Plex Server URL** - Usually `http://192.168.1.X:32400`
2. **Plex Token** - [How to find your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
3. **TMDb API Key** (optional) - [Get free API key](https://www.themoviedb.org/settings/api) for high-quality posters

### Step 3: Add to IPTV Player

**In TiviMate, IPTV Smarters, or any Xtream player:**

- **Server URL**: `http://YOUR_SERVER_IP:8080`
- **Username**: `admin` (or what you set)
- **Password**: Your bridge password (set in settings)
- **Type**: Xtream Codes API

**Done!** 🎉 Your Plex library is now in your IPTV player!

---

## 💻 Other Installation Methods

<details>
<summary><b>Docker Command Line</b></summary>

```bash
docker run -d \
  --name plex-vod \
  -p 8080:8080 \
  -v plex-vod-data:/app/data \
  --restart unless-stopped \
  ghcr.io/kohnorw/plex-vod:latest
```
</details>

<details>
<summary><b>Docker Compose</b></summary>

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  plex-vod:
    image: ghcr.io/kohnorw/plex-vod:latest
    container_name: plex-vod
    restart: unless-stopped
    ports:
      - "8080:8080"
    volumes:
      - plex-vod-data:/app/data

volumes:
  plex-vod-data:
```

Run: `docker-compose up -d`
</details>

<details>
<summary><b>Linux Install (without Docker)</b></summary>

```bash
# Clone the repository
git clone https://github.com/kohnorw/plex-vod.git
cd plex-vod

# Run installer
chmod +x install.sh
./install.sh --install-service

# Access at http://YOUR_IP:8080/admin
```
</details>

---

## 🎬 Features

### Core Features
- 🎥 **Full Plex Integration** - All your movies and TV shows
- 📺 **Xtream Codes API** - Works with all IPTV players
- 🖼️ **TMDb Posters** - High-quality artwork (optional)
- 🔄 **Auto-Matching** - Automatically fetches posters every 30 minutes
- 💾 **Persistent Cache** - Saves poster matches (survives restarts)

### Admin Features
- 🌐 **Web Interface** - Easy configuration
- 🔍 **Manual Matching** - Search and fix poster matches
- 📄 **Pagination** - Browse all your content
- 🔐 **Secure** - Password protected

### Performance
- ⚡ **Multi-User** - Handles many users at once
- 🚀 **Fast** - Optimized caching
- 💪 **Reliable** - Auto-restart on errors

---

## 📱 Tested IPTV Players

Works perfectly with:
- ✅ **TiviMate** (Recommended)
- ✅ **IPTV Smarters Pro**
- ✅ **Chilio**
- ✅ **Dispatcharr**
- ✅ **Perfect Player**
- ✅ **GSE Smart IPTV**

---

## 🔧 Configuration

### Required Settings
- **Plex Server URL** - Your Plex server address
- **Plex Token** - Authentication token from Plex

### Optional Settings
- **TMDb API Key** - For high-quality posters (free)
- **Bridge Username** - For IPTV player login
- **Bridge Password** - For IPTV player login

### How to Get Plex Token

1. Open Plex Web App
2. Play any movie/show
3. Click **"..."** → **Get Info**
4. Click **"View XML"**
5. Look in the URL: `X-Plex-Token=YOUR_TOKEN_HERE`

[Full guide here](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)

### How to Get TMDb API Key (Optional)

1. Create account at [TMDb.org](https://www.themoviedb.org/)
2. Go to [Settings → API](https://www.themoviedb.org/settings/api)
3. Request API key (free)
4. Copy the **"API Key (v3 auth)"**

---

## 🐛 Troubleshooting

### Can't access web interface

**Check container is running:**
```bash
docker ps | grep plex-vod
```

**Check logs:**
```bash
docker logs plex-vod
```

**Check port:**
```bash
netstat -tlnp | grep 8080
```

### Can't connect to Plex

- ✅ Make sure Plex URL is correct
- ✅ Make sure Plex Token is valid
- ✅ Try accessing Plex URL from the container
- ✅ If Plex is in Docker, use `http://host.docker.internal:32400`

### No posters in IPTV player

- ✅ Add TMDb API key (optional but recommended)
- ✅ Wait for auto-match to run (30 minutes)
- ✅ Or manually trigger: Click "Auto-Match Unmatched" in admin

### Content not showing

- ✅ Verify Plex connection in admin panel
- ✅ Check credentials in IPTV player
- ✅ Make sure content exists in Plex libraries

---

## 📊 How It Works

```
┌─────────────┐         ┌──────────────┐         ┌────────────┐
│             │         │              │         │            │
│  Plex VOD   │◄────────│     Plex     │◄────────│   TMDb     │
│   Bridge    │  Auth   │    Server    │  Meta   │    API     │
│             │         │              │         │  (optional)│
└──────┬──────┘         └──────────────┘         └────────────┘
       │
       │ Xtream API
       │
       ▼
┌─────────────┐
│   IPTV      │
│   Player    │
│  (TiviMate) │
└─────────────┘
```

1. Bridge connects to your Plex server
2. Optionally fetches high-quality posters from TMDb
3. Exposes everything via Xtream Codes API
4. IPTV players connect and stream from Plex

---

## 🔐 Security

- 🔒 **Encrypted Storage** - API keys encrypted with AES-256
- 🔑 **Hashed Passwords** - SHA-256 password hashing
- 🛡️ **Session Security** - Secure Flask sessions
- 📝 **Force Password Change** - Must change default password

**Important:** Change the default password on first login!

---

## 🆘 Support

**Need help?**
- 📖 Check the [Troubleshooting](#-troubleshooting) section
- 💬 [Open an issue](https://github.com/kohnorw/plex-vod/issues)
- 📚 See full documentation in [PORTAINER.md](PORTAINER.md) and [GHCR.md](GHCR.md)

---

## 📝 License

MIT License - see [LICENSE](LICENSE) file

---

## 🙏 Credits

- **Plex** - Amazing media server
- **TMDb** - Movie/TV metadata and artwork
- **PlexAPI** - Python library for Plex
- Some code written by bigverm23

---

## ⭐ Star This Repo

If this helped you, give it a star! ⭐ It helps others find it too.

---

**Made with ❤️ for the Plex community**
