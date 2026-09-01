"""k8s_ai_ops.py — AI-powered Kubernetes command operations.

The AI interprets natural-language Kubernetes requests into a small,
strictly validated action schema. The AI never supplies an executable
shell command; this module constructs the kubectl command after validation.

Operation history is persisted through themes.load_settings()/save_settings()
under the ``k8s_ai_ops_history`` settings key, so recent operations survive
application restarts and can be supplied as context to the AI.

Scale operations support two modes:
  - "absolute": AI supplies an exact target replica count.
  - "relative": AI supplies a signed delta ("scale up by 2", "scale down by
    1"). The current replica count is read from the cluster first, and the
    final target is computed as current + delta (clamped to [0, 100]).
"""

import json
import re
import threading
import time
from datetime import datetime
import os
import sys
import shlex

from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QMessageBox,
    QShortcut,
    QProgressBar,
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QGraphicsBlurEffect

import ai_assist
from themes import T, load_settings, save_settings
from workers import CommandWorker, track_worker
from utils import monospace_font


# Classic Google Web Speech API through SpeechRecognition.
# This is deliberately NOT an LLM/AI transcription service.
try:
    import speech_recognition as sr
    _VOICE_AVAILABLE = True
except ImportError:
    sr = None
    _VOICE_AVAILABLE = False

GOOGLE_STT_LANGUAGE = "en-IN"
VOICE_MAX_SECONDS = 90
VOICE_AMBIENT_CALIBRATION_SECONDS = 0.4


ALLOWED_ACTIONS = {
    "scale",
    "restart",
    "delete",
    "get",
    "describe",
    "rollout_status",
}

ALLOWED_RESOURCES = {
    "deployment",
    "statefulset",
    "daemonset",
    "pod",
    "service",
    "ingress",
    "configmap",
    "secret",
    "job",
    "cronjob",
    "hpa",
    "pvc",
    "pv",
}

SCALE_RESOURCES = {"deployment", "statefulset"}
RESTART_RESOURCES = {"deployment", "statefulset", "daemonset"}
DELETE_RESOURCES = {
    "pod", "deployment", "statefulset", "daemonset", "service", "ingress",
    "configmap", "secret", "job", "cronjob", "hpa", "pvc",
}
READ_RESOURCES = ALLOWED_RESOURCES

MIN_REPLICAS = 0
MAX_REPLICAS = 100

MAX_PROMPT_CHARS = 4000
MAX_HISTORY = 40
HISTORY_CONTEXT_ITEMS = 12

# Namespaces matching any of these patterns (case-insensitive substring
# match) are treated as sensitive: AI-driven "scale" operations targeting
# them require an explicit confirmation, the same way "delete" always
# does. Configurable from Settings → Kubernetes Tabs, persisted under the
# "k8s_protected_namespaces" settings key so it survives restarts.
DEFAULT_PROTECTED_NAMESPACE_PATTERNS = ["prod", "production", "mcp"]


# ---------------------------------------------------------------------------
# Protected-namespace configuration
# ---------------------------------------------------------------------------

def get_protected_namespaces() -> list:
    """Returns the configured list of protected-namespace substrings
    (lower-cased, empties dropped), falling back to the built-in default
    the first time this is called (i.e. nothing saved yet)."""
    try:
        stored = load_settings().get("k8s_protected_namespaces")
        if stored is None:
            return list(DEFAULT_PROTECTED_NAMESPACE_PATTERNS)
        if not isinstance(stored, list):
            return list(DEFAULT_PROTECTED_NAMESPACE_PATTERNS)
        cleaned = [str(p).strip().lower() for p in stored if str(p).strip()]
        return cleaned
    except Exception:
        return list(DEFAULT_PROTECTED_NAMESPACE_PATTERNS)


def save_protected_namespaces(patterns: list):
    cleaned = sorted({str(p).strip().lower() for p in (patterns or []) if str(p).strip()})
    try:
        save_settings(k8s_protected_namespaces=cleaned)
    except Exception:
        pass


def is_protected_namespace(namespace: str) -> bool:
    """True if `namespace` matches any configured protected-namespace
    pattern (case-insensitive substring, e.g. pattern "mcp" matches
    namespace "mcp-prod-eks")."""
    ns = (namespace or "").strip().lower()
    if not ns:
        return False
    return any(pattern in ns for pattern in get_protected_namespaces())


# ---------------------------------------------------------------------------
# Persistent operation history
# ---------------------------------------------------------------------------

def _load_history() -> list:
    try:
        rows = load_settings().get("k8s_ai_ops_history", [])
        if not isinstance(rows, list):
            return []

        cleaned = []
        for row in rows[-MAX_HISTORY:]:
            if not isinstance(row, dict):
                continue
            if not row.get("action") or not row.get("resource") or not row.get("name"):
                continue
            cleaned.append(row)
        return cleaned
    except Exception:
        return []


def _save_history(history: list):
    try:
        save_settings(k8s_ai_ops_history=history[-MAX_HISTORY:])
    except Exception:
        # History should never break Kubernetes operations if settings cannot
        # be persisted for any reason.
        pass


AUDIT_MAX_ENTRIES = 500

def _load_audit_log() -> list:
    try:
        rows = load_settings().get("k8s_audit_log", [])
        return [r for r in rows if isinstance(r, dict)][-AUDIT_MAX_ENTRIES:]
    except Exception:
        return []

def _save_audit_log(rows: list):
    try:
        save_settings(k8s_audit_log=rows[-AUDIT_MAX_ENTRIES:])
    except Exception:
        pass

def _append_audit(action: dict, command: str, status: str, output: str = "",
                  confirmed: bool = False, risk: str = "low"):
    rows = _load_audit_log()
    rows.append({
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "context": action.get("context", ""),
        "namespace": action.get("namespace", "default"),
        "action": action.get("action"),
        "resource": action.get("resource"),
        "name": action.get("name"),
        "command": command,
        "status": status,
        "confirmed": bool(confirmed),
        "risk": risk,
        "output": (output or "")[-1000:],
    })
    _save_audit_log(rows)

def _risk_level(action: dict, context: str, protected: bool) -> str:
    op = action.get("action")
    ctx = (context or "").lower()
    prod = any(x in ctx for x in ("prod", "production"))
    if op == "delete" and (protected or prod):
        return "critical"
    if op == "delete" or (op == "restart" and (protected or prod)):
        return "high"
    if op in {"scale", "restart"}:
        return "medium"
    return "low"


def _history_text(history: list, limit: int = HISTORY_CONTEXT_ITEMS) -> str:
    rows = history[-limit:]
    if not rows:
        return "No previous OpsMind operations are recorded."

    lines = []
    for idx, row in enumerate(rows, 1):
        status = row.get("status", "unknown")
        action = row.get("action", "")
        resource = row.get("resource", "")
        name = row.get("name", "")
        namespace = row.get("namespace", "default")
        timestamp = row.get("timestamp", "")
        replicas = row.get("replicas")

        detail = f"{action} {resource}/{name} in namespace {namespace}"
        if replicas is not None:
            detail += f" replicas={replicas}"
        if row.get("previous_replicas") is not None:
            detail += f" previous_replicas={row['previous_replicas']}"

        lines.append(
            f"{idx}. [{status}] {detail}"
            + (f" at {timestamp}" if timestamp else "")
        )

    return "\n".join(lines)


def _build_k8s_ops_prompt(user_request: str, namespace: str, context: str, history: list) -> str:
    return f"""
You are a Kubernetes operations command interpreter.

Convert the user's natural-language request into EXACTLY ONE safe,
allow-listed Kubernetes operation represented as JSON.

You MUST NOT return a shell command.
You MUST NOT return kubectl arguments.
You MUST NOT return Markdown.
You MUST NOT return explanations outside the JSON object.

Allowed actions:
- scale
- restart
- delete
- get
- describe
- rollout_status

Allowed resources:
- deployment
- statefulset
- daemonset
- pod
- service
- ingress
- configmap
- secret
- job
- cronjob
- hpa
- pvc
- pv

Rules:
1. scale is only valid for deployment or statefulset.
2. scale has two modes:
   a. ABSOLUTE — the user gives an exact target replica count
      (e.g. "scale my-app to 5"). Return "mode":"absolute" and an integer
      "replicas" field from 0 through 100.
   b. RELATIVE — the user gives a change relative to the current replica
      count (e.g. "scale up my-app by 1", "scale down my-app by 3",
      "add 2 replicas to my-app", "remove 1 replica from my-app"). Return
      "mode":"relative" and an integer "delta" field: positive to scale up,
      negative to scale down. Do NOT try to compute the resulting replica
      count yourself — the application resolves the current replica count
      from the cluster and applies the delta.
3. restart is valid for deployment, statefulset, or daemonset.
4. delete is valid only for the supported delete resources.
5. get, describe, and rollout_status are read/status operations.
6. Never invent a resource name.
7. Use the selected namespace unless the user explicitly specifies another.
8. Never return more than one operation.
9. If a request refers to a previous operation, use the operation history below.
10. If the user says "undo" a previous scale operation, return a scale action
    using that operation's previous_replica value when it is available
    (mode "absolute", replicas = previous_replicas). If it is not available,
    return clarification_required.
11. If the user says "scale it back", "restore the previous replicas", or
    similar language, use the most recent applicable scale history entry
    (mode "absolute", replicas = previous_replicas from that entry).
12. If the user says "again", "repeat that", or similar language, repeat the
    most recent applicable successful operation when unambiguous (preserve
    its mode: absolute replicas or relative delta).
13. For "scale up/down" with NO numeric target and NO numeric delta at all,
    return clarification_required. If a numeric delta is given (e.g. "by 2",
    "by one"), use mode "relative" instead of asking for clarification.
14. If the request is unsupported, return unsupported.

Valid absolute-scale response example:
{{"action":"scale","resource":"deployment","name":"my-app","namespace":"test-cc","mode":"absolute","replicas":5}}

Valid relative-scale response example (scale up by 2):
{{"action":"scale","resource":"deployment","name":"my-app","namespace":"test-cc","mode":"relative","delta":2}}

Valid relative-scale response example (scale down by 1):
{{"action":"scale","resource":"deployment","name":"my-app","namespace":"test-cc","mode":"relative","delta":-1}}

Clarification example:
{{"action":"clarification_required","reason":"Please specify the target replica count."}}

Unsupported example:
{{"action":"unsupported","reason":"This Kubernetes operation is not supported by OpsMind."}}

Current selected namespace:
{namespace}

Current UI context:
{context}

Previous OpsMind history:
{_history_text(history)}

User request:
{user_request}
""".strip()


# ---------------------------------------------------------------------------
# Local/general questions
# ---------------------------------------------------------------------------

def _general_question_response(request: str, namespace: str = None):
    """Answer common conversational/system questions locally.

    These responses do not call the AI provider and do not create Kubernetes
    operations. Return None when the request should continue through the
    Kubernetes AI interpreter.
    """

    normalized = re.sub(r"\s+", " ", (request or "").strip().lower())
    if not normalized:
        return None

    # Normalize punctuation and a few common speech-to-text contractions.
    clean = re.sub(r"[?!.,:;]+", "", normalized).strip()
    clean = clean.replace("how's", "how is")
    clean = clean.replace("what's", "what is")
    clean = clean.replace("where's", "where is")
    clean = clean.replace("who's", "who is")

    # --------------------------------------------------
    # Audible / microphone checks
    # --------------------------------------------------

    audible_patterns = (
        "am i audible",
        "can you hear me",
        "can you hear me clearly",
        "can you hear my voice",
        "do you hear me",
        "is my voice audible",
        "is my mic working",
        "is my microphone working",
        "is microphone working",
        "is the microphone working",
        "can you hear what i am saying",
        "can you hear what im saying",
    )

    if any(phrase in clean for phrase in audible_patterns):
        return (
            "Yes — I can hear you. Your voice was captured and "
            "transcribed successfully."
        )

    # --------------------------------------------------
    # Combined greetings / conversational questions
    # --------------------------------------------------

    greeting_words = {
        "hi",
        "hello",
        "hey",
        "hiya",
        "hello there",
        "hi there",
        "hey there",
        "good morning",
        "good afternoon",
        "good evening",
    }

    how_are_you_patterns = (
        "how are you",
        "how are you doing",
        "how is it going",
        "how are things",
        "how have you been",
    )

    # This deliberately handles combinations such as:
    # "hi how are you", "hello how are you doing", etc.
    if any(phrase in clean for phrase in how_are_you_patterns):
        if any(
            clean == greeting
            or clean.startswith(greeting + " ")
            for greeting in greeting_words
        ):
            return (
                "Hi! I'm doing well and ready to help with your "
                "Kubernetes operations."
            )

        return (
            "I'm doing well and ready to help with your Kubernetes operations."
        )

    if clean in greeting_words:
        return "Hello! Tell me what you want to do in Kubernetes."

    # --------------------------------------------------
    # Identity
    # --------------------------------------------------

    identity_patterns = (
        "who are you",
        "what are you",
        "what is your name",
        "tell me who you are",
        "tell me about yourself",
    )

    if any(phrase in clean for phrase in identity_patterns):
        return (
            "I'm the Kubernetes OpsMind assistant in Deckhand. "
            "I can interpret Kubernetes requests and run approved "
            "operations."
        )

    # --------------------------------------------------
    # Capabilities
    # --------------------------------------------------

    capability_patterns = (
        "what can you do",
        "what do you do",
        "what are your capabilities",
        "what can i ask",
        "what do you support",
        "what operations can you do",
        "what kubernetes operations can you do",
        "what can i do here",
    )

    if any(phrase in clean for phrase in capability_patterns):
        return (
            "I can scale deployments and statefulsets, restart supported "
            "workloads, inspect resources, describe resources, check "
            "rollout status, and perform supported delete operations. "
            "You can also use voice commands."
        )

    # --------------------------------------------------
    # Time
    # --------------------------------------------------

    time_patterns = (
        "what time is it",
        "what is the time",
        "whats the time",
        "current time",
        "time now",
        "tell me the time",
        "what time",
        "what is the current time",
        "current local time",
    )

    if any(phrase == clean for phrase in time_patterns):
        now = datetime.now().astimezone()
        zone = now.tzname() or "local time"
        return f"The current time is {now.strftime('%I:%M:%S %p')} {zone}."

    # --------------------------------------------------
    # Date
    # --------------------------------------------------

    date_patterns = (
        "what date is it",
        "whats the date",
        "what is the date",
        "current date",
        "todays date",
        "today date",
        "what day is it",
        "what is today",
        "today",
    )

    if clean in date_patterns:
        now = datetime.now().astimezone()
        return f"Today is {now.strftime('%A, %d %B %Y')}."

    # --------------------------------------------------
    # Current namespace
    # --------------------------------------------------

    namespace_patterns = (
        "what namespace am i in",
        "which namespace am i in",
        "current namespace",
        "what is the current namespace",
        "which namespace is selected",
        "what namespace is selected",
        "what is my namespace",
    )

    if clean in namespace_patterns:
        return (
            f"The currently selected Kubernetes namespace is "
            f"'{namespace or 'default'}'."
        )

    # --------------------------------------------------
    # RTC
    # --------------------------------------------------

    rtc_patterns = (
        "what does rtc mean",
        "what is rtc",
        "define rtc",
        "what is a rtc",
        "what is real time clock",
        "what is a real time clock",
    )

    if clean in rtc_patterns:
        return (
            "RTC usually means Real-Time Clock. It is a hardware clock "
            "used to keep track of the current date and time, even when "
            "the main system is powered off."
        )

    # --------------------------------------------------
    # Thanks / acknowledgement
    # --------------------------------------------------

    thanks_patterns = (
        "thanks",
        "thank you",
        "thank you very much",
        "thanks a lot",
        "ok thanks",
        "okay thanks",
        "great thanks",
    )

    if clean in thanks_patterns:
        return "You're welcome."

    # --------------------------------------------------
    # Goodbye
    # --------------------------------------------------

    goodbye_patterns = (
        "bye",
        "goodbye",
        "see you",
        "see you later",
    )

    if clean in goodbye_patterns:
        return "Goodbye!"

    return None


class K8sAIInterpretWorker(QThread):
    """Ask the configured AI provider to interpret one Kubernetes request."""

    done = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, provider, api_key, model, request_text, namespace, context, history, parent=None):
        super().__init__(parent)
        self._provider = provider
        self._api_key = api_key
        self._model = model
        self._request_text = request_text
        self._namespace = namespace
        self._context = context
        self._history = history
        self.finished.connect(self.deleteLater)

    def run(self):
        prompt = _build_k8s_ops_prompt(
            self._request_text,
            self._namespace,
            self._context,
            self._history,
        )
        text, err = ai_assist._call_provider(
            self._provider,
            self._api_key,
            self._model,
            prompt,
        )
        if err:
            self.error.emit(err)
        else:
            self.done.emit(text)


def _extract_json(text: str):
    text = (text or "").strip()
    if not text:
        raise ValueError("The AI returned an empty response.")

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("The AI response was not valid JSON.")

def _get_flac_path():
    """
    Return the native FLAC executable that should be used by
    SpeechRecognition.

    Finder-launched macOS apps do not inherit the user's shell PATH,
    so do not rely on `shutil.which("flac")` alone.
    """

    if sys.platform == "darwin":
        candidates = [
            "/opt/homebrew/bin/flac",   # Apple Silicon Homebrew
            "/usr/local/bin/flac",      # Intel Homebrew
            "/usr/bin/flac",
        ]
    else:
        candidates = ["flac"]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    return None

def _prepare_system_flac():
    """Make a native FLAC executable available to SpeechRecognition.

    Finder-launched macOS apps do not necessarily inherit the shell PATH.
    Prefer Homebrew's native Apple Silicon FLAC over SpeechRecognition's
    bundled fallback executable.
    """

    if os.name != "posix":
        return

    candidates = [
        "/opt/homebrew/bin/flac",
        "/usr/local/bin/flac",
        "/usr/bin/flac",
    ]

    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            # SpeechRecognition's get_flac_converter() checks PATH first.
            current_path = os.environ.get("PATH", "")
            path_parts = current_path.split(os.pathsep) if current_path else []

            if os.path.dirname(path) not in path_parts:
                os.environ["PATH"] = (
                    os.path.dirname(path)
                    + os.pathsep
                    + current_path
                    if current_path
                    else os.path.dirname(path)
                )

            return

    raise RuntimeError(
        "FLAC converter not found. Expected Homebrew FLAC at "
        "/opt/homebrew/bin/flac. Install it with: brew install flac"
    )

def _mic_level_percent(chunk: bytes, sample_width: int) -> int:
    """Convert a PCM microphone chunk into a 0-100 activity level.

    This is only a visual meter. It does not perform speech recognition.
    """

    if not chunk:
        return 0

    try:
        if sample_width == 2:
            # Signed little-endian 16-bit PCM.
            sample_count = len(chunk) // 2

            if sample_count <= 0:
                return 0

            total = 0

            for i in range(0, sample_count * 2, 2):
                sample = int.from_bytes(
                    chunk[i:i + 2],
                    byteorder="little",
                    signed=True,
                )
                total += sample * sample

            rms = (total / sample_count) ** 0.5

            # Typical speech levels are much lower than full-scale PCM.
            # Compress the raw range into something visually useful.
            level = min(100, int((rms / 9000.0) * 100))

            return max(0, level)

        # Generic fallback for other sample widths.
        max_value = float((1 << (8 * sample_width - 1)) - 1)

        if max_value <= 0:
            return 0

        avg = sum(abs(b - 128) for b in chunk) / len(chunk)

        return max(
            0,
            min(100, int((avg / 64.0) * 100)),
        )

    except Exception:
        return 0


class K8sGoogleSpeechWorker(QThread):
    """Record microphone audio until stopped, then transcribe it with
    SpeechRecognition's classic Google Web Speech recognizer.

    No LLM is used here. Only the resulting text is emitted to the OpsMind
    widget, which then uses the existing AI provider for Kubernetes intent.
    """

    done = pyqtSignal(str)
    error = pyqtSignal(str)
    listening = pyqtSignal()
    stopped = pyqtSignal()
    level = pyqtSignal(int)

    def __init__(
        self,
        language: str = GOOGLE_STT_LANGUAGE,
        max_seconds: int = VOICE_MAX_SECONDS,
        parent=None,
    ):
        super().__init__(parent)
        self._language = language
        self._max_seconds = max_seconds
        self._stop_event = threading.Event()
        self.finished.connect(self.deleteLater)

    def stop(self):
        self._stop_event.set()

    def run(self):
        if not _VOICE_AVAILABLE:
            self.error.emit(
                "Voice input is unavailable because SpeechRecognition/PyAudio "
                "is not installed."
            )
            return

        recognizer = sr.Recognizer()

        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(
                    source,
                    duration=VOICE_AMBIENT_CALIBRATION_SECONDS,
                )

                self.listening.emit()

                frames = []
                started = time.monotonic()

                while (
                    not self._stop_event.is_set()
                    and (time.monotonic() - started) < self._max_seconds
                ):
                    try:
                        chunk = source.stream.read(
                            source.CHUNK,
                            exception_on_overflow=False,
                        )

                        frames.append(chunk)

                        # Send microphone activity to the UI.
                        self.level.emit(
                            _mic_level_percent(
                                chunk,
                                source.SAMPLE_WIDTH,
                            )
                        )

                    except TypeError:
                        # Compatibility with older PyAudio versions.
                        chunk = source.stream.read(source.CHUNK)

                        frames.append(chunk)

                        self.level.emit(
                            _mic_level_percent(
                                chunk,
                                source.SAMPLE_WIDTH,
                            )
                        )
                        
                self.level.emit(0)
                self.stopped.emit()

                if not frames:
                    self.error.emit("No audio was captured.")
                    return

                audio = sr.AudioData(
                    b"".join(frames),
                    source.SAMPLE_RATE,
                    source.SAMPLE_WIDTH,
                )
            flac_path = _get_flac_path()
            if not flac_path:
                self.error.emit(
                    "FLAC executable not found. "
                    "Please install FLAC with: brew install flac"
                )
                return

            # Finder-launched macOS apps do not inherit the terminal PATH.
            # Explicitly force SpeechRecognition to use Homebrew's native FLAC.
            flac_dir = os.path.dirname(flac_path)
            os.environ["PATH"] = (
                flac_dir
                + os.pathsep
                + os.environ.get("PATH", "")
            )

            sr.audio.get_flac_converter = lambda: flac_path

            try:
                text = recognizer.recognize_google(
                    audio,
                    language=self._language,
                )
            except sr.UnknownValueError:
                self.error.emit(
                    "Google Speech could not understand the recording."
                )
                return
            except sr.RequestError as exc:
                self.error.emit(
                    f"Google Speech recognition request failed: {exc}"
                )
                return

            text = (text or "").strip()

            if not text:
                self.error.emit(
                    "Google Speech returned an empty transcription."
                )
                return

            self.done.emit(text)

        except AttributeError:
            self.error.emit(
                "Microphone support is unavailable. Install PyAudio "
                "and SpeechRecognition."
            )
        except Exception as exc:
            self.error.emit(f"Voice input failed: {exc}")


def validate_action(data: dict):
    if not isinstance(data, dict):
        raise ValueError("The AI returned an invalid operation object.")

    action = str(data.get("action", "")).strip().lower()

    if action == "clarification_required":
        return {
            "kind": "clarification",
            "reason": str(data.get(
                "reason",
                "Please provide more details about the Kubernetes operation.",
            )).strip(),
        }

    if action == "unsupported":
        return {
            "kind": "unsupported",
            "reason": str(data.get(
                "reason",
                "This Kubernetes operation is not supported.",
            )).strip(),
        }

    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"AI requested unsupported action: {action or '(missing)'}")

    resource = str(data.get("resource", "")).strip().lower()
    name = str(data.get("name", "")).strip()
    namespace = str(data.get("namespace", "")).strip() or "default"

    if resource not in ALLOWED_RESOURCES:
        raise ValueError(
            f"AI requested unsupported Kubernetes resource: {resource or '(missing)'}"
        )
    if not name:
        raise ValueError("The AI did not provide a Kubernetes resource name.")

    if not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", name, flags=re.IGNORECASE):
        raise ValueError(f"Invalid Kubernetes resource name returned by AI: {name}")
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace, flags=re.IGNORECASE):
        raise ValueError(f"Invalid Kubernetes namespace returned by AI: {namespace}")

    if action == "scale":
        if resource not in SCALE_RESOURCES:
            raise ValueError(f"Cannot scale Kubernetes resource type: {resource}")

        mode = str(data.get("mode", "")).strip().lower()
        if not mode:
            # Backwards-compatible default: a bare "replicas" field with no
            # mode is treated as an absolute target, matching the previous
            # (pre-relative-scale) behaviour.
            mode = "relative" if "delta" in data and "replicas" not in data else "absolute"

        if mode == "relative":
            try:
                delta = int(data.get("delta"))
            except (TypeError, ValueError):
                raise ValueError("The AI did not provide a valid replica delta.")
            if delta == 0:
                raise ValueError("A relative scale delta of 0 has no effect.")
            if abs(delta) > MAX_REPLICAS:
                raise ValueError("Replica delta is out of range.")

            return {
                "kind": "operation",
                "action": action,
                "resource": resource,
                "name": name,
                "namespace": namespace,
                "mode": "relative",
                "delta": delta,
            }

        if mode != "absolute":
            raise ValueError(f"AI requested unsupported scale mode: {mode}")

        try:
            replicas = int(data.get("replicas"))
        except (TypeError, ValueError):
            raise ValueError("The AI did not provide a valid replica count.")
        if replicas < MIN_REPLICAS or replicas > MAX_REPLICAS:
            raise ValueError("Replica count must be between 0 and 100.")

        return {
            "kind": "operation",
            "action": action,
            "resource": resource,
            "name": name,
            "namespace": namespace,
            "mode": "absolute",
            "replicas": replicas,
        }

    if action == "restart" and resource not in RESTART_RESOURCES:
        raise ValueError(f"Cannot restart Kubernetes resource type: {resource}")

    if action == "delete" and resource not in DELETE_RESOURCES:
        raise ValueError(f"Cannot delete Kubernetes resource type: {resource}")

    return {
        "kind": "operation",
        "action": action,
        "resource": resource,
        "name": name,
        "namespace": namespace,
    }


def build_kubectl_command(action: dict) -> str:
    operation = action["action"]
    resource = action["resource"]
    name = action["name"]
    namespace = action["namespace"]
    context = str(action.get("context") or "").strip()
    context_flag = f"--context {shlex.quote(context)} " if context else ""
    base = f"kubectl {context_flag}-n {namespace}"

    if operation == "scale":
        if "replicas" not in action:
            raise ValueError(
                "Cannot build a scale command before the target replica "
                "count has been resolved."
            )
        return f"{base} scale {resource}/{name} --replicas={action['replicas']}"
    if operation == "restart":
        return f"{base} rollout restart {resource}/{name}"
    if operation == "delete":
        return f"{base} delete {resource}/{name}"
    if operation == "get":
        return f"{base} get {resource}/{name}"
    if operation == "describe":
        return f"{base} describe {resource}/{name}"
    if operation == "rollout_status":
        return f"{base} rollout status {resource}/{name}"

    raise ValueError(f"Unsupported operation: {operation}")


def operation_description(action: dict) -> str:
    operation = action["action"]
    resource = action["resource"]
    name = action["name"]
    namespace = action["namespace"]

    if operation == "scale":
        if action.get("mode") == "relative" and "replicas" not in action:
            delta = action.get("delta", 0)
            direction = "up" if delta >= 0 else "down"
            return (
                f'Scale {resource} "{name}" in namespace "{namespace}" '
                f'{direction} by {abs(delta)} replica(s) (relative to current count)'
            )
        return (
            f'Scale {resource} "{name}" in namespace "{namespace}" '
            f'to {action["replicas"]} replica(s)'
        )
    if operation == "restart":
        return f'Restart {resource} "{name}" in namespace "{namespace}"'
    if operation == "delete":
        return f'Delete {resource} "{name}" in namespace "{namespace}"'
    if operation == "get":
        return f'Get {resource} "{name}" in namespace "{namespace}"'
    if operation == "describe":
        return f'Describe {resource} "{name}" in namespace "{namespace}"'
    if operation == "rollout_status":
        return (
            f'Check rollout status of {resource} "{name}" '
            f'in namespace "{namespace}"'
        )
    return f"{operation} {resource}/{name}"


class K8sAIOpsWidget(QWidget):
    """Natural-language Kubernetes operations panel with persistent history."""

    operation_finished = pyqtSignal()

    def __init__(self, ssh=None, namespace_getter=None, context_getter=None,
                 kube_context_getter=None, parent=None):
        super().__init__(parent)
        self.ssh = ssh
        self._namespace_getter = namespace_getter
        # NOTE: context_getter feeds free-text UI context ("Selected pod: ...")
        # into the AI prompt only — it is NOT a kubectl context name and must
        # never be used to build a `--context` flag. kube_context_getter is
        # the actual selected cluster context (e.g. from the context combo)
        # and is what build_kubectl_command()/`--context` needs.
        self._context_getter = context_getter
        self._kube_context_getter = kube_context_getter
        self._workers = []
        self._ai_worker = None
        self._operation_worker = None
        self._scale_previous_worker = None
        self._pending_scale_action = None
        self._voice_worker = None
        self._voice_recording = False
        self._busy = False
        self._history = _load_history()
        self._ai_access_allowed = False
        self._ai_blur_effect = None
        self._ai_lock_overlay = None
        self._build_ui()

    def set_ssh(self, ssh):
        self.ssh = ssh

    def _build_ui(self):
        # Keep the real OpsMind UI inside a separate content widget so the
        # content can be blurred while a sharp access-lock overlay remains
        # readable above it.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._ai_content = QWidget(self)
        outer.addWidget(self._ai_content)

        root = QVBoxLayout(self._ai_content)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("✨  AI Kubernetes Operations")
        title.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 16px; font-weight: 700;"
        )
        title_row.addWidget(title)
        title_row.addStretch()

        self.history_lbl = QLabel()
        self.history_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 11px;"
        )
        title_row.addWidget(self.history_lbl)

        self.status_lbl = QLabel("Ready")
        self.status_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 12px;"
        )
        title_row.addWidget(self.status_lbl)
        root.addLayout(title_row)

        description = QLabel(
            "Describe a Kubernetes operation in plain English. Previous OpsMind "
            "operations are remembered across app restarts."
        )
        description.setWordWrap(True)
        description.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        root.addWidget(description)

        examples = QLabel(
            "Examples: scale deployment deployment-name to 5 replicas   •   "
            "scale up deployment-name by 2   •   scale down deployment-name by 1   •   "
            "scale it back   •   restart deployment deployment-name   •   "
            "repeat that"
        )
        examples.setWordWrap(True)
        examples.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px;")
        root.addWidget(examples)

        self.voice_hint_lbl = QLabel(
            "Voice: Google Web Speech (classic ASR, not an LLM) · "
            "Ctrl+Shift+Space to start/stop"
        )
        self.voice_hint_lbl.setWordWrap(True)
        self.voice_hint_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 10px;"
        )
        root.addWidget(self.voice_hint_lbl)

        self.request_input = QLineEdit()
        self.request_input.setPlaceholderText(
            "Ask AI to perform a Kubernetes operation…"
        )
        self.request_input.returnPressed.connect(self._submit)
        root.addWidget(self.request_input)

        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("✨  Run with AI")
        self.run_btn.setObjectName("primary")
        self.run_btn.setFixedHeight(34)
        self.run_btn.clicked.connect(self._submit)
        btn_row.addWidget(self.run_btn)

        self.voice_btn = QPushButton("🎙  Voice")
        self.voice_btn.setFixedHeight(34)
        self.voice_btn.setToolTip(
            "Start/stop voice command. Hotkey: Ctrl+Shift+Space"
        )
        self.voice_btn.clicked.connect(self._toggle_voice_recording)
        btn_row.addWidget(self.voice_btn)

        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setValue(0)
        self.mic_level_bar.setFixedWidth(120)
        self.mic_level_bar.setFixedHeight(12)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setToolTip("Live microphone input level")
        self.mic_level_bar.setStyleSheet(
            f"""
            QProgressBar {{
                background: {T['BG_ITEM']};
                border: 1px solid {T['BORDER']};
                border-radius: 6px;
            }}

            QProgressBar::chunk {{
                background: {T['ACCENT']};
                border-radius: 5px;
            }}
            """
        )

        self.mic_level_label = QLabel("Mic")
        self.mic_level_label.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 10px;"
        )

        btn_row.addWidget(self.mic_level_label)
        btn_row.addWidget(self.mic_level_bar)

        self.voice_shortcut = QShortcut(
            QKeySequence("Ctrl+Shift+Space"),
            self,
        )
        self.voice_shortcut.activated.connect(self._toggle_voice_recording)

        clear_btn = QPushButton("Clear Output")
        clear_btn.setFixedHeight(34)
        clear_btn.clicked.connect(self._clear_output)
        btn_row.addWidget(clear_btn)

        self.clear_history_btn = QPushButton("Clear History")
        self.clear_history_btn.setFixedHeight(34)
        self.clear_history_btn.clicked.connect(self._clear_history)
        btn_row.addWidget(self.clear_history_btn)

        self.audit_btn = QPushButton("Audit Log")
        self.audit_btn.setFixedHeight(34)
        self.audit_btn.clicked.connect(self._show_audit_log)
        btn_row.addWidget(self.audit_btn)
        btn_row.addStretch()
        root.addLayout(btn_row)

        self.output = QTextBrowser()
        self.output.setOpenExternalLinks(False)
        self.output.setFont(monospace_font(11))
        self.output.setStyleSheet(
            f"background: {T['BG_DARK']}; color: {T['TEXT_PRIMARY']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 12px;"
        )
        root.addWidget(self.output, 1)

        self._update_history_label()
        self._write_info(
            "OpsMind is ready.\n\n"
            "Previous operations are remembered and used as context for follow-up requests.\n"
            "Try: scale deployment my-app to 3 replicas\n"
            "     scale up my-app by 2\n"
            "     scale down my-app by 1\n"
            "     scale it back\n"
            "     repeat that\n"
        )

        self._build_ai_access_overlay()
        self.refresh_ai_access()

    def _build_ai_access_overlay(self):
        """Create the blurred/locked surface shown when no AI API key exists."""
        self._ai_blur_effect = QGraphicsBlurEffect(self._ai_content)
        self._ai_blur_effect.setBlurRadius(9)
        self._ai_content.setGraphicsEffect(self._ai_blur_effect)

        self._ai_lock_overlay = QWidget(self)
        self._ai_lock_overlay.setObjectName("aiOpsLockOverlay")
        self._ai_lock_overlay.setStyleSheet(
            f"QWidget#aiOpsLockOverlay {{ background: rgba(0, 0, 0, 150); }}"
        )

        overlay_layout = QVBoxLayout(self._ai_lock_overlay)
        overlay_layout.setContentsMargins(30, 30, 30, 30)

        card = QWidget(self._ai_lock_overlay)
        card.setObjectName("aiOpsLockCard")
        card.setMaximumWidth(520)
        card.setStyleSheet(
            f"QWidget#aiOpsLockCard {{ "
            f"background: {T['BG_PANEL']}; "
            f"border: 1px solid {T['BORDER']}; "
            f"border-radius: 16px; }}"
        )

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 26, 30, 26)
        card_layout.setSpacing(10)

        icon = QLabel("🔒")
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet("font-size: 34px; background: transparent; border: none;")
        card_layout.addWidget(icon)

        title = QLabel("OpsMind is locked")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 18px; "
            f"font-weight: 700; background: transparent; border: none;"
        )
        card_layout.addWidget(title)

        self._ai_lock_message = QLabel()
        self._ai_lock_message.setAlignment(Qt.AlignCenter)
        self._ai_lock_message.setWordWrap(True)
        self._ai_lock_message.setStyleSheet(
            f"color: {T['TEXT_DIM']}; font-size: 13px; "
            f"background: transparent; border: none;"
        )
        card_layout.addWidget(self._ai_lock_message)

        overlay_layout.addStretch(1)
        overlay_layout.addWidget(card, 0, Qt.AlignCenter)
        overlay_layout.addStretch(1)

        self._ai_lock_overlay.hide()

    def _selected_ai_provider_label(self):
        provider = ai_assist.get_provider()
        return ai_assist.PROVIDERS.get(provider, {}).get("label", provider)

    def refresh_ai_access(self):
        """Enable OpsMind only when the selected provider has an API key."""
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        allowed = bool((api_key or "").strip())
        self._ai_access_allowed = allowed

        if hasattr(self, "_ai_lock_overlay") and self._ai_lock_overlay is not None:
            if allowed:
                self._ai_lock_overlay.hide()
                self._ai_content.setGraphicsEffect(None)
                self.status_lbl.setText("Ready")
            else:
                label = self._selected_ai_provider_label()
                self._ai_lock_message.setText(
                    f"Add an API key for <b>{self._escape_html(label)}</b> "
                    "in <b>Settings → 🤖 AI</b> to access Kubernetes AI Operations."
                )
                self._ai_content.setGraphicsEffect(self._ai_blur_effect)
                self._ai_lock_overlay.raise_()
                self._ai_lock_overlay.show()
                self.status_lbl.setText("API key required")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ai_lock_overlay is not None:
            self._ai_lock_overlay.setGeometry(self.rect())
            self._ai_lock_overlay.raise_()

    def showEvent(self, event):
        super().showEvent(event)
        # Settings may have been changed while another dialog/tab was active.
        # Re-check whenever OpsMind becomes visible so it unlocks without a restart.
        self.refresh_ai_access()

    def _namespace(self) -> str:
        if self._namespace_getter:
            try:
                value = self._namespace_getter()
                return str(value or "default").strip() or "default"
            except Exception:
                pass
        return "default"

    def _context(self) -> str:
        if self._context_getter:
            try:
                value = self._context_getter()
                return str(value or "").strip()
            except Exception:
                pass
        return ""

    def _kube_context(self) -> str:
        """The actually-selected kubectl context (cluster), used for the
        `--context` flag. Distinct from `_context()`, which is a free-text
        UI blurb fed to the AI prompt and must never reach the shell."""
        if self._kube_context_getter:
            try:
                value = self._kube_context_getter()
                return str(value or "").strip()
            except Exception:
                pass
        return ""

    def _update_history_label(self):
        count = len(self._history)
        self.history_lbl.setText(
            f"{count} saved operation{'s' if count != 1 else ''}"
        )

    def _write_info(self, text: str):
        self.output.append(
            f'<span style="color:{T["TEXT_DIM"]}">'
            f'{self._escape_html(text).replace(chr(10), "<br>")}'
            f'</span>'
        )

    def _write_success(self, text: str):
        self.output.append(
            f'<span style="color:{T["SUCCESS"]}">'
            f'{self._escape_html(text).replace(chr(10), "<br>")}'
            f'</span>'
        )

    def _write_error(self, text: str):
        self.output.append(
            f'<span style="color:{T["DANGER"]}">'
            f'{self._escape_html(text).replace(chr(10), "<br>")}'
            f'</span>'
        )

    @staticmethod
    def _escape_html(text: str) -> str:
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.request_input.setEnabled(not busy and not self._voice_recording)
        self.run_btn.setEnabled(not busy and not self._voice_recording)
        self.voice_btn.setEnabled(not busy or self._voice_recording)
        self.clear_history_btn.setEnabled(not busy and not self._voice_recording)
        if busy:
            self.run_btn.setText("✨  Thinking…")
            self.status_lbl.setText("AI is interpreting…")
        else:
            self.run_btn.setText("✨  Run with AI")
            self.status_lbl.setText("Ready")


    # ------------------------------------------------------------------
    # Voice input
    # ------------------------------------------------------------------

    def _toggle_voice_recording(self):
        """Start/stop microphone capture without using an LLM for STT."""

        if not self._ai_access_allowed:
            self.refresh_ai_access()
            return

        if self._voice_recording:
            self._stop_voice_recording()
            return

        if self._busy:
            return

        if not _VOICE_AVAILABLE:
            QMessageBox.information(
                self,
                "Voice Input Unavailable",
                "Install the voice-input dependencies first:\n\n"
                "python3 -m pip install SpeechRecognition PyAudio\n\n"
                "On macOS, install PortAudio first if PyAudio cannot build:\n"
                "brew install portaudio",
            )
            return

        if self._ai_worker is not None or self._operation_worker is not None:
            self._write_info(
                "\nPlease wait for the current AI/Kubernetes operation to finish."
            )
            return

        self._start_voice_recording()

    def _start_voice_recording(self):
        if self._voice_worker is not None:
            return

        self._voice_recording = True

        self.voice_btn.setText("⏹  Stop Voice")
        self.voice_btn.setToolTip(
            "Stop recording and send the transcription to OpsMind"
        )

        self.request_input.clear()
        self.request_input.setPlaceholderText(
            "Listening… speak your Kubernetes command"
        )
        self.request_input.setEnabled(False)

        self.run_btn.setEnabled(False)
        self.clear_history_btn.setEnabled(False)

        self.status_lbl.setText("Listening…")

        self._write_info(
            "\n🎙 Listening… say a Kubernetes operation, then press "
            "Stop Voice or Ctrl+Shift+Space."
        )

        worker = K8sGoogleSpeechWorker(
            language=GOOGLE_STT_LANGUAGE,
            max_seconds=VOICE_MAX_SECONDS,
        )

        worker.listening.connect(self._on_voice_listening)
        worker.stopped.connect(self._on_voice_stopped)
        worker.level.connect(self._on_voice_level)
        worker.done.connect(self._on_voice_done)
        worker.error.connect(self._on_voice_error)
        worker.finished.connect(self._on_voice_finished)

        self._voice_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _stop_voice_recording(self):
        worker = self._voice_worker
        if worker is None:
            return

        self.status_lbl.setText("Transcribing…")
        self.voice_btn.setText("⌛  Transcribing…")
        self.voice_btn.setEnabled(False)

        worker.stop()
    def _on_voice_level(self, level: int):
        """Update the live microphone meter."""

        if not hasattr(self, "mic_level_bar"):
            return

        level = max(0, min(100, int(level)))

        self.mic_level_bar.setValue(level)

        if level >= 75:
            self.mic_level_label.setText("Mic 🔊")
        elif level >= 35:
            self.mic_level_label.setText("Mic 🔉")
        elif level >= 5:
            self.mic_level_label.setText("Mic 🔈")
        else:
            self.mic_level_label.setText("Mic")

    def _on_voice_listening(self):
        if self._voice_recording:
            self.status_lbl.setText("Listening…")

    def _on_voice_stopped(self):
        if self._voice_recording:
            self.status_lbl.setText("Transcribing…")

    def _on_voice_done(self, text: str):
        text = (text or "").strip()

        if not text:
            self._on_voice_error("The voice transcription was empty.")
            return

        self.request_input.setPlaceholderText(
            "Ask AI to perform a Kubernetes operation…"
        )
        self.request_input.setText(text)

        self._write_info(
            f'\n🎙 You said: "{self._escape_html(text)}"'
        )

        # Feed the recognized text directly into the existing AI pipeline.
        self._voice_recording = False
        self.voice_btn.setEnabled(True)
        self.voice_btn.setText("🎙  Voice")
        self.voice_btn.setToolTip(
            "Start/stop voice command. Hotkey: Ctrl+Shift+Space"
        )
        self.request_input.setEnabled(True)

        self._submit()

    def _on_voice_error(self, message: str):
        self._write_error("\n🎙 Voice input error:\n" + str(message))

        self._voice_recording = False

        self.request_input.setPlaceholderText(
            "Ask AI to perform a Kubernetes operation…"
        )
        self.request_input.setEnabled(not self._busy)

        self.voice_btn.setEnabled(not self._busy)
        self.voice_btn.setText("🎙  Voice")
        self.voice_btn.setToolTip(
            "Start/stop voice command. Hotkey: Ctrl+Shift+Space"
        )

        self.status_lbl.setText("Ready")

    def _on_voice_finished(self):
        self._voice_worker = None

        if self._voice_recording:
            self._voice_recording = False

        self.voice_btn.setEnabled(not self._busy)
        self.voice_btn.setText("🎙  Voice")
        self.voice_btn.setToolTip(
            "Start/stop voice command. Hotkey: Ctrl+Shift+Space"
        )

        self.request_input.setPlaceholderText(
            "Ask AI to perform a Kubernetes operation…"
        )
        self.request_input.setEnabled(not self._busy)

        if hasattr(self, "mic_level_bar"):
            self.mic_level_bar.setValue(0)

        if hasattr(self, "mic_level_label"):
            self.mic_level_label.setText("Mic")

        if not self._busy:
            self.status_lbl.setText("Ready")


    def _clear_output(self):
        if self._busy:
            return
        self.output.clear()
        self._write_info("OpsMind is ready.")

    def _clear_history(self):
        if self._busy:
            return

        if not self._history:
            self._update_history_label()
            self._write_info("\nNo saved operation history to clear.")
            return

        answer = QMessageBox.question(
            self,
            "Clear OpsMind History",
            "Delete the saved AI Kubernetes operation history?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self._history = []
        _save_history(self._history)
        self._update_history_label()
        self._write_info("\n✓ OpsMind history cleared.")

    def _submit(self):
        if not self._ai_access_allowed:
            self.refresh_ai_access()
            return

        if self._busy:
            return

        request = self.request_input.text().strip()
        if not request:
            return

        if len(request) > MAX_PROMPT_CHARS:
            self._write_error(
                f"Request is too long. Maximum is {MAX_PROMPT_CHARS} characters."
            )
            return

        # --------------------------------------------------
        # Handle common conversational/status questions locally.
        #
        # These must be checked BEFORE SSH/API-key validation because
        # they do not need Kubernetes or an AI provider.
        # --------------------------------------------------

        namespace = self._namespace()

        local_response = _general_question_response(
            request,
            namespace,
        )

        if local_response is not None:
            self.output.append(
                f'<br><span style="color:{T["ACCENT"]}; font-weight:700;">'
                f'Assistant:</span><br>'
                f'<span style="color:{T["TEXT_PRIMARY"]}">'
                f'{self._escape_html(local_response).replace(chr(10), "<br>")}'
                f'</span>'
            )
            self.status_lbl.setText("Ready")
            self.request_input.setFocus()
            return

        # --------------------------------------------------
        # Actual Kubernetes requests require a live connection.
        # --------------------------------------------------

        if not self.ssh:
            self._write_error("No Kubernetes SSH connection is active.")
            return

        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)

        if not api_key:
            label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
            QMessageBox.information(
                self,
                "No API key set",
                f"Add a {label} API key in Settings → 🤖 AI "
                "to use AI Kubernetes Operations.",
            )
            return

        context = self._context()
        kube_context = self._kube_context()
        if not kube_context:
            self._write_error("No Kubernetes context is selected. Refresh contexts and try again.")
            return
        history_for_context = [
            row for row in self._history
            if not row.get("context") or row.get("context") == kube_context
        ]

        self.output.append(
            f'<br><span style="color:{T["ACCENT2"]}">$ '
            f'{self._escape_html(request)}</span>'
        )
        self.output.append(
            f'<span style="color:{T["TEXT_MUTED"]}">'
            f'Namespace: {self._escape_html(namespace)}</span>'
        )

        self._set_busy(True)

        worker = K8sAIInterpretWorker(
            provider,
            api_key,
            ai_assist.get_model(provider),
            request,
            namespace,
            context,
            history_for_context,
        )
        worker.done.connect(self._on_ai_done)
        worker.error.connect(self._on_ai_error)
        worker.finished.connect(self._on_ai_finished)
        self._ai_worker = worker
        track_worker(self._workers, worker)
        worker.start()

    def _on_ai_done(self, text: str):
        try:
            raw_action = _extract_json(text)
            action = validate_action(raw_action)
        except Exception as exc:
            self._write_error(f"AI interpretation failed: {exc}")
            return

        if action["kind"] == "clarification":
            self._write_info("\nAI needs more information:\n" + action["reason"])
            self.request_input.setFocus()
            return

        if action["kind"] == "unsupported":
            self._write_error("\n" + action["reason"])
            return

        self._execute_action(action)

    def _on_ai_error(self, message: str):
        """Show AI/provider errors in a dialog and keep a compact log entry."""

        message = str(message or "Unknown AI error").strip()

        # Keep the output area useful without dumping a huge API response.
        self._write_error(
            f"\nAI error:\n{message}"
        )

        # Pick a useful title based on the error.
        lower = message.lower()

        if "429" in lower or "quota" in lower or "rate limit" in lower:
            title = "AI API Quota Exceeded"
        elif "401" in lower or "unauthorized" in lower or "invalid api key" in lower:
            title = "AI API Authentication Error"
        elif "403" in lower or "forbidden" in lower:
            title = "AI API Access Denied"
        elif "timeout" in lower:
            title = "AI API Timeout"
        elif "connection" in lower or "network" in lower:
            title = "AI API Connection Error"
        else:
            title = "AI API Error"

        # Show the complete provider message so quota/reset information is
        # still visible to the user.
        dlg = QMessageBox(self)
        dlg.setIcon(QMessageBox.Warning)
        dlg.setWindowTitle(title)
        dlg.setText(title)

        # Detailed text keeps long provider responses readable.
        dlg.setInformativeText(
            "The AI request could not be completed."
        )
        dlg.setDetailedText(message)

        # Make the dialog wide enough for API messages.
        dlg.setStyleSheet(
            f"""
            QMessageBox {{
                min-width: 520px;
            }}
            """
        )

        dlg.exec_()

    def _on_ai_finished(self):
        self._ai_worker = None
        if self._operation_worker is None:
            self._set_busy(False)

    def _execute_action(self, action: dict):
        kube_context = self._kube_context()
        if not kube_context:
            self._write_error("No Kubernetes context is selected; operation blocked for safety.")
            self._set_busy(False)
            return
        action["context"] = kube_context
        description = operation_description(action)
        is_relative_scale = (
            action["action"] == "scale" and action.get("mode") == "relative"
        )

        if is_relative_scale:
            # The exact target replica count is not known yet — it depends on
            # the current replica count, which is read from the cluster next.
            self.output.append(
                f'<br><span style="color:{T["ACCENT"]}; font-weight:700;">'
                f'AI interpreted:</span><br>'
                f'<span style="color:{T["TEXT_PRIMARY"]}">'
                f'{self._escape_html(description)}</span><br>'
                f'<span style="color:{T["TEXT_DIM"]}">'
                f'Reading current replica count before applying the change…'
                f'</span>'
            )
            command = None
        else:
            command = build_kubectl_command(action)
            self.output.append(
                f'<br><span style="color:{T["ACCENT"]}; font-weight:700;">'
                f'AI interpreted:</span><br>'
                f'<span style="color:{T["TEXT_PRIMARY"]}">'
                f'{self._escape_html(description)}</span><br>'
                f'<span style="color:{T["TEXT_DIM"]}">'
                f'Command: {self._escape_html(command)}</span>'
            )

        namespace = action.get("namespace", "")
        protected = is_protected_namespace(namespace)

        # Confirmation gate. "delete" always confirms. "restart" always
        # confirms (rollout restarts every pod in the workload, which is
        # disruptive even though it isn't destructive). "scale" only
        # confirms when the target namespace matches a configured
        # protected pattern (Settings → Kubernetes Tabs) — scaling dev/test
        # is a routine, frequent action and shouldn't need a click-through
        # every time, but scaling prod/mcp should.
        risk = _risk_level(action, kube_context, protected)
        needs_confirmation = action["action"] in {"delete", "restart"}
        if action["action"] == "scale" and protected:
            needs_confirmation = True
        if risk in {"high", "critical"}:
            needs_confirmation = True

        if needs_confirmation:
            if action["action"] == "delete":
                title = "Confirm Kubernetes Delete"
            elif action["action"] == "restart":
                title = "Confirm Kubernetes Restart"
            else:
                title = "Confirm Kubernetes Scale — Protected Namespace"

            extra_note = ""
            if any(x in kube_context.lower() for x in ("prod", "production")):
                extra_note += "\n\n⚠ Selected context looks like a production cluster."
            if protected and action["action"] != "delete":
                extra_note = (
                    f'\n\n⚠ "{namespace}" matches a protected namespace '
                    "pattern configured in Settings."
                )
            elif action["action"] == "restart":
                extra_note = "\n\nThis restarts every pod in the workload."

            answer = QMessageBox.question(
                self,
                title,
                (
                    f"{description}\n\n"
                    "This operation will modify the cluster."
                    f"{extra_note}\n\n"
                    "Do you want to continue?"
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._write_info("\nOperation cancelled.")
                self._set_busy(False)
                return

        if action["action"] == "scale":
            # Capture the replica count before changing it. This makes later
            # requests such as "scale it back" meaningful instead of merely
            # repeating the new target, and it's also what lets relative
            # ("scale up/down by N") requests compute their target.
            self._pending_scale_action = dict(action)
            self._read_previous_replicas(action, command)
            return

        self._start_kubectl_operation(action, command)

    def _read_previous_replicas(self, action: dict, command: str):
        resource = action["resource"]
        name = action["name"]
        namespace = action["namespace"]

        probe = (
            f"kubectl {((f"--context {shlex.quote(str(action.get("context") or ""))} ") if action.get("context") else "")} -n {namespace} get {resource}/{name} "
            f"-o jsonpath={{.spec.replicas}}"
        )

        self.status_lbl.setText("Reading current replicas…")
        worker = CommandWorker(self.ssh, probe)
        self._scale_previous_worker = worker
        worker._k8s_probe_action = action
        worker.result.connect(self._on_previous_replicas_result)
        worker.error.connect(
            lambda error: self._on_previous_replicas_error(error)
        )
        worker.finished.connect(
            lambda: setattr(self, "_scale_previous_worker", None)
        )
        track_worker(self._workers, worker)
        worker.start()

    def _on_previous_replicas_result(self, output: str, err: str, exit_code: int):
        action = self._pending_scale_action
        if action is None:
            return

        if exit_code != 0:
            self._pending_scale_action = None
            self._write_error(
                "\n✗ Could not read the current replica count, so the scale "
                "operation was not executed.\n"
                + ((err or output or "Unknown error").strip())
            )
            self._set_busy(False)
            return

        raw = (output or "").strip()
        try:
            previous = int(raw)
        except (TypeError, ValueError):
            previous = None

        action = dict(action)
        action["previous_replicas"] = previous
        self._pending_scale_action = None

        if action.get("mode") == "relative":
            if previous is None:
                self._write_error(
                    "\n✗ Could not determine the current replica count, so "
                    "the relative scale request could not be resolved."
                )
                self._set_busy(False)
                return

            delta = action.get("delta", 0)
            target = previous + delta
            clamped = max(MIN_REPLICAS, min(MAX_REPLICAS, target))

            self._write_info(
                f"\nCurrent replicas: {previous}. "
                f"Requested change: {'+' if delta >= 0 else ''}{delta}. "
                f"Target replicas: {clamped}."
                + (
                    f" (clamped from {target})"
                    if clamped != target else ""
                )
            )

            action["replicas"] = clamped
            action.pop("delta", None)
            action["mode"] = "absolute"

        command = build_kubectl_command(action)
        self.output.append(
            f'<span style="color:{T["TEXT_DIM"]}">'
            f'Command: {self._escape_html(command)}</span>'
        )

        self._start_kubectl_operation(action, command)

    def _on_previous_replicas_error(self, error: str):
        # result() normally carries command failures, but transport-level
        # errors still arrive here. Do not execute the mutating scale.
        if self._pending_scale_action is None:
            return
        self._pending_scale_action = None
        self._write_error(
            "\n✗ Could not read the current replica count, so the scale "
            "operation was not executed.\n" + str(error)
        )
        self._set_busy(False)

    def _start_kubectl_operation(self, action: dict, command: str):
        self.status_lbl.setText("Executing kubectl…")

        worker = CommandWorker(self.ssh, command + " 2>&1")
        worker._k8s_action = action
        worker._k8s_command = command
        worker._k8s_confirmed = True
        self._operation_worker = worker
        worker.result.connect(
            lambda output, err, exit_code, a=action, c=command:
                self._on_operation_result(a, c, output, err, exit_code)
        )
        worker.error.connect(self._on_operation_error)
        worker.finished.connect(self._on_operation_finished)
        track_worker(self._workers, worker)
        worker.start()

    def _on_operation_result(
        self,
        action: dict,
        command: str,
        output: str,
        err: str,
        exit_code: int,
    ):
        output = (output or "").strip()
        error_text = (err or "").strip()

        if output:
            self.output.append(
                f'<br><span style="color:{T["TEXT_DIM"]}">kubectl output:</span><br>'
                f'<span style="color:{T["TEXT_PRIMARY"]}">'
                f'{self._escape_html(output).replace(chr(10), "<br>")}'
                f'</span>'
            )

        if exit_code != 0 or error_text:
            combined = error_text or output or "kubectl returned a non-zero exit code."
            self._remember_operation(action, command, "failed", combined)
            self._write_error(
                f"\n✗ Kubernetes operation failed (exit code {exit_code}):\n{combined}"
            )
            return

        self._remember_operation(action, command, "success", output)
        self._write_success("\n✓ Kubernetes operation completed successfully.")
        self.operation_finished.emit()

    def _on_operation_error(self, error: str):
        self._write_error("\n✗ Kubernetes operation failed:\n" + str(error))

        if self._operation_worker is not None:
            action = getattr(self._operation_worker, "_k8s_action", None)
            command = getattr(self._operation_worker, "_k8s_command", "")
            if action:
                self._remember_operation(action, command, "failed", str(error))
                _append_audit(action, command, "failed", str(error),
                              getattr(self._operation_worker, "_k8s_confirmed", False),
                              _risk_level(action, action.get("context", ""), is_protected_namespace(action.get("namespace", ""))))

    def _on_operation_finished(self):
        self._operation_worker = None
        self._set_busy(False)

    def _remember_operation(self, action: dict, command: str, status: str, output: str):
        row = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "action": action.get("action"),
            "resource": action.get("resource"),
            "name": action.get("name"),
            "namespace": action.get("namespace", "default"),
            "context": action.get("context", ""),
            "command": command,
        }

        # For scale history, preserve the value before and after the operation.
        # By the time we get here, relative ("scale up/down by N") requests
        # have already been resolved to an absolute replica count, so this
        # always records the concrete before/after values.
        if action.get("action") == "scale":
            row["replicas"] = action.get("replicas")
            if action.get("previous_replicas") is not None:
                row["previous_replicas"] = action.get("previous_replicas")

        if output:
            row["output"] = output[-1000:]

        _append_audit(
            action, command, status, output,
            confirmed=(status == "success"),
            risk=_risk_level(action, action.get("context", ""), is_protected_namespace(action.get("namespace", ""))),
        )
        self._history.append(row)
        self._history = self._history[-MAX_HISTORY:]
        _save_history(self._history)
        self._update_history_label()

    def _show_audit_log(self):
        rows = _load_audit_log()
        dlg = QDialog(self)
        dlg.setWindowTitle("Kubernetes Audit Log")
        dlg.resize(900, 560)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setFont(monospace_font(10))
        if not rows:
            browser.setPlainText("No Kubernetes operations have been audited yet.")
        else:
            lines = []
            for row in reversed(rows):
                lines.append(
                    f"[{row.get('timestamp','')}] {str(row.get('status','')).upper()} "
                    f"risk={row.get('risk','low')}\n"
                    f"context:   {row.get('context','—')}\n"
                    f"namespace: {row.get('namespace','default')}\n"
                    f"operation: {row.get('action','')} {row.get('resource','')}/{row.get('name','')}\n"
                    f"command:   {row.get('command','')}\n"
                    f"confirmed: {row.get('confirmed', False)}\n"
                    f"output:    {row.get('output','').strip()}\n"
                    + "-" * 88 + "\n"
                )
            browser.setPlainText("".join(lines))
        lay.addWidget(browser)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(dlg.reject)
        close.accepted.connect(dlg.accept)
        lay.addWidget(close)
        dlg.exec_()

    def closeEvent(self, event):
        if self._voice_worker is not None:
            try:
                self._voice_worker.stop()
            except Exception:
                pass

        if self._ai_worker is not None:
            try:
                self._ai_worker.quit()
            except Exception:
                pass
        if self._operation_worker is not None:
            try:
                self._operation_worker.quit()
            except Exception:
                pass
        super().closeEvent(event)