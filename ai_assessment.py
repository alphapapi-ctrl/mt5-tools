"""
ai_assessment.py
================
Shared AI assessment helper for MT5 Tools.
Provides get_ai_assessment() and render_ai_assessment().
Supports Anthropic, OpenAI, and Ollama providers.
"""

import json
import requests
from pathlib import Path

SETTINGS_FILE = Path("mt5_ai_settings.json")

DEFAULT_SETTINGS = {
    "ai_features": {
        "enabled": False,
        "provider": "anthropic",
        "anthropic_api_key": "",
        "model": "claude-sonnet-4-6",
        "openai_api_key": "",
        "openai_model": "gpt-4o",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
    },
    "ai_prompts": {
        "adv_portfolio": (
            "You are an algorithmic trading portfolio analyst.\n"
            "Analyse the portfolio data provided and give a 4-6 sentence assessment.\n"
            "Focus on: (1) overall portfolio health and risk-adjusted returns, "
            "(2) account diversification, (3) top performing and underperforming algos, "
            "(4) any concentration risk across symbols or accounts.\n"
            "Be direct and specific — mention actual numbers."
        ),
        "adv_correlation": (
            "You are a quantitative risk analyst.\n"
            "Analyse the correlation data between accounts and algos.\n"
            "Focus on: (1) high correlation pairs that represent concentrated risk, "
            "(2) diversification opportunities, (3) algos that move independently, "
            "(4) any concerning patterns.\n"
            "Be direct — reference specific correlation values."
        ),
        "adv_weak_algos": (
            "You are an algo trading portfolio optimisation advisor.\n"
            "Analyse the per-account algo performance data and provide specific actions.\n\n"
            "For EACH account, recommend:\n"
            "1. Which algos to REMOVE (CRITICAL/WEAK with no redeeming metrics)\n"
            "2. Which algos to MONITOR with specific conditions to trigger removal "
            "(e.g. 'remove if win rate stays below 40% over next 30 trades')\n"
            "3. Which STRONG algos from other accounts could be ADDED to weaker accounts "
            "to improve diversification and performance\n"
            "4. Any symbol concentration risks per account\n\n"
            "Structure your response BY ACCOUNT with clear action items. "
            "Flag accounts that are overall underperforming vs others. "
            "Be direct — name specific algos and accounts with numbers."
        ),
        "trade_analysis": (
            "You are a trade performance analyst.\n"
            "Analyse the trade statistics provided and give a 4-5 sentence assessment.\n"
            "Focus on: (1) overall performance quality (win rate, profit factor, expectancy), "
            "(2) risk management (drawdown, R:R ratio), (3) consistency patterns, "
            "(4) specific areas for improvement.\n"
            "Be direct and mention actual numbers."
        ),
        "portfolio_builder": (
            "You are a portfolio construction analyst.\n"
            "Analyse this portfolio composition and give a 5-7 sentence assessment.\n"
            "Focus on: (1) overall portfolio metrics vs individual strategies, "
            "(2) diversification quality, (3) risk concentration, "
            "(4) suggestions for portfolio improvement, "
            "(5) lot balancing recommendations.\n\n"
            "IMPORTANT — Lot balancing methodology:\n"
            "All backtests use 0.01 lots (minimum) to establish max historical drawdown per strategy. "
            "To balance a portfolio, normalise each strategy's DD to a common target by adjusting lot size. "
            "For example if Strategy A has max DD $150 and Strategy B has max DD $300, "
            "double A's lots to 0.02 so both contribute ~$300 DD.\n"
            "Then derive a lot step size: divide the normalised DD by the desired max DD risk % "
            "to get the account balance per 0.01 lots. "
            "E.g. target 5% max DD: $300 / 0.05 = $6,000 per 0.01 lots. "
            "If Strategy A was doubled (0.02), its lot step is $3,000 per 0.01 lots.\n"
            "Include lot multipliers and lot step sizes in your recommendations where per-strategy DD data is available. "
            "Be specific with numbers."
        ),
        "portfolio_master": (
            "You are a senior portfolio strategist.\n"
            "Analyse this portfolio comparison data and give a 5-7 sentence assessment.\n"
            "Focus on: (1) relative performance ranking, (2) risk-adjusted returns, "
            "(3) which strategies complement each other, "
            "(4) optimal portfolio allocation suggestions, "
            "(5) lot balancing recommendations.\n\n"
            "IMPORTANT — Lot balancing methodology:\n"
            "All backtests use 0.01 lots (minimum) to establish max historical drawdown per strategy. "
            "To balance a portfolio, normalise each strategy's DD to a common target by adjusting lot size. "
            "For example if Strategy A has max DD $150 and Strategy B has max DD $300, "
            "double A's lots to 0.02 so both contribute ~$300 DD.\n"
            "Then derive a lot step size: divide the normalised DD by the desired max DD risk % "
            "to get the account balance per 0.01 lots. "
            "E.g. target 5% max DD: $300 / 0.05 = $6,000 per 0.01 lots. "
            "If Strategy A was doubled (0.02), its lot step is $3,000 per 0.01 lots.\n"
            "Include lot multipliers and lot step sizes in your recommendations where per-strategy DD data is available. "
            "Be direct and mention actual numbers."
        ),
    },
}


def load_ai_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text())
            merged = json.loads(json.dumps(DEFAULT_SETTINGS))
            merged["ai_features"].update(saved.get("ai_features", {}))
            merged["ai_prompts"].update(saved.get("ai_prompts", {}))
            return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_ai_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def get_ai_assessment(prompt, settings, max_tokens=1500):
    provider = settings.get("provider", "anthropic")

    if provider == "ollama":
        url = settings.get("ollama_url", "http://localhost:11434")
        model = settings.get("ollama_model", "llama3.1:8b")
        return _call_ollama(prompt, url, model, max_tokens)
    elif provider == "openai":
        api_key = settings.get("openai_api_key", "")
        model = settings.get("openai_model", "gpt-4o")
        if not api_key:
            return None, "No OpenAI API key configured — add in Settings"
        return _call_openai(prompt, api_key, model, max_tokens)
    else:
        api_key = settings.get("api_key", settings.get("anthropic_api_key", ""))
        model = settings.get("model", "claude-sonnet-4-6")
        if not api_key:
            return None, "No Anthropic API key configured — add in Settings"
        return _call_anthropic(prompt, api_key, model, max_tokens)


def _call_anthropic(prompt, api_key, model, max_tokens):
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if response.status_code != 200:
            try:
                err = response.json()
                return None, f"API error {response.status_code}: {err.get('error', {}).get('message', response.text)}"
            except Exception:
                return None, f"API error {response.status_code}: {response.text}"
        data = response.json()
        return data["content"][0]["text"], None
    except requests.exceptions.Timeout:
        return None, "API request timed out"
    except Exception as e:
        return None, f"API error: {str(e)}"


def _call_openai(prompt, api_key, model, max_tokens):
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        if response.status_code != 200:
            try:
                err = response.json()
                return None, f"API error {response.status_code}: {err.get('error', {}).get('message', response.text)}"
            except Exception:
                return None, f"API error {response.status_code}: {response.text}"
        data = response.json()
        return data["choices"][0]["message"]["content"], None
    except requests.exceptions.Timeout:
        return None, "API request timed out"
    except Exception as e:
        return None, f"API error: {str(e)}"


def _call_ollama(prompt, url, model, max_tokens):
    try:
        response = requests.post(
            f"{url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": max_tokens, "num_ctx": 8192},
            },
            timeout=180,
        )
        if response.status_code != 200:
            return None, f"Ollama error {response.status_code}: {response.text}"
        data = response.json()
        return data.get("message", {}).get("content", ""), None
    except requests.exceptions.ConnectionError:
        return None, f"Cannot connect to Ollama at {url} — is it running?"
    except requests.exceptions.Timeout:
        return None, "Ollama request timed out (180s)"
    except Exception as e:
        return None, f"Ollama error: {str(e)}"


def render_ai_button(prompt, section_key, max_tokens=1500):
    import streamlit as st

    settings = load_ai_settings()
    ai_cfg = settings.get("ai_features", {})
    enabled = ai_cfg.get("enabled", False)
    provider = ai_cfg.get("provider", "anthropic")

    if not enabled:
        return None

    if provider == "anthropic":
        api_key = ai_cfg.get("anthropic_api_key", "")
        if not api_key:
            st.warning("AI features enabled but no Anthropic API key set — add in Settings")
            return None
    elif provider == "openai":
        api_key = ai_cfg.get("openai_api_key", "")
        if not api_key:
            st.warning("AI features enabled but no OpenAI API key set — add in Settings")
            return None

    def _render_box(text):
        html_text = text.replace("\n", "<br>")
        if provider == "ollama":
            border_color = "#22c55e"
            bg_color = "rgba(34,197,94,0.05)"
            model_name = ai_cfg.get("ollama_model", "llama3.1:8b")
            label = f"🤖 AI ASSESSMENT — OLLAMA ({model_name})"
        elif provider == "openai":
            border_color = "#22c55e"
            bg_color = "rgba(34,197,94,0.05)"
            label = f'🤖 AI ASSESSMENT — OPENAI ({ai_cfg.get("openai_model", "gpt-4o")})'
        else:
            border_color = "#9b5de5"
            bg_color = "rgba(155,93,229,0.05)"
            label = f'🤖 AI ASSESSMENT — CLAUDE ({ai_cfg.get("model", "claude-sonnet-4-6")})'

        st.markdown(
            f'<div style="border-left:3px solid {border_color};padding:12px 16px;'
            f'border-radius:0 8px 8px 0;margin:8px 0;background:{bg_color}">'
            f'<div style="color:{border_color};font-size:10px;font-weight:bold;'
            f'letter-spacing:1px;margin-bottom:8px">{label}</div>'
            f'<div style="font-size:13px;line-height:1.7;white-space:pre-wrap">{html_text}</div>'
            f"</div>",
            unsafe_allow_html=True,
        )

    cache_key = f"ai_cache_{section_key}"
    if cache_key in st.session_state:
        _render_box(st.session_state[cache_key])

    if st.button("🤖 Generate AI Assessment", key=f"ai_{section_key}"):
        with st.spinner("Generating assessment..."):
            text, error = get_ai_assessment(prompt, ai_cfg, max_tokens=max_tokens)
        if error:
            st.error(error)
        elif text:
            st.session_state[cache_key] = text
            _render_box(text)
            return text

    return None
