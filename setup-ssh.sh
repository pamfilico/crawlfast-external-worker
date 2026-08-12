#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# crawlfast external worker — SSH bootstrap (run ON the target box, once).
#
# Onboarding a node (the CLI `advertcafe nodes onboard`, or the setup-node.sh
# path) needs the box to already accept inbound SSH. A fresh Linux desktop/laptop
# usually has the *client* but not the *server*, so `ssh <box>` refuses the
# connection. Run this here first to install + enable sshd, then you can drive
# everything else remotely.
#
#   curl -fsSL https://raw.githubusercontent.com/pamfilico/crawlfast-external-worker/main/setup-ssh.sh | bash
#   # or, from a clone:
#   ./setup-ssh.sh
#
# It is idempotent — safe to re-run. Enables the service on boot and prints the
# address(es) to SSH to. Nothing here is crawlfast-specific; it just gets sshd up.
#
# Flags:
#   --user NAME   also print the exact `advertcafe nodes onboard` line for this login
#   -h|--help     this help
#
# The manual equivalents it runs (for reference / to paste yourself):
#   Debian/Ubuntu:  sudo apt-get update && sudo apt-get install -y openssh-server
#                   sudo systemctl enable --now ssh
#   Fedora/RHEL:    sudo dnf install -y openssh-server
#                   sudo systemctl enable --now sshd
#   Arch:           sudo pacman -S --noconfirm openssh
#                   sudo systemctl enable --now sshd
#   macOS:          sudo systemsetup -setremotelogin on   (System Settings → General → Sharing → Remote Login)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SSH_USER="$(id -un)"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) SSH_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,45p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

say(){ printf '\033[36m[setup-ssh]\033[0m %s\n' "$*"; }

# sudo wrapper — interactive by default; set CRAWLFAST_SUDO_PASS to run unattended.
_sudo(){
  if [ "$(id -u)" = "0" ]; then "$@"
  elif [ -n "${CRAWLFAST_SUDO_PASS:-}" ]; then printf '%s\n' "$CRAWLFAST_SUDO_PASS" | sudo -S -p '' "$@"
  else sudo "$@"; fi
}

install_and_enable(){
  # $1 = package, $2 = systemd unit name (ssh vs sshd differs by distro)
  local pkg="$1" unit="$2"
  if command -v apt-get >/dev/null 2>&1; then
    _sudo apt-get update -qq && _sudo apt-get install -y -qq "$pkg"
  elif command -v dnf >/dev/null 2>&1; then
    _sudo dnf install -y -q "$pkg"
  elif command -v yum >/dev/null 2>&1; then
    _sudo yum install -y -q "$pkg"
  elif command -v pacman >/dev/null 2>&1; then
    _sudo pacman -S --noconfirm --needed "$pkg"
  elif command -v zypper >/dev/null 2>&1; then
    _sudo zypper install -y "$pkg"
  else
    echo "WARN: no known package manager — install '$pkg' manually" >&2
  fi
  _sudo systemctl enable --now "$unit"
}

case "$(uname -s)" in
  Linux)
    # Debian/Ubuntu ship the unit as `ssh`; RHEL/Fedora/Arch/SUSE as `sshd`.
    if command -v apt-get >/dev/null 2>&1; then
      say "installing openssh-server + enabling ssh"
      install_and_enable openssh-server ssh
    else
      say "installing openssh-server + enabling sshd"
      install_and_enable openssh-server sshd || install_and_enable openssh sshd
    fi

    # If ufw is active, make sure SSH isn't firewalled off.
    if command -v ufw >/dev/null 2>&1 && _sudo ufw status 2>/dev/null | grep -qi '^Status: active'; then
      say "ufw active — allowing OpenSSH"
      _sudo ufw allow OpenSSH >/dev/null 2>&1 || _sudo ufw allow 22/tcp >/dev/null 2>&1 || true
    fi

    _sudo systemctl is-active --quiet ssh 2>/dev/null || _sudo systemctl is-active --quiet sshd 2>/dev/null \
      || { echo "ERROR: sshd is not active after setup" >&2; exit 1; }
    say "sshd is up and enabled on boot"
    ;;
  Darwin)
    say "enabling macOS Remote Login (sshd)"
    _sudo systemsetup -setremotelogin on 2>/dev/null \
      || say "WARN: could not toggle Remote Login — enable it in System Settings → General → Sharing → Remote Login"
    ;;
  *)
    echo "Unsupported OS: $(uname -s). Enable SSH manually." >&2
    exit 1
    ;;
esac

# Report the address(es) to SSH to.
HOST="$(hostname 2>/dev/null || echo this-box)"
IPS="$( { command -v hostname >/dev/null 2>&1 && hostname -I 2>/dev/null; } \
        || { command -v ip >/dev/null 2>&1 && ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1; } \
        || true )"
say "reachable as: ${SSH_USER}@${HOST}.local  (or one of: ${IPS:-check your LAN IP})"

echo
say "Next, from your machine (needs your worker key / admin secret configured in ~/.advertcaferc):"
echo "    advertcafe nodes onboard --host ${HOST}.local --name crawlfast-node2   # via .local (survives DHCP)"
echo "    # or by IP:  advertcafe nodes onboard --host <one-of-the-IPs-above> --name crawlfast-node2"
