# Portainer Deployment Guide

## Method 1: Portainer Stack (Recommended)

### Step 1: Build the Docker Image

First, build the image locally or use GitHub Actions to build it.

**Local Build:**
```bash
# Clone the repository
git clone https://github.com/kohnorw/PLEX-VOD.git
cd PLEX-VOD

# Build the image
docker build -t plex-xtream-bridge:latest .

### Step 2: Deploy in Portainer

1. **Open Portainer** - Navigate to your Portainer instance
2. **Go to Stacks** - Click "Stacks" in the sidebar
3. **Add Stack** - Click "+ Add stack"
4. **Name your stack** - e.g., "plex-xtream-bridge"
5. **Paste this compose file:**

version: '3.8'

services:
  plex-xtream-bridge:
    image: plex-xtream-bridge:latest
    container_name: plex-xtream-bridge
    restart: unless-stopped
    
    ports:
      - "8080:8080"
    
    volumes:
      - plex-bridge-data:/app/data
    
    environment:
      - BRIDGE_HOST=0.0.0.0
      - BRIDGE_PORT=8080
    
    networks:
      - plex-bridge-network

networks:
  plex-bridge-network:
    driver: bridge

volumes:
  plex-bridge-data:
```

6. **Click "Deploy the stack"**

### Step 3: Access the Web Interface

Visit: `http://YOUR_SERVER_IP:8080/admin`

Default credentials:
- Username: `admin`
- Password: `admin123` (change on first login)

---

## Method 2: Docker Run (Manual)

In Portainer's **Containers** section:

1. Click **+ Add container**
2. Fill in:

**Name:** `plex-xtream-bridge`

**Image:** `plex-xtream-bridge:latest`

**Port mapping:**
- `8080:8080`

**Volumes:**
- `/path/on/host/data:/app/data`

**Restart policy:** Unless stopped

**Network:** bridge

3. Click **Deploy the container**

---

## Accessing from Other Containers

If Plex is also in Docker and you need to access it:

### Option 1: Same Docker Network

Put both containers on the same network:

```yaml
services:
  plex-xtream-bridge:
    networks:
      - plex-network

networks:
  plex-network:
    external: true
```

Then use: `http://plex:32400` as your Plex URL

### Option 2: Host Network Mode

```yaml
services:
  plex-xtream-bridge:
    network_mode: host
```

Then use: `http://localhost:32400` as your Plex URL

---

## Environment Variables

You can configure these in Portainer's environment variables section:

| Variable | Description | Default |
|----------|-------------|---------|
| `BRIDGE_HOST` | Bind address | `0.0.0.0` |
| `BRIDGE_PORT` | Port to listen on | `8080` |

**Note:** Plex URL, Token, and TMDb key should be configured via the web interface for security (they're encrypted).

---

## Updating the Container

### Update via Portainer

1. Go to **Images**
2. Pull latest image
3. Go to **Containers**
4. Stop the container
5. **Recreate** with new image
6. Start the container

### Update via Stack

1. Edit your stack
2. Change image tag or pull latest
3. Click **Update the stack**
4. Select **Re-pull image and redeploy**

---

## Troubleshooting

### Container won't start

**Check logs in Portainer:**
1. Go to Containers
2. Click on plex-xtream-bridge
3. Click "Logs"

Common issues:
- Port 8080 already in use → Change port mapping
- Permission issues → Check volume permissions
- Can't reach Plex → Check network settings

### Can't access from IPTV player

1. Make sure port 8080 is exposed
2. Check firewall rules
3. Use correct IP address (container host IP)
4. Verify container is running

### Plex connection fails

If Plex URL uses `localhost` or `127.0.0.1`:
- Use host machine's IP instead
- Or use `host.docker.internal` (Docker Desktop)
- Or use host network mode

---

## Data Persistence

The `/app/data` volume contains:
- `config.json` - Your configuration (encrypted)
- `tmdb_cache.json` - TMDb matches cache

**Backup recommendations:**
```bash
# Backup data
docker cp plex-xtream-bridge:/app/data ./backup/

# Restore data
docker cp ./backup/data plex-xtream-bridge:/app/
```

---

## Performance Tips

### For large libraries:

```yaml
services:
  plex-xtream-bridge:
    deploy:
      resources:
        limits:
          memory: 512M
        reservations:
          memory: 256M
```

### For multiple users:

The container is already optimized for concurrent users with:
- Multi-threaded request handling
- Session-based caching
- Efficient memory usage

---

## Security Notes

- Change default admin password immediately
- Don't expose port 8080 to the internet directly
- Use reverse proxy (Nginx, Traefik) for SSL
- Keep config.json secure (contains encrypted credentials)

---

## Example: Full Production Stack with Reverse Proxy

```yaml
version: '3.8'

services:
  plex-xtream-bridge:
    image: plex-xtream-bridge:latest
    container_name: plex-xtream-bridge
    restart: unless-stopped
    volumes:
      - plex-bridge-data:/app/data
    environment:
      - BRIDGE_HOST=0.0.0.0
      - BRIDGE_PORT=8080
    networks:
      - traefik-network
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.plex-bridge.rule=Host(`iptv.yourdomain.com`)"
      - "traefik.http.services.plex-bridge.loadbalancer.server.port=8080"

networks:
  traefik-network:
    external: true

volumes:
  plex-bridge-data:
```

---

Need help? Open an issue on GitHub!
