#!/usr/bin/env bash
# Update the monitor: pull the latest code, install/refresh dependencies,
# validate the config, run the tests, and restart the services.
#
#   ./deploy.sh                    normal update + restart
#   ./deploy.sh --install-service  first-time setup: also generate and
#                                  enable the systemd units with this
#                                  checkout's paths, interpreter and user
#   ./deploy.sh --rebuild-venv     discard and rebuild the virtualenv
#                                  (use when Python dependencies are
#                                  broken, e.g. a numpy/matplotlib ABI
#                                  mismatch)
#   ./deploy.sh --no-venv          use the system python3 instead of the
#                                  virtualenv (expects apt's
#                                  python3-matplotlib)
#
# Flags may be combined. Fails loudly at the first problem (set -e): a
# deploy that can't pass config validation or the test suite never
# restarts the services, so a working monitor keeps running.
#
# Why a virtualenv by default: matplotlib and numpy must be a matched
# pair, built against the same ABI. Mixing apt's python3-matplotlib with
# a pip-installed numpy produces
# "ImportError: numpy.core.multiarray failed to import". A virtualenv
# built from requirements.txt gets both from pip together, isolated from
# whatever else the system Python has.

set -euo pipefail

# Both units are installed and restarted together: the monitor collects
# the evidence, the web UI serves it.
SERVICES=(netmon netmon-web)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"
VENV_DIR="$REPO_DIR/.venv"

SUDO=""
if [[ $EUID -ne 0 ]]; then
    SUDO="sudo"
fi

INSTALL_SERVICE=0
REBUILD_VENV=0
USE_VENV=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-service) INSTALL_SERVICE=1 ;;
        --rebuild-venv)    REBUILD_VENV=1 ;;
        --no-venv)         USE_VENV=0 ;;
        *)
            echo "ERROR: unknown argument: $1" >&2
            echo "       valid: --install-service --rebuild-venv --no-venv" >&2
            exit 2
            ;;
    esac
    shift
done

echo "==> Pulling latest code..."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git pull --ff-only origin "$BRANCH"

echo "==> Checking system packages..."
MISSING=()
command -v traceroute >/dev/null || MISSING+=(traceroute)
if [[ $USE_VENV -eq 1 ]]; then
    # python3 -m venv needs the venv module, which Ubuntu splits out.
    python3 -c "import venv, ensurepip" 2>/dev/null || MISSING+=(python3-venv)
else
    python3 -c "import matplotlib" 2>/dev/null || MISSING+=(python3-matplotlib)
fi
if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "    installing: ${MISSING[*]}"
    $SUDO apt-get update -qq
    $SUDO apt-get install -y "${MISSING[@]}"
else
    echo "    all present"
fi

if [[ $USE_VENV -eq 1 ]]; then
    if [[ $REBUILD_VENV -eq 1 && -d "$VENV_DIR" ]]; then
        echo "==> Removing existing virtualenv (--rebuild-venv)..."
        rm -rf "$VENV_DIR"
    fi
    if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
        echo "==> Creating virtualenv in $VENV_DIR..."
        # No --system-site-packages: inheriting the system's packages is
        # exactly what causes the ABI mismatch this venv exists to avoid.
        python3 -m venv "$VENV_DIR"
    fi
    PYTHON="$VENV_DIR/bin/python3"
    echo "==> Installing Python dependencies into the virtualenv..."
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet --upgrade -r requirements.txt
else
    PYTHON="$(command -v python3)"
    echo "==> Using system python3 ($PYTHON) — virtualenv disabled."
fi

# Verify the chart stack actually imports and can draw. A bare
# "is matplotlib installed" check would pass on the broken ABI pairing
# that motivated the virtualenv, so exercise the real import chain.
echo "==> Verifying chart dependencies..."
if ! "$PYTHON" - <<'PY'
import sys
try:
    import numpy
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception as exc:
    print(f"    FAILED: {exc!r}", file=sys.stderr)
    sys.exit(1)
fig = plt.figure()
plt.close(fig)
print(f"    matplotlib {matplotlib.__version__}, numpy {numpy.__version__}")
PY
then
    echo "ERROR: matplotlib/numpy cannot be imported by $PYTHON." >&2
    echo "       This is usually a numpy ABI mismatch. Rebuild the" >&2
    echo "       virtualenv from scratch:  ./deploy.sh --rebuild-venv" >&2
    exit 1
fi

if [[ ! -f config.toml ]]; then
    echo "ERROR: no config.toml in $REPO_DIR." >&2
    echo "       cp config.example.toml config.toml  # then edit it" >&2
    exit 2
fi

echo "==> Validating config..."
"$PYTHON" netmon.py --config config.toml --check

echo "==> Running tests..."
"$PYTHON" -m unittest discover -s tests

service_installed() {
    systemctl cat "$1.service" >/dev/null 2>&1
}

# The unit this checkout should be running: real paths, the interpreter
# selected above, and the invoking user as the service account.
render_unit() {
    local svc="$1"
    local run_user run_group
    run_user="${SUDO_USER:-$(id -un)}"
    run_group="$(id -gn "$run_user")"
    sed -e "s|/opt/netmon|$REPO_DIR|g" \
        -e "s|/usr/bin/python3|$PYTHON|g" \
        -e "s|^User=.*|User=$run_user|" \
        -e "s|^Group=.*|Group=$run_group|" \
        "systemd/$svc.service"
}

install_unit() {
    local svc="$1"
    render_unit "$svc" | $SUDO tee "/etc/systemd/system/$svc.service" >/dev/null
}

if [[ $INSTALL_SERVICE -eq 1 ]]; then
    echo "==> Installing systemd units for $REPO_DIR..."
    if ! command -v systemctl >/dev/null; then
        echo "ERROR: systemctl not found; is this a systemd system?" >&2
        exit 2
    fi
    for svc in "${SERVICES[@]}"; do
        install_unit "$svc"
        echo "    installed $svc.service"
    done
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable "${SERVICES[@]}"
else
    # An already-installed unit can be stale — most importantly it may
    # still point at the system python after switching to the virtualenv,
    # which would leave the services running the very interpreter whose
    # broken dependencies prompted the switch. Refresh those in place.
    UNITS_CHANGED=0
    for svc in "${SERVICES[@]}"; do
        service_installed "$svc" || continue
        if ! render_unit "$svc" \
            | diff -q - "/etc/systemd/system/$svc.service" >/dev/null 2>&1
        then
            echo "==> $svc.service is out of date (interpreter or paths"
            echo "    changed); updating it to run $PYTHON..."
            install_unit "$svc"
            UNITS_CHANGED=1
        fi
    done
    if [[ $UNITS_CHANGED -eq 1 ]]; then
        $SUDO systemctl daemon-reload
    fi
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
    echo "      $PYTHON netmon.py --config config.toml"
    echo "      $PYTHON netmon_web.py --config config.toml"
else
    WEB_IP=$("$PYTHON" - <<'PY'
from netmon_config import load_config
c = load_config("config.toml", prepare_dirs=False)
host = f"[{c.web_bind_ip}]" if ":" in c.web_bind_ip else c.web_bind_ip
print(f"http://{host}:{c.web_port}/")
PY
)
    echo "==> Web UI: $WEB_IP"
fi

echo "==> Deploy complete."
