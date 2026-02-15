# Plex Xtream Bridge

Transform your Plex Media Server into an Xtream Codes API-compatible IPTV service. Access your Plex content through any IPTV player that supports Xtream Codes API (TiviMate, IPTV Smarters, etc.).

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)
![Plex](https://img.shields.io/badge/plex-compatible-orange.svg)

## ✨ Features

### Core Functionality
- 🎬 **Full Plex Integration** - Access all your movies and TV shows
- 📺 **Xtream Codes API** - Compatible with popular IPTV players
- 🖼️ **TMDb Integration** - High-quality posters and metadata (optional)
- 🔄 **Auto-Matching** - Automatically fetches TMDb posters every 30 minutes
- 💾 **Persistent Cache** - TMDb matches survive restarts
- 🎯 **Manual Matching** - Search and fix TMDb matches for any content

### Web Interface
- 🌐 **Modern Admin Panel** - Easy configuration and management
- 🔍 **Search & Match** - Find and manually match content to TMDb
- 📄 **Pagination** - Browse through all unmatched content
- 📊 **Library Stats** - View your Plex libraries at a glance
- 🔐 **Secure Authentication** - Password-protected admin interface

### Performance
- ⚡ **Lightning Fast** - Optimized for multiple concurrent users
- 🚀 **Multi-threaded** - Handle many users simultaneously
- 💽 **Smart Caching** - Session-based caching for instant responses
- 🔄 **Auto-Refresh** - Library sections cached for 5 minutes

## 📋 Requirements

- Python 3.8 or higher
- Plex Media Server with active content
- TMDb API key (optional, for high-quality posters)
- Linux/Ubuntu server (tested on Ubuntu 24.04)

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/plex-xtream-bridge.git
cd plex-xtream-bridge
```

### 2. Run the Installer

```bash
chmod +x install.sh
./install.sh --install-service
```

The installer will:
- Install system dependencies (Python, pip, venv)
- Create a Python virtual environment
- Install required Python packages
- Set up the systemd service (if `--install-service` flag is used)

### 3. Configure the Bridge

Visit the admin panel:
```
http://YOUR_SERVER_IP:8080/admin
```

Default credentials:
- Username: `admin`
- Password: `admin123` (you'll be forced to change this on first login)

Configure:
1. **Plex Server URL** - Your Plex server address (e.g., `http://192.168.1.100:32400`)
2. **Plex Token** - [Get your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)
3. **TMDb API Key** (optional) - [Get a free TMDb API key](https://www.themoviedb.org/settings/api)

### 4. Add to Your IPTV Player

Use these credentials in your IPTV player:

- **Server URL**: `http://YOUR_SERVER_IP:8080`
- **Username**: `admin` (or whatever you set in settings)
- **Password**: `yoursetpassword` (or whatever you set in settings)
- **Type**: Xtream Codes API

## 📱 Supported IPTV Players

Tested and working with:
- ✅ TiviMate
- ✅ IPTV Smarters Pro
- ✅ Chilio
- ✅ Dispatcharr
- ✅ Perfect Player
- ✅ GSE Smart IPTV

## 🛠️ Installation Options

### Option 1: Systemd Service (Recommended)

```bash
./install.sh --install-service
```

Manage the service:
```bash
# Start the service
sudo systemctl start plex-xtream-bridge

# Stop the service
sudo systemctl stop plex-xtream-bridge

# Restart the service
sudo systemctl restart plex-xtream-bridge

# View logs
sudo journalctl -u plex-xtream-bridge -f
```

### Option 2: Manual Start

```bash
./install.sh
source venv/bin/activate
python3 plex_xtream_bridge_web.py
```

## 📖 Configuration

### Settings Page

Access via: `http://YOUR_SERVER_IP:8080/admin/settings`

**Plex Configuration:**
- Plex Server URL
- Plex Token

**Authentication:**
- Bridge Username (for IPTV players)
- Bridge Password (for IPTV players)
- Admin Password (for web interface)

**Optional Features:**
- TMDb API Key (for high-quality posters)

## 🎬 TMDb Integration

### Why Use TMDb?

- 🖼️ **High-Quality Posters** - Professional movie/TV posters
- 📊 **Rich Metadata** - Cast, crew, ratings, trailers
- 🌐 **Universal URLs** - Works from anywhere (no Plex token needed)

### Auto-Matching

When TMDb is configured:
- Runs automatically on startup
- Re-runs every 30 minutes
- Matches new content automatically
- Saves matches to disk (survives restarts)

### Manual Matching

Visit: `http://YOUR_SERVER_IP:8080/admin/match-tmdb`

Features:
- 🔍 **Search your Plex library** - Find any movie or TV show
- 🎯 **Search TMDb** - Find the correct match
- ✅ **One-click matching** - Click to match
- 📄 **Pagination** - Browse through all unmatched content
- 🔄 **Trigger auto-match** - Run matching on demand

## 🔧 Advanced Configuration

### File Locations

```
your-install-directory/
├── plex_xtream_bridge_web.py  # Main application
├── install.sh                  # Installation script
├── venv/                       # Python virtual environment
└── data/                       # Data directory
    ├── config.json             # Configuration (encrypted)
    └── tmdb_cache.json         # TMDb matches cache
```

### Port Configuration

Default port: `8080`

To change, edit the config file or set environment variable:
```bash
export BRIDGE_PORT=9090
```

### Network Access

Make sure port 8080 is accessible:
```bash
sudo ufw allow 8080
```

## 🔐 Security

- 🔒 **Encrypted Storage** - API keys and tokens are encrypted with AES-256
- 🔑 **Hashed Passwords** - Passwords stored with SHA-256 hashing
- 🛡️ **Secure Sessions** - Flask sessions with secret keys
- 📝 **First-Time Password Change** - Forces password change on first login
- 🔐 **File Permissions** - Config files have restricted permissions (600)

## 📊 Performance Tips

### For Multiple Users

The bridge is optimized for concurrent users:
- Multi-threaded request handling
- Session-based caching
- Library section caching (5-minute refresh)
- Minimal response sizes

### For Large Libraries

- TMDb auto-matching runs in background
- Pagination prevents timeouts
- Maximum 500 movies per request (configurable)
- Maximum 300 TV shows per request (configurable)

## 🐛 Troubleshooting

### Bridge Won't Start

Check logs:
```bash
sudo journalctl -u plex-xtream-bridge -n 50
```

Common issues:
- Port already in use (change port in config)
- Missing Python dependencies (run installer again)
- Plex not accessible (check Plex URL and token)

### No Posters in IPTV Player

1. Check if TMDb is configured (optional)
2. Verify TMDb API key is valid
3. Check if auto-match has run
4. Try manual matching

### Content Not Showing

1. Verify Plex connection in admin panel
2. Check Plex token is valid
3. Ensure content is in Plex libraries
4. Check IPTV player credentials

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## ⚠️ Disclaimer

This project is not affiliated with Plex Inc. or TMDb. It's a third-party bridge that uses the Plex API and TMDb API.

## 🙏 Acknowledgments

- [Plex](https://www.plex.tv/) - For the amazing media server
- [TMDb](https://www.themoviedb.org/) - For the movie/TV metadata
- [PlexAPI](https://github.com/pkkid/python-plexapi) - Python library for Plex

## 📞 Support

- Open an issue on GitHub
- Check existing issues for solutions
- Review the troubleshooting section

---

Made with ❤️ for the Plex community
