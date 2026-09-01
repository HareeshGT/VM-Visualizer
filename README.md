# Deckhand

**Deckhand** (also referred to as **VM Visualizer**) is a cross-platform desktop GUI application built with PyQt5 for managing AWS EC2 instances and Kubernetes clusters.

It combines SSH/SFTP-based remote file management, a built-in code editor, an integrated terminal, live VM/Kubernetes monitoring, Kubernetes resource management, AI-assisted Kubernetes operations, and voice-controlled Kubernetes commands in a single dark-themed, Finder-style interface.

---

## Features

### Connections & Security

- **EC2 Instance Management** — Connect to AWS EC2 instances over SSH/SFTP via `ConnectDialog`, with a recent-instances list containing host, port, user, PEM key, and alias. Recent instances are persisted to CSV.
- **User-Context Switching** — Filesystem operations can be routed through `sudo`-prefixed SSH exec commands through `SudoFS`, allowing actions to run as another remote user while using the same SSH/Paramiko session.
- **App Lock** — Optional PIN protection through `security.py` and `lock_screen.py`.
  - PBKDF2-SHA256 hashed PIN.
  - Lock-on-launch.
  - Configurable automatic locking after inactivity.
  - `InactivityWatcher` handles idle-time tracking.
  - Correct PIN entry shows the animated ring-to-checkmark success state.
  - Incorrect PIN entry shows an error without closing the application.

### Remote File Manager

- Dark-themed macOS Finder-style SFTP file browser.
- List and grid views using `FileRowWidget` and `FileGridWidget`.
- Sidebar with dynamic remote path probing.
- Breadcrumb navigation.
- File preview pane.
- Right-click context menus.
- **File Editor** — Edit remote files in place through `FileEditorDialog`.
  - Line-numbered gutter.
  - Current-line highlighting.
  - Zoom.
  - Find/replace.
  - Lightweight syntax highlighting for common languages.
- **File Executor** — Run remote scripts/executables and stream output back into the application.
  - PTY-backed interactive input.
  - Real-time output.
- **Media Player** — Stream and play remote audio/video through the local range-request HTTP relay.
- **Remote Search** — Grep/find-based searching across the remote filesystem.
- **File Transfers** — Non-blocking upload/download with:
  - Live transfer speed.
  - ETA.
  - Cancellation support.
  - Background transfer workers.

### Integrated Terminal

- `TerminalWidget` provides a single scrolling terminal surface where commands and output share the same view.
- Command history.
- Ctrl+C interrupt support.
- Commands execute one at a time over the active SSH connection.
- This is not a full PTY/VT100 terminal emulator, so interactive curses applications such as `vim` and `top` are not supported.

### Live Dashboard

- `DashboardTab` shows the connected instance's:
  - Hostname.
  - Operating system.
  - Kernel.
  - CPU.
  - RAM.
  - Disk.
- When Kubernetes is reachable, the dashboard also shows a node table containing:
  - CPU usage.
  - Memory usage.
  - Kubelet `MemoryPressure`.
  - Kubelet `DiskPressure`.
  - Kubelet `PIDPressure`.
- Double-clicking a node opens `NodeDetailWindow` with:
  - Pods scheduled on the node.
  - Per-pod CPU/memory usage.
  - Restart count.
  - Ready state.
- Dashboard polling only runs while the dashboard is connected and active, avoiding unnecessary SSH round-trips when the dashboard is not being viewed.

---

# Kubernetes Management

`KubernetesTab` provides dedicated card-based sub-tabs for Kubernetes resources.

### Resource Sub-Tabs

- Pods
- Deployments
- StatefulSets
- DaemonSets
- HPA
- Services
- Ingress
- Jobs & CronJobs
- Storage (PVCs/PVs)
- Config & Secrets
- Events
- Terminal
- Tunnels
- **OpsMind**

Individual Kubernetes sub-tabs can be hidden per user from Settings.

### Kubernetes Terminal

The in-tab terminal is ready for `kubectl` commands and uses the active Kubernetes SSH connection.

### Kubernetes Tunnels

- CSV-driven service configuration through `load_tunnel_services`.
- One-click local `kubectl port-forward` tunnels.
- Automatic tunnel status refresh.
- `ManageTunnelServicesDialog` for editing the service list.

### Pod Operations

- Live log streaming through `LogViewerDialog`.
- Exec into containers through `ExecDialog`.
- `ContainerPickerDialog` is used when pods contain multiple containers.

---

# Kubernetes OpsMind

The **OpsMind** tab provides natural-language Kubernetes operations through the configured AI provider.

The AI is used to interpret the user's request into a small, allow-listed operation schema. The application itself validates the result and constructs the actual `kubectl` command.

The AI does **not** provide an executable shell command directly.

### Supported AI Operations

The current OpsMind command interpreter supports:

- `scale`
- `restart`
- `delete`
- `get`
- `describe`
- `rollout_status`

Supported resource types include:

- Deployment
- StatefulSet
- DaemonSet
- Pod
- Service
- Ingress
- ConfigMap
- Secret
- Job
- CronJob
- HPA
- PVC
- PV

### Relative Scaling

Scaling supports both absolute and relative requests.

#### Absolute

```text
scale deployment my-app to 5
```

The application executes the requested target replica count.

#### Relative

```text
scale up my-app by 1
```

If the deployment currently has 2 replicas:

```text
2 + 1 = 3
```

Likewise:

```text
scale up my-app by 3
```

with 2 replicas becomes:

```text
2 + 3 = 5
```

And:

```text
scale down my-app by 2
```

with 5 replicas becomes:

```text
5 - 2 = 3
```

For relative scaling, the application first reads the **live replica count from Kubernetes**, calculates the new target, and only then executes the scale operation. Replica values are clamped to the supported range of `0` through `100`.

### Operation History

OpsMind remembers recent operations using the application's persistent settings.

History includes information such as:

- Timestamp.
- Success/failed state.
- Action.
- Resource type.
- Resource name.
- Namespace.
- Replica count for scaling operations.
- Previous replica count for scaling operations.
- Command/output information.

Up to 40 recent operations are retained.

This history allows follow-up requests such as:

```text
scale it back
```

and:

```text
repeat that
```

when the previous operation is unambiguous.

The stored history survives application restarts.

### API-Key Access Lock

OpsMind is only available when the currently selected AI provider has an API key configured.

When no API key is available:

- The OpsMind content is blurred.
- The controls are covered by a lock overlay.
- The user is told to add an API key under `Settings → AI`.
- AI operations and voice commands cannot be executed.

When a valid key is configured, the OpsMind tab becomes available again.

### AI Error Handling

Provider/API failures are surfaced through a dialog instead of only being written to the output panel.

Common errors such as:

- HTTP 429 / quota exceeded.
- Authentication failures.
- Access denied.
- Timeouts.
- Connection/network failures.

are given a descriptive dialog title while the complete provider response remains available as detailed error information.

---

# Voice-Controlled Kubernetes Operations

OpsMind also supports microphone-based voice commands.

## Speech-to-Text Engine

Voice transcription uses:

**Google Web Speech API through the `SpeechRecognition` Python package.**

The speech-to-text stage does **not** use an LLM.

The flow is:

```text
Microphone
   ↓
PyAudio
   ↓
SpeechRecognition
   ↓
Google Web Speech
   ↓
Plain text
   ↓
OpsMind interpreter
   ↓
Validated Kubernetes action
   ↓
kubectl
```

Only the resulting text is passed to the configured AI provider.

### Voice Controls

Voice recording can be controlled with:

- **Voice button** in the OpsMind panel.
- **Ctrl+Shift+Space** keyboard shortcut.

The button changes to a stop action while recording.

### Live Microphone Indicator

While recording, OpsMind shows a live microphone activity meter.

The meter is calculated directly from the microphone PCM samples and does not use AI.

The indicator rises when sound is detected and falls when the microphone is quiet.

### Microphone Permissions on macOS

When the application is packaged as a macOS `.app`, macOS microphone permission must be granted to the application.

The app includes the `NSMicrophoneUsageDescription` privacy metadata so macOS can request microphone access.

If permission has not been granted, enable the application under:

**System Settings → Privacy & Security → Microphone**

The target application may appear as **Deckhand**.

### Voice Recognition Notes

Voice recognition requires:

- Microphone hardware.
- macOS/OS microphone permission.
- PyAudio.
- SpeechRecognition.
- An internet connection for Google Web Speech recognition.

The packaged application includes the Python-side voice dependencies.

---

# Theming & Settings

- Multiple built-in color themes.
- Theme selection through a visual swatch-based `ThemePicker`.
- `SettingsDialog` provides:
  - App-lock configuration.
  - PIN configuration.
  - Auto-lock interval.
  - Kubernetes sub-tab visibility.
  - AI provider/API-key configuration.
- Settings are persisted locally in `settings.json`.
- Settings are shared across the application's feature groups through `themes.load_settings()` and `themes.save_settings()`.

---

# Downloads

Remote files are downloaded to:

```text
~/Downloads/
```

by default.

---

# Project Structure

| File | Purpose |
|---|---|
| `main.py` | Application entry point. Builds the Qt application/palette, shows the splash-to-window transition, and creates the main window. |
| `main_window.py` | `EC2FileManager` — main application window, tab layout, file-manager orchestration, dashboard integration, and settings flow. |
| `dashboard_tab.py` | Live VM + Kubernetes node dashboard (`DashboardTab`, `NodeDetailWindow`). |
| `kubernetes_tab.py` | `KubernetesTab` — Kubernetes resources, terminal, tunneling, and OpsMind integration. |
| `k8s_ai_ops.py` | AI-powered Kubernetes operations, persistent operation history, voice input, microphone level indicator, API-key gating, and safe `kubectl` execution. |
| `k8s_cards.py` | Card-style widgets for Kubernetes resource lists. |
| `dialogs.py` | Modal dialogs including Connect, Connecting, FileTransfer, LogViewer, ContainerPicker, Exec, FileEditor, FileExec, MediaPlayer, Search, and ManageTunnelServices. |
| `editor_widgets.py` | `CodeEditor` and dependency-free `SyntaxHighlighter`. |
| `lock_screen.py` | `AppLockDialog` and `SetPinDialog`. |
| `security.py` | PIN hashing, persisted security settings, and `InactivityWatcher`. |
| `settings_dialog.py` | Application Settings dialog, including security, AI configuration, and Kubernetes tab visibility. |
| `sudo_fs.py` | `SudoFS` — routes filesystem operations through `sudo`-prefixed SSH commands. |
| `workers.py` | Background QThread workers for SSH checks, commands, file streaming/transfers, and media streaming. |
| `ai_assist.py` | AI provider configuration, API key handling, selected model handling, and provider request abstraction. |
| `themes.py` | Theme palettes, QSS builder, and settings load/save functions. |
| `theme_picker.py` | Visual swatch-based theme selector. |
| `progress_ring.py` | Animated circular progress indicator. |
| `utils.py` | File classification, size formatting, recent-instance CSV handling, monospace-font detection, and tunnel-service CSV loading. |
| `sidebar.py` | Sidebar navigation with dynamic remote path probing. |
| `preview.py` | `PreviewPane` — remote file preview. |
| `file_widgets.py` | `FileRowWidget` and `FileGridWidget`. |
| `terminal_widget.py` | `TerminalWidget` — integrated terminal surface. |

---

# Requirements

## Core Python Packages

The application requires:

```text
PyQt5>=5.15,<6
paramiko>=3.0
```

## Voice Input Packages

Kubernetes OpsMind voice input additionally requires:

```text
SpeechRecognition>=3.10,<4
PyAudio>=0.2.13
```

## Build Dependency

For packaging the application:

```text
pyinstaller>=6.0
```

## macOS Native Dependencies

On macOS, voice input also requires:

- **PortAudio** — used by PyAudio.
- **FLAC** — used by SpeechRecognition when converting recorded audio.

Homebrew is used by the provided installer to install the native dependencies.

---

# Installation

The repository includes an installation/build script that detects the operating system and installs the required dependencies before packaging the application.

```bash
chmod +x build_app.sh
./build_app.sh
```

## macOS

The installer:

1. Checks for Homebrew.
2. Installs Homebrew Python 3.14 when needed.
3. Uses the Homebrew Python interpreter for the application build.
4. Installs PortAudio.
5. Installs FLAC.
6. Installs the Python requirements.
7. Verifies PyQt5, Paramiko, SpeechRecognition, PyAudio, and PyInstaller.
8. Builds the standalone application.
9. Configures the macOS microphone privacy metadata.
10. Installs the resulting application into:

```text
/Applications/Deckhand.app
```

The macOS installer uses the Homebrew Python interpreter rather than Apple's system Python.

## Linux

The installer supports common package managers and installs the native dependencies required for PyAudio/voice recognition where applicable.

Supported package managers include:

- `apt-get`
- `dnf`
- `yum`
- `pacman`

The application is installed to:

```text
/opt/Deckhand
```

## Windows

The installer supports Git Bash/MSYS/Cygwin environments and requests administrator privileges when necessary.

The packaged application is installed under:

```text
C:\Program Files\Deckhand
```

---

# Running from Source

After installing the dependencies:

```bash
python3 main.py
```

For voice input, make sure the OS has granted microphone permission to the process/application.

---

# Building a Standalone macOS App

The application can also be packaged manually with PyInstaller.

From the project root:

```bash
python3 -m PyInstaller \
    --windowed \
    --onedir \
    --icon=VM_Visualizer.icns \
    --name="Deckhand" \
    --hidden-import=paramiko \
    --collect-all=paramiko \
    --hidden-import=ai_assist \
    --hidden-import=k8s_ai_ops \
    --hidden-import=speech_recognition \
    --hidden-import=pyaudio \
    main.py
```

The output will be:

```text
dist/Deckhand.app
```

## Command Breakdown

| Flag | Description |
|---|---|
| `--windowed` | Builds the application as a GUI app without a terminal/console window. |
| `--onedir` | Creates a directory-style application bundle containing the executable and its dependencies. |
| `--icon=VM_Visualizer.icns` | Uses the supplied macOS application icon. |
| `--name="Deckhand"` | Sets the bundle and executable name. |
| `--hidden-import=paramiko` | Explicitly includes Paramiko. |
| `--collect-all=paramiko` | Collects Paramiko submodules, data, and binaries that may not be detected automatically. |
| `--hidden-import=ai_assist` | Ensures the AI provider module is included. |
| `--hidden-import=k8s_ai_ops` | Ensures the Kubernetes OpsMind module is included. |
| `--hidden-import=speech_recognition` | Explicitly includes the voice recognition dependency. |
| `--hidden-import=pyaudio` | Explicitly includes the microphone/PortAudio Python extension. |
| `main.py` | Application entry point. |

### macOS Microphone Metadata

The packaged application must include the macOS microphone privacy key:

```text
NSMicrophoneUsageDescription
```

The provided build script adds this metadata to the generated app so macOS can request microphone permission.

After packaging, grant microphone access to **Deckhand** under:

**System Settings → Privacy & Security → Microphone**

### Voice and FLAC in the Packaged App

The packaged Python environment contains the application's Python dependencies, but native operating-system resources such as microphone permissions are still controlled by the OS.

For macOS voice recognition, the application expects a native FLAC converter. The provided build/install workflow installs Homebrew FLAC and the application code prefers the native Homebrew FLAC executable on Apple Silicon.

---

# Standalone Application Model

PyInstaller packages the Python runtime and the Python dependencies used by the application into the application bundle.

Therefore, a user running:

```text
/Applications/Deckhand.app
```

does not need to separately install:

- Python.
- PyQt5.
- Paramiko.
- SpeechRecognition.
- PyAudio.

The following are still external/runtime requirements:

- The operating system itself.
- OS-level permissions such as microphone access.
- Internet access when using Google Web Speech.
- Access to AWS/Kubernetes systems and the required SSH credentials.
- The configured AI provider's API availability and quota when using OpsMind.

---

# Notes

- Keep `VM_Visualizer.icns` in the project root when using the manual macOS PyInstaller command.
- Use `build_app.sh` when you want the complete cross-platform dependency/build flow.
- Delete `build/` and `dist/` when troubleshooting packaging issues, then rebuild from scratch.
- New modules that use dynamic imports may require additional PyInstaller hidden-import or collection options.
- OpsMind operations are validated against allow-listed actions/resources before any `kubectl` command is constructed.
- The AI never directly supplies executable shell commands.
- Relative scaling is calculated from the live Kubernetes replica count immediately before the scale operation.
- Operation history is persisted locally and is used for contextual follow-up requests.
- General conversational/status questions supported by OpsMind are handled locally when possible, so they do not unnecessarily consume AI provider quota.

---

# Example OpsMind Commands

```text
scale deployment my-app to 5
```

```text
scale up my-app by 2
```

```text
scale down my-app by 1
```

```text
scale it back
```

```text
restart deployment my-app
```

```text
describe pod my-app-123
```

```text
get service my-service
```

```text
rollout status deployment my-app
```

Voice commands can use the same natural-language requests.

---

# License

Add the project's license information here if/when a license is formally assigned.
