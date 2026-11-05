#!/bin/sh
# =============================================================================
# Alpine Recording Station - Idempotent Installer & Updater Script
# =============================================================================
# Supports x86_64, x86, aarch64, and armv7 Alpine Linux systems.
# Safe to run multiple times (acts as both initial installer and updater)
# without overwriting existing user configurations or Samba user databases.
# =============================================================================

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP_DEST="/opt/alpine-recorder"
ETC_CONFIG_DIR="/etc/alpine-recorder"
STORAGE_DIR="/storage"
FORCE_CONFIG=0

for arg in "$@"; do
    case "$arg" in
        --force-config)
            FORCE_CONFIG=1
            ;;
        --help|-h)
            echo "Usage: $0 [--force-config]"
            echo "  --force-config   Overwrite existing smb.conf, xinitrc, and openbox configs."
            exit 0
            ;;
    esac
done

log_info() {
    printf "\033[1;34m[INFO]\033[0m %s\n" "$1"
}

log_ok() {
    printf "\033[1;32m[OK]\033[0m   %s\n" "$1"
}

log_warn() {
    printf "\033[1;33m[WARN]\033[0m %s\n" "$1"
}

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: install.sh must be run as root." >&2
    exit 1
fi

ARCH="$(uname -m)"
log_info "Detected architecture: ${ARCH}"

# -----------------------------------------------------------------------------
# 1. Enable Alpine Community Repository (if commented out)
# -----------------------------------------------------------------------------
if [ -f /etc/apk/repositories ]; then
    if ! grep -Eq '^[^#].*/community' /etc/apk/repositories; then
        log_info "Enabling Alpine community repository in /etc/apk/repositories..."
        sed -i -E 's|^#[[:space:]]*(https?://.*/v[0-9]+\.[0-9]+/community)|\1|g' /etc/apk/repositories
        sed -i -E 's|^#[[:space:]]*(https?://.*/edge/community)|\1|g' /etc/apk/repositories
    fi
fi

# -----------------------------------------------------------------------------
# 2. Install Required Alpine Packages
# -----------------------------------------------------------------------------
log_info "Updating package indexes and installing dependencies..."

BASE_PKGS="
    python3
    py3-qt6
    py3-numpy
    alsa-lib
    alsa-utils
    alsa-ucm-conf
    samba
    samba-common-tools
    xorg-server
    xinit
    xset
    openbox
    xf86-input-libinput
    xf86-input-evdev
    xf86-video-fbdev
    mesa-dri-gallium
    ttf-dejavu
    eudev
    util-linux
    iproute2
"

ARCH_PKGS=""
case "$ARCH" in
    x86_64|i*86)
        ARCH_PKGS="xf86-video-vesa"
        ;;
    aarch64|armv7*|arm*)
        ARCH_PKGS=""
        ;;
esac

if command -v apk >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    apk update
    # shellcheck disable=SC2086
    apk add --no-cache $BASE_PKGS $ARCH_PKGS
    log_ok "All Alpine packages installed."
else
    log_warn "apk package manager not found (non-Alpine environment). Skipping package installation."
fi

# -----------------------------------------------------------------------------
# 3. Configure USB Audio Hardware & ALSA Group Permissions
# -----------------------------------------------------------------------------
addgroup -S audio 2>/dev/null || true
addgroup root audio 2>/dev/null || true
addgroup -S sambashare 2>/dev/null || true

# Ensure kernel module snd-usb-audio loads at boot
mkdir -p /etc/modules-load.d
if [ ! -f /etc/modules-load.d/usb-audio.conf ]; then
    echo "snd-usb-audio" > /etc/modules-load.d/usb-audio.conf
fi
modprobe snd-usb-audio 2>/dev/null || true

# -----------------------------------------------------------------------------
# 4. Prepare Persistent /storage Directory & Boot Permission Enforcement
# -----------------------------------------------------------------------------
log_info "Configuring persistent storage directory at ${STORAGE_DIR}..."
mkdir -p "${STORAGE_DIR}"
mkdir -p "${STORAGE_DIR}/.alpine-recorder"
chown root:sambashare "${STORAGE_DIR}" 2>/dev/null || true
chmod 0777 "${STORAGE_DIR}"

mkdir -p "${ETC_CONFIG_DIR}"

# Create OpenRC local.d script so /storage permissions are masked on every boot
# (even when an external partition is mounted at /storage via /etc/fstab)
mkdir -p /etc/local.d
LOCAL_STORAGE_SCRIPT="/etc/local.d/alpine-recorder-storage.start"
if [ ! -f "${LOCAL_STORAGE_SCRIPT}" ] || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cat << 'EOF' > "${LOCAL_STORAGE_SCRIPT}"
#!/bin/sh
mkdir -p /storage /storage/.alpine-recorder
chgrp sambashare /storage 2>/dev/null || true
chmod 0777 /storage 2>/dev/null || true
find /storage -maxdepth 1 -type f -name "*.wav" -exec chmod 0666 {} + 2>/dev/null || true
EOF
    chmod +x "${LOCAL_STORAGE_SCRIPT}"
fi

# -----------------------------------------------------------------------------
# 5. Deploy Application Files to /opt/alpine-recorder (Safe on Re-runs/Updates)
# -----------------------------------------------------------------------------
log_info "Deploying application code to ${APP_DEST}..."
mkdir -p "${APP_DEST}"
rm -rf "${APP_DEST}/app"
cp -r "${SCRIPT_DIR}/app" "${APP_DEST}/app"
chmod -R a+rX "${APP_DEST}"
log_ok "Application code updated in ${APP_DEST}."

# -----------------------------------------------------------------------------
# 6. Configure Samba Share (Preserving Existing Configuration on Update)
# -----------------------------------------------------------------------------
mkdir -p /etc/samba /var/log/samba /var/lib/samba/private

if [ ! -f /etc/samba/smb.conf ] || ! grep -q "\[storage\]" /etc/samba/smb.conf || [ "${FORCE_CONFIG}" -eq 1 ]; then
    if [ -f /etc/samba/smb.conf ] && [ ! -f /etc/samba/smb.conf.orig ]; then
        cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
    fi
    cp "${SCRIPT_DIR}/config/smb.conf.template" /etc/samba/smb.conf
    log_ok "Installed /etc/samba/smb.conf for /storage share."
else
    log_info "Preserving existing /etc/samba/smb.conf (already contains [storage] share)."
fi

# -----------------------------------------------------------------------------
# 7. Configure Openbox & Fullscreen Kiosk Autostart on tty1
# -----------------------------------------------------------------------------
mkdir -p /root/.config/openbox
if [ ! -f /root/.config/openbox/rc.xml ] || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cp "${SCRIPT_DIR}/config/openbox-rc.xml" /root/.config/openbox/rc.xml
    log_ok "Installed /root/.config/openbox/rc.xml."
else
    log_info "Preserving existing /root/.config/openbox/rc.xml."
fi

if [ ! -f /root/.xinitrc ] || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cp "${SCRIPT_DIR}/config/xinitrc" /root/.xinitrc
    chmod +x /root/.xinitrc
    log_ok "Installed /root/.xinitrc."
else
    log_info "Preserving existing /root/.xinitrc."
fi

# Configure autologin on tty1 in /etc/inittab (idempotent)
if [ -f /etc/inittab ]; then
    if ! grep -q "autologin" /etc/inittab; then
        log_info "Configuring tty1 autologin in /etc/inittab..."
        cp /etc/inittab /etc/inittab.bak
        sed -i 's|^tty1::respawn:/sbin/getty 38400 tty1|tty1::respawn:/sbin/getty -n -l /bin/sh 38400 tty1 -a root|g' /etc/inittab
        # If standard getty doesn't support -a root, create an autologin helper script
        cat << 'EOF' > /usr/local/bin/alpine-recorder-autologin
#!/bin/sh
exec /bin/login -f root
EOF
        chmod +x /usr/local/bin/alpine-recorder-autologin
        sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/getty -n -l /usr/local/bin/alpine-recorder-autologin 38400 tty1|g' /etc/inittab
        log_ok "Configured tty1 autologin."
    else
        log_info "tty1 autologin already present in /etc/inittab."
    fi
fi

# Configure /root/.profile to launch X11 Kiosk on tty1 (idempotent)
touch /root/.profile
if ! grep -q "ALPINE_RECORDER_KIOSK_AUTOSTART" /root/.profile; then
    cat << 'EOF' >> /root/.profile

# ALPINE_RECORDER_KIOSK_AUTOSTART
if [ -z "${DISPLAY:-}" ] && [ "$(tty 2>/dev/null)" = "/dev/tty1" ]; then
    exec startx /root/.xinitrc -- -nocursor >/var/log/alpine-recorder-xorg.log 2>&1
fi
EOF
    log_ok "Added fullscreen X11 kiosk autostart to /root/.profile."
else
    log_info "Fullscreen X11 kiosk autostart already present in /root/.profile."
fi

# -----------------------------------------------------------------------------
# 8. Enable OpenRC System Services
# -----------------------------------------------------------------------------
if command -v rc-update >/dev/null 2>&1; then
    log_info "Enabling system services (udev, alsa, samba, local)..."
    if command -v setup-devd >/dev/null 2>&1; then
        setup-devd udev 2>/dev/null || true
    fi
    rc-update add alsa default 2>/dev/null || true
    rc-update add samba default 2>/dev/null || true
    rc-update add local default 2>/dev/null || true

    rc-service samba restart 2>/dev/null || rc-service samba start 2>/dev/null || true
    rc-service alsa start 2>/dev/null || true
    log_ok "OpenRC services enabled."
fi

# -----------------------------------------------------------------------------
# 9. Register Paths in Alpine LBU (Local Backup Utility) & Commit
# -----------------------------------------------------------------------------
if command -v lbu >/dev/null 2>&1; then
    log_info "Registering application and configuration files with Alpine LBU..."
    lbu include "${APP_DEST}" 2>/dev/null || true
    lbu include "${ETC_CONFIG_DIR}" 2>/dev/null || true
    lbu include /etc/samba 2>/dev/null || true
    lbu include /var/lib/samba/private 2>/dev/null || true
    lbu include /root/.xinitrc 2>/dev/null || true
    lbu include /root/.profile 2>/dev/null || true
    lbu include /root/.config/openbox 2>/dev/null || true
    lbu include /usr/local/bin/alpine-recorder-autologin 2>/dev/null || true
    lbu include "${LOCAL_STORAGE_SCRIPT}" 2>/dev/null || true
    lbu include /etc/inittab 2>/dev/null || true

    # Exclude /storage so recorded WAV files are never packed into RAM overlay
    lbu exclude "${STORAGE_DIR}" 2>/dev/null || true

    if grep -Eq '^[^#]*LBU_MEDIA=' /etc/lbu/lbu.conf 2>/dev/null || [ -n "${LBU_MEDIA:-}" ]; then
        log_info "Committing changes to LBU persistent overlay..."
        if lbu commit -d; then
            log_ok "LBU commit completed successfully."
        else
            log_warn "lbu commit returned non-zero status; verify LBU_MEDIA in /etc/lbu/lbu.conf."
        fi
    else
        log_warn "LBU_MEDIA is not configured in /etc/lbu/lbu.conf yet. Run 'lbu commit -d' once LBU media is configured."
    fi
else
    log_info "lbu utility not found on this machine; skipping LBU commit."
fi

log_ok "Alpine Recording Station installation/update complete!"
