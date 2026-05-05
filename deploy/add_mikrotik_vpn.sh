#!/bin/bash
# Ajouter un MikroTik au VPN WireGuard
# Usage: ./add_mikrotik_vpn.sh NOM_ROUTEUR
# Exemple: ./add_mikrotik_vpn.sh hotspot-centre-ville

NAME=$1
if [ -z "$NAME" ]; then
  echo "Usage: $0 NOM_ROUTEUR"
  exit 1
fi

# Trouver le prochain IP disponible
LAST_IP=$(grep "AllowedIPs" /etc/wireguard/wg0.conf | tail -1 | grep -oP '10\.8\.0\.\K\d+' || echo "1")
NEXT_IP=$((LAST_IP + 1))
VPN_IP="10.8.0.$NEXT_IP"

# Générer clés pour ce MikroTik
CLIENT_PRIVATE=$(wg genkey)
CLIENT_PUBLIC=$(echo "$CLIENT_PRIVATE" | wg pubkey)
SERVER_PUBLIC=$(cat /etc/wireguard/server_public.key)
SERVER_IP=$(curl -s ifconfig.me)

# Ajouter au serveur WireGuard
cat >> /etc/wireguard/wg0.conf << EOF

# $NAME
[Peer]
PublicKey = $CLIENT_PUBLIC
AllowedIPs = $VPN_IP/32
EOF

wg addconf wg0 <(wg-quick strip wg0) 2>/dev/null || systemctl restart wg-quick@wg0

echo ""
echo "=== Configuration MikroTik: $NAME ==="
echo "IP VPN attribuee: $VPN_IP"
echo ""
echo "Commandes a executer sur le MikroTik:"
echo ""
echo "/interface wireguard add name=wg-ketamon private-key=\"$CLIENT_PRIVATE\""
echo "/interface wireguard peers add interface=wg-ketamon \\"
echo "  public-key=\"$SERVER_PUBLIC\" \\"
echo "  endpoint-address=$SERVER_IP \\"
echo "  endpoint-port=51820 \\"
echo "  allowed-address=10.8.0.0/24 \\"
echo "  persistent-keepalive=25"
echo "/ip address add address=$VPN_IP/24 interface=wg-ketamon"
echo ""
echo "Dans KetaMon, utiliser comme Host: $VPN_IP"
