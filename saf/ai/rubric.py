"""Grounded rubric scoring — multi-provider LLM layer.
FULL PATCHED FILE — v6:
  - Nous Research PRIMARY: stealth/ox-alpha (free tier, massive token budget)
  - Correct base URL: https://inference-api.nousresearch.com/v1
  - Groq kept as emergency last-resort only
  - max_tokens 8192 start (reasoning models burn budget on thinking)
  -  blocks stripped, finish_reason-aware, auto-bump
  - Tolerant citation matching retained."""
import json
import os
import re
import time
from openai import OpenAI
from ..security import load_env

# load_env() puts every .env key into os.environ, returns the GROQ key
GROQ_KEY = load_env()
NOUS_KEY = os.environ.get("NOUS_API_KEY", "")

# Correct base URL for Nous Portal inference API
NOUS_BASE = "https://inference-api.nousresearch.com/v1"
# Exact model ID from the /v1/models list
NOUS_MODELS = [
    "stealth/ox-alpha",
]
# Emergency fallback only — never touched while Ox Alpha works.
GROQ_MODELS = [
    "qwen/qwen3.6-27b",
]

MAX_TOKENS_START = 8192
MAX_TOKENS_CAP   = 16384
REQUEST_TIMEOUT  = 120  # Ox Alpha is a reasoning model, give it time to think

RUBRIC_GROUNDED_SYS = """You are a bottleneck analyst. You will receive an
EVIDENCE PACK containing the company's own business description and fundamentals.
Rules:
1. Score each criterion 1-5 ONLY if the evidence pack contains direct support.
2. For each score, quote the SHORT exact supporting sentence from the pack.
3. If no support exists, score the criterion 2 (neutral) and write "INSUFFICIENT EVIDENCE".
4. Never use your own knowledge to fill gaps.
5. CRITICAL: Do NOT use  tags. Do NOT output any chain-of-thought reasoning.
   Your entire response must be strictly the JSON object, starting with '{' and ending with '}'.
   No markdown fences, no commentary, no thinking blocks.
   Return ONLY valid JSON:
{"scores": {"market_concentration": N, "substitutability": N, "capital_intensity": N, "regulatory_moat": N, "demand_inelasticity": N, "cross_sector_demand": N},
 "citations": {"market_concentration": "...", "substitutability": "...", "capital_intensity": "...", "regulatory_moat": "...", "demand_inelasticity": "...", "cross_sector_demand": "..."},
 "total": N}"""


def extract_json(text):
    if not text:
        return None
    # Strip reasoning/thinking blocks if any leaked through
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
    """Ordered provider/model slots: Ox Alpha (Nous) first, Groq last resort."""
    chain = []
    if NOUS_KEY:
        nous = OpenAI(api_key=NOUS_KEY, base_url=NOUS_BASE)
        for m in NOUS_MODELS:
            chain.append({"provider": "nous", "model": m, "client": nous,
                          "json_mode": False})   # strict prompt + extract_json
    if GROQ_KEY:
        groq = OpenAI(api_key=GROQ_KEY, base_url="https://api.groq.com/openai/v1")
        for m in GROQ_MODELS:
            chain.append({"provider": "groq", "model": m, "client": groq,
                          "json_mode": True})
    return chain


def call_llm(system, user):
    """Walk the provider chain until one slot returns parseable JSON.
    Returns (parsed_json_or_None, debug_dict)."""
    if not NOUS_KEY and not GROQ_KEY:
        return None, {"error": "no NOUS_API_KEY or GROQ_API_KEY in .env"}
    chain = _chain()
    if not chain:
        return None, {"error": "no providers configured"}

    max_tokens = MAX_TOKENS_START
    last_err, last_finish, last_model = "", "", "?"

    for idx, slot in enumerate(chain):
        model, client = slot["model"], slot["client"]
        use_json = slot["json_mode"]
        retries = 0
        while retries < 3:
            retries += 1
            try:
                kwargs = dict(
                    model=model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    temperature=0.2,
                    max_tokens=max_tokens,
                    timeout=REQUEST_TIMEOUT,
                )
                if use_json:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**kwargs)
                choice = resp.choices[0]
                last_finish = getattr(choice, "finish_reason", "") or ""
                last_model = model
                raw = (choice.message.content or "").strip()
                parsed = extract_json(raw)
                if parsed is not None:
                    return parsed, {"model": model, "provider": slot["provider"],
                                    "finish_reason": last_finish}
                # Truncated by token cap -> double the budget, retry same model
                if last_finish == "length" and max_tokens < MAX_TOKENS_CAP:
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
                    last_err = f"truncated (finish=length); max_tokens -> {max_tokens}"
                    continue
                last_err = f"unparseable (finish={last_finish}): {raw[:100]}"
                if use_json:                     # retry once without strict JSON mode
                    use_json = False
                    continue
                break                            # two bad outputs -> next slot
            except Exception as e:
                err = str(e)
                last_err, last_model = err[:200], model
                # JSON-mode param rejected -> retry plain
                if "response_format" in err or ("json" in err.lower() and "400" in err):
                    use_json = False
                    continue
                # Rate-limit CLASS: 429, 413 (request > minute budget), quota
                if ("429" in err or "413" in err or "rate" in err.lower()
                        or "too large" in err.lower() or "quota" in err.lower()):
                    if idx < len(chain) - 1:
                        time.sleep(1)
                        break                    # next provider
                    time.sleep(30)               # last resort: cool down, retry
                    continue
                # Model unavailable or bad key -> next slot
                if ("404" in err or "not_found" in err or "decommissioned" in err
                        or "does not exist" in err.lower()
                        or "401" in err or "unauthorized" in err.lower()):
                    break
                break                            # unknown -> next slot

    return None, {"model": last_model, "error": last_err or "exhausted all providers",
                  "finish_reason": last_finish}


# ── citation matching (tolerant — the false-positive fix) ─────────
def _norm(s):
    return re.sub(r"\s+", " ", str(s)).lower().strip()

def _clean_quote(q):
    q = str(q).strip()
    q = re.sub(r"^[\s\"'`]+|[\s\"'`]+$", "", q)
    q = re.sub(r"[.…\s]+$", "", q)
    return _norm(q)

def _cite_present(quote, pack_text_norm):
    c = _clean_quote(quote)
    if not c:
        return False
    if c in pack_text_norm:
        return True
    prefix = c[:40]
    return len(prefix) >= 20 and prefix in pack_text_norm


def score_bottleneck(ticker: str, pack: dict) -> dict:
    if not pack.get("business_desc"):
        return {"error": "No business description available"}

    prompt = f"EVIDENCE PACK FOR {ticker}:\n{json.dumps(pack, indent=1)}"
    out, debug = call_llm(RUBRIC_GROUNDED_SYS, prompt)

    if not out or "scores" not in out:
        return {"error": "LLM failed to return valid JSON", "debug": debug}

    pack_text = _norm(pack.get("business_desc", "") + " " +
                      " ".join(pack.get("concentration_hits", [])) + " " +
                      " ".join(pack.get("recent_headlines", [])))

    flagged = []
    citations = out.get("citations", {})
    scores = out.get("scores", {})

    for crit, quote in citations.items():
        if quote and quote != "INSUFFICIENT EVIDENCE":
            if not _cite_present(quote, pack_text):
                if crit in scores:
                    scores[crit] = 2
                flagged.append(crit)

    out["scores"] = scores
    out["total"] = sum(v for v in scores.values() if isinstance(v, (int, float)))
    out["flagged_hallucinations"] = flagged
    out["llm_meta"] = debug
    return out