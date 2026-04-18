"""Shell — root terminal UI + /exec POST endpoint. Approve auth.

GET /shell  → shell UI (embedded HTML)
POST /exec  → bash/powershell, 30s timeout, text/plain output.
"""
DESCRIPTION = "/shell UI + /exec POST. Approve auth."
AUTH = "approve"
import base64, platform, subprocess

SHELL_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>elastik shell</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{background:#1a1a2e;color:#e0e0e0;font-family:'Cascadia Code','Fira Code','SF Mono',Consolas,monospace;font-size:16px;-webkit-text-size-adjust:100%;display:flex;flex-direction:column}
#output{flex:1;overflow-y:auto;padding:12px;white-space:pre-wrap;word-break:break-all;line-height:1.4}
#bottom{background:#1a1a2e;border-top:1px solid #2a2a4e;padding:8px 12px;flex-shrink:0}
#tab-bar{display:flex;gap:6px;padding:0 0 6px;overflow-x:auto;-webkit-overflow-scrolling:touch}
#tab-bar:empty{display:none;padding:0}
#input-line{display:flex;align-items:center}
#prompt{color:#7fdbca;margin-right:8px;white-space:nowrap}
#cmd{flex:1;background:transparent;border:none;outline:none;color:#e0e0e0;font:inherit;caret-color:#7fdbca;font-size:16px}
.out{color:#ccc}.err{color:#ff6b6b}.info{color:#7fdbca}
.tab-btn{background:#2a2a4e;color:#7fdbca;border:1px solid #3a3a5e;border-radius:4px;padding:6px 12px;font:inherit;font-size:14px;white-space:nowrap;cursor:pointer;-webkit-tap-highlight-color:transparent}
.tab-btn:active{background:#3a3a6e}
.render-frame{display:block;border:1px solid #3a3a5e;border-radius:4px;margin:4px 0;background:#fff;width:100%;height:70vh;white-space:normal;word-break:normal}
.render-root{display:block;border:1px solid #ff6b6b;border-radius:4px;margin:4px 0;background:#fff;padding:0;overflow:auto;color:#000;white-space:normal;word-break:normal}
.warn{color:#ffd93d}
</style></head><body>
<div id="output"></div>
<div id="bottom"><div id="tab-bar"></div><div id="input-line"><span id="prompt"></span><input id="cmd" autofocus spellcheck="false" autocapitalize="none" autocomplete="off" autocorrect="off"></div></div>
<script>
const out=document.getElementById('output'),cmd=document.getElementById('cmd'),promptEl=document.getElementById('prompt');
let _cwd='',_history=[],_hi=-1,_worlds=[],_pending=null,_user='__ELASTIK_USER__';

// fetch world list for tab completion
fetch('/proc/worlds').then(r=>r.json()).then(d=>{_worlds=d.map(s=>s.name)}).catch(()=>{});

// convenience object: el.r('work') → GET, el.w('work','hi') → PUT, el.a → POST (append)
const el={
  r:(w)=>fetch('/home/'+(w||_cwd)).then(r=>r.json()),
  w:(w,b)=>fetch('/home/'+(w||_cwd),{method:'PUT',body:b}).then(r=>r.json()),
  a:(w,b)=>fetch('/home/'+(w||_cwd),{method:'POST',body:b}).then(r=>r.json()),
  stages:()=>fetch('/proc/worlds').then(r=>r.json()),
  grep:(q,w)=>fetch('/grep?q='+encodeURIComponent(q)+(w?'&world='+w:'')).then(r=>r.text()),
  post:(url,body,headers)=>fetch('/postman',{method:'POST',body:JSON.stringify({url,method:'POST',body,headers:headers||{}})}).then(r=>r.text()),
  get:(url)=>fetch('/fetch?url='+encodeURIComponent(url)).then(r=>r.text()),
  exec:(cmd)=>fetch('/exec',{method:'POST',body:cmd}).then(r=>r.text()),
};

function updatePrompt(){
  promptEl.textContent=(_user?_user+'@':'')+'elastik:'+(_cwd||'~')+'$';
}

function appendOut(text,cls){
  if(text===undefined||text===null)text='';
  if(typeof text==='object')try{text=JSON.stringify(text,null,2)}catch(e){text=String(text)}
  else text=String(text);
  const span=document.createElement('span');
  span.className=cls||'out';
  span.textContent=text+'\n';
  out.appendChild(span);
  out.scrollTop=out.scrollHeight;
}

function appendPromptLine(input){
  const span=document.createElement('span');
  span.className='info';
  span.textContent='elastik:'+(_cwd||'~')+'$ '+input+'\n';
  out.appendChild(span);
}

// ── render helpers ──────────────────────────────────────────────────
function renderRoot(html){
  const div=document.createElement('div');
  div.className='render-root';
  div.innerHTML=html;
  div.querySelectorAll('script').forEach(s=>{
    const ns=document.createElement('script');
    if(s.src)ns.src=s.src; else ns.textContent=s.textContent;
    s.replaceWith(ns);
  });
  out.appendChild(div);
  out.scrollTop=out.scrollHeight;
}

function renderSafe(html){
  const iframe=document.createElement('iframe');
  iframe.sandbox='allow-scripts allow-popups';
  iframe.className='render-frame';
  iframe.srcdoc=html;
  out.appendChild(iframe);
  out.scrollTop=out.scrollHeight;
}

function previewSource(html){
  const lines=html.split('\n');
  const preview=lines.slice(0,8).join('\n');
  const suffix=lines.length>8?'\n  ... ('+lines.length+' lines total)':'';
  return preview+suffix;
}

// ── command parser ──────────────────────────────────────────────────
function parseCommand(input){
  const parts=input.match(/(?:[^\s"]+|"[^"]*")+/g)||[];
  if(!parts.length)return undefined;
  const c=parts[0].toLowerCase();

  // !command → system shell (/exec)
  if(input.startsWith('!')){
    const sh=input.slice(1);
    if(!sh)return{v:'usage: !<command>'};
    return{p:fetch('/exec',{method:'POST',body:sh}).then(r=>{if(!r.ok)throw new Error('exec: '+r.status);return r.text()}).then(t=>t||'(no output)')};
  }

  if(c==='help')return{v:'\n  Worlds\n    ls                  list worlds\n    cat [world]         render (root, confirm y/n)\n    cat -s [world]      view source (safe)\n    cat --safe [world]  render in sandbox (iframe)\n    open [world]        open in new tab (user)\n    open! [world]       open in new tab (root)\n    cd <world>          set context\n    pwd                 show current world\n    echo text > world   write\n    echo text >> world  append\n\n  Search\n    grep <q> [world]    search lines\n    head [-n N] [world] first N lines\n    tail [-n N] [world] last N lines\n    wc [world]          count lines/words/bytes\n\n  Network\n    curl <url>          proxy request\n    whoami              node info\n\n  System Shell\n    !<command>          run in bash/powershell\n    !ls /tmp            list real files\n    !pwd                real working directory\n\n  JS API (el object)\n    el.r(world)         read world → Promise\n    el.w(world, html)   write world → Promise\n    el.a(world, html)   append to world → Promise\n    el.stages()         list all worlds → Promise\n    el.exec(cmd)        system shell → Promise\n    el.grep(q, world)   search world → Promise\n    el.get(url)         proxy GET → Promise\n    el.post(url, body)  proxy POST → Promise\n\n  Shell\n    history             past commands\n    clear               clear screen\n    help                this\n\n  Anything else runs as JavaScript.\n  Tab or tap to autocomplete. Type el. to see API methods.'};
  if(c==='pwd')return{v:_cwd||'~'};
  if(c==='clear')return{v:(out.textContent='',null)};
  if(c==='history')return{v:_history.map((h,i)=>(i+1)+' '+h).join('\n')||'(empty)'};
  if(c==='ls')return{p:fetch('/proc/worlds').then(r=>r.json()).then(d=>d.map(s=>s.name+'  (v'+s.version+')').join('\n')||'(no worlds)')};
  if(c==='whoami')return{p:fetch('/whoami').then(r=>r.text())};

  if(c==='cd'){
    const target=parts[1]||'';
    if(!target||target==='~'||target==='..'){_cwd='';updatePrompt();return{v:'~'};}
    _cwd=target;updatePrompt();return{v:_cwd};
  }

  if(c==='cat'){
    let mode='root',i=1;
    if(parts[i]==='-s'){mode='source';i++;}
    else if(parts[i]==='--safe'){mode='safe';i++;}
    const w=parts[i]||_cwd;
    if(!w)return{v:'usage: cat [-s|--safe] <world>'};
    if(mode==='source'){
      return{p:fetch('/home/'+w).then(r=>r.json()).then(d=>d.stage_html||'(empty)')};
    }
    if(mode==='safe'){
      return{p:fetch('/home/'+w).then(r=>r.json()).then(d=>{
        if(!d.stage_html)return '(empty)';
        renderSafe(d.stage_html);
        return null;
      })};
    }
    return{p:fetch('/home/'+w).then(r=>r.json()).then(d=>{
      if(!d.stage_html)return '(empty)';
      appendOut(previewSource(d.stage_html));
      appendOut('Render in root context? [y/n]','warn');
      _pending={type:'render',html:d.stage_html};
      return null;
    })};
  }

  if(c==='grep'){
    if(!parts[1])return{v:'usage: grep <query> [world]'};
    const q=parts[1].replace(/^"|"$/g,'');
    const w=parts[2]||_cwd||'';
    return{p:fetch('/grep?q='+encodeURIComponent(q)+(w?'&world='+w:'')).then(r=>r.text()).then(t=>t||'(no matches)')};
  }

  if(c==='head'){
    let n=10,i=1;
    if(parts[i]==='-n'&&parts[i+1]){n=parseInt(parts[i+1]);i+=2;}
    const w=parts[i]||_cwd;
    if(!w)return{v:'usage: head [-n N] <world>'};
    return{p:fetch('/head?world='+w+'&n='+n).then(r=>r.text()).then(t=>t||'(empty)')};
  }

  if(c==='tail'){
    let n=10,i=1;
    if(parts[i]==='-n'&&parts[i+1]){n=parseInt(parts[i+1]);i+=2;}
    const w=parts[i]||_cwd;
    if(!w)return{v:'usage: tail [-n N] <world>'};
    return{p:fetch('/tail?world='+w+'&n='+n).then(r=>r.text()).then(t=>t||'(empty)')};
  }

  if(c==='wc'){
    const w=parts[1]||_cwd;
    if(!w)return{v:'usage: wc <world>'};
    return{p:fetch('/wc?world='+w).then(r=>r.text())};
  }

  if(c==='echo'){
    const raw=input.slice(5).trim();
    const am=raw.match(/^(.+?)\s*>>\s*(\S+)$/);
    const wm=raw.match(/^(.+?)\s*>\s*(\S+)$/);
    if(am)return{p:fetch('/home/'+am[2],{method:'POST',body:am[1]}).then(r=>r.json())};
    if(wm)return{p:fetch('/home/'+wm[2],{method:'PUT',body:wm[1]}).then(r=>r.json())};
    return{v:raw};
  }

  if(c==='open'||c==='open!'){
    const w=parts[1]||_cwd;
    if(!w)return{v:'usage: open[!] <world>'};
    if(c==='open!'){
      window.open('/view/'+w,'_blank');
      return{v:'opened /view/'+w+' (root)'};
    }
    window.open('/'+w,'_blank');
    return{v:'opened /'+w};
  }

  if(c==='curl'){
    const url=parts[1]||'';
    if(!url)return{v:'usage: curl <url>'};
    return{p:fetch('/fetch?url='+encodeURIComponent(url)).then(r=>r.text())};
  }

  return undefined;
}

// ── tab completion ─────
const CMDS=['ls','cd','cat','open','open!','grep','head','tail','wc','echo','curl','whoami','history','clear','help','pwd'];
const EL_METHODS=['el.r(','el.w(','el.a(','el.stages()','el.exec(','el.grep(','el.get(','el.post('];
const tabBar=document.getElementById('tab-bar');

function doComplete(){
  const val=cmd.value;
  const parts=val.split(/\s+/);
  const last=parts[parts.length-1];
  if(cmd.value.startsWith('el.')){const m=EL_METHODS.filter(x=>x.startsWith(cmd.value));if(m.length===1)cmd.value=m[0];showTabs(m);return}
  const pool=parts.length<=1?CMDS:_worlds;
  if(!last){showTabs(pool);return}
  const matches=pool.filter(x=>x.startsWith(last));
  if(matches.length===1){
    parts[parts.length-1]=matches[0];
    cmd.value=parts.join(' ')+' ';
    hideTabs();
  }else if(matches.length>1){
    showTabs(matches);
  }else{hideTabs()}
}

function showTabs(items){
  tabBar.innerHTML='';
  items.slice(0,20).forEach(t=>{
    const b=document.createElement('button');
    b.className='tab-btn';b.textContent=t;
    b.onclick=e=>{
      e.preventDefault();
      const parts=cmd.value.split(/\s+/);
      if(parts.length<=1)cmd.value=t+' ';
      else{parts[parts.length-1]=t;cmd.value=parts.join(' ')+' ';}
      hideTabs();cmd.focus();
    };
    tabBar.appendChild(b);
  });
}
function hideTabs(){tabBar.innerHTML=''}

cmd.addEventListener('focus',()=>{if(!cmd.value)showTabs(CMDS)});
cmd.addEventListener('input',()=>{
  if(!cmd.value){showTabs(CMDS);return}
  if(cmd.value.startsWith('el.')){const m=EL_METHODS.filter(x=>x.startsWith(cmd.value));showTabs(m);return}
  const parts=cmd.value.split(/\s+/);
  const last=parts[parts.length-1];
  const pool=parts.length<=1?CMDS:_worlds;
  const matches=pool.filter(x=>x.startsWith(last));
  if(matches.length>0&&matches.length<=20)showTabs(matches);
  else hideTabs();
});

// ── input handling ──────────────────────────────────────────────────
cmd.addEventListener('keydown',function(e){
  if(e.key==='Tab'){e.preventDefault();doComplete();return}
  if(e.key==='ArrowUp'){
    e.preventDefault();
    if(_hi<_history.length-1){_hi++;cmd.value=_history[_history.length-1-_hi];}
    return;
  }
  if(e.key==='ArrowDown'){
    e.preventDefault();
    if(_hi>0){_hi--;cmd.value=_history[_history.length-1-_hi];}
    else{_hi=-1;cmd.value='';}
    return;
  }
  if(e.key!=='Enter')return;
  execInput();
});

function execInput(){
  const input=cmd.value.trim();
  cmd.value='';
  _hi=-1;
  hideTabs();
  if(!input)return;

  if(_pending){
    const p=_pending;
    _pending=null;
    appendPromptLine(input);
    if(input.toLowerCase()==='y'||input.toLowerCase()==='yes'){
      renderRoot(p.html);
    }else{
      appendOut('cancelled.','info');
    }
    return;
  }

  _history.push(input);
  appendPromptLine(input);

  const parsed=parseCommand(input);
  if(parsed!==undefined){
    if(parsed===null)return;
    if(parsed.v!==undefined){
      if(parsed.v!==null)appendOut(parsed.v);
    }else if(parsed.p){
      parsed.p.then(r=>{if(r!==null&&r!==undefined)appendOut(r)}).catch(e=>appendOut('ERROR: '+e.message,'err'));
    }
    return;
  }

  try{
    const r=eval(input);
    if(r&&typeof r.then==='function'){
      r.then(v=>appendOut(v)).catch(e=>appendOut('ERROR: '+e.message,'err'));
    }else{
      if(r!==undefined)appendOut(r);
    }
  }catch(e){appendOut('ERROR: '+e.message,'err');}
}

document.addEventListener('click',e=>{if(!e.target.closest('.tab-btn'))cmd.focus()});
updatePrompt();
appendOut('elastik shell — type help for commands, or any JS.\n','info');
const _q=new URLSearchParams(location.search).get('q');
if(_q){appendPromptLine(_q);const _p=parseCommand(_q);if(_p!==undefined){if(_p&&_p.p)_p.p.then(r=>{if(r!=null)appendOut(r)}).catch(e=>appendOut('ERROR: '+e.message,'err'));else if(_p&&_p.v!==undefined&&_p.v!==null)appendOut(_p.v)}}
</script></body></html>"""


def _basic_user(scope):
    """Extract user field from Basic Auth header (already validated by server)."""
    for k, v in scope.get("headers", []):
        if k == b"authorization":
            try: return base64.b64decode(v.decode()[6:]).decode().split(":", 1)[0]
            except (ValueError, UnicodeDecodeError): return ""
    return ""


async def handle(method, body, params):
    scope = params.get("_scope", {})
    path = scope.get("path", "")
    if method == "GET" and path == "/shell":
        html = SHELL_HTML.replace("__ELASTIK_USER__", _basic_user(scope))
        return {"_html": html}
    if method == "POST" and path == "/exec":
        sh = ["powershell", "-Command", body] if platform.system() == "Windows" else ["bash", "-c", body]
        try:
            r = subprocess.run(sh, capture_output=True, timeout=30, text=True)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            out = "(timeout after 30s)"
        return {"_body": out, "_ct": "text/plain"}
    return {"error": "method not allowed", "_status": 405}


ROUTES = ["/shell", "/exec"]
