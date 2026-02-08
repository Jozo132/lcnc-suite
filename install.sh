#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# lcnc-suite installer
# Clones the repository and installs all dependencies.
#
# Usage:
#   ./install.sh [target-dir]   # default: ~/lcnc-suite
#
# Prerequisites (checked automatically):
#   - git, git-lfs
#   - python3 (>= 3.9), python3-venv
#   - node (>= 18), npm
#   - LinuxCNC Python bindings (system package)
# ============================================================

REPO_URL="https://github.com/YOUR_USERNAME/lcnc-suite.git"   # <-- set your repo URL
TARGET_DIR="${1:-$HOME/lcnc-suite}"

# -- Colors --
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }
info() { echo -e "  ${YELLOW}→${NC} $*"; }
step() { echo -e "\n${BOLD}[$1/$TOTAL_STEPS] $2${NC}"; }

TOTAL_STEPS=5
ERRORS=0

# ============================================================
# Step 1: Check prerequisites
# ============================================================
step 1 "Checking prerequisites"

# git
if command -v git >/dev/null 2>&1; then
  ok "git $(git --version | awk '{print $3}')"
else
  fail "git not found — install with: sudo apt install git"
  ERRORS=$((ERRORS + 1))
fi

# git-lfs
if git lfs version >/dev/null 2>&1; then
  ok "git-lfs $(git lfs version | awk '{print $1}' | cut -d/ -f2)"
else
  fail "git-lfs not found — install with: sudo apt install git-lfs && git lfs install"
  ERRORS=$((ERRORS + 1))
fi

# python3 >= 3.9
if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
  PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"
  if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 9 ]]; then
    ok "python3 $PY_VER"
  else
    fail "python3 $PY_VER found, but >= 3.9 required"
    ERRORS=$((ERRORS + 1))
  fi
else
  fail "python3 not found — install with: sudo apt install python3"
  ERRORS=$((ERRORS + 1))
fi

# python3-venv
if python3 -m venv --help >/dev/null 2>&1; then
  ok "python3-venv"
else
  fail "python3-venv not found — install with: sudo apt install python3-venv"
  ERRORS=$((ERRORS + 1))
fi

# node >= 18
if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node -v | tr -d 'v')"
  NODE_MAJOR="$(echo "$NODE_VER" | cut -d. -f1)"
  if [[ "$NODE_MAJOR" -ge 18 ]]; then
    ok "node v$NODE_VER"
  else
    fail "node v$NODE_VER found, but >= 18 required"
    ERRORS=$((ERRORS + 1))
  fi
else
  fail "node not found — install Node.js 18+ from https://nodejs.org"
  ERRORS=$((ERRORS + 1))
fi

# npm
if command -v npm >/dev/null 2>&1; then
  ok "npm $(npm -v)"
else
  fail "npm not found — comes with Node.js"
  ERRORS=$((ERRORS + 1))
fi

# linuxcnc python bindings
if python3 -c "import linuxcnc" 2>/dev/null; then
  ok "linuxcnc python bindings"
else
  fail "linuxcnc python bindings not found — install LinuxCNC 2.8+ first"
  ERRORS=$((ERRORS + 1))
fi

if [[ "$ERRORS" -gt 0 ]]; then
  echo -e "\n${RED}${BOLD}$ERRORS prerequisite(s) missing. Fix the above and re-run.${NC}"
  exit 1
fi

echo -e "\n  All prerequisites satisfied."

# ============================================================
# Step 2: Clone repository
# ============================================================
step 2 "Cloning repository"

if [[ -d "$TARGET_DIR/.git" ]]; then
  info "Repository already exists at $TARGET_DIR — skipping clone"
  cd "$TARGET_DIR"
  info "Pulling latest changes..."
  git pull --ff-only || info "Pull skipped (may have local changes)"
else
  if [[ "$REPO_URL" == *"YOUR_USERNAME"* ]]; then
    fail "REPO_URL not configured in install.sh — edit the REPO_URL variable at the top of the script"
    exit 1
  fi
  info "Cloning $REPO_URL → $TARGET_DIR"
  git clone "$REPO_URL" "$TARGET_DIR"
  cd "$TARGET_DIR"
fi

info "Fetching Git LFS objects..."
git lfs pull
ok "Repository ready at $TARGET_DIR"

# ============================================================
# Step 3: Setup Python virtual environment
# ============================================================
step 3 "Setting up Python virtual environment"

VENV_DIR="$TARGET_DIR/lcnc-gateway/.venv"
REQ_FILE="$TARGET_DIR/lcnc-gateway/requirements.txt"

if [[ ! -f "$REQ_FILE" ]]; then
  fail "requirements.txt not found at $REQ_FILE"
  exit 1
fi

info "Creating venv with system-site-packages (for linuxcnc bindings)..."
rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR" --system-site-packages

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

info "Upgrading pip..."
python3 -m pip install -U pip --quiet

info "Installing Python dependencies..."
python3 -m pip install -r "$REQ_FILE" --quiet

info "Verifying Python imports..."
python3 -c "import linuxcnc, fastapi, uvicorn; print()" 2>/dev/null
ok "Python environment ready"

deactivate

# ============================================================
# Step 4: Install Node.js dependencies
# ============================================================
step 4 "Installing Node.js dependencies"

cd "$TARGET_DIR/lcnc-webui"
info "Running npm install..."
npm install --loglevel=warn
ok "Node.js dependencies installed"

cd "$TARGET_DIR"

# ============================================================
# Step 5: Done
# ============================================================
step 5 "Installation complete"

echo -e "
  ${GREEN}${BOLD}lcnc-suite installed successfully!${NC}

  ${BOLD}Location:${NC}  $TARGET_DIR

  ${BOLD}To start:${NC}
    cd $TARGET_DIR
    ./restart.sh local    # localhost only
    ./restart.sh lan      # accessible from LAN

  ${BOLD}Ports:${NC}
    Gateway:  http://localhost:8000   (WebSocket: ws://localhost:8000/ws)
    Web UI:   http://localhost:5173

  ${BOLD}Logs:${NC}     $TARGET_DIR/runlogs/
"
