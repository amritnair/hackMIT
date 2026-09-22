#!/usr/bin/env bash
# Put Prophecy on a fresh Ubuntu box, permanently.
#
# Written against Oracle Cloud's Always Free tier, but there is nothing
# Oracle-specific in here: it works on any Ubuntu 22.04+ machine you can ssh
# into. Run it as root, once.
#
#   sudo bash deploy.sh yourdomain.com          # with TLS on a real domain
#   sudo bash deploy.sh                         # plain HTTP on port 80
#
# Afterwards:
#   systemctl status prophecy      is it running
#   journalctl -u prophecy -f      what it is doing
#   bash deploy.sh --update        pull the latest code and restart

set -euo pipefail

DOMAIN=${1:-}
APP=/opt/prophecy
DEMO=/srv/prophecy-demo
PORT=8000
CODE_REPO=https://github.com/amritnair/hackMIT.git
DEMO_REPO=https://github.com/amritnair/prophecy-demo.git

say() { printf '\n== %s\n' "$*"; }

if [[ "${1:-}" == "--update" ]]; then
  say "updating"
  git -C "$APP" pull --ff-only
  git -C "$DEMO" pull --ff-only || true
  systemctl restart prophecy
  systemctl --no-pager status prophecy | head -5
  exit 0
fi

[[ $EUID -eq 0 ]] || { echo "run this with sudo"; exit 1; }

say "packages"
apt-get update -qq
apt-get install -y -qq git python3 python3-pip python3-venv curl

say "code"
[[ -d $APP/.git ]] || git clone --quiet "$CODE_REPO" "$APP"
git -C "$APP" pull --ff-only --quiet || true
[[ -d $DEMO/.git ]] || git clone --quiet "$DEMO_REPO" "$DEMO"

# A venv rather than --break-system-packages, because Ubuntu 24.04 refuses
# the latter and the package has no dependencies to conflict with anyway.
say "install"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --quiet -e "$APP"

say "service user"
id -u prophecy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin prophecy
# The service writes .prophecy/ inside the repo it serves, so it has to own
# that tree. Owning it also settles git's "dubious ownership" refusal, which
# is otherwise the error people spend an hour on.
chown -R prophecy:prophecy "$DEMO"
git config --global --add safe.directory "$APP"

say "service"
install -m 644 "$APP/tools/prophecy.service" /etc/systemd/system/prophecy.service
systemctl daemon-reload
systemctl enable --now prophecy
sleep 3
curl -fsS -o /dev/null "http://127.0.0.1:$PORT/" && echo "serving on :$PORT"

say "web front"
if [[ -n $DOMAIN ]]; then
  # Caddy gets certificates on its own and renews them; this is the whole
  # reason not to hand-roll nginx and certbot here.
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy
  printf '%s {\n  reverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT" > /etc/caddy/Caddyfile
  systemctl restart caddy
  echo "https://$DOMAIN"
else
  echo "no domain given: reach it at http://$(curl -fsS ifconfig.me):$PORT"
  echo "open that port in your cloud firewall AND in iptables (see below)"
fi

# Oracle images ship with an iptables REJECT rule that silently eats
# everything but ssh, which is the single most common reason a correctly
# configured box looks dead from outside.
say "firewall"
if command -v netfilter-persistent >/dev/null; then
  for p in 80 443 "$PORT"; do
    iptables -I INPUT 5 -p tcp --dport "$p" -j ACCEPT 2>/dev/null || true
  done
  netfilter-persistent save >/dev/null 2>&1 || true
  echo "opened 80, 443 and $PORT locally: open them in the cloud console too"
fi

# Always Free instances get reclaimed when they look idle for a week, and a
# demo nobody clicks is exactly that. A heartbeat is cheaper than losing it.
say "heartbeat"
cat > /etc/cron.d/prophecy-heartbeat <<CRON
*/20 * * * * root curl -fsS -o /dev/null http://127.0.0.1:$PORT/api/risk || true
CRON

say "done"
systemctl --no-pager status prophecy | head -5
