# Alpine Recording Station

An optimized **Alpine Linux** kiosk configuration and fullscreen studio application for single-track microphone recording from a USB audio interface to uncompressed `.wav` files stored on `/storage` and shared over **Samba**.

---

## Key Features

- **Hardware-Native ALSA Audio Engine**:
  - Automatically detects USB audio interfaces (`/proc/asound` + `libasound.so.2`).
  - Queries hardware-supported bit depths (`16-bit`, `24-bit`, `32-bit` PCM) and sample rates (`44.1 kHz` through `192 kHz`) and **defaults to the maximum bit depth and sample rate** supported by the selected interface.
  - Persistent settings across runs and reboots (stored in `/storage/.alpine-recorder/settings.json` and `/etc/alpine-recorder/settings.json` with Alpine `lbu commit` integration).
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
- **Fullscreen Responsive Kiosk UI (`x86_64`, `x86`, `aarch64`, `armv7`)**:
  - Launches automatically on boot via minimal `Xorg` + `Openbox` on `tty1`.
  - Dynamically scales buttons, fonts, and controls to fit any monitor or touchscreen resolution.
  - Displays the device's LAN IP address and Samba path subtly in the bottom status bar.

---

## Keyboard Hotkeys & Tooltips

All buttons display their keyboard hotkeys on mouse hover:

| Action | Hotkey | Description |
| :--- | :--- | :--- |
| **Record / Stop** | `R` or `Space` | Start or stop recording (`Green` = Ready, `Red` = Recording, `Grey` = No/Invalid Interface) |
| **Talkback (Studio Mode)** | Hold `T` | Momentary push-to-talk: mutes Left ch, plays 0.5s tone + records mic on Right ch while held |
| **Toggle Mode** | `M` | Switch between Normal Mode and Studio Mode (when stopped) |
| **Play / Pause Take** | `P` | Play or pause the selected project recording |
| **Stop Playback** | `S` | Stop reference audio playback |
| **Settings & Samba Admin** | `F2` or `Ctrl+,` | Open Audio Hardware, Recording Format, and Samba User Administration |
| **Refresh Hardware & Takes** | `F5` | Rescan ALSA USB devices, `/storage` recordings, and LAN IP |
| **Toggle Fullscreen** | `F11` | Toggle between fullscreen kiosk mode and windowed mode |

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

- `install.sh` is **safe to run multiple times**: running it again updates the application in `/opt/alpine-recorder` and installs any missing Alpine packages while preserving your existing `/etc/samba/smb.conf`, Samba user database, and saved recorder settings.
- Pass `./install.sh --force-config` if you want to reset `smb.conf`, `openbox/rc.xml`, and `.xinitrc` back to the repository templates.

---

## Repository Structure

- [`install.sh`](install.sh) — Idempotent Alpine Linux installer, updater, and LBU registration script.
- [`config/smb.conf.template`](config/smb.conf.template) — Samba share configuration for `/storage` with shared read/write/delete file masking.
- [`config/openbox-rc.xml`](config/openbox-rc.xml) — Minimal Openbox window manager configuration for fullscreen kiosk display.
- [`config/xinitrc`](config/xinitrc) — X11 startup script disabling screen blanking/DPMS and launching the recorder UI.
- [`app/main.py`](app/main.py) — Application entry point and CLI options (`--windowed`, `--storage-dir`, `--config-dir`, `--simulate-audio`).
- [`app/audio_engine.py`](app/audio_engine.py) — ALSA hardware capability prober, 16/24/32-bit WAV writer, Studio Mode Talkback DSP router, and playback engine.
- [`app/config_manager.py`](app/config_manager.py) — Persistent settings manager, project prefix filter, and `<Project>_yyyy-mm-dd_HH.MM.SS_TZ.wav` filename generator.
- [`app/samba_manager.py`](app/samba_manager.py) — Samba user administration (`add_user`, `change_password`, `remove_user`) with automatic `lbu commit`.
- [`app/network_info.py`](app/network_info.py) — LAN IPv4 address discovery for the subtle status bar display.
- [`app/ui/main_window.py`](app/ui/main_window.py) — Responsive fullscreen PyQt6 studio interface.
- [`app/ui/settings_dialog.py`](app/ui/settings_dialog.py) — Audio hardware settings and Samba user administration dialog.
- [`app/ui/styles.py`](app/ui/styles.py) — Resolution-adaptive dark studio theme and button state styles.
- [`tests/test_recorder_suite.py`](tests/test_recorder_suite.py) — Unit tests for Studio Mode Talkback DSP, 16/24/32-bit WAV I/O, USB stream parsing, project prefix filtering, and Samba user management.
