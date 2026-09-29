#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

say() {
  printf '\033[1;36m==>\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2
}

die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  die "This setup script is intended for macOS."
fi

# ------------------------------------------------------------------
# Homebrew
# ------------------------------------------------------------------

if ! command -v brew >/dev/null 2>&1; then
  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

if ! command -v brew >/dev/null 2>&1; then
  say "Homebrew is not installed."
  printf "Install Homebrew now? [Y/n] "
  read -r answer
  answer="${answer:-Y}"

  if [[ "$answer" =~ ^[Yy]$ ]]; then
    say "Installing Homebrew..."
    NONINTERACTIVE=1 /bin/bash -c \
      "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    if [[ -x /opt/homebrew/bin/brew ]]; then
      eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -x /usr/local/bin/brew ]]; then
      eval "$(/usr/local/bin/brew shellenv)"
    fi
  else
    die "Homebrew is required to install Dundee GDU automatically."
  fi
fi

command -v brew >/dev/null 2>&1 || die "Homebrew installation was not found."

say "Using Homebrew: $(command -v brew)"

# ------------------------------------------------------------------
# Dundee GDU
#
# IMPORTANT:
# /opt/homebrew/bin/gdu may be GNU coreutils du.
# Homebrew's Dundee GDU is intentionally named gdu-go.
# ------------------------------------------------------------------

is_dundee_gdu() {
  local candidate="$1"

  [[ -x "$candidate" ]] || return 1

  # Feature-detection is much more reliable than guessing the exact
  # --version output. GNU du does not have these Dundee GDU options.
  local help_text=""
  help_text="$("$candidate" --help 2>&1 || true)"

  echo "$help_text" | grep -q -- "--non-interactive" || return 1
  echo "$help_text" | grep -q -- "--no-prefix" || return 1

  return 0
}

find_dundee_gdu() {
  local candidate=""
  local prefix=""

  # 1. Already linked into PATH.
  if command -v gdu-go >/dev/null 2>&1; then
    candidate="$(command -v gdu-go)"
    if is_dundee_gdu "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  # 2. Ask Homebrew for the exact installed files.
  if brew list --formula gdu >/dev/null 2>&1; then
    candidate="$(
      brew list gdu 2>/dev/null \
        | awk '/\/bin\/gdu-go$/ {print; exit}'
    )"

    if [[ -n "$candidate" ]] && is_dundee_gdu "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  # 3. Formula prefix.
  prefix="$(brew --prefix gdu 2>/dev/null || true)"
  if [[ -n "$prefix" ]]; then
    candidate="$prefix/bin/gdu-go"

    if is_dundee_gdu "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  # 4. Standard macOS Homebrew locations.
  for candidate in \
    /opt/homebrew/bin/gdu-go \
    /usr/local/bin/gdu-go \
    /opt/homebrew/opt/gdu/bin/gdu-go \
    /usr/local/opt/gdu/bin/gdu-go
  do
    if is_dundee_gdu "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  # 5. Last-resort search inside the installed Cellar formula.
  local cellar=""
  cellar="$(brew --cellar gdu 2>/dev/null || true)"

  if [[ -n "$cellar" && -d "$cellar" ]]; then
    candidate="$(
      find "$cellar" \
        -type f \
        -path '*/bin/gdu-go' \
        -perm -111 \
        -print \
        -quit 2>/dev/null || true
    )"

    if [[ -n "$candidate" ]] && is_dundee_gdu "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  return 1
}

GDU_BIN="$(find_dundee_gdu || true)"

if [[ -z "$GDU_BIN" ]]; then
  if brew list --formula gdu >/dev/null 2>&1; then
    say "Homebrew formula gdu exists but gdu-go was not usable."
    say "Reinstalling Dundee GDU..."
    HOMEBREW_NO_AUTO_UPDATE=1 brew reinstall gdu
  else
    say "Installing Dundee GDU..."
    HOMEBREW_NO_AUTO_UPDATE=1 brew install gdu
  fi

  GDU_BIN="$(find_dundee_gdu || true)"
fi

if [[ -z "$GDU_BIN" ]]; then
  warn "Homebrew says formula gdu is installed, but setup could not locate a usable gdu-go."
  warn "Diagnostic output follows:"
  brew --prefix gdu 2>/dev/null || true
  brew list gdu 2>/dev/null || true
  die "Could not locate Dundee GDU."
fi

say "Using Dundee GDU: $GDU_BIN"

# Persist the exact resolved binary path so the Python app never accidentally
# selects GNU coreutils gdu from PATH.
printf '%s\n' "$GDU_BIN" > "$PROJECT_DIR/.gdu-bin"

"$GDU_BIN" --version 2>&1 || true

# Purely informational: GNU coreutils gdu may coexist and that's fine.
if command -v gdu >/dev/null 2>&1; then
  if gdu --version 2>&1 | grep -qi "GNU coreutils"; then
    warn "'gdu' is GNU coreutils du: $(command -v gdu)"
    warn "The cleaner will ignore it and use: $GDU_BIN"
  fi
fi

# ------------------------------------------------------------------
# Python
# ------------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  say "Python 3 not found; installing with Homebrew..."
  HOMEBREW_NO_AUTO_UPDATE=1 brew install python
fi

PYTHON_BIN="$(command -v python3)"
say "Using Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

[[ -f requirements.txt ]] || die "requirements.txt not found in $PROJECT_DIR"

if [[ ! -d .venv ]]; then
  say "Creating Python virtual environment..."
  "$PYTHON_BIN" -m venv .venv
fi

say "Installing Python dependencies..."
source .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

chmod +x main.py

# ------------------------------------------------------------------
# Smoke check
# ------------------------------------------------------------------

say "Running smoke checks..."

GDU_BIN="$GDU_BIN" python - <<'PY'
import os
import subprocess
import textual

gdu = os.environ["GDU_BIN"]

help_text = subprocess.check_output(
    [gdu, "--help"],
    text=True,
    stderr=subprocess.STDOUT,
)

required = ("--non-interactive", "--no-prefix")
missing = [flag for flag in required if flag not in help_text]

if missing:
    raise SystemExit(
        f"Wrong GDU binary selected: {gdu}; missing options: {missing}"
    )

print("Textual:", textual.__version__)
print("Dundee GDU:", gdu)
print("Dundee GDU feature check: OK")
print("Python environment: OK")
PY

say "Testing GDU top-level parser against the real installed binary..."

GDU_BIN="$GDU_BIN" python - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

os.environ["GDU_BIN"] = os.environ["GDU_BIN"]

from mac_disk_scanner.utils import gdu_top_level_sizes

root = Path(
    tempfile.mkdtemp(
        prefix="mac-disk-scanner-gdu-smoke-",
    )
)

try:
    alpha = root / "alpha"
    beta = root / "beta with space"

    alpha.mkdir()
    beta.mkdir()

    (alpha / "a.bin").write_bytes(
        b"x" * (256 * 1024)
    )
    (beta / "b.bin").write_bytes(
        b"y" * (384 * 1024)
    )

    sizes = gdu_top_level_sizes(root)

    alpha_key = str(alpha.resolve())
    beta_key = str(beta.resolve())

    if alpha_key not in sizes:
        raise SystemExit(
            "GDU parser smoke test failed: alpha missing"
        )

    if beta_key not in sizes:
        raise SystemExit(
            "GDU parser smoke test failed: "
            "directory with spaces missing"
        )

    if sizes[alpha_key] <= 0 or sizes[beta_key] <= 0:
        raise SystemExit(
            "GDU parser smoke test failed: invalid size"
        )

    print(
        "GDU parser smoke test: OK "
        f"(alpha={sizes[alpha_key]}, "
        f"beta={sizes[beta_key]})"
    )
finally:
    shutil.rmtree(
        root,
        ignore_errors=True,
    )
PY

say "Setup complete."

printf '\nRun:\n\n'
printf '  cd %q\n' "$PROJECT_DIR"
printf '  source .venv/bin/activate\n'
printf '  ./main.py\n\n'
