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

    echo
    echo "Git status..."
    git status
else
    git clone "$REPO" "$DIR"
    cd "$DIR"

    echo
    echo "Git status..."
    git status
fi

# --------------------------------------------------
# Find / Install Python
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

    echo
    echo "Checking Homebrew..."

    if ! command -v brew >/dev/null 2>&1; then
        echo
        echo "Homebrew is required on macOS."
        echo
        echo "Install Homebrew from:"
        echo "https://brew.sh/"
        echo
        exit 1
    fi

    echo "Homebrew: $(brew --version | head -n 1)"

    # Always use Homebrew Python 3.14.
    if ! brew list --formula python@3.14 >/dev/null 2>&1; then
        echo
        echo "Installing Python 3.14..."
        brew install python@3.14
    else
        echo
        echo "Python 3.14 already installed."
    fi

    PYTHON="$(brew --prefix python@3.14)/bin/python3"

    if [ ! -x "$PYTHON" ]; then
        echo
        echo "ERROR: Homebrew Python 3.14 was not found:"
        echo "$PYTHON"
        exit 1
    fi

    # Make Homebrew tools available to child processes as well.
    export PATH="$(brew --prefix python@3.14)/bin:$(brew --prefix)/bin:$PATH"

elif command -v python3 >/dev/null 2>&1; then

    PYTHON="$(command -v python3)"

elif command -v python >/dev/null 2>&1; then

    PYTHON="$(command -v python)"

else

    echo
    echo "Python not found."
    exit 1

fi

echo
echo "Using Python:"
echo "$PYTHON"
"$PYTHON" --version

# --------------------------------------------------
# Install Native Dependencies
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

    echo
    echo "Installing macOS native dependencies..."

    # PyAudio -> PortAudio
    if ! brew list --formula portaudio >/dev/null 2>&1; then
        echo "Installing PortAudio..."
        brew install portaudio
    else
        echo "PortAudio already installed."
    fi

    # SpeechRecognition -> FLAC
    #
    # This is especially important on Apple Silicon.
    # SpeechRecognition can otherwise fall back to its bundled flac-mac
    # executable, which can be Intel-only.
    if ! brew list --formula flac >/dev/null 2>&1; then
        echo "Installing FLAC..."
        brew install flac
    else
        echo "FLAC already installed."
    fi

    export PATH="$(brew --prefix)/bin:$PATH"

    # Help PyAudio find Homebrew PortAudio headers/libraries.
    export CPPFLAGS="${CPPFLAGS:-} -I$(brew --prefix portaudio)/include"
    export LDFLAGS="${LDFLAGS:-} -L$(brew --prefix portaudio)/lib"
    export PKG_CONFIG_PATH="${PKG_CONFIG_PATH:-}:$(brew --prefix portaudio)/lib/pkgconfig"

elif [[ "$OS" == "Linux" ]]; then

    echo
    echo "Checking Linux native dependencies..."

    # PyAudio needs PortAudio development headers.
    if command -v apt-get >/dev/null 2>&1; then

        echo "Using apt-get..."

        sudo apt-get update
        sudo apt-get install -y \
            portaudio19-dev \
            libportaudiocpp0 \
            flac \
            ffmpeg

    elif command -v dnf >/dev/null 2>&1; then

        echo "Using dnf..."

        sudo dnf install -y \
            portaudio-devel \
            flac \
            ffmpeg

    elif command -v yum >/dev/null 2>&1; then

        echo "Using yum..."

        sudo yum install -y \
            portaudio-devel \
            flac \
            ffmpeg

    elif command -v pacman >/dev/null 2>&1; then

        echo "Using pacman..."

        sudo pacman -Sy --noconfirm \
            portaudio \
            flac \
            ffmpeg

    else

        echo
        echo "WARNING: Could not determine Linux package manager."
        echo "Make sure PortAudio and FLAC are installed manually."

    fi

elif [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then

    echo
    echo "Windows detected."
    echo "Python wheels will provide the required Python dependencies."

fi

# --------------------------------------------------
# Pip Configuration
# --------------------------------------------------

echo
echo "Checking pip..."

if [[ "$OS" == "Darwin" ]]; then
    "$PYTHON" -m pip --version
else
    "$PYTHON" -m pip --version
fi

# --------------------------------------------------
# Install Python Requirements
# --------------------------------------------------

if [ -f requirements.txt ]; then

    echo
    echo "Installing Python requirements..."

    if [[ "$OS" == "Darwin" ]]; then

        "$PYTHON" -m pip install \
            --break-system-packages \
            -r requirements.txt

    else

        "$PYTHON" -m pip install \
            -r requirements.txt

    fi

else

    echo
    echo "ERROR: requirements.txt not found."
    exit 1

fi

# --------------------------------------------------
# Verify Critical Dependencies
# --------------------------------------------------

echo
echo "Verifying Python dependencies..."

"$PYTHON" - <<'PY'
import sys

print("Python:", sys.executable)

required = [
    ("PyQt5", "PyQt5"),
    ("paramiko", "paramiko"),
    ("SpeechRecognition", "speech_recognition"),
    ("PyAudio", "pyaudio"),
    ("PyInstaller", "PyInstaller"),
]

failed = []

for label, module in required:
    try:
        imported = __import__(module)
        version = getattr(imported, "__version__", "installed")
        print(f"✓ {label}: {version}")
    except Exception as exc:
        print(f"✗ {label}: {exc}")
        failed.append(label)

if failed:
    print()
    print("Missing/broken dependencies:")
    for name in failed:
        print(" -", name)
    sys.exit(1)

print()
print("All Python dependencies are available.")
PY

# --------------------------------------------------
# Verify FLAC on macOS/Linux
# --------------------------------------------------

if [[ "$OS" == "Darwin" || "$OS" == "Linux" ]]; then

    echo
    echo "Verifying FLAC..."

    if ! command -v flac >/dev/null 2>&1; then
        echo
        echo "ERROR: FLAC executable was not found."
        exit 1
    fi

    echo "FLAC: $(command -v flac)"
    flac --version | head -n 1

fi

# --------------------------------------------------
# Install PyInstaller
# --------------------------------------------------

if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then

    echo
    echo "Installing PyInstaller..."

    if [[ "$OS" == "Darwin" ]]; then

        "$PYTHON" -m pip install \
            --break-system-packages \
            "pyinstaller>=6.0"

    else

        "$PYTHON" -m pip install \
            "pyinstaller>=6.0"

    fi

fi

# --------------------------------------------------
# Select Icon
# --------------------------------------------------

ICON=""

case "$OS" in

    Darwin)
        if [ -f VM_Visualizer.icns ]; then
            ICON="VM_Visualizer.icns"
        fi
        ;;

    MINGW*|MSYS*|CYGWIN*)
        if [ -f VM_Visualizer.ico ]; then
            ICON="VM_Visualizer.ico"
        fi
        ;;

esac

# --------------------------------------------------
# Clean Previous Build
# --------------------------------------------------

echo
echo "Cleaning previous PyInstaller build..."

rm -rf build
rm -rf "dist/EC2 Manager.app"
rm -rf "dist/EC2 Manager"

# Remove stale spec so the generated build always reflects
# the current project state.
rm -f "EC2 Manager.spec"

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
    --osx-bundle-identifier
    "com.hareeshgt.ec2manager"

    # Existing SSH/Paramiko support
    --hidden-import=paramiko
    --collect-all=paramiko

    # AI provider support
    --hidden-import=ai_assist

    # Kubernetes OpsMind
    --hidden-import=k8s_ai_ops

    # Voice input
    --hidden-import=speech_recognition
    --hidden-import=pyaudio

    main.py
)

# Add the icon if available.
if [ -n "$ICON" ]; then
    CMD=(
        "${CMD[@]:0:${#CMD[@]}-1}"
        "--icon=$ICON"
        "main.py"
    )
fi

"${CMD[@]}"

# --------------------------------------------------
# macOS privacy + Apple Silicon voice support
# --------------------------------------------------

if [[ "$OS" == "Darwin" ]]; then

    APP_PATH="dist/EC2 Manager.app"
    APP_PLIST="$APP_PATH/Contents/Info.plist"

    # Finder-launched apps need an explicit microphone usage description.
    # Without NSMicrophoneUsageDescription, macOS may not present the
    # microphone permission prompt and the application may not appear under
    # System Settings -> Privacy & Security -> Microphone.
    echo
    echo "Configuring macOS microphone permission..."

    if [ ! -f "$APP_PLIST" ]; then
        echo
        echo "ERROR: App Info.plist not found:"
        echo "$APP_PLIST"
        exit 1
    fi

    /usr/libexec/PlistBuddy         -c "Delete :NSMicrophoneUsageDescription"         "$APP_PLIST" 2>/dev/null || true

    /usr/libexec/PlistBuddy         -c "Add :NSMicrophoneUsageDescription string 'EC2 Manager uses the microphone for Kubernetes voice commands.'"         "$APP_PLIST"

    # Give the application a stable bundle identifier.
    /usr/libexec/PlistBuddy         -c "Delete :CFBundleIdentifier"         "$APP_PLIST" 2>/dev/null || true

    /usr/libexec/PlistBuddy         -c "Add :CFBundleIdentifier string 'com.hareeshgt.ec2manager'"         "$APP_PLIST"

    echo "Microphone usage description added."
    echo "Bundle identifier: com.hareeshgt.ec2manager"

    # PyInstaller may have signed the bundle before Info.plist was changed.
    # Re-sign the completed bundle so the final application has a consistent
    # code signature after the privacy metadata update.
    echo
    echo "Re-signing macOS application..."

    codesign         --deep         --force         --sign -         "$APP_PATH"

    echo "Application re-signed."

    # The application is launched from Finder, so its PATH cannot be
    # assumed to contain Homebrew's bin directory.
    #
    # The Python application itself adds /opt/homebrew/bin to PATH before
    # speech recognition, while this check verifies that native FLAC is
    # available during installation.
    if [ -x "$(brew --prefix)/bin/flac" ]; then
        echo
        echo "Using native Homebrew FLAC:"
        echo "$(brew --prefix)/bin/flac"
    else
        echo
        echo "WARNING: Homebrew FLAC was not found."
    fi

    # Verify the privacy key survived the final bundle/signing step.
    MICROPHONE_DESC=$(
        /usr/libexec/PlistBuddy             -c "Print :NSMicrophoneUsageDescription"             "$APP_PLIST" 2>/dev/null || true
    )

    if [ -z "$MICROPHONE_DESC" ]; then
        echo
        echo "ERROR: NSMicrophoneUsageDescription was not added."
        exit 1
    fi

    echo
    echo "Microphone permission metadata verified:"
    echo "$MICROPHONE_DESC"

fi

# --------------------------------------------------
# Install
# --------------------------------------------------

case "$OS" in

Darwin)

    echo
    echo "Installing on macOS..."

    APP_PATH="dist/EC2 Manager.app"

    if [ ! -d "$APP_PATH" ]; then
        echo
        echo "ERROR: PyInstaller did not create:"
        echo "$APP_PATH"
        exit 1
    fi

    sudo rm -rf "/Applications/EC2 Manager.app"
    sudo cp -R "$APP_PATH" "/Applications/"

    echo
    echo "Installed:"
    echo "/Applications/EC2 Manager.app"
    ;;

Linux)

    echo
    echo "Installing on Linux..."

    if [ ! -d "dist/EC2 Manager" ]; then
        echo
        echo "ERROR: PyInstaller did not create:"
        echo "dist/EC2 Manager"
        exit 1
    fi

    sudo rm -rf "/opt/EC2 Manager"
    sudo mkdir -p "/opt/EC2 Manager"
    sudo cp -R "dist/EC2 Manager/." "/opt/EC2 Manager/"

    echo
    echo "Installed:"
    echo "/opt/EC2 Manager"
    ;;

MINGW*|MSYS*|CYGWIN*)

    echo
    echo "Installing on Windows..."

    INSTALL_DIR="/c/Program Files/EC2 Manager"

    if [ ! -d "dist/EC2 Manager" ]; then
        echo
        echo "ERROR: PyInstaller did not create:"
        echo "dist/EC2 Manager"
        exit 1
    fi

    rm -rf "$INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"

    cp -R "dist/EC2 Manager/." "$INSTALL_DIR/"

    echo
    echo "Installed to:"
    echo "$INSTALL_DIR"
    ;;

*)

    echo
    echo "Unsupported operating system:"
    echo "$OS"
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
echo
echo "Included:"
echo "  ✓ Kubernetes OpsMind"
echo "  ✓ Google Web Speech voice input"
echo "  ✓ PyAudio microphone support"
echo "  ✓ Native FLAC support"
echo "  ✓ AI operation history"
echo