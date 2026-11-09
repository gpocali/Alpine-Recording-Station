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
- **`/storage` Drive Utilization & Recording Time Remaining Monitor**:
  - Dedicated horizontal telemetry strip positioned between the Studio Deck and the Project Recordings pane (visually distinct from the audio VU meter), featuring a **Smooth Continuous Capacity Pill Bar** with overlaid capacity text (`XX.X% USED • X.X GB FREE`) and a **Digital Recording Time Remaining Readout Box** (`HHh MMm SSs` at `MB/min`) calculated dynamically for the active Bit Depth, Sample Rate, and Channel/Studio Mode settings (and live in **Settings** as you adjust format options).
  - Automatically ensures `/storage` is mounted from `/etc/fstab` before measuring capacity (resolving symlinks and `/proc/mounts` device info so it always measures the persistent `/storage` drive rather than the diskless `tmpfs` RAM root).
  - Escalates to high-visibility **Amber Warning** (`⚠ LOW SPACE` / `DRIVE GETTING FULL` at `>= 80%` used or `<= 30 minutes` remaining) and **Red Critical** (`⚠ CRITICAL` / `DRIVE CRITICALLY FULL` at `>= 90%` used or `<= 10 minutes` remaining) banners and borders so it is immediately clear when the drive is getting close to full.
- **Normal Mode & Studio Mode (with Momentary Talkback)**:
  - **Normal Mode**: Records Mono (`1ch`) or Stereo (`2ch`) `.wav` files at the selected bit depth and sample rate.
  - **Studio Mode**: Defaults to Stereo (`2ch`) output where:
    - **Left Channel**: Records the microphone take.
    - **Right Channel**: Silent during normal recording. Holding the **Talkback** button (via mouse, touchscreen, or holding `[T]`) immediately **mutes the Left channel**, emits a configurable **press marker tone** (`1.0 second` by default) on the Right channel, records spoken notes from the microphone onto the Right channel while held, and emits a configurable **release marker tone** (`0.5 seconds` by default) on the Right channel when Talkback is concluded.
    - **Adjustable Tone Frequency & Durations**: Both the press and release tone durations (`Off / 0.0s` through `3.0s`) and the marker tone frequency (`330 Hz`, `440 Hz`, `528 Hz`, `660 Hz`, `800 Hz`, `1000 Hz / 1 kHz`, `1200 Hz`) are adjustable in **Settings & Samba → Audio & Recording Settings** so you can choose a warmer, easier-on-the-ear tone.
    - Provides high-contrast visual indication whenever Talkback is active, enabling fast visual or AI-assisted assembly in a DAW.
- **Remote Producer Option (Guided VO over HTTPS / WSS)**:
  - Optional low-latency, bidirectional audio & telemetry web console (`https://<IP>:8443/`) that allows a remote producer or director to listen live to the talent's performance and use a momentary **Talkback** control (`Hold T` or `Space`) to prompt the talent in real time.
  - **Full Mix-Minus & Right-Channel Producer + Talent Mix**:
    - **Right Channel (Studio Mode Talkback Track)**: Whenever Talkback is active (triggered by either the Remote Producer or the Local Talent), the Left (VO) channel is muted (`0.0`) and **both the Remote Producer's voice and the Local Talent's microphone** are summed onto the Right channel (between the press and release marker tones) so two-way direction and talent responses are captured together on the Talkback track.
    - **Mix-Minus to Remote Producer**: The live stream sent to the Remote Producer contains **only the Local Talent's microphone** (minus the producer's own voice and marker tones).
    - **Mix-Minus to Local Talent Headphones**: The headphone monitor output feeds **only the Remote Producer's Talkback audio** to the local talent's headphones (minus the local talent's microphone, preventing software-loopback echo).
  - **Stripped-Down Studio Web UI**: Matches the talent's dark studio theme with a live 60-segment LED VU & Peak Hold meter, elapsed timer, recording/pause/talkback state banner, session take count, and remaining storage time, while intentionally omitting hardware configuration, Samba administration, and transport mutation controls so the talent retains full independent control.
  - **SSL/TLS by Default with Easy Certificate Replacement**: Automatically generates a valid X.509 v3 self-signed certificate (with SANs for `localhost` and LAN IPv4 addresses) out of the box, and supports one-click replacement with CA-issued certificates dropped into `/storage/ssl/` (`server.crt` + `server.key`) over Samba or via custom certificate paths.
  - **Session-Tied Authentication, QR Code & Invite Link**: Generates a unique **Session ID** (`ARS-XXXX`), 6-digit **Session Passcode (PIN)**, scannable **QR Code**, and copyable HTTPS invite link; rotating the session immediately revokes prior tokens, cookies, and active WebSocket connections.
  - **Bidirectional Mic Privacy & Cough Controls**: Both the local Talent Studio Deck (`◉ MIC TO PRODUCER` / `Hold [B]` Cough Mute) and the Remote Producer web console (`Send Producer Mic` switch defaulting to `OFF (Private)` + `Hold [C]` / `[M]` Cough Mute) provide dedicated privacy and cough controls so neither microphone is ever unexpectedly hot.
- **Project Subfolders, Fullscreen Storage File Manager & Direct File Management**:
  - Each project stores its recordings inside a dedicated subfolder: `/storage/<ProjectName>/<ProjectName>_yyyy-mm-dd_HH.MM.SS_TZ.wav`.
  - Accessible via the **✎ Project Name** button on the **Project Recordings** header (opening a dedicated dialog with live subfolder/filename preview and On-Screen Keyboard support), leaving the top bar open to display the full **Next / Recording / Paused** relative path.
  - **Direct UI File Renaming**: Once a recording is completed, click **✎ Rename File** (or press `[N]` / `[F4]`) to rename the `.wav` file directly inside its project folder (with On-Screen Keyboard support) so takes can be categorized immediately instead of only having the project and timestamp.
  - **Fullscreen Storage File Manager (`▤ File Manager` / `[F6]`)**:
    - Displays a full-screen file list across **All Projects** or filtered by any specific project folder, with search filtering (and On-Screen Keyboard support) and multi-mode sorting (`Newest First`, `Oldest First`, `Largest Size First`, `Smallest Size First`, `Filename A → Z`, `Project Name A → Z`), plus a `⇄ Fullscreen File List` toggle (`[F11]`) that collapses the project sidebar so the file list spans 100% of the screen width.
    - Supports **playing/pausing** (`[P]` / `Space`), **seeking ±3s** (`[Left]` / `[Right]`), **renaming** (`[N]` / `[F4]`), and **deleting** (`[Del]`) any recording file across any project, with a modal confirmation dialog that previews the exact disk space and recording time to be recovered and the before/after drive utilization.
    - **Project Folder Cleanup & Recursive Deletion with Extra Confirmations**: Supports cleaning up empty project folders (`↺ Clean Empty`), emptying all files inside a project folder while keeping the folder (`◈ Empty Folder...`), and recursively deleting a project folder and all of its contents (`✕ Delete Folder...`). Both recursive operations require a **2-step extra confirmation** workflow (Step 1: review file list and drive utilization impact; Step 2: check the mandatory final confirmation checkbox to unlock the destructive action button).
    - **Live Drive Utilization & Space Cleared Counter**: Embeds the live `/storage` `StorageMonitorWidget` capacity bar alongside a real-time `SPACE CLEARED` session badge (`+MB/GB` and `+HHh MMm SSs` recording time gained) so it is immediately clear how much drive space is being freed.
  - **Corruption-Safe Record Pause & Resume**: Pausing an active recording (`⏸ PAUSE RECORDING` or pressing `[P]` / `[Pause]`) flushes the RIFF/WAVE header and **closes the `.wav` file on disk** so it cannot be accidentally corrupted while paused, then **reopens the exact same `.wav` file** in append mode when resumed (`▶ RESUME RECORDING`). While paused, the Studio Deck displays unmistakable flashing Amber/Gold warning banners, a pulsing `⏸ HH:MM:SS.d [PAUSED]` timer, a glowing deck border, and a pulsing **Resume Recording** button so a paused session is never forgotten.
  - The scrollable recordings list at the bottom of the UI automatically lists all `.wav` recordings inside the active project subfolder (`/storage/<ProjectName>/`, including renamed takes), with built-in reference playback (`Play / Pause`, `Stop`).
- **Samba Share & User Administration**:
  - Shares `/storage` over SMB (`\\<IP>\storage`).
  - Built-in **Samba Share Administration** tab in Settings to **Add Users**, **Change Passwords**, and **Remove Users** (persisted via `lbu commit`).
  - All files and subdirectories anywhere within the `/storage` shared folder structure are permission- and ownership-masked (`create mask = 0777`, `force create mode = 0666`, `directory mask = 2777`, `force directory mode = 2777`, `force user = root`, `force group = sambashare`, `admin users = @sambashare`, `inherit owner = windows and unix`, `nt acl support = no`, `map readonly = no`, plus recursive filesystem permission/ownership normalization) so authenticated users always have full read, write, and delete access even if new files appear with mismatched permissions or ownership.
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
| **Pause / Resume Recording** | `P` or `Pause` | Pause active recording (safely closes `.wav` file on disk) or reopen & resume into the same file |
| **Reset Peak Hold** | `C` | Reset maintained Peak Hold level and `CLIP` indicator on the VU meter |
| **Talkback (Studio Mode)** | Hold `T` | Momentary push-to-talk: mutes Left ch, plays 1.0s press tone + records mic on Right ch while held, then plays 0.5s release tone |
| **Mic to Producer / Cough Mute** | Hold `B` (or Click) | Hold `B` for momentary Cough Mute of feed to Remote Producer, or click button to toggle persistent Privacy Mute |
| **Remote Producer & SSL Setup** | `F3` | Open Guided VO Remote Producer setup dialog (QR code, session link, passcode rotation, and SSL certificate import) |
| **Toggle Mode** | `M` | Switch between Normal Mode and Studio Mode (when stopped) |
| **Rename Recording** | `N` or `F4` | Rename the selected completed `.wav` recording inside the project folder (when not recording) |
| **Delete Recording** | `Del` | Delete the selected `.wav` recording after confirmation showing drive space recovered |
| **Storage File Manager** | `F6` | Open fullscreen Storage File Manager to browse, play, rename, and delete files or project folders across all projects |
| **Play / Pause Take** | `P` | Play or pause the selected project recording (when not recording) |
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
- [`app/qr_code.py`](app/qr_code.py) — Pure-Python ISO/IEC 18004 QR Code matrix and SVG generator for session invite links.
- [`app/ssl_manager.py`](app/ssl_manager.py) — Default X.509 v3 self-signed certificate generator and `/storage/ssl` issued-certificate importer.
- [`app/remote_producer.py`](app/remote_producer.py) — HTTPS + RFC 6455 WebSocket server, session-bound authenticator, and stripped-down Guided VO Remote Producer web console.
- [`app/ui/main_window.py`](app/ui/main_window.py) — Responsive fullscreen PyQt6 studio interface.
- [`app/ui/file_manager_dialog.py`](app/ui/file_manager_dialog.py) — Fullscreen Storage File Manager across all projects with file playback/renaming/deletion, 2-step confirmed recursive project cleanup/deletion, and live drive utilization.
- [`app/ui/remote_producer_dialog.py`](app/ui/remote_producer_dialog.py) — Remote Producer session setup, QR code display, passcode rotation, and SSL certificate dialog.
- [`app/ui/vu_meter.py`](app/ui/vu_meter.py) — Graphical studio LED VU & Peak Hold meter widget (`-60` to `0 dBFS`) for live input level setting.
- [`app/ui/storage_monitor.py`](app/ui/storage_monitor.py) — Graphical `/storage` drive utilization bar (`0%` to `100%`), low-space/critical warning panel, and real-time recording time remaining monitor.
- [`app/ui/settings_dialog.py`](app/ui/settings_dialog.py) — Audio hardware settings, On-Screen Keyboard toggle, Exit to Console action, and Samba user administration dialog.
- [`app/ui/on_screen_keyboard.py`](app/ui/on_screen_keyboard.py) — Touch On-Screen Keyboard dialog and automatic `QLineEdit` event controller.
- [`app/ui/styles.py`](app/ui/styles.py) — Resolution-adaptive dark studio theme and button state styles.
- [`tests/test_recorder_suite.py`](tests/test_recorder_suite.py) — Unit tests for Studio Mode Talkback DSP, 16/24/32-bit WAV I/O, USB stream parsing, project prefix filtering, On-Screen Keyboard, Samba user management, and boot/service configuration scripts.
