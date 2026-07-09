# EC2 Manager

**EC2 Manager** (also referred to as *VM Visualizer*) is a cross-platform desktop GUI application, built with PyQt5, for managing AWS EC2 instances and Kubernetes clusters. It combines SSH/SFTP-based remote file management, a built-in file editor, an integrated terminal emulator, and a dedicated Kubernetes management tab into a single dark-themed, Finder-style interface.

## Features

- **EC2 Instance Management** — Browse and connect to AWS EC2 instances directly from the app.
- **Remote File Manager** — A dark-themed, macOS Finder-style file browser over SFTP, with a sidebar for dynamic remote path navigation, breadcrumb navigation, file previews, and right-click context menus.
- **User-Context Switching** — Filesystem operations can be routed through `sudo`-prefixed SSH exec commands, allowing actions to be performed as a different remote user within the same SSH/paramiko session.
- **File Editor** — Edit remote files directly within the app without needing to download them manually first.
- **File Executor** — Run remote files/scripts and stream their execution output back into the app in real time.
- **Integrated Terminal Emulator** — A built-in terminal for direct shell access to remote hosts, including proper handling of rich text/ANSI output for tools like `kubectl`.
- **Kubernetes Management Tab** — View and manage Kubernetes resources (Pods, Config/Secrets, etc.) across clusters (AKS/EKS), including:
  - A port-tunneling feature with a CSV-driven service list and one-click local SSH tunnels to remote services.
  - Support for multiple environments (dev, test, and production/MCP clusters).
- **Theming** — A `ThemePicker` widget for switching between UI themes.
- **Downloads** — Remote files download to `~/Downloads/` by default.

## Project Structure

The application is organized into modular components:

| File | Purpose |
|---|---|
| `main.py` | Application entry point |
| `main_window.py` | Main application window and overall UI layout |
| `dialogs.py` | Dialog windows, including `FileEditorDialog` and `FileExecutorDialog` |
| `kubernetes_tab.py` | Kubernetes management tab (pods, config/secrets, tunneling) |
| `sudo_fs.py` | `SudoFS` class — routes filesystem operations through `sudo`-prefixed SSH commands |
| `workers.py` | Background worker threads for long-running/async operations |
| `themes.py` | Application theming and the `ThemePicker` widget |
| `utils.py` | Shared utility/helper functions |
| `sidebar.py` | Sidebar navigation with dynamic remote path probing |
| `preview.py` | File preview pane |
| `file_widgets.py` | Custom widgets used in the file manager |
| `terminal_widget.py` | Integrated terminal emulator widget |

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
