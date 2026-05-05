#!/bin/bash
# Script d'installation KetaMon + WireGuard sur Oracle Cloud Ubuntu 22.04
set -e

echo "=== KetaMon Server Setup ==="

# 1. Mise à jour système
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx wireguard ufw

# 2. Cloner le projet
cd /opt
git clone https://github.com/neb-keta/ketamon.git
cd ketamon

# 3. Environnement Python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Service systemd pour KetaMon
cat > /etc/systemd/system/ketamon.service << 'EOF'
[Unit]
Description=KetaMon Web App
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/ketamon
Environment="PATH=/opt/ketamon/venv/bin"
ExecStart=/opt/ketamon/venv/bin/gunicorn app:app --bind 127.0.0.1:5001 --workers 2 --timeout 120
Restart=always

[Install]
WantedBy=multi-user.target
EOF

chown -R www-data:www-data /opt/ketamon
systemctl daemon-reload
systemctl enable ketamon
systemctl start ketamon

# 5. Nginx reverse proxy
cat > /etc/nginx/sites-available/ketamon << 'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF

ln -sf /etc/nginx/sites-available/ketamon /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

# 6. WireGuard serveur
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
SERVER_PRIVATE=$(cat /etc/wireguard/server_private.key)
SERVER_PUBLIC=$(cat /etc/wireguard/server_public.key)

cat > /etc/wireguard/wg0.conf << EOF
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = $SERVER_PRIVATE
PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
PostDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o eth0 -j MASQUERADE
EOF

systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

# 7. Firewall
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 51820/udp
ufw --force enable

echo ""
echo "=== Installation terminee ==="
echo "Cle publique WireGuard serveur:"
cat /etc/wireguard/server_public.key
echo ""
echo "Site accessible sur http://$(curl -s ifconfig.me)"
