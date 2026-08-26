#!/usr/bin/env bash
set -euo pipefail

REPO="https://github.com/HareeshGT/VM-Visualizer.git"
DIR="VM-Visualizer"

OS="$(uname -s)"

echo "Detected OS: $OS"

# --------------------------------------------------
# Windows: Relaunch as Administrator if needed
# --------------------------------------------------
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then

    if ! net session >/dev/null 2>&1; then
        echo
        echo "Administrator privileges are required."
        echo "Requesting elevation..."
        echo

        SCRIPT="$(cygpath -w "$0")"

        powershell.exe -NoProfile -ExecutionPolicy Bypass \
            -Command "Start-Process 'C:\Program Files\Git\bin\bash.exe' -ArgumentList '\"$SCRIPT\"' -Verb RunAs"

        exit 0
    fi
fi

# --------------------------------------------------
# Clone / Update Repository
# --------------------------------------------------

if [ -d "$DIR/.git" ]; then
    echo "Repository already exists. Updating..."
    cd "$DIR"
    git pull
    echo "Git status..."
    git status
else
    git clone "$REPO" "$DIR"
    cd "$DIR"
    echo "Git status..."
    git status
fi

# --------------------------------------------------
# Find Python
# --------------------------------------------------

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "Python not found."
    exit 1
fi

echo "Using Python: $PYTHON"

# --------------------------------------------------
# Install PyInstaller
# --------------------------------------------------

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "Installing PyInstaller..."
    "$PYTHON" -m pip install --upgrade pip
    "$PYTHON" -m pip install pyinstaller
fi

# --------------------------------------------------
# Install Requirements
# --------------------------------------------------

if [ -f requirements.txt ]; then
    echo "Installing requirements..."
    "$PYTHON" -m pip install -r requirements.txt
fi

# --------------------------------------------------
# Select Icon
# --------------------------------------------------

ICON=""

case "$OS" in
    Darwin)
        [ -f VM_Visualizer.icns ] && ICON="VM_Visualizer.icns"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        [ -f VM_Visualizer.ico ] && ICON="VM_Visualizer.ico"
        ;;
esac

# --------------------------------------------------
# Build
# --------------------------------------------------

echo
echo "Building application..."

CMD=(
    "$PYTHON"
    -m
    PyInstaller
    --windowed
    --onedir
    --name
    "EC2 Manager"
    --hidden-import=paramiko
    --collect-all=paramiko
)

if [ -n "$ICON" ]; then
    CMD+=(--icon="$ICON")
fi

CMD+=(main.py)

"${CMD[@]}"

# --------------------------------------------------
# Install
# --------------------------------------------------

case "$OS" in

Darwin)

    echo
    echo "Installing on macOS..."

    sudo rm -rf "/Applications/EC2 Manager.app"
    sudo cp -R "dist/EC2 Manager.app" "/Applications/"
    ;;

Linux)

    echo
    echo "Installing on Linux..."

    sudo rm -rf "/opt/EC2 Manager"
    sudo mkdir -p "/opt/EC2 Manager"
    sudo cp -R dist/* "/opt/EC2 Manager"
    ;;

MINGW*|MSYS*|CYGWIN*)

    echo
    echo "Installing on Windows..."

    INSTALL_DIR="/c/Program Files/EC2 Manager"

    rm -rf "$INSTALL_DIR"

    mkdir -p "$INSTALL_DIR"

    cp -R dist/* "$INSTALL_DIR"

    echo
    echo "Installed to:"
    echo "$INSTALL_DIR"
    ;;

*)

    echo "Unsupported operating system."
    exit 1
    ;;

esac

# --------------------------------------------------
# Cleanup
# --------------------------------------------------

cd ..

rm -rf "$DIR"

echo
echo "=========================================="
echo "EC2 Manager installed successfully!"
echo "=========================================="


