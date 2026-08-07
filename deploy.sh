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

SERVICE=netmon
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
    systemctl cat "$SERVICE.service" >/dev/null 2>&1
}

if [[ $INSTALL_SERVICE -eq 1 ]]; then
    echo "==> Installing systemd unit for $REPO_DIR..."
    if ! command -v systemctl >/dev/null; then
        echo "ERROR: systemctl not found; is this a systemd system?" >&2
        exit 2
    fi
    # Run the service as the invoking (non-root) user.
    RUN_USER="${SUDO_USER:-$(id -un)}"
    RUN_GROUP="$(id -gn "$RUN_USER")"
    sed -e "s|/opt/netmon|$REPO_DIR|g" \
        -e "s|^User=.*|User=$RUN_USER|" \
        -e "s|^Group=.*|Group=$RUN_GROUP|" \
        systemd/netmon.service | $SUDO tee "/etc/systemd/system/$SERVICE.service" >/dev/null
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "$SERVICE"
fi

if service_installed; then
    echo "==> Restarting $SERVICE (clean shutdown first: finishes in-flight"
    echo "    traceroutes and runs IP ownership lookups; can take a minute)..."
    $SUDO systemctl restart "$SERVICE"
    sleep 2
    if systemctl is-active --quiet "$SERVICE"; then
        echo "==> $SERVICE is running."
        $SUDO journalctl -u "$SERVICE" -n 3 --no-pager || true
    else
        echo "ERROR: $SERVICE failed to start. Recent log:" >&2
        $SUDO journalctl -u "$SERVICE" -n 20 --no-pager >&2 || true
        exit 1
    fi
else
    echo "==> No systemd unit installed for $SERVICE."
    echo "    First-time setup:  ./deploy.sh --install-service"
    echo "    Or run in the foreground:  python3 netmon.py --config config.toml"
fi

echo "==> Deploy complete."
