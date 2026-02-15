#!/bin/bash
# Simple Plex Xtream Bridge Installer
# Run this from the directory where you want to install

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BLUE}"
echo "============================================================"
echo "  Plex Xtream Bridge - Simple Installer"
echo "============================================================"
echo -e "${NC}"

# Use current directory as install location
INSTALL_DIR="$(pwd)"

echo -e "${CYAN}Installing in: $INSTALL_DIR${NC}"
echo ""

# Check if running as root
if [ "$EUID" -eq 0 ]; then 
    echo -e "${RED}Please do not run as root. Run as your regular user.${NC}"
    exit 1
fi

# Check if plex_xtream_bridge_web.py exists
if [ ! -f "$INSTALL_DIR/plex_xtream_bridge_web.py" ]; then
    echo -e "${RED}ERROR: plex_xtream_bridge_web.py not found!${NC}"
    echo ""
    echo "This installer must be run from the directory containing plex_xtream_bridge_web.py"
    echo ""
    echo "Current directory: $INSTALL_DIR"
    echo ""
    exit 1
fi

echo -e "${GREEN}✓ Found plex_xtream_bridge_web.py${NC}"

echo -e "${CYAN}[1/5] Installing system packages...${NC}"
sudo apt update -qq
sudo apt install -y python3 python3-pip python3-venv

echo -e "${CYAN}[2/5] Creating data directory...${NC}"
mkdir -p "$INSTALL_DIR/data"

echo -e "${CYAN}[3/5] Setting up Python virtual environment...${NC}"
[ ! -d "venv" ] && python3 -m venv venv

echo -e "${CYAN}[4/5] Installing Python packages...${NC}"
source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet flask==3.0.0 plexapi==4.15.7 requests==2.31.0 cryptography==41.0.7

echo -e "${CYAN}[5/5] Setting permissions...${NC}"
chmod +x plex_xtream_bridge_web.py

# Check for --install-service flag
if [ "$1" == "--install-service" ]; then
    echo ""
    echo -e "${CYAN}Installing as system service...${NC}"
    
    sudo tee /etc/systemd/system/plex-xtream-bridge.service > /dev/null << SVCEOF
[Unit]
Description=Plex to Xtream Codes API Bridge
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/plex_xtream_bridge_web.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVCEOF
    
    sudo systemctl daemon-reload
    sudo systemctl enable plex-xtream-bridge.service
    sudo systemctl start plex-xtream-bridge.service
    
    echo -e "${GREEN}✓ Service installed and started${NC}"
fi

SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}============================================================"
echo "  Installation Complete!"
echo "============================================================${NC}"
echo ""

if [ "$1" == "--install-service" ]; then
    echo -e "${CYAN}Service Status:${NC}"
    systemctl is-active plex-xtream-bridge >/dev/null 2>&1 && echo -e "  ${GREEN}Running ✓${NC}" || echo -e "  ${RED}Not running ✗${NC}"
    echo ""
    echo -e "${CYAN}Service Commands:${NC}"
    echo -e "  ${BLUE}sudo systemctl status plex-xtream-bridge${NC}"
    echo -e "  ${BLUE}sudo systemctl restart plex-xtream-bridge${NC}"
    echo -e "  ${BLUE}sudo journalctl -u plex-xtream-bridge -f${NC}"
else
    echo -e "${CYAN}To start manually:${NC}"
    echo -e "  ${BLUE}cd $INSTALL_DIR${NC}"
    echo -e "  ${BLUE}source venv/bin/activate${NC}"
    echo -e "  ${BLUE}python3 plex_xtream_bridge_web.py${NC}"
    echo ""
    echo -e "${CYAN}To install as service:${NC}"
    echo -e "  ${BLUE}bash $0 --install-service${NC}"
fi

echo ""
echo -e "${CYAN}Access:${NC}"
echo -e "  Web: ${GREEN}http://$SERVER_IP:8080/admin${NC}"
echo -e "  Login: ${GREEN}admin / admin123${NC} ${YELLOW}(change on first login!)${NC}"
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  Ready to go! 🚀${NC}"
echo -e "${GREEN}============================================================${NC}"
echo ""
