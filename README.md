# EC2 Manager

**EC2 Manager** (also referred to as *VM Visualizer*) is a cross-platform desktop GUI application, built with PyQt5, for managing AWS EC2 instances and Kubernetes clusters. It combines SSH/SFTP-based remote file management, a built-in code editor, an integrated terminal emulator, a live VM/Kubernetes dashboard, and a full Kubernetes management tab into a single dark-themed, Finder-style interface.

## Features

### Connections & security
- **EC2 Instance Management** — Connect to AWS EC2 instances over SSH/SFTP via `ConnectDialog`, with a "recent instances" list (host, port, user, PEM key, alias) persisted to CSV.
- **User-Context Switching** — Filesystem operations can be routed through `sudo`-prefixed SSH exec commands (`SudoFS`), allowing actions to be performed as a different remote user within the same SSH/paramiko session.
- **App Lock** — An optional PIN lock (`security.py`, `lock_screen.py`) protects the app itself: PBKDF2-SHA256 hashed PIN, lock-on-launch, and auto-lock after a configurable idle timeout (`InactivityWatcher`). The lock screen animates a ring-to-checkmark success state on a correct PIN and shows a clear error (without closing) on an incorrect one.

### Remote File Manager
- A dark-themed, macOS Finder-style file browser over SFTP, with list and grid views (`FileRowWidget` / `FileGridWidget`), a sidebar with dynamic remote path probing, breadcrumb navigation, a preview pane, and right-click context menus.
- **File Editor** — Edit remote files in place (`FileEditorDialog`) without downloading them manually, with a line-numbered gutter, current-line highlighting, zoom, find/replace, and lightweight syntax highlighting for common languages (`editor_widgets.py`).
- **File Executor** — Run remote scripts/executables and stream their output back into the app in real time, including PTY-backed interactive input (`FileExecDialog`, `_ExecStreamWorker`).
- **Media Player** — Stream and play remote audio/video files directly in the app via a local range-request HTTP relay (`MediaPlayerDialog`, `MediaStreamServer`).
- **Remote Search** — Grep/find-based search across the remote filesystem (`SearchDialog`).
- **File Transfers** — Non-blocking upload/download with live speed, ETA, and cancel support (`FileTransferDialog`, `_TransferWorker`/`ScpTransferWorker`).

### Integrated Terminal
- A single scrolling terminal surface (`TerminalWidget`) where the prompt and its output share one view, just like a real terminal — including command history and Ctrl+C interrupt support. Commands are executed one at a time over the existing SSH session (not a full PTY/VT100 emulator, so curses apps like `vim`/`top` aren't supported).

### Live Dashboard
- `DashboardTab` shows the connected instance's specs (hostname/OS/kernel, CPU, RAM, disk) and, when a cluster is reachable, a one-row-per-node table (CPU %, memory %, and kubelet MemoryPressure/DiskPressure/PIDPressure conditions).
- Double-clicking a node opens a separate `NodeDetailWindow` listing the pods scheduled on it, with per-pod CPU/memory usage, restart count, and ready state.
- Polling only runs while the dashboard is both connected and the active tab, so it does zero SSH round-trips when unattended.

### Kubernetes Management Tab
`KubernetesTab` provides a dedicated sub-tab for each resource type, all card-based (`k8s_cards.py`) rather than flat tree views:
- **Pods**, **Deployments**, **StatefulSets**, **DaemonSets**, **HPA**, **Services**, **Ingress**, **Jobs & CronJobs**, **Storage** (PVCs/PVs), **Config & Secrets**, **Events**
- **Terminal** — an in-tab `kubectl`-ready terminal
- **Tunnels** — a CSV-driven service list (`load_tunnel_services`) with one-click local `kubectl port-forward` tunnels, tunnel status auto-refresh, and a `ManageTunnelServicesDialog` for editing the service list
- Pod actions include live log streaming (`LogViewerDialog`) and exec-into-container (`ExecDialog`, with `ContainerPickerDialog` for multi-container pods)
- Individual Kubernetes sub-tabs can be hidden per-user from Settings

### Theming & Settings
- Multiple built-in color themes, switchable via a visual swatch-based `ThemePicker` (not a plain dropdown).
- `SettingsDialog` covers app-lock security (enable/disable, set/change PIN, auto-lock interval) and Kubernetes sub-tab visibility.
- Settings persist to a local `settings.json` merged across all setting groups (`themes.load_settings` / `save_settings`).

### Downloads
- Remote files download to `~/Downloads/` by default.

## Project Structure

The application is organized into modular components:

| File | Purpose |
|---|---|
| `main.py` | Application entry point — builds the Qt app/palette, shows the splash-to-window crossfade, then the main window |
| `main_window.py` | `EC2FileManager` — the main application window, tab layout (File Manager / Kubernetes / Dashboard), and file-manager orchestration |
| `dashboard_tab.py` | Live VM + Kubernetes node dashboard tab (`DashboardTab`, `NodeDetailWindow`) |
| `kubernetes_tab.py` | `KubernetesTab` — pods, deployments, workloads, storage, config/secrets, events, terminal, and tunneling sub-tabs |
| `k8s_cards.py` | Card-style row widgets for every Kubernetes resource list (pods, deployments, services, ingress, jobs, PV/PVC, HPA, events, etc.) |
| `dialogs.py` | Modal dialogs: Connect, Connecting, FileTransfer, LogViewer, ContainerPicker, Exec, FileEditor, FileExec, MediaPlayer, Search, ManageTunnelServices |
| `editor_widgets.py` | `CodeEditor` (gutter line numbers, current-line highlight, zoom) and a dependency-free `SyntaxHighlighter` used by `FileEditorDialog` |
| `lock_screen.py` | `AppLockDialog` (PIN entry at launch/after inactivity, with success-ring/checkmark animation) and `SetPinDialog` |
| `security.py` | PIN hashing (salted PBKDF2-SHA256), persisted lock settings, and `InactivityWatcher` for auto-lock |
| `settings_dialog.py` | App-wide Settings dialog: security/app-lock and Kubernetes tab visibility |
| `sudo_fs.py` | `SudoFS` — routes filesystem operations through `sudo`-prefixed SSH commands |
| `workers.py` | Background `QThread` workers: SSH connect/health checks, one-shot commands, file streaming/transfers, media streaming server |
| `themes.py` | Theme palette definitions, QSS builder, and settings load/save |
| `theme_picker.py` | Visual swatch-based `ThemePicker` widget (replaces a plain theme dropdown) |
| `progress_ring.py` | `CircularProgress` — animated circular ("donut") progress indicator used in the dashboard and lock screen |
| `utils.py` | File-type classification, size formatting, recent-instances CSV, monospace-font detection, tunnel-services CSV loading |
| `sidebar.py` | Sidebar navigation with dynamic remote path probing |
| `preview.py` | `PreviewPane` — file preview pane (right column of the file manager) |
| `file_widgets.py` | `FileRowWidget` / `FileGridWidget` — per-item widgets for the file manager's list and grid views |
| `terminal_widget.py` | `TerminalWidget` — the integrated terminal emulator surface |

## Requirements
- Python 3
- PyQt5
- [paramiko](https://www.paramiko.org/) (for SSH/SFTP connectivity)

Install dependencies, for example:

```bash
pip install PyQt5 paramiko
```

## Running from Source

```bash
python3 main.py
```

## Building a Standalone macOS App (.app)

The application can be packaged into a standalone macOS `.app` bundle using [PyInstaller](https://pyinstaller.org/). Run the following command from the project root:

```bash
python3 -m PyInstaller \
    --windowed \
    --onedir \
    --icon=VM_Visualizer.icns \
    --name="EC2 Manager" \
    --hidden-import=paramiko \
    --collect-all=paramiko \
    main.py
```

### Command breakdown

| Flag | Description |
|---|---|
| `--windowed` | Suppresses the terminal/console window, so the app launches as a standard GUI application (no background console window). |
| `--onedir` | Bundles the app as a single directory (rather than a single file), producing a `.app` bundle under `dist/EC2 Manager.app`. This generally results in faster startup than `--onefile`. |
| `--icon=VM_Visualizer.icns` | Sets the custom application icon. The `.icns` file must be present in the working directory (or path specified). |
| `--name="EC2 Manager"` | Sets the name of the generated app bundle and executable to `EC2 Manager`. |
| `--hidden-import=paramiko` | Explicitly tells PyInstaller to include the `paramiko` module, in case its import isn't automatically detected through static analysis. |
| `--collect-all=paramiko` | Collects all submodules, data files, and binaries associated with `paramiko`, ensuring SSH/SFTP functionality works correctly in the packaged app (paramiko has dynamic imports that PyInstaller can otherwise miss). |
| `main.py` | The application entry point script that PyInstaller builds from. |

After the build completes, the packaged application will be located at:

```
dist/EC2 Manager.app
```

You can then move this `.app` bundle to `/Applications` or run it directly by double-clicking it in Finder.

### Notes

- Make sure `VM_Visualizer.icns` is in the same directory as `main.py` (or update the path in the command accordingly) before building.
- If you add new dependencies with dynamic imports (similar to `paramiko`), you may need additional `--hidden-import` or `--collect-all` flags for those packages as well.
- Build artifacts are placed in the `build/` and `dist/` directories; the `build/` directory can be safely deleted after packaging, and `dist/EC2 Manager.app` is the final distributable output.
