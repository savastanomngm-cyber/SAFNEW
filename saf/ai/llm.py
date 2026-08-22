"""Shared multi-provider LLM layer (PART 1: single source of truth).
Provider chain: Nous Research stealth/ox-alpha (primary) -> Groq fallback.
Handles think-block stripping, JSON-mode fallback, token-cap doubling,
rate-limit class (429/413), and provider/model fallback."""
import json
import os
import re
import time
from openai import OpenAI
from ..security import load_env

GROQ_KEY = load_env()
NOUS_KEY = os.environ.get("NOUS_API_KEY", "")

NOUS_BASE = "https://inference-api.nousresearch.com/v1"
NOUS_MODELS = ["stealth/ox-alpha"]          # free tier, massive budget
GROQ_MODELS = ["qwen/qwen3.6-27b"]          # emergency fallback only

MAX_TOKENS_START = 8192
MAX_TOKENS_CAP   = 16384
REQUEST_TIMEOUT  = 120


def extract_json(text):
    if not text:
        return None
    text = re.sub(r"", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _chain():
    chain = []
    if NOUS_KEY:
        nous = OpenAI(api_key=NOUS_KEY, base_url=NOUS_BASE)
        for m in NOUS_MODELS:
            chain.append({"provider": "nous", "model": m, "client": nous,
                          "json_mode": False})
    if GROQ_KEY:
        groq = OpenAI(api_key=GROQ_KEY, base_url="https://api.groq.com/openai/v1")
        for m in GROQ_MODELS:
            chain.append({"provider": "groq", "model": m, "client": groq,
                          "json_mode": True})
    return chain


def complete(system, user, temperature=0.4, max_tokens=None, force_json=False):
    """Walk the provider chain. Returns (raw_text_or_None, debug_dict)."""
    if not NOUS_KEY and not GROQ_KEY:
        return None, {"error": "no NOUS_API_KEY or GROQ_API_KEY in .env"}
    chain = _chain()
    if not chain:
        return None, {"error": "no providers configured"}
    max_tokens = max_tokens or MAX_TOKENS_START
    last_err, last_model = "", "?"

    for idx, slot in enumerate(chain):
        model, client = slot["model"], slot["client"]
        use_json = force_json and slot["json_mode"]
        retries = 0
        while retries < 3:
            retries += 1
            try:
                kwargs = dict(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=REQUEST_TIMEOUT,
                )
                if use_json:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                finish = getattr(choice, "finish_reason", "") or ""
                raw = (choice.message.content or "").strip()
                if finish == "length" and max_tokens < MAX_TOKENS_CAP:
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
                    last_err = f"truncated (finish=length); max_tokens -> {max_tokens}"
                    continue
                if raw:
                    return raw, {"model": model, "provider": slot["provider"],
                                 "finish_reason": finish}
                last_err = "empty response"
                break
            except Exception as e:
                err = str(e)
                last_err, last_model = err[:200], model
                if "response_format" in err or ("json" in err.lower() and "400" in err):
                    use_json = False
                    continue
                if ("429" in err or "413" in err or "rate" in err.lower()
                        or "too large" in err.lower() or "quota" in err.lower()):
                    if idx < len(chain) - 1:
                        time.sleep(1)
                        break
                    time.sleep(30)
                    continue
                if ("404" in err or "not_found" in err or "decommissioned" in err
                        or "does not exist" in err.lower()
                        or "401" in err or "unauthorized" in err.lower()):
                    break
                break
    return None, {"model": last_model, "error": last_err or "exhausted all providers"}


def complete_json(system, user, temperature=0.2):
    """complete() + JSON extraction. Returns (parsed_or_None, debug_dict)."""
    raw, debug = complete(system, user, temperature=temperature, force_json=True)
    if raw is None:
        return None, debug
    parsed = extract_json(raw)
    if parsed is None:
        debug = {**debug, "error": "unparseable JSON", "raw": raw[:200]}
    return parsed, debug