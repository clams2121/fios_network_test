#!/usr/bin/env bash
# Update the monitor: pull the latest code, install/refresh dependencies,
# validate the config, run the tests, and restart the service.
#
#   ./deploy.sh                    normal update + restart
#   ./deploy.sh --install-service  first-time setup: also generate and
#                                  enable the systemd unit with this
#                                  checkout's paths and your user
#
# Fails loudly at the first problem (set -e): a deploy that can't pass
# config validation or the test suite never restarts the service, so a
# working monitor keeps running.

set -euo pipefail

# Both units are installed and restarted together: the monitor collects
# the evidence, the web UI serves it.
SERVICES=(netmon netmon-web)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

SUDO=""
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

INSTALL_SERVICE=0
if [[ "${1:-}" == "--install-service" ]]; then
    INSTALL_SERVICE=1
elif [[ $# -gt 0 ]]; then
    echo "ERROR: unknown argument: $1 (only --install-service is accepted)" >&2
    exit 2
fi

echo "==> Pulling latest code..."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull --ff-only origin "$BRANCH"

echo "==> Checking dependencies..."
MISSING=()
command -v traceroute >/dev/null || MISSING+=(traceroute)
python3 -c "import matplotlib" 2>/dev/null || MISSING+=(python3-matplotlib)
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "    installing: ${MISSING[*]}"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y "${MISSING[@]}"
else
    echo "    all present (traceroute, matplotlib)"
fi

if [[ ! -f config.toml ]]; then
    echo "ERROR: no config.toml in $REPO_DIR." >&2
    echo "       cp config.example.toml config.toml  # then edit it" >&2
    exit 2
fi

echo "==> Validating config..."
python3 netmon.py --config config.toml --check

echo "==> Running tests..."
python3 -m unittest discover -s tests

service_installed() {
    systemctl cat "$1.service" >/dev/null 2>&1
}

if [[ $INSTALL_SERVICE -eq 1 ]]; then
    echo "==> Installing systemd units for $REPO_DIR..."
    if ! command -v systemctl >/dev/null; then
        echo "ERROR: systemctl not found; is this a systemd system?" >&2
        exit 2
    fi
    # Run the services as the invoking (non-root) user.
    RUN_USER="${SUDO_USER:-$(id -un)}"
    RUN_GROUP="$(id -gn "$RUN_USER")"
    for svc in "${SERVICES[@]}"; do
        sed -e "s|/opt/netmon|$REPO_DIR|g" \
            -e "s|^User=.*|User=$RUN_USER|" \
            -e "s|^Group=.*|Group=$RUN_GROUP|" \
            "systemd/$svc.service" \
            | $SUDO tee "/etc/systemd/system/$svc.service" >/dev/null
        echo "    installed $svc.service"
    done
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "${SERVICES[@]}"
fi

ANY_INSTALLED=0
for svc in "${SERVICES[@]}"; do
    service_installed "$svc" || continue
    ANY_INSTALLED=1
    if [[ "$svc" == "netmon" ]]; then
        echo "==> Restarting $svc (clean shutdown first: finishes in-flight"
        echo "    traceroutes and runs IP ownership lookups; can take a minute)..."
    else
        echo "==> Restarting $svc..."
    fi
    $SUDO systemctl restart "$svc"
    sleep 2
    if systemctl is-active --quiet "$svc"; then
        echo "==> $svc is running."
        $SUDO journalctl -u "$svc" -n 3 --no-pager || true
    else
        echo "ERROR: $svc failed to start. Recent log:" >&2
        $SUDO journalctl -u "$svc" -n 20 --no-pager >&2 || true
        exit 1
    fi
done

if [[ $ANY_INSTALLED -eq 0 ]]; then
    echo "==> No systemd units installed yet."
    echo "    First-time setup:  ./deploy.sh --install-service"
    echo "    Or run in the foreground:"
    echo "      python3 netmon.py --config config.toml"
    echo "      python3 netmon_web.py --config config.toml"
else
    WEB_IP=$(python3 - <<'PY'
from netmon_config import load_config
c = load_config("config.toml", prepare_dirs=False)
host = f"[{c.web_bind_ip}]" if ":" in c.web_bind_ip else c.web_bind_ip
print(f"http://{host}:{c.web_port}/")
PY
)
    echo "==> Web UI: $WEB_IP"
fi

echo "==> Deploy complete."
