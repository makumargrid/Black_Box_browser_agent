"""
Current agent implementation using browser-use + Claude/Gemini.

TO SWAP YOUR TEAM'S AGENT:
  Replace this file with one that exports:
      async def run(url: str) -> None
  That's the only contract.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel

from browser_use import Agent, BrowserProfile
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.google.chat import ChatGoogle

from agents.display import attach

_env = dotenv_values(Path(__file__).parent.parent / ".env")
_console = Console()
_REPORTS_DIR = Path(__file__).parent.parent / "reports"

# ── Task prompt ───────────────────────────────────────────────────────────────
# Generic blackbox + active exploitation.
# NO app-specific knowledge hardcoded — agent discovers everything by browsing.
# Key principle: find it AND exploit it, prove it, capture evidence.
_TASK = """\
You are an expert blackbox penetration tester. Your target is: {url}

You have ZERO prior knowledge of this application. Discover everything by interacting with it.

CORE RULE: For every vulnerability you find, you MUST actively exploit it and prove impact.
Do not just "test" — actually execute the attack and capture proof.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOUR TOOLS WORK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
evaluate(code) — Runs JavaScript DIRECTLY in the browser via Chrome DevTools Protocol.
  This is IDENTICAL to typing code into the browser's DevTools Console (F12 → Console tab).
  You have full access to: window, document, localStorage, sessionStorage, fetch(), XMLHttpRequest, cookies.
  Use it for: reading auth tokens, making API calls with auth headers, reading DOM, injecting payloads.

navigate(url) — Navigates the browser to a URL, same as typing in address bar.
input(index, text) — Types text into an input field.
click(index) — Clicks an element on the page.
find_elements(selector) — Finds elements matching a CSS/XPath selector.
send_keys(keys) — Sends keyboard shortcuts (e.g. Enter, Escape, Tab).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 0 — INSTRUMENT NETWORK + CONSOLE (do this as your FIRST evaluate())
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Run this evaluate() before anything else to intercept all network and console activity:

(function(){{
  if(window.__bb_ready)return 'already active';
  window.__bb_ready=true;
  window.__bb_net=[];
  window.__bb_console=[];
  const origFetch=window.fetch;
  window.fetch=function(...args){{
    const req={{url:String(args[0]),method:((args[1]||{{}}).method||'GET'),ts:Date.now()}};
    window.__bb_net.push(req);
    return origFetch.apply(this,args).then(r=>{{
      const clone=r.clone();
      clone.text().then(b=>{{req.status=r.status;req.body=b.slice(0,800);}});
      return r;
    }});
  }};
  ['log','warn','error','info'].forEach(k=>{{
    const o=console[k];
    console[k]=function(...a){{window.__bb_console.push({{level:k,msg:a.join(' ')}});o.apply(console,a);}};
  }});
  return 'monitoring active — use window.__bb_net and window.__bb_console';
}})()

NOTE: An Eruda DevTools panel (Console + Network tabs) is already injected for you.
Look for the ⚙ icon at the bottom of the page — click it to open/close Eruda.

To read captured network requests:  evaluate("JSON.stringify(window.__bb_net.slice(-15))")
To read captured console logs:      evaluate("JSON.stringify(window.__bb_console.slice(-15))")

IMPORTANT: After EVERY evaluate() that returns data, add console.log so it appears in
the Eruda Console tab for full visibility:
  evaluate("(async()=>{{const r=await fetch('/api/Users',{{headers:{{Authorization:'Bearer '+localStorage.getItem('token')}}}});const d=await r.json();console.log('[RESULT]',JSON.stringify(d));return d;}})()")

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 1 — RECON (2-3 steps max)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Map the app: what pages exist, what features, what requires login
- Look at the navigation menu, note all accessible sections
- Check for an /ftp, /admin, /api, /.git or similar paths directly

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 2 — AUTHENTICATION ATTACKS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Find the login page and EXPLOIT these in order:

A) SQL INJECTION — try these payloads in the email/username field:
   Payload 1: ' OR 1=1--
   Payload 2: ' OR '1'='1
   Payload 3: admin'--
   PROOF REQUIRED: If login succeeds, use evaluate JS to read the token:
     evaluate("localStorage.getItem('token') || document.cookie")
   console.log('[AUTH-BYPASS] token='+result) so it appears in the console log.

B) DEFAULT CREDENTIALS — try common weak credentials if SQLi fails:
   admin/admin, admin/password, test/test, user/user

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 3 — INJECTION ATTACKS (XSS)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Find every input field (search, feedback, comments, profile, registration)
and try these XSS payloads in sequence until one works:
   1. <script>alert(document.domain)</script>
   2. <img src=x onerror=alert(document.domain)>
   3. <svg onload=alert(document.domain)>
   4. <iframe src="javascript:alert(document.domain)">
   5. javascript:alert(document.domain)
PROOF REQUIRED: A JavaScript alert dialog appeared. Note which input and which payload worked.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 4 — API & ENDPOINT DISCOVERY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- While browsing, note every API call the app makes
- Directly test discovered API endpoints:
  * Try without authentication header
  * Try with different HTTP methods (GET on a POST endpoint)
  * Try accessing admin endpoints you discover
PROOF: Use evaluate() to call fetch() and console.log('[API] endpoint=...', result) the response body.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 5 — ACCESS CONTROL & IDOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Navigate directly to any restricted/admin pages you discover
- If authenticated, note your user ID from any API response
- Exploit IDOR: use evaluate JS to fetch other users' data:
    fetch('/api/SomeResource/1').then(r=>r.json()).then(d=>console.log(JSON.stringify(d)))
  Then try IDs 1, 2, 3, etc.
PROOF: console.log('[IDOR] id='+id+' data='+JSON.stringify(data)) to capture the result.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PHASE 6 — SENSITIVE DATA
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Check error pages for stack traces
- Check any /ftp, /backup, /.env, /robots.txt, /sitemap.xml paths
- Look at API responses for passwords, tokens, seed phrases, PII
PROOF: console.log('[SENSITIVE] path=... data=...') the actual data found.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EVIDENCE CAPTURE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use console.log('[TAG] ...') to capture every finding in real-time.
Use tags: [AUTH-BYPASS], [XSS], [IDOR], [SQLI], [FTP], [SENSITIVE], [RECON], [API], [RESULT].
Example: console.log('[AUTH-BYPASS] JWT='+localStorage.getItem('token'))

Do NOT use write_file during the run — it wastes steps. Just exploit and console.log results.

At the very end of your run, provide a structured summary in your final response covering
all vulnerabilities found, their severity, the exact proof, and the attack chain.

Begin immediately. Exploit first.
"""


_ANTHROPIC_FALLBACK_HINTS = (
    "usage limit",
    "usage limits",
    "workspace api usage limits",
    "rate limit",
    "quota",
    "insufficient credit",
    "payment required",
    "authentication",
    "api key",
    "auth",
)


def _should_convert_anthropic_error_to_rate_limit(error: ModelProviderError) -> bool:
    if error.status_code != 400:
        return False
    message = (error.message or "").lower()
    return any(hint in message for hint in _ANTHROPIC_FALLBACK_HINTS)


class ChatAnthropicWithFallback(ChatAnthropic):
    async def ainvoke(self, messages: list[Any], output_format: Any = None, **kwargs: Any) -> Any:
        try:
            return await super().ainvoke(messages, output_format=output_format, **kwargs)
        except ModelProviderError as error:
            if _should_convert_anthropic_error_to_rate_limit(error):
                raise ModelRateLimitError(
                    message=error.message,
                    status_code=429,
                    model=error.model or self.name,
                ) from error
            raise


def _build_llms(
    anthropic_api_key: str,
    anthropic_model: str,
    gemini_api_key: str,
    gemini_model: str,
) -> tuple[Any, Any | None, str]:
    def _gemini_llm(api_key: str, model_name: str) -> ChatGoogle:
        # Force API-key mode even if Vertex-related env vars exist in the shell.
        return ChatGoogle(model=model_name, api_key=api_key, vertexai=False)

    if anthropic_api_key:
        primary = ChatAnthropicWithFallback(model=anthropic_model, api_key=anthropic_api_key, max_tokens=8192)
        fallback = _gemini_llm(api_key=gemini_api_key, model_name=gemini_model) if gemini_api_key else None
        return primary, fallback, anthropic_model
    primary = _gemini_llm(api_key=gemini_api_key, model_name=gemini_model)
    return primary, None, gemini_model


async def run(url: str) -> None:
    anthropic_api_key = _env.get("ANTHROPIC_API_KEY", "")
    gemini_api_key = _env.get("GEMINI_API_KEY", "") or _env.get("GOOGLE_API_KEY", "")
    model = _env.get("BLACKBOX_AGENT_MODEL", "claude-opus-4-7")
    gemini_model = _env.get("BLACKBOX_GEMINI_MODEL", "gemini-2.5-flash")
    max_steps = int(_env.get("BLACKBOX_AGENT_MAX_STEPS", "20"))

    if not anthropic_api_key and not gemini_api_key:
        _console.print("[bold red][ERROR][/bold red] Missing API keys in .env (`ANTHROPIC_API_KEY` or `GEMINI_API_KEY`).")
        sys.exit(1)

    # Prepare reports directory
    _REPORTS_DIR.mkdir(exist_ok=True)
    timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = _REPORTS_DIR / f"{timestamp}_report.md"

    llm, fallback_llm, active_model = _build_llms(
        anthropic_api_key=anthropic_api_key,
        anthropic_model=model,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
    )

    _console.print(Panel.fit(
        f"[bold cyan]BLACKBOX SECURITY AGENT[/bold cyan]\n"
        f"[dim]target [/dim][white]{url}[/white]\n"
        f"[dim]model  [/dim][white]{active_model}[/white]\n"
        f"[dim]steps  [/dim][white]{max_steps}[/white]\n"
        f"[dim]report [/dim][white]{report_path}[/white]",
        border_style="cyan",
    ))

    profile = BrowserProfile(headless=False, demo_mode=False)

    agent = Agent(
        task=_TASK.format(url=url),
        llm=llm,
        fallback_llm=fallback_llm,
        browser_profile=profile,
        llm_timeout=300,          # 5 min — opus is slow on large contexts
        max_actions_per_step=3,   # limit action batching to keep responses shorter
    )

    attach(agent)

    history = await agent.run(max_steps=max_steps)

    # Save final result to the reports directory
    final = history.final_result() if hasattr(history, "final_result") else ""
    if final:
        report_path.write_text(final, encoding="utf-8")
        _console.print(f"\n[green]✓[/green] Report saved → [white]{report_path}[/white]")

        _console.print()
        _console.print(Panel(
            final,
            title="[bold green]EXPLOITATION REPORT[/bold green]",
            border_style="green",
        ))
    else:
        _console.print("[yellow]Agent finished with no final result.[/yellow]")
