"""ai_assist.py — AI-powered log/crash diagnosis for the Kubernetes tab.

Talks to whichever AI provider (and model) the user has picked in
Settings → 🤖 AI, directly over `urllib` (stdlib only, no extra
dependency). Providers are declared in the PROVIDERS registry below —
each one is just "how do I build a request from a prompt+key+model" and
"how do I pull the text back out of the response", plus a short list of
sample model ids shown in Settings, so adding a new provider (say, a
local/self-hosted API) is a matter of adding one more entry, not
touching the worker or the dialog.

    get_ai_settings() / save_ai_settings(provider, api_keys, models)
                                         — persisted the same way
                                           security.py persists lock
                                           settings: merged under a
                                           dedicated "ai" key in
                                           settings.json via
                                           themes.save_settings. Keys and
                                           model choices are stored
                                           per-provider so switching
                                           providers doesn't clobber the
                                           others.
    get_provider() / get_api_key(provider=None) / get_model(provider=None)
                                         — convenience readers for the
                                           currently-selected provider (or
                                           whichever one you pass in).
    AIExplainWorker(QThread)            — takes a provider id, api key,
                                           model id, pod/namespace/
                                           container + raw log/event
                                           text, calls the provider's API
                                           in a background thread, and
                                           emits done(str) / error(str) —
                                           the same signal shape as
                                           workers.CommandWorker, so call
                                           sites (progress spinner,
                                           button re-enable, etc.) look
                                           identical regardless of
                                           provider.

The API key is never logged. It's sent only to the selected provider's
own API host, as a request header in every case (Anthropic's `x-api-key`,
Google's `x-goog-api-key`, OpenAI's `Authorization: Bearer`).
"""

import json
import urllib.request
import urllib.error

from PyQt5.QtCore import QThread, pyqtSignal

from themes import load_settings, save_settings

MAX_TOKENS = 4096

# How much of the tail of the log/event text to send. Keeps requests fast
# and cheap — a crash-looping pod's most recent output is almost always
# where the actual error/traceback lives, and 12k chars is comfortably
# inside a small-model context window even after the system prompt.
MAX_CONTEXT_CHARS = 12_000


# ─── Provider registry ─────────────────────────────────────────
# Each provider supplies:
#   build_request(prompt, api_key, model) -> (url, headers, body_bytes)
#   parse_response(data)                  -> plain-text answer (str)
#   parse_error(detail, code)             -> a human-readable message
#                                             pulled out of an HTTP-error
#                                             body, or None to fall back
#                                             on the raw exception text
#   was_truncated(data)                   -> True if the response was cut
#                                             off by the token cap rather
#                                             than finishing naturally —
#                                             lets the worker say so
#                                             explicitly instead of just
#                                             silently handing back a
#                                             sentence that stops mid-word
#   model_samples                         -> a few known-good model ids,
#                                             shown as suggestions in
#                                             Settings (newest/cheapest
#                                             first); the field itself is
#                                             free text, so any current or
#                                             future model id works too —
#                                             this list will drift out of
#                                             date as providers ship new
#                                             models and is just a
#                                             starting point.
#   default_model                         -> what's pre-filled the first
#                                             time this provider is used
# `detail` in parse_error is the JSON-decoded error body when the API
# returned one, or the raw response text (str) when it didn't.
#
# A number of current models (Gemini 3.x, GPT-5.6) reason internally
# before writing their visible answer, and those reasoning tokens are
# billed against the same token cap as the answer itself. With a small
# cap, how much the model happens to "think" on a given call — which
# varies request to request — decides how much room is left for the
# actual answer, so the previous 1000-token cap made responses look
# truncated at random. Two changes fix that: MAX_TOKENS above is now
# generous enough to cover both, and each provider that supports it is
# explicitly told to keep reasoning light for this task (a short,
# structured diagnosis doesn't need deep reasoning).

def _anthropic_request(prompt, api_key, model):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": model,
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


def _anthropic_truncated(data):
    return data.get("stop_reason") == "max_tokens"


def _gemini_request(prompt, api_key, model):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            # Gemini 3.x thinks by default (medium) even for simple
            # prompts; "minimal" leaves the most of the cap for the
            # visible answer instead of invisible reasoning tokens.
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
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


def _gemini_truncated(data):
    candidates = data.get("candidates") or []
    return bool(candidates) and candidates[0].get("finishReason") == "MAX_TOKENS"


def _openai_request(prompt, api_key, model):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = json.dumps({
        "model": model,
        # Reasoning models (the gpt-5.x family) reject the legacy
        # "max_tokens" field outright on /chat/completions and require
        # "max_completion_tokens" instead — and that cap covers both
        # invisible reasoning tokens and the visible answer.
        "max_completion_tokens": MAX_TOKENS,
        # Keep reasoning to a minimum so the budget above goes almost
        # entirely toward the actual answer rather than internal
        # deliberation — this is a short, structured diagnosis, not a
        # task that benefits from deep planning. Ignored by non-reasoning
        # models.
        "reasoning_effort": "minimal",
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


def _openai_truncated(data):
    choices = data.get("choices") or []
    return bool(choices) and choices[0].get("finish_reason") == "length"


PROVIDERS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_placeholder": "sk-ant-…",
        "build_request": _anthropic_request,
        "parse_response": _anthropic_parse,
        "parse_error": _anthropic_error,
        "was_truncated": _anthropic_truncated,
        # Fast/cheap → most capable.
        "model_samples": [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-fable-5",
        ],
        "default_model": "claude-sonnet-5",
    },
    "gemini": {
        "label": "Google (Gemini)",
        "key_placeholder": "AIza…",
        "build_request": _gemini_request,
        "parse_response": _gemini_parse,
        "parse_error": _gemini_error,
        "was_truncated": _gemini_truncated,
        "model_samples": [
            "gemini-3.6-flash",
            "gemini-3.7-flash",
        ],
        "default_model": "gemini-3.6-flash",
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "key_placeholder": "sk-…",
        "build_request": _openai_request,
        "parse_response": _openai_parse,
        "parse_error": _openai_error,
        "was_truncated": _openai_truncated,
        "model_samples": [
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "gpt-5.6-sol",
        ],
        "default_model": "gpt-5.6-terra",
    },
}

DEFAULT_PROVIDER = "anthropic"


# ─── Persisted settings ────────────────────────────────────────
def get_ai_settings() -> dict:
    """Returns {"provider": <id>, "api_keys": {<id>: <key>, ...},
    "models": {<id>: <model>, ...}} — every provider id is guaranteed to
    have an entry in "models" (falling back to that provider's
    default_model), so callers never need to guard against a missing key."""
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

    stored_models = dict(stored.get("models") or {})
    models = {pid: stored_models.get(pid) or info["default_model"]
              for pid, info in PROVIDERS.items()}

    return {"provider": provider, "api_keys": api_keys, "models": models}


def get_provider() -> str:
    return get_ai_settings()["provider"]


def get_api_key(provider: str = None) -> str:
    settings = get_ai_settings()
    provider = provider or settings["provider"]
    return settings["api_keys"].get(provider, "")


def get_model(provider: str = None) -> str:
    settings = get_ai_settings()
    provider = provider or settings["provider"]
    return settings["models"].get(provider) or PROVIDERS.get(
        provider, PROVIDERS[DEFAULT_PROVIDER]
    )["default_model"]


def save_ai_settings(provider: str, api_keys: dict, models: dict):
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    cleaned_keys = {pid: (key or "").strip() for pid, key in api_keys.items() if pid in PROVIDERS}
    cleaned_models = {
        pid: (model or "").strip() or PROVIDERS[pid]["default_model"]
        for pid, model in models.items() if pid in PROVIDERS
    }
    # save under "ai" only — this replaces the whole sub-dict (including
    # any legacy "api_key" field), which is what finishes the migration.
    save_settings(ai={"provider": provider, "api_keys": cleaned_keys, "models": cleaned_models})


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
        f"\n"
        f"2. **Evidence** — the specific line(s) that point to it.\n"
        f"\n"
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
    provider/model is backing it."""

    done  = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, provider: str, api_key: str, model: str, pod: str,
                 namespace: str, container: str, log_text: str, parent=None):
        super().__init__(parent)
        self._provider   = provider
        self._api_key    = api_key
        self._model      = model
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

        model = self._model or provider["default_model"]
        prompt = _build_prompt(self._pod, self._namespace, self._container, self._log_text)
        url, headers, body = provider["build_request"](prompt, self._api_key, model)

        req = urllib.request.Request(url, data=body, method="POST", headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
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

        truncated = False
        try:
            truncated = provider["was_truncated"](data)
        except Exception:
            pass

        if not text:
            if truncated:
                self.error.emit(
                    "The model used its whole token budget on internal "
                    "reasoning and never got to an answer. Try a lower-"
                    "reasoning model (e.g. a Flash/mini/Luna variant) in "
                    "Settings → 🤖 AI, or a shorter log excerpt."
                )
            else:
                self.error.emit("Empty response from the API.")
            return

        if truncated:
            text += (
                "\n\n*(⚠ Response was cut off by the model's token limit — "
                "the diagnosis above may be incomplete.)*"
            )

        self.done.emit(text)