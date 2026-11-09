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
APK_CACHE_DIR="${STORAGE_DIR}/.alpine-recorder/apk-cache"
FORCE_CONFIG=0
OSK_MODE=""
REMOTE_PRODUCER_MODE=""
ENABLE_SERVICE=1

for arg in "$@"; do
    case "$arg" in
        --force-config)
            FORCE_CONFIG=1
            ;;
        --enable-osk)
            OSK_MODE="true"
            ;;
        --disable-osk)
            OSK_MODE="false"
            ;;
        --enable-remote-producer)
            REMOTE_PRODUCER_MODE="true"
            ;;
        --disable-remote-producer)
            REMOTE_PRODUCER_MODE="false"
            ;;
        --enable-service)
            ENABLE_SERVICE=1
            ;;
        --disable-service|--no-autostart)
            ENABLE_SERVICE=0
            ;;
        --help|-h)
            echo "Usage: $0 [--force-config] [--enable-osk | --disable-osk] [--enable-remote-producer | --disable-remote-producer] [--enable-service | --disable-service]"
            echo "  --force-config              Overwrite existing smb.conf, xinitrc, and openbox configs."
            echo "  --enable-osk                Enable the touch On-Screen Keyboard in saved settings."
            echo "  --disable-osk               Disable the touch On-Screen Keyboard in saved settings."
            echo "  --enable-remote-producer    Enable the Remote Producer (Guided VO) HTTPS server by default."
            echo "  --disable-remote-producer   Disable the Remote Producer (Guided VO) HTTPS server by default."
            echo "  --enable-service            Enable post-boot OpenRC kiosk service on VT7 (default)."
            echo "  --disable-service           Do not auto-start kiosk at boot; start manually via 'alpine-recorder'."
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
    xauth
    xset
    openbox
    xf86-input-libinput
    xf86-input-evdev
    xf86-video-fbdev
    mesa-dri-gallium
    ttf-dejavu
    fontconfig
    libxkbcommon-x11
    xcb-util-wm
    xcb-util-image
    xcb-util-keysyms
    xcb-util-renderutil
    xcb-util-cursor
    dbus
    kbd
    eudev
    util-linux
    iproute2
    openssl
    ca-certificates
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
    # Ensure persistent APK cache exists on /storage so diskless (LBU/RAM) Alpine
    # systems retain Xorg, Openbox, PyQt6, NumPy, ALSA, and Samba across reboots.
    mkdir -p "${STORAGE_DIR}"
    if ! mountpoint -q "${STORAGE_DIR}" 2>/dev/null; then
        mount "${STORAGE_DIR}" 2>/dev/null || mount -a 2>/dev/null || true
    fi
    mkdir -p "${STORAGE_DIR}/.alpine-recorder" "${APK_CACHE_DIR}" /etc/apk
    if [ ! -e /etc/apk/cache ] && [ ! -L /etc/apk/cache ]; then
        ln -snf "${APK_CACHE_DIR}" /etc/apk/cache
        log_info "Linked /etc/apk/cache -> ${APK_CACHE_DIR} for persistent package caching across reboots."
    fi

    # shellcheck disable=SC2086
    apk update
    apk fix 2>/dev/null || true
    # Never use --no-cache here: diskless Alpine relies on cached .apk archives at boot
    # shellcheck disable=SC2086
    apk add $BASE_PKGS $ARCH_PKGS
    # Install qt6-qtbase-x11 and X11 cursor packages if available on this Alpine release
    OPT_PKGS_INSTALLED=""
    for opt_pkg in qt6-qtbase-x11 xsetroot font-cursor-misc libxcursor xcursor-themes adwaita-icon-theme; do
        if apk add "$opt_pkg" 2>/dev/null; then
            OPT_PKGS_INSTALLED="${OPT_PKGS_INSTALLED} ${opt_pkg}"
        fi
    done

    # Sync /etc/apk/cache and mirror required .apk packages into /storage/.alpine-recorder/apk-cache
    apk cache sync 2>/dev/null || apk cache download 2>/dev/null || true
    if [ -d /etc/apk/cache ] && [ "$(cd /etc/apk/cache 2>/dev/null && pwd -P)" != "$(cd "${APK_CACHE_DIR}" 2>/dev/null && pwd -P)" ]; then
        cp -f /etc/apk/cache/*.apk /etc/apk/cache/APKINDEX*.tar.gz "${APK_CACHE_DIR}/" 2>/dev/null || true
    fi
    if ! ls "${APK_CACHE_DIR}"/*.apk >/dev/null 2>&1; then
        log_info "Caching required .apk archives into ${APK_CACHE_DIR} for offline reboot recovery..."
        # shellcheck disable=SC2086
        apk fetch --recursive -o "${APK_CACHE_DIR}" $BASE_PKGS $ARCH_PKGS $OPT_PKGS_INSTALLED 2>/dev/null || true
    fi
    log_ok "All Alpine packages installed and cached for persistence across reboots."
else
    log_warn "apk package manager not found (non-Alpine environment). Skipping package installation."
fi

# -----------------------------------------------------------------------------
# 3. Configure USB Audio Hardware, Touchscreen Input, Video & ALSA Permissions
# -----------------------------------------------------------------------------
addgroup -S audio 2>/dev/null || true
addgroup root audio 2>/dev/null || true
addgroup -S input 2>/dev/null || true
addgroup root input 2>/dev/null || true
addgroup -S video 2>/dev/null || true
addgroup root video 2>/dev/null || true
addgroup -S sambashare 2>/dev/null || true

# Allow Xorg to start from a post-boot OpenRC service without a controlling tty
mkdir -p /etc/X11
cat << 'EOF' > /etc/X11/Xwrapper.config
allowed_users=anybody
needs_root_rights=yes
EOF

# Ensure kernel module snd-usb-audio and USB HID modules load at boot and ALSA mixer state dir exists
mkdir -p /etc/modules-load.d /var/lib/alsa /etc/udev/rules.d
if [ ! -f /etc/modules-load.d/usb-audio.conf ]; then
    echo "snd-usb-audio" > /etc/modules-load.d/usb-audio.conf
fi
if [ ! -f /etc/modules-load.d/usb-hid.conf ]; then
    printf "usbhid\nhid-generic\n" > /etc/modules-load.d/usb-hid.conf
fi
modprobe snd-usb-audio 2>/dev/null || true
modprobe usbhid 2>/dev/null || true
modprobe hid-generic 2>/dev/null || true

# Allow direct /dev/hidraw* and /dev/input/event* LED control on SayoDevice (VID 0x8089) macro pads
cat << 'EOF' > /etc/udev/rules.d/99-sayodevice.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="8089", MODE="0666", GROUP="input"
SUBSYSTEM=="input", ATTRS{idVendor}=="8089", MODE="0666", GROUP="input"
EOF
udevadm control --reload-rules 2>/dev/null || true
udevadm trigger 2>/dev/null || true

# -----------------------------------------------------------------------------
# 4. Prepare Persistent /storage Directory & Boot Permission Enforcement
# -----------------------------------------------------------------------------
log_info "Configuring persistent storage directory at ${STORAGE_DIR}..."
mkdir -p "${STORAGE_DIR}"
if ! mountpoint -q "${STORAGE_DIR}" 2>/dev/null; then
    mount "${STORAGE_DIR}" 2>/dev/null || mount -a 2>/dev/null || true
fi
mkdir -p "${STORAGE_DIR}/.alpine-recorder"
mkdir -p "${STORAGE_DIR}/ssl"
mkdir -p "${APK_CACHE_DIR}"
chown root:sambashare "${STORAGE_DIR}" 2>/dev/null || true
chmod 2777 "${STORAGE_DIR}" 2>/dev/null || chmod 0777 "${STORAGE_DIR}"
find "${STORAGE_DIR}" \( -path "${STORAGE_DIR}/.alpine-recorder" -o -path "${STORAGE_DIR}/lost+found" \) -prune -o -type d -exec chown root:sambashare {} + -exec chmod 2777 {} + 2>/dev/null || true
find "${STORAGE_DIR}" \( -path "${STORAGE_DIR}/.alpine-recorder" -o -path "${STORAGE_DIR}/lost+found" \) -prune -o -type f -exec chown root:sambashare {} + -exec chmod 0666 {} + 2>/dev/null || true

mkdir -p "${ETC_CONFIG_DIR}"
mkdir -p "${ETC_CONFIG_DIR}/ssl"
chmod 0750 "${ETC_CONFIG_DIR}/ssl" 2>/dev/null || true

# Create OpenRC local.d script so /storage is mounted and permissions are masked on every boot
# (even when /storage directory was excluded from the LBU overlay when localmount ran)
mkdir -p /etc/local.d
LOCAL_STORAGE_SCRIPT="/etc/local.d/alpine-recorder-storage.start"
if [ ! -f "${LOCAL_STORAGE_SCRIPT}" ] || ! grep -q "mountpoint -q /storage" "${LOCAL_STORAGE_SCRIPT}" || ! grep -q "/storage/ssl" "${LOCAL_STORAGE_SCRIPT}" || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cat << 'EOF' > "${LOCAL_STORAGE_SCRIPT}"
#!/bin/sh
mkdir -p /storage
if ! mountpoint -q /storage 2>/dev/null; then
    mount /storage 2>/dev/null || mount -a 2>/dev/null || true
fi
mkdir -p /storage/.alpine-recorder /storage/.alpine-recorder/apk-cache /storage/ssl
chown root:sambashare /storage 2>/dev/null || chgrp sambashare /storage 2>/dev/null || true
chmod 2777 /storage 2>/dev/null || chmod 0777 /storage 2>/dev/null || true
find /storage \( -path /storage/.alpine-recorder -o -path /storage/lost+found \) -prune -o -type d -exec chown root:sambashare {} + -exec chmod 2777 {} + 2>/dev/null || true
find /storage \( -path /storage/.alpine-recorder -o -path /storage/lost+found \) -prune -o -type f -exec chown root:sambashare {} + -exec chmod 0666 {} + 2>/dev/null || true
EOF
    chmod +x "${LOCAL_STORAGE_SCRIPT}"
fi

# -----------------------------------------------------------------------------
# 5. Deploy Application Files to /opt/alpine-recorder (Safe on Re-runs/Updates)
# -----------------------------------------------------------------------------
log_info "Deploying application code to ${APP_DEST}..."
mkdir -p "${APP_DEST}" "${APP_DEST}/app" "${APP_DEST}/app/ui"
rm -rf "${APP_DEST}/app"
cp -r "${SCRIPT_DIR}/app" "${APP_DEST}/app"
if [ -d "${SCRIPT_DIR}/tests" ]; then
    rm -rf "${APP_DEST}/tests"
    cp -r "${SCRIPT_DIR}/tests" "${APP_DEST}/tests"
fi
chmod -R a+rX "${APP_DEST}"

if command -v python3 >/dev/null 2>&1; then
    PYTHONPATH="${APP_DEST}" python3 -m py_compile \
        "${APP_DEST}/app/main.py" \
        "${APP_DEST}/app/audio_engine.py" \
        "${APP_DEST}/app/config_manager.py" \
        "${APP_DEST}/app/samba_manager.py" \
        "${APP_DEST}/app/network_info.py" \
        "${APP_DEST}/app/qr_code.py" \
        "${APP_DEST}/app/ssl_manager.py" \
        "${APP_DEST}/app/remote_producer.py" \
        "${APP_DEST}/app/sayodevice_led.py" \
        "${APP_DEST}/app/ui/main_window.py" \
        "${APP_DEST}/app/ui/file_manager_dialog.py" \
        "${APP_DEST}/app/ui/remote_producer_dialog.py" \
        "${APP_DEST}/app/ui/vu_meter.py" \
        "${APP_DEST}/app/ui/storage_monitor.py" \
        "${APP_DEST}/app/ui/settings_dialog.py" \
        "${APP_DEST}/app/ui/on_screen_keyboard.py" \
        "${APP_DEST}/app/ui/styles.py" 2>/dev/null || true

    # Ensure SSL certificates exist in /etc/alpine-recorder/ssl (or import from /storage/ssl if present)
    # so they are included when LBU commits below.
    PYTHONPATH="${APP_DEST}" python3 -c "
from pathlib import Path
from app.ssl_manager import SslCertificateManager
mgr = SslCertificateManager(
    cert_dir=Path('${ETC_CONFIG_DIR}') / 'ssl',
    drop_dir=Path('${STORAGE_DIR}') / 'ssl',
)
imported, _ = mgr.import_from_drop_dir()
if not imported:
    mgr.ensure_certificates()
" 2>/dev/null || true
    log_ok "Verified SSL certificate pair in ${ETC_CONFIG_DIR}/ssl."

    if [ -n "${OSK_MODE}" ]; then
        PYTHONPATH="${APP_DEST}" python3 -c "
from app.config_manager import ConfigManager
cm = ConfigManager(storage_dir='${STORAGE_DIR}', etc_config_dir='${ETC_CONFIG_DIR}')
cm.settings.on_screen_keyboard = ('${OSK_MODE}' == 'true')
cm.save()
" 2>/dev/null || true
        log_ok "On-Screen Keyboard setting updated to: ${OSK_MODE}"
    fi

    if [ -n "${REMOTE_PRODUCER_MODE}" ]; then
        PYTHONPATH="${APP_DEST}" python3 -c "
from app.config_manager import ConfigManager
cm = ConfigManager(storage_dir='${STORAGE_DIR}', etc_config_dir='${ETC_CONFIG_DIR}')
cm.settings.remote_producer_enabled = ('${REMOTE_PRODUCER_MODE}' == 'true')
cm.save()
" 2>/dev/null || true
        log_ok "Remote Producer HTTPS server default updated to: ${REMOTE_PRODUCER_MODE}"
    fi
fi

log_ok "Application code updated in ${APP_DEST}."

# -----------------------------------------------------------------------------
# 6. Configure Samba Share (Preserving Existing Configuration on Update)
# -----------------------------------------------------------------------------
mkdir -p /etc/samba /var/log/samba /var/lib/samba/private

if [ ! -f /etc/samba/smb.conf ] || ! grep -q "\[storage\]" /etc/samba/smb.conf || ! grep -q "force directory mode = 2777" /etc/samba/smb.conf || [ "${FORCE_CONFIG}" -eq 1 ]; then
    if [ -f /etc/samba/smb.conf ] && [ ! -f /etc/samba/smb.conf.orig ]; then
        cp /etc/samba/smb.conf /etc/samba/smb.conf.orig
    fi
    cp "${SCRIPT_DIR}/config/smb.conf.template" /etc/samba/smb.conf
    log_ok "Installed /etc/samba/smb.conf for /storage share."
else
    log_info "Preserving existing /etc/samba/smb.conf (already contains [storage] share)."
fi

# -----------------------------------------------------------------------------
# 7. Clean Up Any Legacy tty1 Login Hijack & Configure Post-Boot VT7 Service
# -----------------------------------------------------------------------------
# Restore tty1 in /etc/inittab if a previous install configured autologin on tty1
if [ -f /etc/inittab ] && grep -Eq 'alpine-recorder-autologin|getty.*-a root' /etc/inittab; then
    log_info "Restoring normal console login on tty1 in /etc/inittab..."
    sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/getty 38400 tty1|g' /etc/inittab
    kill -HUP 1 2>/dev/null || true
    log_ok "Restored normal getty login on tty1."
fi
rm -f /usr/local/bin/alpine-recorder-autologin

# Remove legacy startx block from /root/.profile so root logins never auto-exec startx
if [ -f /root/.profile ] && grep -q "ALPINE_RECORDER_KIOSK_AUTOSTART" /root/.profile; then
    log_info "Removing legacy tty1 startx hook from /root/.profile..."
    sed -i '/# ALPINE_RECORDER_KIOSK_AUTOSTART/,/^fi$/d' /root/.profile
    log_ok "Cleaned /root/.profile (console login shell is now separate from X11 kiosk)."
fi

mkdir -p /root/.config/openbox
if [ ! -f /root/.config/openbox/rc.xml ] || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cp "${SCRIPT_DIR}/config/openbox-rc.xml" /root/.config/openbox/rc.xml
    log_ok "Installed /root/.config/openbox/rc.xml."
else
    log_info "Preserving existing /root/.config/openbox/rc.xml."
fi

# Install or upgrade /root/.xinitrc (upgrade automatically if missing crash-loop guard or cursor init)
if [ ! -f /root/.xinitrc ] || ! grep -q "ALPINE_RECORDER_SINGLE_RUN" /root/.xinitrc || ! grep -q "left_ptr" /root/.xinitrc || [ "${FORCE_CONFIG}" -eq 1 ]; then
    cp "${SCRIPT_DIR}/config/xinitrc" /root/.xinitrc
    chmod +x /root/.xinitrc
    log_ok "Installed /root/.xinitrc with visible cursor, crash-loop protection, and logging."
else
    log_info "Preserving existing /root/.xinitrc."
fi

# Install CLI launcher / diagnostic tool and OpenRC post-boot service
mkdir -p /usr/local/bin /etc/init.d
cp "${SCRIPT_DIR}/config/alpine-recorder" /usr/local/bin/alpine-recorder
chmod +x /usr/local/bin/alpine-recorder
log_ok "Installed /usr/local/bin/alpine-recorder CLI launcher and diagnostic tool."

cp "${SCRIPT_DIR}/config/alpine-recorder.initd" /etc/init.d/alpine-recorder
chmod +x /etc/init.d/alpine-recorder
log_ok "Installed /etc/init.d/alpine-recorder post-boot OpenRC service."

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

    if [ "${ENABLE_SERVICE}" -eq 1 ]; then
        rc-update add alpine-recorder default 2>/dev/null || true
        rc-service alpine-recorder restart 2>/dev/null || rc-service alpine-recorder start 2>/dev/null || true
        log_ok "Enabled and started alpine-recorder OpenRC service on VT7 (tty1 remains a normal login console)."
    else
        rc-update del alpine-recorder default 2>/dev/null || true
        rc-service alpine-recorder stop 2>/dev/null || true
        log_info "Disabled automatic boot service (--disable-service). Run 'alpine-recorder' manually when ready."
    fi
fi

# -----------------------------------------------------------------------------
# 9. Register Paths in Alpine LBU (Local Backup Utility) & Commit
# -----------------------------------------------------------------------------
if command -v lbu >/dev/null 2>&1; then
    log_info "Registering application, SSL certificates, and configuration files with Alpine LBU..."
    lbu include "${APP_DEST}" 2>/dev/null || true
    lbu include "${APP_DEST}/app" 2>/dev/null || true
    lbu include "${APP_DEST}/app/qr_code.py" 2>/dev/null || true
    lbu include "${APP_DEST}/app/ssl_manager.py" 2>/dev/null || true
    lbu include "${APP_DEST}/app/remote_producer.py" 2>/dev/null || true
    lbu include "${APP_DEST}/app/sayodevice_led.py" 2>/dev/null || true
    lbu include "${APP_DEST}/app/ui/file_manager_dialog.py" 2>/dev/null || true
    lbu include "${APP_DEST}/app/ui/remote_producer_dialog.py" 2>/dev/null || true
    lbu include "${ETC_CONFIG_DIR}" 2>/dev/null || true
    lbu include "${ETC_CONFIG_DIR}/ssl" 2>/dev/null || true
    lbu include /etc/modules-load.d 2>/dev/null || true
    lbu include /etc/udev/rules.d/99-sayodevice.rules 2>/dev/null || true
    lbu include /etc/X11/Xwrapper.config 2>/dev/null || true
    lbu include /var/lib/alsa 2>/dev/null || true
    lbu include /etc/samba 2>/dev/null || true
    lbu include /var/lib/samba/private 2>/dev/null || true
    lbu include /root/.xinitrc 2>/dev/null || true
    lbu include /root/.profile 2>/dev/null || true
    lbu include /root/.config/openbox 2>/dev/null || true
    lbu include /usr/local/bin/alpine-recorder 2>/dev/null || true
    lbu include /etc/init.d/alpine-recorder 2>/dev/null || true
    lbu include "${LOCAL_STORAGE_SCRIPT}" 2>/dev/null || true
    lbu include /etc/inittab 2>/dev/null || true

    # Exclude /storage so recorded WAV files are never packed into RAM overlay
    lbu exclude "${STORAGE_DIR}" 2>/dev/null || true
    # Exclude Samba runtime sockets in /var/lib/samba/private/msg.sock to prevent lbu commit errors
    lbu exclude /var/lib/samba/private/msg.sock 2>/dev/null || true

    if grep -Eq '^[^#]*LBU_MEDIA=' /etc/lbu/lbu.conf 2>/dev/null || [ -n "${LBU_MEDIA:-}" ]; then
        log_info "Committing changes to LBU persistent overlay..."
        if lbu commit -d || lbu commit; then
            log_ok "LBU commit completed successfully."
        else
            log_warn "lbu commit returned non-zero status; verify LBU_MEDIA in /etc/lbu/lbu.conf."
        fi
    else
        log_info "Attempting LBU commit with default LBU configuration..."
        if lbu commit -d 2>/dev/null || lbu commit 2>/dev/null; then
            log_ok "LBU commit completed successfully."
        else
            log_warn "LBU_MEDIA is not configured in /etc/lbu/lbu.conf yet. Run 'lbu commit -d' once LBU media is configured."
        fi
    fi
else
    log_info "lbu utility not found on this machine; skipping LBU commit."
fi

log_ok "Alpine Recording Station installation/update complete!"
log_info "Console login on tty1 (Ctrl+Alt+F1) is independent of the kiosk UI on VT7 (Ctrl+Alt+F7)."
log_info "Use 'alpine-recorder status', 'alpine-recorder logs', or 'alpine-recorder doctor' to troubleshoot."
