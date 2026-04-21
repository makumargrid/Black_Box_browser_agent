"""
Browser overlay display for the blackbox security agent.

Architecture:
  on_step()  → structured AgentOutput per step (primary, awaited directly)
  emit()     → logging handler for tool result lines only (nav, click, type, file)

Both push to an injected sidebar inside the Playwright browser.
Eruda DevTools panel is also injected so Console + Network tabs are visible.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

# ── ANSI stripping ───────────────────────────────────────────────────────────
_ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

def _strip(s: str) -> str:
    return _ANSI.sub("", s).strip()

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))

# ── Loggers that produce internal noise — skip them ─────────────────────────
_SKIP_LOGGERS = {
    "watchdog", "dom_watchdog", "dom_service", "serializer",
    "har_recording", "aboutblank", "downloads", "crash",
    "default_action", "pagination", "browser_state_request",
}

def _is_noisy(logger_name: str) -> bool:
    low = logger_name.lower()
    return any(s in low for s in _SKIP_LOGGERS)

# ── Colors ───────────────────────────────────────────────────────────────────
_C = {
    "step":    "#3b9eff",
    "eval_ok": "#22c55e",
    "eval_ng": "#ef4444",
    "eval_pt": "#f59e0b",
    "memory":  "#818cf8",
    "goal":    "#38bdf8",
    "action":  "#f0abfc",
    "exploit": "#ef4444",
    "tool":    "#6b7280",
    "retry":   "#f59e0b",
    "error":   "#ef4444",
}

_BORDER = {
    "thinking":   "#3b9eff",
    "exploiting": "#ef4444",
    "success":    "#22c55e",
    "idle":       "#1e2d42",
}

# ── Sidebar JS — 65/35 split, animated glow border, premium design ───────────
_SIDEBAR_JS = r"""
() => {
  /* ── idempotency ── */
  if (document.getElementById('__bb__')) {
    /* sidebar already present */
    return;
  }

  /* ── CSS keyframes (injected once into head) ── */
  if (!document.getElementById('__bb_css__')) {
    var s = document.createElement('style');
    s.id = '__bb_css__';
    s.textContent =
      '@keyframes bb-think{0%,100%{box-shadow:inset 0 0 0 2px rgba(59,158,255,0.55),inset 0 0 12px rgba(59,158,255,0.07)}50%{box-shadow:inset 0 0 0 2px rgba(59,158,255,1),inset 0 0 35px rgba(59,158,255,0.2)}}' +
      '@keyframes bb-exploit{0%,100%{box-shadow:inset 0 0 0 3px rgba(239,68,68,0.65),inset 0 0 16px rgba(239,68,68,0.1)}50%{box-shadow:inset 0 0 0 3px rgba(239,68,68,1),inset 0 0 50px rgba(239,68,68,0.26)}}' +
      '@keyframes bb-success{0%{box-shadow:inset 0 0 0 3px rgba(34,197,94,1),inset 0 0 60px rgba(34,197,94,0.32)}100%{box-shadow:inset 0 0 0 2px rgba(34,197,94,0.45),inset 0 0 8px rgba(34,197,94,0.07)}}' +
      '#__bb_log__::-webkit-scrollbar{width:3px}' +
      '#__bb_log__::-webkit-scrollbar-thumb{background:#1a2e47;border-radius:2px}' +
      '#__bb_log__::-webkit-scrollbar-track{background:transparent}';
    document.head.appendChild(s);
  }

  /* ── glow border overlay covering left 65% ── */
  var bord = document.createElement('div');
  bord.id = '__bb_border__';
  bord.setAttribute('data-browser-use-exclude','true');
  Object.assign(bord.style,{
    position:'fixed',top:'0',left:'0',right:'35vw',bottom:'0',
    pointerEvents:'none',zIndex:'2147483646',
    boxShadow:'inset 0 0 0 2px rgba(26,46,71,0.45)',
    transition:'box-shadow 0.4s ease',
  });
  document.body.appendChild(bord);

  /* ── sidebar panel (right 35%) ── */
  var panel = document.createElement('div');
  panel.id = '__bb__';
  panel.setAttribute('data-browser-use-exclude','true');
  Object.assign(panel.style,{
    position:'fixed',top:'0',right:'0',
    width:'35vw',minWidth:'300px',height:'100vh',
    background:'linear-gradient(180deg,#0c1422 0%,#080d17 100%)',
    color:'#c9d8ee',
    fontFamily:"'SF Mono','Cascadia Code',Menlo,Consolas,monospace",
    fontSize:'11px',lineHeight:'1.55',
    display:'flex',flexDirection:'column',
    zIndex:'2147483647',
    boxShadow:'-1px 0 0 rgba(59,158,255,0.1),-14px 0 40px rgba(0,0,0,0.7)',
    boxSizing:'border-box',
  });

  /* header with gradient */
  var hdr = document.createElement('div');
  Object.assign(hdr.style,{
    padding:'10px 14px',
    background:'linear-gradient(135deg,#0d1a30 0%,#0f2040 50%,#0a1828 100%)',
    borderBottom:'1px solid rgba(59,158,255,0.1)',
    flexShrink:'0',display:'flex',alignItems:'center',justifyContent:'space-between',
  });
  var hL = document.createElement('div');
  hL.style.cssText='display:flex;align-items:center;gap:8px';
  var dot = document.createElement('div');
  dot.id='__bb_dot__';
  Object.assign(dot.style,{width:'7px',height:'7px',borderRadius:'50%',
    background:'#1e3a5f',boxShadow:'0 0 5px #1e3a5f',transition:'background 0.3s,box-shadow 0.3s'});
  var titleEl = document.createElement('span');
  titleEl.textContent='BLACKBOX';
  Object.assign(titleEl.style,{color:'#3b9eff',fontWeight:'700',letterSpacing:'0.14em',fontSize:'11px',textTransform:'uppercase'});
  hL.appendChild(dot); hL.appendChild(titleEl);
  var badge = document.createElement('span');
  badge.id='__bb_badge__'; badge.textContent='idle';
  Object.assign(badge.style,{fontSize:'9px',color:'#2d4a6a',
    background:'rgba(13,21,34,0.9)',padding:'2px 9px',
    borderRadius:'10px',border:'1px solid #1a2e47',fontWeight:'500',letterSpacing:'0.04em'});
  hdr.appendChild(hL); hdr.appendChild(badge);
  panel.appendChild(hdr);

  /* log scroll area */
  var log = document.createElement('div');
  log.id='__bb_log__';
  Object.assign(log.style,{flex:'1',overflowY:'auto',padding:'10px 12px',scrollBehavior:'smooth'});
  var ph = document.createElement('div');
  ph.setAttribute('data-ph',''); ph.textContent='waiting for agent\u2026';
  Object.assign(ph.style,{color:'#1e3a5f',padding:'28px 0',textAlign:'center',fontSize:'10px',letterSpacing:'0.05em'});
  log.appendChild(ph);
  panel.appendChild(log);

  /* status bar */
  var bar = document.createElement('div');
  bar.id='__bb_bar__'; bar.textContent='\u25cf ready';
  Object.assign(bar.style,{padding:'5px 14px',
    background:'rgba(6,10,18,0.95)',borderTop:'1px solid rgba(26,46,71,0.4)',
    fontSize:'9px',color:'#2d4a6a',flexShrink:'0',
    whiteSpace:'nowrap',overflow:'hidden',textOverflow:'ellipsis',letterSpacing:'0.03em'});
  panel.appendChild(bar);

  document.body.appendChild(panel);
  document.body.style.marginRight='35vw';
  document.body.style.transition='margin-right 0.3s ease';

  /* ── border controller ── */
  window.__bbBorder = function(color) {
    var b=document.getElementById('__bb_border__');
    var d=document.getElementById('__bb_dot__');
    if(!b) return;
    b.style.animation='none'; void b.offsetWidth;
    if(color==='#ef4444'){
      b.style.animation='bb-exploit 1.5s ease-in-out infinite';
      if(d){d.style.background='#ef4444';d.style.boxShadow='0 0 8px #ef4444';}
    } else if(color==='#22c55e'){
      b.style.animation='bb-success 0.7s ease-out forwards';
      if(d){d.style.background='#22c55e';d.style.boxShadow='0 0 8px #22c55e';}
    } else if(color==='#3b9eff'||color==='#38bdf8'){
      b.style.animation='bb-think 2s ease-in-out infinite';
      if(d){d.style.background='#3b9eff';d.style.boxShadow='0 0 6px #3b9eff';}
    } else {
      b.style.boxShadow='inset 0 0 0 2px rgba(26,46,71,0.45)';
      if(d){d.style.background='#1e3a5f';d.style.boxShadow='0 0 4px #1e3a5f';}
    }
  };

  /* ── entry renderer ── */
  window.__bbAdd = function(e) {
    var log=document.getElementById('__bb_log__');
    if(!log) return;
    var ph=log.querySelector('[data-ph]'); if(ph) ph.remove();
    var row=document.createElement('div');
    row.setAttribute('data-bb-entry','');
    Object.assign(row.style,{marginBottom:'7px',borderLeft:'3px solid '+e.color,
      paddingLeft:'9px',paddingTop:'4px',paddingBottom:'4px',
      background:'rgba(255,255,255,0.018)',borderRadius:'0 5px 5px 0'});

    if(e.isCode){
      var isConsole=e.label.indexOf('CONSOLE')>=0;
      var hB=isConsole?'rgba(239,68,68,0.07)':'rgba(10,16,26,0.5)';
      var cB=isConsole?'rgba(239,68,68,0.04)':'rgba(8,12,22,0.6)';
      var cF=isConsole?'#fca5a5':'#94a3b8';
      var cBd=isConsole?'rgba(239,68,68,0.12)':'#1a2e47';
      var ch=document.createElement('div');
      Object.assign(ch.style,{background:hB,padding:'3px 8px',borderRadius:'3px 3px 0 0',
        display:'flex',alignItems:'center',gap:'6px',flexWrap:'wrap'});
      var cl=document.createElement('span'); cl.textContent=e.label;
      Object.assign(cl.style,{color:e.color,fontWeight:'700',fontSize:'9px',textTransform:'uppercase',letterSpacing:'0.06em'});
      var ct=document.createElement('span'); ct.textContent=e.time; ct.style.cssText='color:#1e3a5f;font-size:9px';
      ch.appendChild(cl); ch.appendChild(ct);
      if(e.step){var cs=document.createElement('span');cs.textContent='\u00b7s'+e.step;cs.style.cssText='color:#1e3a5f;font-size:9px';ch.appendChild(cs);}
      if(isConsole){var cd=document.createElement('span');cd.textContent='CDP \u2193';cd.style.cssText='color:rgba(239,68,68,0.4);font-size:8px;margin-left:auto;letter-spacing:0.04em';ch.appendChild(cd);}
      var pre=document.createElement('pre'); pre.textContent=e.rawtext;
      Object.assign(pre.style,{margin:'0',padding:'7px 9px',background:cB,
        border:'1px solid '+cBd,borderTop:'none',borderRadius:'0 0 4px 4px',
        fontSize:'9.5px',color:cF,whiteSpace:'pre-wrap',wordBreak:'break-all',
        maxHeight:'200px',overflowY:'auto',fontFamily:'inherit',lineHeight:'1.5'});
      row.appendChild(ch); row.appendChild(pre);
    } else {
      var meta=document.createElement('div');
      Object.assign(meta.style,{display:'flex',alignItems:'center',gap:'6px',marginBottom:'2px'});
      var ml=document.createElement('span'); ml.textContent=e.label;
      Object.assign(ml.style,{color:e.color,fontWeight:'700',fontSize:'9px',textTransform:'uppercase',letterSpacing:'0.05em'});
      var mt=document.createElement('span'); mt.textContent=e.time; mt.style.cssText='color:#1a2e47;font-size:9px';
      meta.appendChild(ml); meta.appendChild(mt);
      if(e.step){var ms=document.createElement('span');ms.textContent='\u00b7s'+e.step;ms.style.cssText='color:#1a2e47;font-size:9px';meta.appendChild(ms);}
      var txt=document.createElement('div'); txt.textContent=e.rawtext;
      Object.assign(txt.style,{color:'#7d9ab5',wordBreak:'break-word',fontSize:'10.5px',lineHeight:'1.5'});
      row.appendChild(meta); row.appendChild(txt);
    }

    log.appendChild(row); log.scrollTop=log.scrollHeight;
    var bdg=document.getElementById('__bb_badge__');
    if(bdg&&e.step){bdg.textContent='step '+e.step;bdg.style.color='#4a6a8a';}
    var sb=document.getElementById('__bb_bar__');
    if(sb) sb.textContent='\u25cf '+e.label+'  '+e.rawtext.slice(0,60);
  };

  /* ── Pinned thinking bar ── */
  var thinkBar=document.createElement('div');
  thinkBar.id='__bb_think__';
  thinkBar.setAttribute('data-browser-use-exclude','true');
  Object.assign(thinkBar.style,{display:'none',flexShrink:'0',padding:'6px 12px',
    borderBottom:'1px solid rgba(59,158,255,0.1)',background:'rgba(59,158,255,0.04)',fontSize:'10px'});
  panel.insertBefore(thinkBar,log);
  window.__bbSetThinking=function(eval_text,goal_text){
    var el=document.getElementById('__bb_think__');
    if(!el) return;
    el.style.display='block';
    while(el.firstChild) el.removeChild(el.firstChild);
    if(eval_text){var ev=document.createElement('div');ev.style.cssText='color:#22c55e;font-size:9px;font-weight:700;margin-bottom:2px';ev.textContent='EVAL  '+eval_text.slice(0,120);el.appendChild(ev);}
    if(goal_text){var gl=document.createElement('div');gl.style.cssText='color:#38bdf8;font-size:9px;margin-top:2px';gl.textContent='GOAL  '+goal_text.slice(0,120);el.appendChild(gl);}
  };
  var devTools=document.createElement('div');
  devTools.id='__bb_devtools__';
  devTools.setAttribute('data-browser-use-exclude','true');
  Object.assign(devTools.style,{height:'32vh',minHeight:'180px',flexShrink:'0',
    borderTop:'1px solid rgba(59,158,255,0.12)',display:'flex',flexDirection:'column',
    background:'#060a10',boxSizing:'border-box'});
  var tabBar=document.createElement('div');
  Object.assign(tabBar.style,{display:'flex',alignItems:'stretch',background:'#08111c',
    flexShrink:'0',borderBottom:'1px solid rgba(26,46,71,0.7)'});
  function makeTab(label,active,color){
    var t=document.createElement('button');
    t.textContent=label; t.dataset.tab=label;
    var c=color||'#3b9eff';
    Object.assign(t.style,{background:'none',border:'none',cursor:'pointer',padding:'5px 11px 4px',
      fontSize:'9px',fontFamily:'inherit',fontWeight:'600',letterSpacing:'0.07em',textTransform:'uppercase',
      color:active?c:'#253a52',borderBottom:active?'2px solid '+c:'2px solid transparent',transition:'color 0.15s'});
    return t;
  }
  var tabCon=makeTab('CONSOLE',true,'#3b9eff');
  var tabNet=makeTab('NETWORK',false,'#818cf8');
  var tabErr=makeTab('ERRORS',false,'#ef4444');
  var clrBtn=document.createElement('button');
  clrBtn.textContent='CLR';
  Object.assign(clrBtn.style,{background:'none',border:'none',cursor:'pointer',padding:'5px 8px',
    fontSize:'8px',fontFamily:'inherit',color:'#1e3a5f',letterSpacing:'0.05em',marginLeft:'auto'});
  tabBar.appendChild(tabCon); tabBar.appendChild(tabNet); tabBar.appendChild(tabErr); tabBar.appendChild(clrBtn);
  devTools.appendChild(tabBar);
  function makePaneDiv(){
    var p=document.createElement('div');
    Object.assign(p.style,{flex:'1',overflowY:'auto',padding:'4px 6px',
      fontFamily:"'SF Mono',Menlo,Consolas,monospace",fontSize:'9.5px',lineHeight:'1.55'});
    p.style.cssText+=';scrollbar-width:thin;scrollbar-color:#1a2e47 transparent';
    return p;
  }
  var conPane=makePaneDiv(); conPane.id='__bb_con__';
  var netPane=makePaneDiv(); netPane.id='__bb_net_pane__'; netPane.style.display='none';
  var errPane=makePaneDiv(); errPane.id='__bb_err_pane__'; errPane.style.display='none';
  devTools.appendChild(conPane); devTools.appendChild(netPane); devTools.appendChild(errPane);
  panel.appendChild(devTools);
  var tabs=[[tabCon,conPane,'#3b9eff'],[tabNet,netPane,'#818cf8'],[tabErr,errPane,'#ef4444']];
  function switchTab(activeTab){
    tabs.forEach(function(t){
      var isActive=(t[0]===activeTab);
      t[0].style.color=isActive?t[2]:'#253a52';
      t[0].style.borderBottom=isActive?'2px solid '+t[2]:'2px solid transparent';
      t[1].style.display=isActive?'block':'none';
    });
  }
  tabs.forEach(function(t){ t[0].onclick=function(){ switchTab(t[0]); }; });
  clrBtn.onclick=function(){
    [conPane,netPane,errPane].forEach(function(p){ while(p.firstChild) p.removeChild(p.firstChild); });
    window.__bb_console_cur=0; window.__bb_net_cur=0; window.__bb_err_cur=0;
    if(window.__bb_console) window.__bb_console.length=0;
    if(window.__bb_net) window.__bb_net.length=0;
    if(window.__bb_errors) window.__bb_errors.length=0;
  };
  window.__bb_console_cur=0; window.__bb_net_cur=0; window.__bb_err_cur=0;
  window.__bb_errors=[];
  var _showTags=['[BLACKBOX]','[RECON]','[SQLI','[AUTH','[FTP]','[XSS]','[IDOR]','[JWT]','[RESULT]','[VULN]','[EXPLOIT'];
  var _hidePfx=['Removing','browser-use','highlight element','%cCode'];
  function _isAgentLog(msg){
    if(!msg) return false;
    for(var h=0;h<_hidePfx.length;h++){ if(msg.indexOf(_hidePfx[h])>=0) return false; }
    for(var s=0;s<_showTags.length;s++){ if(msg.indexOf(_showTags[s])>=0) return true; }
    var t=msg.trim();
    if(t.indexOf('(async')===0||t.indexOf('(function')===0) return true;
    if(t.length>120&&(t.indexOf('{')>=0||t.indexOf('[')>=0)) return true;
    return false;
  }
  function appendConEntry(e){
    if(!_isAgentLog(e.msg)) return;
    var row=document.createElement('div');
    Object.assign(row.style,{padding:'3px 4px',borderBottom:'1px solid rgba(26,46,71,0.3)',wordBreak:'break-all'});
    var tagMatch=e.msg.match(/^\[([A-Z0-9_\-]+)\]/);
    if(tagMatch){
      var tag=document.createElement('span');
      tag.textContent='['+tagMatch[1]+'] ';
      tag.style.cssText='color:#3b9eff;font-weight:700;font-size:8.5px';
      var rest=document.createElement('span');
      rest.textContent=e.msg.slice(tagMatch[0].length);
      rest.style.cssText='color:#7d9ab5';
      row.appendChild(tag); row.appendChild(rest);
    } else {
      var sp=document.createElement('span');
      sp.textContent=e.msg;
      sp.style.cssText='color:'+(e.level==='error'?'#ef4444':e.level==='warn'?'#f59e0b':'#7d9ab5');
      row.appendChild(sp);
    }
    conPane.appendChild(row);
    conPane.scrollTop=conPane.scrollHeight;
  }
  function appendNetEntry(r){
    var row=document.createElement('div');
    var sc=r.status>=400?'#ef4444':r.status>=300?'#f59e0b':'#22c55e';
    Object.assign(row.style,{padding:'3px 6px',borderBottom:'1px solid rgba(26,46,71,0.3)',
      display:'flex',gap:'6px',alignItems:'center'});
    function mksp(txt,css){var s=document.createElement('span');s.textContent=txt;s.style.cssText=css;return s;}
    row.appendChild(mksp(r.status||'?','color:'+sc+';font-weight:700;min-width:26px;font-size:9px'));
    row.appendChild(mksp(r.method,'color:#818cf8;min-width:34px;font-size:9px'));
    row.appendChild(mksp(r.url.replace(/^https?:\/\/[^\/]+/,'').slice(0,55),'color:#7d9ab5;font-size:9px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'));
    netPane.appendChild(row);
    netPane.scrollTop=netPane.scrollHeight;
  }
  function appendErrEntry(e){
    var row=document.createElement('div');
    Object.assign(row.style,{padding:'4px 6px 4px 8px',borderBottom:'1px solid rgba(239,68,68,0.15)',
      borderLeft:'2px solid #ef4444',background:'rgba(239,68,68,0.04)'});
    var lbl=document.createElement('span');
    lbl.textContent='s'+e.step+' '+e.label;
    lbl.style.cssText='color:#ef4444;font-weight:700;font-size:8.5px;display:block';
    var msg=document.createElement('span');
    msg.textContent=e.text;
    msg.style.cssText='color:#b91c1c;font-size:9.5px;word-break:break-all;display:block;margin-top:1px';
    row.appendChild(lbl); row.appendChild(msg);
    errPane.appendChild(row);
    errPane.scrollTop=errPane.scrollHeight;
    tabErr.style.color='#ef4444';
  }
  function _bbTick(){
    if(window.__bb_console){
      var arr=window.__bb_console;
      for(var i=window.__bb_console_cur;i<arr.length;i++) appendConEntry(arr[i]);
      window.__bb_console_cur=arr.length;
    }
    if(window.__bb_net){
      var net=window.__bb_net;
      for(var j=window.__bb_net_cur;j<net.length;j++) appendNetEntry(net[j]);
      window.__bb_net_cur=net.length;
    }
    if(window.__bb_errors){
      var errs=window.__bb_errors;
      for(var k=window.__bb_err_cur;k<errs.length;k++) appendErrEntry(errs[k]);
      window.__bb_err_cur=errs.length;
    }
  }
  setInterval(_bbTick,1200);
  window.__bb_devtools_ready=true;
}
"""

_ADD_JS    = "(e) => { if(window.__bbAdd)   window.__bbAdd(e); }"
_BORDER_JS = "(c) => { if(window.__bbBorder) window.__bbBorder(c); }"


# ── AgentDisplay ─────────────────────────────────────────────────────────────

class AgentDisplay(logging.Handler):
    """
    Dual-source display:
      on_step()  → full structured AgentOutput (awaited directly — no create_task lag)
      emit()     → logging handler for tool result confirmations only
    """

    def __init__(self) -> None:
        super().__init__()
        self._agent: Any  = None
        self._step: int   = 0
        self._step_start  = time.monotonic()

    def set_agent(self, agent: Any) -> None:
        self._agent = agent

    # ── Step callback (primary) ───────────────────────────────────────────────
    async def on_step(self, browser_state: Any, agent_output: Any, step_num: int) -> None:
        # CRITICAL: display errors must NEVER propagate to the agent and cause step failures
        try:
            await self._on_step_impl(browser_state, agent_output, step_num)
        except Exception:
            pass

    async def _on_step_impl(self, browser_state: Any, agent_output: Any, step_num: int) -> None:
        self._step       = step_num
        self._step_start = time.monotonic()

        page = await self._get_page()
        if page is None:
            return

        # Try to inject sidebar — silently skip on CSP-restricted pages (YouTube etc.)
        try:
            await page.evaluate(_SIDEBAR_JS)
        except Exception:
            return  # Can't inject on this page (CSP), skip display for this step

        _push_err_js = (
            "(e) => { if(!window.__bb_errors) window.__bb_errors=[]; window.__bb_errors.push(e); }"
        )

        async def push(label, text, color, border=None, is_code=False):
            entry = {
                "label":   label,
                "text":    _esc(text),
                "rawtext": text,
                "color":   color,
                "time":    datetime.now().strftime("%H:%M:%S"),
                "step":    step_num,
                "isCode":  is_code,
            }
            await page.evaluate(_ADD_JS, entry)
            if border:
                await page.evaluate(_BORDER_JS, border)
            # Push retries and failed evals to the ERROR tab
            if label in ("EVAL ✗", "EVAL ~") or "RETRY" in label:
                await page.evaluate(_push_err_js, {"label": label, "text": text, "step": step_num})

        # Step header
        await push("STEP", f"── Step {step_num} ──────────────────────",
                   _C["step"], border=_BORDER["thinking"])

        # Evaluation of previous step
        eval_text = getattr(agent_output, "evaluation_previous_goal", None) or ""
        goal      = getattr(agent_output, "next_goal", None) or ""
        if eval_text:
            low = eval_text.lower()
            if any(w in low for w in ("success", "✅", "achieved", "complete", "done", "correct", "worked")):
                await push("EVAL ✓", eval_text, _C["eval_ok"], border=_BORDER["success"])
            elif any(w in low for w in ("fail", "error", "wrong", "incorrect", "❌", "partial", "mistake")):
                await push("EVAL ✗", eval_text, _C["eval_ng"], border=_BORDER["exploiting"])
            else:
                await push("EVAL ~", eval_text, _C["eval_pt"])

        # Update pinned thinking bar — always visible at top of log area
        if eval_text or goal:
            try:
                await page.evaluate(
                    "(args) => { if(window.__bbSetThinking) window.__bbSetThinking(args.eval_text, args.goal); }",
                    {"eval_text": eval_text[:120] if eval_text else "", "goal": goal[:120] if goal else ""}
                )
            except Exception:
                pass

        # Memory — contains results of previous actions
        memory = getattr(agent_output, "memory", None) or ""
        if memory:
            await push("MEM", memory, _C["memory"])

        # Next goal (already fetched above for the thinking bar)
        if goal:
            await push("GOAL", goal, _C["goal"], border=_BORDER["thinking"])

        # Actions about to execute
        actions = getattr(agent_output, "action", []) or []
        for i, action_model in enumerate(actions):
            try:
                d = action_model.model_dump(exclude_none=True)
            except Exception:
                continue
            if not d:
                continue
            action_name = next(iter(d))
            params = d.get(action_name, {}) or {}

            if action_name == "evaluate":
                code = params.get("code", "")
                label = f"🖥 BROWSER CONSOLE  [{i+1}/{len(actions)}]"
                await push(label, code, _C["exploit"],
                           border=_BORDER["exploiting"], is_code=True)
                # Echo to Eruda console BEFORE execution
                await self._echo_to_eruda(page, code, step_num)

            elif action_name == "write_file":
                fname   = params.get("file_name", "")
                content = params.get("content", "")
                await push(f"FILE WRITE [{i+1}/{len(actions)}]",
                           f"{fname}\n{content[:300]}{'...' if len(content)>300 else ''}",
                           _C["tool"], is_code=True)
            else:
                parts = []
                for k, v in params.items():
                    if v is None:
                        continue
                    sv = str(v)
                    if k in ("url", "text", "value", "selector", "keys", "content", "code"):
                        parts.append(f"{k}: {sv}")
                    else:
                        parts.append(f"{k}: {sv[:80]}")
                exploit_acts = {"input", "send_keys", "fill", "type"}
                border = _BORDER["exploiting"] if action_name in exploit_acts else _BORDER["thinking"]
                await push(f"ACT [{i+1}/{len(actions)}]",
                           f"{action_name}  {'  '.join(parts)}",
                           _C["action"], border=border)

    # ── Logging handler (tool results only) ──────────────────────────────────
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Skip noisy internal browser-use infrastructure loggers
            if _is_noisy(record.name):
                return
            msg = _strip(record.getMessage())
            self._route_log(msg, record.levelname)
        except Exception:
            pass

    def _route_log(self, msg: str, level: str) -> None:
        if "🔗 Navigated" in msg:
            self._fire("NAV →", re.sub(r"🔗\s*Navigated to\s*", "", msg), _C["tool"])
        elif "🖱️" in msg:
            self._fire("CLICK →", re.sub(r"🖱️\s*Clicked?\s*", "", msg)[:120], _C["tool"])
        elif "⌨" in msg:
            text = re.sub(r"⌨️?\s*(?:Typed?|Sent keys?:?)\s*", "", msg)[:120]
            self._fire("TYPE →", text, _C["tool"], border=_BORDER["exploiting"])
        elif "💾" in msg:
            self._fire("FILE →", re.sub(r"💾\s*", "", msg)[:100], _C["tool"])
        elif "🗑️" in msg:
            self._fire("CLOSE →", re.sub(r"🗑️\s*", "", msg)[:80], _C["tool"])
        elif "🔄" in msg:
            self._fire("SWITCH →", re.sub(r"🔄\s*", "", msg)[:80], _C["tool"])
        elif "🔍" in msg and "DOMWatchdog" not in msg and "dom_tree" not in msg:
            self._fire("FIND →", re.sub(r"🔍\s*", "", msg)[:100], _C["tool"])
        elif "❌ Result failed" in msg:
            m = re.search(r"failed (\d+)/(\d+) times:\s*(.*)", msg, re.DOTALL)
            if m:
                self._fire(f"RETRY {m.group(1)}/{m.group(2)}",
                           m.group(3).strip()[:160], _C["retry"])
        elif "❌ Stopping" in msg:
            self._fire("FATAL", msg, _C["error"], border=_BORDER["exploiting"])
        elif "📋 Plan" in msg:
            self._fire("PLAN", msg, _C["tool"])

    # ── Internal helpers ──────────────────────────────────────────────────────
    async def _echo_to_eruda(self, page: Any, code: str, step_num: int) -> None:
        """Echo code to console.log so it appears in Eruda's Console tab."""
        try:
            safe = json.dumps(code)
            js = (
                f"(()=>{{try{{"
                f"console.group('%c[BLACKBOX] Step {step_num} — Console Execute',"
                f"'color:#ef4444;font-weight:bold;font-size:12px');"
                f"console.log('%cCode:','color:#fca5a5;font-weight:bold');"
                f"console.log({safe});"
                f"console.log('%c— result will appear below after execution —','color:#374151;font-style:italic');"
                f"console.groupEnd();"
                f"}}catch(_){{}}}})()"
            )
            await page.evaluate(js)
        except Exception:
            pass

    def _fire(self, label: str, text: str, color: str,
              border: str | None = None, is_code: bool = False) -> None:
        """Fire a sidebar update from the synchronous logging handler."""
        if not self._agent:
            return
        entry = {
            "label":   label,
            "text":    _esc(text),
            "rawtext": text,
            "color":   color,
            "time":    datetime.now().strftime("%H:%M:%S"),
            "step":    self._step,
            "isCode":  is_code,
        }
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._push_log_entry(entry, border))
        except Exception:
            pass

    async def _push_log_entry(self, entry: dict, border: str | None) -> None:
        """Used only by the sync logging handler path."""
        page = await self._get_page()
        if page is None:
            return
        try:
            await page.evaluate(_ADD_JS, entry)
            if border:
                await page.evaluate(_BORDER_JS, border)
        except Exception:
            pass

    async def _get_page(self) -> Any:
        try:
            return await self._agent.browser_session.get_current_page()
        except Exception:
            return None


# ── Public setup ─────────────────────────────────────────────────────────────

def attach(agent: Any) -> AgentDisplay:
    """
    Call AFTER Agent is created.
    Attaches display as both a step callback and a logging handler.
    """
    display = AgentDisplay()
    display.set_agent(agent)

    # Primary: structured step callback (async, direct await)
    agent.register_new_step_callback = display.on_step

    # Secondary: tool result logs only (filtered)
    display.setLevel(logging.INFO)
    for name in [
        "browser_use.agent.service",   # step reasoning: eval/mem/goal/retries
        "browser_use.tools.service",   # tool results: nav/click/type/file
    ]:
        lg = logging.getLogger(name)
        lg.addHandler(display)
        lg.setLevel(logging.INFO)

    # Silence noise
    for name in ["httpx", "anthropic", "urllib3", "asyncio", "posthog"]:
        logging.getLogger(name).setLevel(logging.ERROR)

    return display
