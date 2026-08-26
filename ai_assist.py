"""ai_assist.py — AI-powered log/crash diagnosis for the Kubernetes tab.

Talks to whichever AI provider the user has picked in Settings → 🤖 AI,
directly over `urllib` (stdlib only, no extra dependency). Providers are
declared in the PROVIDERS registry below — each one is just "how do I
build a request from a prompt+key" and "how do I pull the text back out
of the response", so adding a new one (say, a local/self-hosted API) is
a matter of adding one more entry, not touching the worker or the dialog.

    get_ai_settings() / save_ai_settings(provider, api_keys)
                                         — persisted the same way
                                           security.py persists lock
                                           settings: merged under a
                                           dedicated "ai" key in
                                           settings.json via
                                           themes.save_settings. Keys are
                                           stored per-provider so switching
                                           providers doesn't clobber the
                                           others.
    get_provider() / get_api_key(provider=None)
                                         — convenience readers for the
                                           currently-selected provider (or
                                           whichever one you pass in).
    AIExplainWorker(QThread)            — takes a provider id, api key,
                                           pod/namespace/container + raw
                                           log/event text, calls the
                                           provider's API in a background
                                           thread, and emits done(str) /
                                           error(str) — the same signal
                                           shape as workers.CommandWorker,
                                           so call sites (progress
                                           spinner, button re-enable,
                                           etc.) look identical regardless
                                           of provider.

The API key is never logged. It's sent only to the selected provider's
own API host, as a request header in every case (Anthropic's `x-api-key`,
Google's `x-goog-api-key`, OpenAI's `Authorization: Bearer`).
"""

import json
import urllib.request
import urllib.error

from PyQt5.QtCore import QThread, pyqtSignal

from themes import load_settings, save_settings

MAX_TOKENS = 1000

# How much of the tail of the log/event text to send. Keeps requests fast
# and cheap — a crash-looping pod's most recent output is almost always
# where the actual error/traceback lives, and 12k chars is comfortably
# inside a small-model context window even after the system prompt.
MAX_CONTEXT_CHARS = 12_000


# ─── Provider registry ─────────────────────────────────────────
# Each provider supplies:
#   build_request(prompt, api_key) -> (url, headers, body_bytes)
#   parse_response(data)           -> plain-text answer (str)
#   parse_error(detail, code)      -> a human-readable message pulled out
#                                      of an HTTP-error body, or None to
#                                      fall back on the raw exception text
# `detail` in parse_error is the JSON-decoded error body when the API
# returned one, or the raw response text (str) when it didn't.

def _anthropic_request(prompt, api_key):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    return url, headers, body


def _anthropic_parse(data):
    return "".join(
        block.get("text", "") for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()


def _anthropic_error(detail, code):
    return detail.get("error", {}).get("message") if isinstance(detail, dict) else None


def _gemini_request(prompt, api_key):
    model = "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": MAX_TOKENS},
    }).encode("utf-8")
    return url, headers, body


def _gemini_parse(data):
    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def _gemini_error(detail, code):
    return detail.get("error", {}).get("message") if isinstance(detail, dict) else None


def _openai_request(prompt, api_key):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = json.dumps({
        "model": "gpt-4o-mini",
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    return url, headers, body


def _openai_parse(data):
    try:
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _openai_error(detail, code):
    return detail.get("error", {}).get("message") if isinstance(detail, dict) else None


PROVIDERS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_placeholder": "sk-ant-…",
        "build_request": _anthropic_request,
        "parse_response": _anthropic_parse,
        "parse_error": _anthropic_error,
    },
    "gemini": {
        "label": "Google (Gemini)",
        "key_placeholder": "AIza…",
        "build_request": _gemini_request,
        "parse_response": _gemini_parse,
        "parse_error": _gemini_error,
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "key_placeholder": "sk-…",
        "build_request": _openai_request,
        "parse_response": _openai_parse,
        "parse_error": _openai_error,
    },
}

DEFAULT_PROVIDER = "anthropic"


# ─── Persisted settings ────────────────────────────────────────
def get_ai_settings() -> dict:
    """Returns {"provider": <id>, "api_keys": {<id>: <key>, ...}}."""
    stored = load_settings().get("ai") or {}

    provider = stored.get("provider", DEFAULT_PROVIDER)
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER

    api_keys = dict(stored.get("api_keys") or {})
    # Migrate the pre-multi-provider layout (a single top-level "api_key",
    # implicitly Anthropic) so upgrading doesn't drop an already-saved key.
    legacy_key = stored.get("api_key")
    if legacy_key and not api_keys.get("anthropic"):
        api_keys["anthropic"] = legacy_key

    return {"provider": provider, "api_keys": api_keys}


def get_provider() -> str:
    return get_ai_settings()["provider"]


def get_api_key(provider: str = None) -> str:
    settings = get_ai_settings()
    provider = provider or settings["provider"]
    return settings["api_keys"].get(provider, "")


def save_ai_settings(provider: str, api_keys: dict):
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    cleaned = {pid: (key or "").strip() for pid, key in api_keys.items() if pid in PROVIDERS}
    # save under "ai" only — this replaces the whole sub-dict (including
    # any legacy "api_key" field), which is what finishes the migration.
    save_settings(ai={"provider": provider, "api_keys": cleaned})


# ─── Prompt construction ───────────────────────────────────────
def _build_prompt(pod: str, namespace: str, container: str, log_text: str) -> str:
    log_text = (log_text or "").strip()
    if len(log_text) > MAX_CONTEXT_CHARS:
        # Keep the tail — the most recent output is what's most likely to
        # contain the actual error/traceback for a crash-looping pod.
        log_text = "…(truncated)…\n" + log_text[-MAX_CONTEXT_CHARS:]

    where = f"pod `{pod}` in namespace `{namespace}`"
    if container:
        where += f" (container `{container}`)"

    return (
        f"You are helping a DevOps engineer diagnose a Kubernetes issue for "
        f"{where}. Below is the raw log output.\n\n"
        f"Log output:\n```\n{log_text}\n```\n\n"
        f"Reply concisely in three short sections:\n"
        f"1. **Likely cause** — one or two sentences.\n"
        f"2. **Evidence** — the specific line(s) that point to it.\n"
        f"3. **Suggested fix** — concrete next step(s), including a "
        f"kubectl command to investigate further if useful.\n\n"
        f"If the log doesn't show an obvious problem, say so plainly "
        f"instead of guessing."
    )


# ─── Background worker ──────────────────────────────────────────
class AIExplainWorker(QThread):
    """Sends pod log/event text to the selected provider's API and returns
    a plain-English diagnosis. Mirrors workers.CommandWorker's done/error
    signal shape so call sites can reuse the same progress-spinner/
    button-state wiring already used for SSH commands, regardless of which
    provider is backing it."""

    done  = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, provider: str, api_key: str, pod: str, namespace: str,
                 container: str, log_text: str, parent=None):
        super().__init__(parent)
        self._provider   = provider
        self._api_key    = api_key
        self._pod        = pod
        self._namespace  = namespace
        self._container  = container
        self._log_text   = log_text
        self.finished.connect(self.deleteLater)

    def run(self):
        provider = PROVIDERS.get(self._provider)
        if provider is None:
            self.error.emit(f"Unknown AI provider: {self._provider!r}")
            return
        if not self._api_key:
            self.error.emit(
                f"No {provider['label']} API key set. Add one in Settings → 🤖 AI."
            )
            return
        if not (self._log_text or "").strip():
            self.error.emit("Nothing to analyze — the log is empty.")
            return

        prompt = _build_prompt(self._pod, self._namespace, self._container, self._log_text)
        url, headers, body = provider["build_request"](prompt, self._api_key)

        req = urllib.request.Request(url, data=body, method="POST", headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw)
            except Exception:
                detail = raw
            msg = provider["parse_error"](detail, e.code) or (raw.strip() or str(e))
            self.error.emit(f"API error ({e.code}): {msg}")
            return
        except urllib.error.URLError as e:
            self.error.emit(f"Network error: {e.reason}")
            return
        except Exception as e:
            self.error.emit(f"Unexpected error: {e}")
            return

        try:
            text = provider["parse_response"](data)
        except Exception:
            text = ""

        if not text:
            self.error.emit("Empty response from the API.")
            return

        self.done.emit(text)