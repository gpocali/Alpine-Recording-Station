# Alpine Recording Station

An optimized **Alpine Linux** kiosk configuration and fullscreen studio application for single-track microphone recording from a USB audio interface to uncompressed `.wav` files stored on `/storage` and shared over **Samba**.

---

## Key Features

- **Hardware-Native ALSA Audio Engine**:
  - Automatically detects USB audio interfaces (`/proc/asound` + `libasound.so.2`).
  - Queries hardware-supported bit depths (`16-bit`, `24-bit`, `32-bit` PCM) and sample rates (`44.1 kHz` through `192 kHz`) and **defaults to the maximum bit depth and sample rate** supported by the selected interface.
  - Persistent settings across runs and reboots (stored in `/storage/.alpine-recorder/settings.json` and `/etc/alpine-recorder/settings.json` with Alpine `lbu commit` integration).
- **Real-Time Graphical VU & Peak Hold Meter (Active Level Setting)**:
  - Continuously monitors the selected USB audio input while the application is active (both in standby/ready mode and during recording) so preamp gain can be level-set before or during a session.
  - Displays a 60-segment studio LED bar graph across a calibrated `-60 dBFS` to `0 dBFS` scale (`Green` safe range `-60..-18 dB`, `Amber` nominal peak target `-18..-6 dB`, `Red` headroom warning `-6..0 dB`) with both RMS body and instantaneous Peak levels.
  - **Maintained Peak Hold**: Locks and displays the maximum peak level both graphically (via a high-contrast marker on the meter scale) and numerically (`PEAK HOLD: dBFS` readout + latching `CLIP` indicator at `-0.1 dBFS`) until reset via the **Reset Peak** button, clicking the meter, or pressing `[C]`.
- **Normal Mode & Studio Mode (with Momentary Talkback)**:
  - **Normal Mode**: Records Mono (`1ch`) or Stereo (`2ch`) `.wav` files at the selected bit depth and sample rate.
  - **Studio Mode**: Defaults to Stereo (`2ch`) output where:
    - **Left Channel**: Records the microphone take.
    - **Right Channel**: Silent during normal recording. Holding the **Talkback** button (via mouse, touchscreen, or holding `[T]`) immediately **mutes the Left channel**, emits a **0.5-second 1000 Hz marker tone** on the Right channel, and records spoken notes from the microphone onto the Right channel until released.
    - Provides high-contrast visual indication whenever Talkback is active, enabling fast visual or AI-assisted assembly in a DAW.
- **Project Prefix & Take Management**:
  - Output filenames follow the format: `<ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav`.
  - The scrollable recordings list at the bottom of the UI automatically filters `/storage` to show only recordings matching the active **Project Prefix**, with built-in reference playback (`Play / Pause`, `Stop`).
- **Samba Share & User Administration**:
  - Shares `/storage` over SMB (`\\<IP>\storage`).
  - Built-in **Samba Share Administration** tab in Settings to **Add Users**, **Change Passwords**, and **Remove Users** (persisted via `lbu commit`).
  - All files and directories in `/storage` are permission-masked (`0666` / `0777` with `force group = sambashare`) so authenticated users have full read, write, and delete capability for remote cleanup.
- **Touchscreen On-Screen Keyboard (with Hardware Keyboard Toggle)**:
  - Built-in responsive touch keyboard for strictly touchscreen kiosk deployments—pops up automatically when tapping the **Project Prefix** field, **Samba Share Administration** username/password fields, or any other text input (plus dedicated `⌨` launch buttons).
  - Includes full QWERTY, numeric, and symbol layers (`?123`), `Shift`, `Caps Lock`, cursor movement, `Clear`, password visibility toggle (`👁 Show` / `🙈 Hide`), and `Next Field ⇥` navigation across multi-field forms.
  - Can be **enabled or disabled in Settings** (`Audio & Recording Settings` → `On-Screen Keyboard`, or via `./install.sh --disable-osk` / `--no-osk`) when a physical hardware keyboard is attached.
- **Post-Boot OpenRC Service on VT7 & Independent `tty1` Console Recovery**:
  - Launches **after** the system boot process completes via a dedicated OpenRC service (`/etc/init.d/alpine-recorder`) on **`VT7`** (`Ctrl+Alt+F7`), leaving **`tty1`–`tty6`** (`Ctrl+Alt+F1`) untouched as standard Linux login consoles for troubleshooting and recovery.
  - Includes crash-loop protection: if Xorg or display drivers fail to initialize, the service stops retrying and logs detailed diagnostics to `/var/log/alpine-recorder.log` and `/var/log/alpine-recorder-xorg.log` without interfering with console login.
  - Includes the `alpine-recorder` CLI utility (`run`, `start`, `stop`, `restart`, `status`, `enable`, `disable`, `logs`, `doctor`, `recover`) and an **Exit to Console** button in Settings (`Ctrl+Q`).

---

## Keyboard Hotkeys & Tooltips

All buttons display their keyboard hotkeys on mouse hover:

| Action | Hotkey | Description |
| :--- | :--- | :--- |
| **Record / Stop** | `R` or `Space` | Start or stop recording (`Green` = Ready, `Red` = Recording, `Grey` = No/Invalid Interface) |
| **Reset Peak Hold** | `C` | Reset maintained Peak Hold level and `CLIP` indicator on the VU meter |
| **Talkback (Studio Mode)** | Hold `T` | Momentary push-to-talk: mutes Left ch, plays 0.5s tone + records mic on Right ch while held |
| **Toggle Mode** | `M` | Switch between Normal Mode and Studio Mode (when stopped) |
| **Play / Pause Take** | `P` | Play or pause the selected project recording |
| **Stop Playback** | `S` | Stop reference audio playback |
| **Settings & Samba Admin** | `F2` or `Ctrl+,` | Open Audio Hardware, Recording Format, and Samba User Administration |
| **Refresh Hardware & Takes** | `F5` | Rescan ALSA USB devices, `/storage` recordings, and LAN IP |
| **Toggle Fullscreen** | `F11` | Toggle between fullscreen kiosk mode and windowed mode |
| **Exit to Console** | `Ctrl+Q` | Close the kiosk UI and return to the Linux console (`tty1`) |

---

## Installation & Updating on Alpine Linux

### 1. Mount Persistent Storage at `/storage`

Ensure your persistent storage partition is mounted at `/storage` in `/etc/fstab` (for example, `/dev/sda2` or `/dev/mmcblk0p2`) so your system root remains protected in RAM by **LBU** while `/storage` holds your `.wav` files:

```sh
mkdir -p /storage
# Example /etc/fstab entry:
# /dev/sda2   /storage   ext4   defaults,noatime   0 2
mount -a
```

### 2. Run the Idempotent Installer / Updater

Run `install.sh` as `root`:

```sh
chmod +x install.sh
./install.sh
```

- `install.sh` is **safe to run multiple times**: running it again updates the application in `/opt/alpine-recorder`, installs any missing Alpine packages, and automatically cleans up any legacy `tty1` autologin/autostart hooks from older versions while preserving your existing `/etc/samba/smb.conf`, Samba user database, and saved recorder settings.
- Pass `./install.sh --disable-service` (or `--no-autostart`) if you want the system to boot to the text console only and start the kiosk manually via `alpine-recorder`.
- Pass `./install.sh --enable-osk` or `./install.sh --disable-osk` to pre-configure the On-Screen Keyboard setting during installation (it can also be toggled anytime in the UI via **Settings & Samba → Audio & Recording Settings → On-Screen Keyboard**).
- Pass `./install.sh --force-config` if you want to reset `smb.conf`, `openbox/rc.xml`, and `.xinitrc` back to the repository templates.

---

## Troubleshooting, Diagnostics & Recovery

### Recovering a Machine Stuck in an Older `tty1` MOTD Loop

If you previously installed an older revision that used `/etc/inittab` autologin + `/root/.profile` on `tty1` and `startx` failed (causing `tty1` to flash the MOTD repeatedly):

1. Press **`Alt+F2`** (or **`Ctrl+Alt+F2`**) on the keyboard to switch to **`tty2`** (or connect via **SSH**). `tty2`–`tty6` are unaffected by the `tty1` hook and provide a normal login prompt.
2. Log in as `root` and run the updated installer (or clean up the hooks directly):
   ```sh
   ./install.sh
   # Or manually restore tty1 immediately:
   sed -i 's|^tty1::respawn:.*|tty1::respawn:/sbin/getty 38400 tty1|g' /etc/inittab
   sed -i '/# ALPINE_RECORDER_KIOSK_AUTOSTART/,/^fi$/d' /root/.profile
   rm -f /usr/local/bin/alpine-recorder-autologin
   kill -HUP 1
   ```

### Using the `alpine-recorder` CLI Tool

Once logged in on `tty1` (`Ctrl+Alt+F1`) or over SSH, use `alpine-recorder` to manage and troubleshoot the kiosk:

```sh
alpine-recorder doctor        # Check display nodes (/dev/dri, /dev/fb), Xorg, PyQt6, ALSA & /storage
alpine-recorder fix-packages  # Restore and cache missing X11/PyQt6 packages across diskless reboots
alpine-recorder logs          # View recent errors from /var/log/alpine-recorder.log and Xorg logs
alpine-recorder status        # Check OpenRC service status, running PIDs, and recent logs
alpine-recorder run           # Launch X11 & recorder in the foreground with console error output
alpine-recorder stop          # Stop the kiosk service and switch back to tty1
alpine-recorder restart       # Restart the background VT7 kiosk service
alpine-recorder disable       # Disable automatic startup at boot for troubleshooting
alpine-recorder enable        # Re-enable automatic post-boot startup on VT7
alpine-recorder recover       # Emergency recovery: stop Xorg, clean tty1 config, switch to tty1
```

---

## Repository Structure

- [`install.sh`](install.sh) — Idempotent Alpine Linux installer, updater, legacy `tty1` recovery, and LBU registration script.
- [`config/alpine-recorder`](config/alpine-recorder) — Standalone CLI launcher, VT7 service supervisor with crash-loop protection, log viewer, and hardware diagnostic utility (`/usr/local/bin/alpine-recorder`).
- [`config/alpine-recorder.initd`](config/alpine-recorder.initd) — OpenRC service script (`/etc/init.d/alpine-recorder`) that starts the kiosk on VT7 after boot completes.
- [`config/smb.conf.template`](config/smb.conf.template) — Samba share configuration for `/storage` with shared read/write/delete file masking.
- [`config/openbox-rc.xml`](config/openbox-rc.xml) — Minimal Openbox window manager configuration for fullscreen kiosk display.
- [`config/xinitrc`](config/xinitrc) — X11 startup script disabling screen blanking/DPMS, logging output to `/var/log/alpine-recorder.log`, and guarding against rapid crash loops.
- [`app/main.py`](app/main.py) — Application entry point and CLI options (`--windowed`, `--storage-dir`, `--config-dir`, `--simulate-audio`, `--osk`, `--no-osk`).
- [`app/audio_engine.py`](app/audio_engine.py) — ALSA hardware capability prober, 16/24/32-bit WAV writer, Studio Mode Talkback DSP router, and playback engine.
- [`app/config_manager.py`](app/config_manager.py) — Persistent settings manager, project prefix filter, and `<Project>_yyyy-mm-dd_HH.MM.SS_TZ.wav` filename generator.
- [`app/samba_manager.py`](app/samba_manager.py) — Samba user administration (`add_user`, `change_password`, `remove_user`) with automatic `lbu commit`.
- [`app/network_info.py`](app/network_info.py) — LAN IPv4 address discovery for the subtle status bar display.
- [`app/ui/main_window.py`](app/ui/main_window.py) — Responsive fullscreen PyQt6 studio interface.
- [`app/ui/vu_meter.py`](app/ui/vu_meter.py) — Graphical studio LED VU & Peak Hold meter widget (`-60` to `0 dBFS`) for live input level setting.
- [`app/ui/settings_dialog.py`](app/ui/settings_dialog.py) — Audio hardware settings, On-Screen Keyboard toggle, Exit to Console action, and Samba user administration dialog.
- [`app/ui/on_screen_keyboard.py`](app/ui/on_screen_keyboard.py) — Touch On-Screen Keyboard dialog and automatic `QLineEdit` event controller.
- [`app/ui/styles.py`](app/ui/styles.py) — Resolution-adaptive dark studio theme and button state styles.
- [`tests/test_recorder_suite.py`](tests/test_recorder_suite.py) — Unit tests for Studio Mode Talkback DSP, 16/24/32-bit WAV I/O, USB stream parsing, project prefix filtering, On-Screen Keyboard, Samba user management, and boot/service configuration scripts.
