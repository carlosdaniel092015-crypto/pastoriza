"""HTML del panel (una sola página, vanilla JS, sin dependencias externas).

Diseño "planta de inyección": paleta grafito/resina/señal, tipografía del sistema
(sans para UI, monoespaciada para datos), navegación numerada. Toda la lógica habla
con /panel/api/* (ver app/panel/router.py).
"""
from __future__ import annotations

PANEL_HTML = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Panel · Pastoriza Bot</title>
<style>
  :root{
    --bg:#16150F; --panel:#1c1b14; --panel2:#242219; --line:#332f24;
    --tx:#EDE8DC; --mut:#8A8474; --senal:#C8571E; --cinta:#D9B95C; --err:#C0553D;
    --sans:system-ui,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,"Cascadia Code","Consolas","Courier New",monospace;
    --r:5px;
  }
  :root[data-theme="light"]{
    --bg:#F3F0E8; --panel:#ECE7DB; --panel2:#E3DDCD; --line:#D3CBB8;
    --tx:#23211A; --mut:#7C755F; --senal:#B84E17; --cinta:#9A7A1E; --err:#A93226;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 var(--sans)}
  h1,h2,h3{margin:0;font-weight:700;letter-spacing:-.01em}
  .mono{font-family:var(--mono)}
  .eyebrow{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut)}
  button{font:inherit;cursor:pointer;color:inherit}
  ::selection{background:var(--senal);color:#fff}
  :focus-visible{outline:2px solid var(--senal);outline-offset:1px}

  .app{display:grid;grid-template-rows:56px 1fr;height:100vh}

  /* Header */
  header{display:flex;align-items:center;gap:18px;padding:0 18px;background:var(--panel);border-bottom:1px solid var(--line)}
  .brand{font-weight:800;font-size:15px;line-height:1.05;letter-spacing:-.02em}
  .live{display:flex;align-items:center;gap:7px;font-size:13px}
  .live .dot{width:8px;height:8px;border-radius:50%;background:var(--senal)}
  .live .dot.off{background:var(--mut)} .live .dot.bad{background:var(--err)}
  .stats{display:flex;align-items:center;gap:16px}
  .stat{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
  .stat b{font-family:var(--mono);font-size:14px;color:var(--tx);font-weight:700;margin-right:4px}
  .stat.acc b{color:var(--senal)} .stat.rev b{color:var(--cinta)}
  .sp{margin-left:auto}
  .htime{font-family:var(--mono);font-size:12px;color:var(--mut)}
  .kbtn{background:transparent;border:1px solid var(--line);color:var(--tx);padding:7px 14px;border-radius:var(--r);font-size:13px}
  .kbtn:hover{border-color:var(--senal)}

  /* Body */
  .body{display:grid;grid-template-columns:154px 1fr;min-height:0}
  nav.side{background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:8px 0}
  nav.side .it{display:flex;align-items:center;gap:10px;padding:11px 14px;background:transparent;border:0;border-left:3px solid transparent;color:var(--mut);width:100%;text-align:left;font-size:14px}
  nav.side .it .n{font-family:var(--mono);font-size:11px;opacity:.7}
  nav.side .it:hover{color:var(--tx)}
  nav.side .it.active{color:var(--tx);border-left-color:var(--senal);background:linear-gradient(90deg,rgba(200,87,30,.10),transparent)}
  nav.side .it.active .n{color:var(--senal);opacity:1}
  nav.side .it .badge{margin-left:auto;min-width:16px;height:16px;padding:0 4px;background:var(--err);color:#fff;border-radius:8px;font-size:10px;line-height:16px;text-align:center;font-family:var(--mono)}
  nav.side .foot{margin-top:auto;padding:10px 14px}
  .themebtn{width:34px;height:34px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--mut);font-size:15px}

  main{min-width:0;min-height:0;overflow:hidden}
  .view{display:none;height:100%}
  .view.active{display:block}

  /* Conversaciones */
  .conv{display:grid;grid-template-columns:290px 1fr;height:100%}
  .chats{border-right:1px solid var(--line);overflow-y:auto;display:flex;flex-direction:column}
  .chdr{display:flex;align-items:center;padding:12px 16px 8px;color:var(--mut)}
  .chdr .c{font-family:var(--mono);margin-left:6px;color:var(--tx)}
  .chdr .col{margin-left:auto;background:transparent;border:0;color:var(--mut);font-size:16px}
  .chat{display:block;width:100%;text-align:left;background:transparent;border:0;border-bottom:1px solid var(--line);
    border-left:3px solid transparent;padding:11px 15px}
  .chat:hover{background:var(--panel)} .chat.sel{background:var(--panel2);border-left-color:var(--senal)}
  .chat .top{display:flex;align-items:center;gap:8px;margin-bottom:3px}
  .chat .n{font-weight:600;font-size:14px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .chat .n.num{font-family:var(--mono);font-weight:500;letter-spacing:-.02em}
  .chat .h{font-family:var(--mono);font-size:11px;color:var(--mut)}
  .chat .m{color:var(--mut);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .adot{width:7px;height:7px;border-radius:50%;background:var(--senal);flex:none}
  .ttag{font-size:9.5px;color:var(--mut);border:1px solid var(--line);border-radius:3px;padding:0 5px;letter-spacing:.06em}

  .thread{display:flex;flex-direction:column;height:100%;min-width:0;min-height:0}
  .thead{padding:12px 20px;border-bottom:1px solid var(--line)}
  .thead .row1{display:flex;align-items:flex-start}
  .thead .cn{font-family:var(--mono);font-weight:700;font-size:15px}
  .thead .cs{color:var(--mut);font-size:12px;margin-top:2px}
  .thead .date{margin-left:auto}
  .thead .acts{display:flex;gap:8px;margin-top:11px}
  .abtn{background:transparent;border:1px solid var(--line);color:var(--tx);padding:6px 12px;border-radius:var(--r);font-size:12.5px}
  .abtn:hover{border-color:var(--senal)}
  .abtn.mut{color:var(--mut)}
  .msgs{flex:1;min-height:0;overflow-y:auto;padding:22px 26px;display:flex;flex-direction:column;gap:14px}
  .row{display:flex;flex-direction:column;max-width:66%}
  .row.l{align-self:flex-start;align-items:flex-start}
  .row.r{align-self:flex-end;align-items:flex-end}
  .bub{padding:11px 15px;border-radius:10px;white-space:pre-wrap;word-wrap:break-word;font-size:14.5px;line-height:1.5}
  .bub.user{background:var(--panel);border-radius:10px 10px 10px 3px}
  .bub.bot{background:var(--panel2);border-left:3px solid var(--senal);border-radius:10px 10px 3px 10px}
  .bub.super{background:var(--panel2);border-left:3px solid var(--cinta);border-radius:10px 10px 3px 10px}
  .btime{font-family:var(--mono);font-size:10.5px;color:var(--mut);margin-top:4px}
  .pchips{display:flex;flex-direction:column;gap:6px;margin-top:10px}
  .pchip{display:flex;align-items:center;gap:10px;background:var(--bg);border:1px solid var(--line);border-radius:var(--r);padding:6px 10px}
  .pchip .sku{font-family:var(--mono);font-size:11px;color:var(--mut)}
  .pchip .pn{font-size:13px;flex:1}
  .pchip .pp{font-family:var(--mono);font-size:12.5px;color:var(--senal)}
  .badge-esc{display:inline-block;background:var(--senal);color:#160b03;font-size:10px;font-weight:700;letter-spacing:.06em;
    text-transform:uppercase;padding:2px 8px;border-radius:3px;margin-bottom:8px}
  .tool{align-self:flex-start;color:var(--mut);font-family:var(--mono);font-size:11px;background:transparent;border:0;padding:0;letter-spacing:.02em}
  .tfoot{padding:11px 20px;border-top:1px solid var(--line);color:var(--mut)}
  .replyrow{display:flex;gap:8px;padding:10px 20px;border-top:1px solid var(--line);background:var(--panel)}
  .replyrow input{flex:1;background:var(--bg);border:1px solid var(--line);color:var(--tx);padding:9px 12px;border-radius:var(--r)}
  .empty{color:var(--mut);padding:44px 20px;text-align:center;font-size:13px}

  /* Botones genéricos */
  .btn{background:var(--senal);color:#160b03;border:0;padding:8px 14px;border-radius:var(--r);font-weight:600;font-size:13px}
  .btn.sec{background:var(--panel2);color:var(--tx);border:1px solid var(--line);font-weight:500}
  .btn.sm{padding:4px 9px;font-size:12px}
  .ok{color:var(--senal)} .bad{color:var(--err)} .warn{color:var(--cinta)}

  .pane{height:100%;overflow-y:auto;padding:20px 24px}
  .pane h2{font-size:20px;margin-bottom:2px}
  .pane .sub{color:var(--mut);font-size:13px;margin-bottom:16px}

  /* Alertas: pestañas subrayadas */
  .atabs{display:flex;gap:22px;border-bottom:1px solid var(--line);margin-bottom:14px}
  .atabs button{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--mut);padding:9px 0;font-size:11.5px;letter-spacing:.07em;text-transform:uppercase}
  .atabs button b{font-family:var(--mono);margin-left:6px;color:var(--tx);font-weight:600}
  .atabs button.on{color:var(--tx);border-bottom-color:var(--senal)}
  .verconv{background:transparent;border:1px solid var(--line);color:var(--tx);padding:5px 12px;border-radius:var(--r);font-size:12px;margin-top:8px}
  .verconv:hover{border-color:var(--senal)}
  /* Aprendizaje */
  .rchip{font-size:10px;letter-spacing:.06em;text-transform:uppercase;border:1px solid var(--line);border-radius:3px;padding:2px 7px;color:var(--mut)}
  .rchip.alto{color:var(--err);border-color:var(--err)} .rchip.bajo{color:var(--senal);border-color:var(--senal)}
  .dupwarn{border-left:3px solid var(--cinta);background:rgba(217,185,92,.06);border-radius:var(--r);padding:9px 12px;margin-bottom:8px;display:flex;align-items:center;gap:12px;color:var(--cinta);font-size:13px}
  .dupwarn button{margin-left:auto}
  .rrow{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:9px 12px;margin-bottom:6px}
  .rrow.dup{border-left:3px solid var(--cinta);background:rgba(217,185,92,.05)}
  .rrow .rn{font-family:var(--mono);font-size:11px;color:var(--mut)}
  .rrow .rt{flex:1}
  .rrow .ro{color:var(--mut);font-size:12px} .rrow .rx{background:transparent;border:0;color:var(--mut);font-size:15px}
  .stbar{display:flex;align-items:center;gap:8px;margin-bottom:12px}
  .stbar .d{width:8px;height:8px;border-radius:50%;background:var(--senal)} .stbar .d.base{background:var(--mut)}
  /* Filtros pill (otros usos) */
  .filters{display:flex;gap:6px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
  .filters .f{background:transparent;border:1px solid var(--line);color:var(--mut);border-radius:999px;padding:4px 12px;font-size:12px}
  .filters .f.on{color:var(--tx);border-color:var(--senal)}
  .daysep{color:var(--mut);font-family:var(--mono);font-size:11px;margin:14px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
  .ev{display:grid;grid-template-columns:56px 1fr;gap:12px;padding:6px 0}
  .ev .ts{font-family:var(--mono);font-size:11px;color:var(--mut);padding-top:2px}
  .ev.turn .b{color:var(--mut);font-size:13px}
  .ev .k{font-size:11px;letter-spacing:.05em;text-transform:uppercase;font-weight:700}
  .ev.control .k{color:var(--tx)} .ev.revision .k,.ev.handoff .k,.ev.manual .k{color:var(--cinta)}
  .ev.error .k{color:var(--err)} .ev.order .k{color:var(--senal)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:10px 12px;margin-top:3px}
  .card.err{border-left:3px solid var(--err)} .card.rev{border-left:3px solid var(--cinta)}
  .meta{color:var(--mut);font-size:12px}
  pre{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:11px;background:var(--bg);border:1px solid var(--line);border-radius:var(--r);padding:8px;overflow-x:auto}

  /* Formularios */
  .group{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;margin-bottom:14px}
  .group h3{font-size:14px} .group .gsub{color:var(--mut);font-size:12px;margin:2px 0 12px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  .fld{display:flex;flex-direction:column;gap:4px}
  .fld label{font-size:12px;color:var(--mut)}
  .fld .hint{font-size:11px;color:var(--mut)}
  .fld input,.fld textarea,.fld select{background:var(--bg);border:1px solid var(--line);color:var(--tx);padding:8px 10px;border-radius:var(--r);font:inherit}
  .prefix{display:flex}
  .prefix span{background:var(--panel2);border:1px solid var(--line);border-right:0;border-radius:var(--r) 0 0 var(--r);padding:8px 10px;color:var(--mut);font-family:var(--mono)}
  .prefix input{border-radius:0 var(--r) var(--r) 0;font-family:var(--mono)}
  textarea{min-height:300px;font-family:var(--mono);font-size:12px;line-height:1.5}
  .savebar{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line);padding:10px 24px;display:flex;align-items:center;gap:12px;margin:0 -24px -20px}
  .promptcols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .editor{display:grid;grid-template-columns:38px 1fr;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
  .gutter{background:var(--panel);color:var(--mut);font-family:var(--mono);font-size:12px;line-height:1.5;text-align:right;padding:8px 6px;overflow:hidden;white-space:pre;user-select:none}
  .editor textarea{min-height:340px;border:0;border-radius:0;background:var(--bg);color:var(--tx)}
  .ro{opacity:.6}

  #tokbar{display:none;gap:8px;align-items:center;padding:8px 16px;background:#3a2a00;border-bottom:1px solid var(--cinta)}
  #tokbar input{background:#000;border:1px solid var(--cinta);color:#fff;padding:6px 10px;border-radius:var(--r)}

  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
  @media (max-width:1080px){.conv{grid-template-columns:230px 1fr}.promptcols{grid-template-columns:1fr}.stats{gap:10px}}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">Pastoriza<br>Bot</div>
    <div class="live"><span class="dot" id="hdot"></span><span id="pwlabel">Bot activo</span></div>
    <div class="stats">
      <span class="stat"><b id="stChats">0</b>chats hoy</span>
      <span class="stat acc"><b id="stAsesor">0</b>con asesor</span>
      <span class="stat rev"><b id="stRev">0</b>revisar</span>
    </div>
    <span class="sp"></span>
    <span class="htime" id="htime"></span>
    <button class="kbtn" id="pwbtn" onclick="toggleGlobal()">Pausar bot</button>
  </header>

  <div id="tokbar">
    <span>🔒 Token del panel:</span>
    <input id="tokin" type="password" placeholder="PANEL_TOKEN"/>
    <button class="btn" onclick="setTok()">Entrar</button>
    <span class="meta" id="tokmsg"></span>
  </div>

  <div class="body">
    <nav class="side" id="side">
      <button class="it active" data-v="conv"><span class="n">01</span>Conversaciones</button>
      <button class="it" data-v="alertas"><span class="n">02</span>Alertas<span class="badge" id="badge" style="display:none">0</span></button>
      <button class="it" data-v="config"><span class="n">03</span>Config</button>
      <button class="it" data-v="prompt"><span class="n">04</span>Prompt</button>
      <button class="it" data-v="aprendizaje"><span class="n">05</span>Aprendizaje<span class="badge" id="badgeSug" style="display:none">0</span></button>
      <div class="foot"><button class="themebtn" onclick="toggleTheme()" id="themebtn" title="Cambiar tema">☾</button></div>
    </nav>

    <main>
      <!-- Conversaciones -->
      <section class="view active" id="v-conv">
        <div class="conv">
          <div class="chats">
            <div class="chdr"><span class="eyebrow">Conversaciones</span><span class="c mono" id="chcount">0</span><button class="col" title="Colapsar">‹</button></div>
            <div id="chats"><div class="empty">Cargando…</div></div>
          </div>
          <div class="thread">
            <div class="thead" id="thead"><div class="row1"><div><div class="cn">Elegí una conversación</div></div></div></div>
            <div class="msgs" id="msgs"><div class="empty">Selecciona un chat de la izquierda.</div></div>
            <div class="replyrow" id="replyrow">
              <input id="rin" placeholder="Escribe como asesor (pausa el bot 30 min)…" onkeydown="if(event.key==='Enter')responder()"/>
              <button class="btn" onclick="responder()">Enviar</button>
            </div>
            <div class="tfoot eyebrow" id="tfoot">El bot responde solo · escribe desde WhatsApp Web si hace falta</div>
          </div>
        </div>
      </section>

      <!-- Alertas -->
      <section class="view" id="v-alertas">
        <div class="pane">
          <h2>Alertas</h2>
          <div class="sub">Todo lo que pasa, en vivo: respuestas, cambios, avisos y errores.</div>
          <div class="atabs" id="filtros"></div>
          <div id="feed"><div class="empty">—</div></div>
        </div>
      </section>

      <!-- Config -->
      <section class="view" id="v-config">
        <div class="pane">
          <h2>Configuración del negocio</h2>
          <div class="sub">Lo que el bot le dice al cliente. Los cambios se aplican al instante.</div>
          <div id="cfg"></div>
          <div class="savebar"><span class="meta" id="cfgmsg">Sin cambios sin guardar.</span><span class="sp"></span><button class="btn sec" onclick="loadCfg()">Descartar</button><button class="btn" onclick="saveCfg()">Guardar cambios</button></div>
        </div>
      </section>

      <!-- Prompt -->
      <section class="view" id="v-prompt">
        <div class="pane">
          <h2>Prompts de los agentes</h2>
          <div class="sub">Cada agente tiene su instrucción. Vacío = usa el .md base. Mínimo 40 caracteres.</div>
          <div class="filters">
            <label class="meta">Agente:</label>
            <select id="pagente" class="mono" onchange="mostrarPrompt()" style="background:var(--panel2);border:1px solid var(--line);color:var(--tx);padding:6px 10px;border-radius:var(--r)"></select>
            <span class="sp"></span>
            <label class="btn sec" style="cursor:pointer">Subir .md<input type="file" accept=".md,.txt,text/markdown" style="display:none" onchange="subirMd(event)"/></label>
            <button class="btn sec" onclick="resetPrompt()">Volver al prompt base</button>
            <button class="btn" onclick="savePrompt()">Guardar</button>
          </div>
          <div class="stbar"><span class="d" id="pdot"></span><b id="pstate"></b><span class="meta" id="pmsg"></span></div>
          <div class="promptcols">
            <div>
              <div class="eyebrow" style="margin-bottom:6px">Override editable</div>
              <div class="editor"><div class="gutter" id="pgut">1</div><textarea id="pov" oninput="syncGutter()" onscroll="syncGutter()"></textarea></div>
            </div>
            <div>
              <div class="eyebrow" style="margin-bottom:6px">Prompt base · solo lectura</div>
              <div class="editor"><div class="gutter" id="pgutb">1</div><textarea id="pbase" class="ro" readonly onscroll="syncGutterBase()"></textarea></div>
            </div>
          </div>
        </div>
      </section>

      <!-- Aprendizaje -->
      <section class="view" id="v-aprendizaje">
        <div class="pane">
          <h2>Aprendizaje</h2>
          <div class="sub">El bot mejora contigo: aprueba sugerencias, agrega reglas y enséñale con correcciones.</div>
          <div class="filters">
            <button class="btn" onclick="analizar()">Analizar fallos y proponer</button>
            <span class="meta" id="anmsg">Revisa los casos recientes y propone reglas.</span>
          </div>
          <h3 style="margin:16px 0 6px">Sugerencias pendientes <span class="meta mono" id="hsug">0</span></h3><div id="sugs" class="meta">—</div>
          <h3 style="margin:22px 0 6px">Reglas activas <span class="meta mono" id="hreg">0</span></h3>
          <div class="filters"><input id="regin" placeholder="Ej: Al interior del país, aclara mínimo 3 días hábiles." style="flex:1;background:var(--bg);border:1px solid var(--line);color:var(--tx);padding:8px 10px;border-radius:var(--r)"/><button class="btn" onclick="addRegla()">Agregar</button></div>
          <div id="reglas" class="meta" style="margin-top:8px">—</div>
          <h3 style="margin:22px 0 6px">Correcciones</h3>
          <div class="sub">Cuando el bot conteste algo que no va, escribe la situación y la respuesta correcta. La próxima vez la usa.</div>
          <div class="fld" style="margin-bottom:10px"><label>Situación</label><textarea id="corsit" style="min-height:56px" placeholder="El cliente pregunta por descuento de 100 unidades o más"></textarea></div>
          <div class="fld"><label>Respuesta correcta</label><textarea id="corresp" style="min-height:56px" placeholder="Dile que el precio por volumen lo confirma un asesor y pásalo enseguida"></textarea></div>
          <div class="filters" style="margin-top:10px"><button class="btn" onclick="addCorreccion()">Guardar corrección</button></div>
          <div id="correcciones" class="meta" style="margin-top:8px">—</div>
        </div>
      </section>
    </main>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
let TOKEN = localStorage.getItem('panel_token') || '';
let lastEventId=0, selChat=null, chatsCache=[], alertCount=0, alerts=[], filtro='todos', prodMap={}, curItems=[];
const TITULOS={conv:'Conversaciones',alertas:'Alertas',config:'Config',prompt:'Prompt',aprendizaje:'Aprendizaje'};

function headers(){ return TOKEN?{'X-Panel-Token':TOKEN,'Content-Type':'application/json'}:{'Content-Type':'application/json'}; }
async function api(path,opt){ const r=await fetch('/panel/api'+path,{headers:headers(),...(opt||{})});
  if(r.status===401){ $('#tokbar').style.display='flex'; $('#tokmsg').textContent='Token requerido o inválido.'; throw new Error('401'); }
  if(!r.ok) throw new Error('http '+r.status); return r.json(); }
function setTok(){ TOKEN=$('#tokin').value.trim(); localStorage.setItem('panel_token',TOKEN); $('#tokbar').style.display='none'; boot(); }
function fmtTime(t){ if(!t)return''; return new Date(t*1000).toLocaleTimeString('es-DO',{hour:'2-digit',minute:'2-digit'}); }
function fmtDay(t){ if(!t)return''; return new Date(t*1000).toLocaleDateString('es-DO',{weekday:'long',day:'2-digit',month:'2-digit'}); }
function fmtRel(t){ if(!t)return''; const s=(Date.now()/1000)-t; if(s<60)return'ahora'; if(s<3600)return Math.floor(s/60)+' min'; if(s<86400)return Math.floor(s/3600)+' h'; const d=Math.floor(s/86400); return d===1?'ayer':d+' d'; }
function esc(s){ return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function esNum(s){ return /^[+0-9()\- ]{6,}$/.test(s||''); }
function esTest(id){ return /^(1809|1829|1849)0{4,}|^18091110|^18092220|^18093330|^18094440|^18095550|^18095557|^18095558/.test(id||''); }

// tema
function toggleTheme(){ const l=document.documentElement.getAttribute('data-theme')==='light'; document.documentElement.setAttribute('data-theme',l?'dark':'light'); $('#themebtn').textContent=l?'☾':'☀'; localStorage.setItem('panel_theme',l?'dark':'light'); }
if(localStorage.getItem('panel_theme')==='light'){ document.documentElement.setAttribute('data-theme','light'); }

// nav
document.querySelectorAll('nav.side .it').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav.side .it').forEach(x=>x.classList.remove('active')); b.classList.add('active');
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active')); $('#v-'+b.dataset.v).classList.add('active');
  if(b.dataset.v==='config') loadCfg(); if(b.dataset.v==='prompt') loadPrompt(); if(b.dataset.v==='aprendizaje') loadAprendizaje();
  if(b.dataset.v==='alertas'){ alertCount=0; renderBadge(); }
});

// reloj header
function tick(){ $('#htime').textContent=new Date().toLocaleTimeString('es-DO',{hour:'2-digit',minute:'2-digit'}); }

// encendido global
async function loadGlobal(){ try{ const d=await api('/bot'); pintarGlobal(d.encendido); }catch(e){} }
function pintarGlobal(on){ $('#hdot').className='dot'+(on?'':' off'); $('#pwlabel').textContent=on?'Bot activo':'Bot en pausa'; $('#pwbtn').textContent=on?'Pausar bot':'Encender bot'; }
async function toggleGlobal(){ const on=$('#pwlabel').textContent!=='Bot activo'; if(!on&&!confirm('¿Pausar el bot para TODOS los clientes?'))return; const d=await api('/bot/'+(on?'on':'off'),{method:'POST'}); pintarGlobal(d.encendido); }

// stats
async function loadStats(){
  const hoy=new Date(); hoy.setHours(0,0,0,0); const t0=hoy.getTime()/1000;
  const chatsHoy=chatsCache.filter(c=>(c.ultimo_ts||0)>=t0).length||chatsCache.length;
  const asesor=chatsCache.filter(c=>c.pausado).length;
  $('#stChats').textContent=chatsHoy; $('#stAsesor').textContent=asesor;
  try{ const r=await api('/revision?limite=200'); $('#stRev').textContent=r.total||0; }catch(e){}
}

// conversaciones
async function loadChats(){ try{ const d=await api('/chats'); chatsCache=d.chats; renderChats(); loadStats(); }catch(e){} }
let _chatsSig='';
function renderChats(){
  const sig=JSON.stringify(chatsCache.map(c=>[c.chat_id,c.ultimo,c.ultimo_ts,c.pausado]))+'|'+selChat;
  if(sig===_chatsSig) return; _chatsSig=sig;
  $('#chcount').textContent=chatsCache.length;
  const el=$('#chats');
  if(!chatsCache.length){ el.innerHTML='<div class="empty">Sin conversaciones aún.</div>'; return; }
  el.innerHTML=chatsCache.map(c=>{
    const nombre=c.user_name||c.chat_id;
    const dot=c.pausado?'<span class="adot" title="en control humano"></span>':'';
    const tag=esTest(c.chat_id)?'<span class="ttag">PRUEBA</span>':'';
    return `<button class="chat ${c.chat_id===selChat?'sel':''}" onclick="openChat('${c.chat_id}')">
      <div class="top"><span class="n ${esNum(nombre)?'num':''}">${esc(nombre)}</span>${tag}<span class="h">${fmtRel(c.ultimo_ts)}</span>${dot}</div>
      <div class="m">${esc(c.ultimo)||'—'}</div></button>`;
  }).join('');
}
async function openChat(id){
  selChat=id; renderChats();
  const d=await api('/chats/'+encodeURIComponent(id)); const m=d.meta||{}; curItems=d.items||[];
  const canal=(m.telefono?'WhatsApp':'WhatsApp')+' · Santo Domingo';
  const hoy=new Date().toLocaleDateString('es-DO',{day:'2-digit',month:'long'}).toUpperCase();
  $('#thead').innerHTML=`<div class="row1"><div><div class="cn">${esc(m.user_name)||esc(id)}</div><div class="cs">${esc(canal)}</div></div>
    <div class="date eyebrow">Hoy · ${hoy}</div></div>
    <div class="acts">
      <button class="abtn" onclick="toggleBot('${id}',${d.pausado})">${d.pausado?'Reactivar bot':'Pausar para este cliente'}</button>
      <button class="abtn" onclick="marcarRevision('${id}')">Marcar para revisión</button>
      <button class="abtn mut" onclick="toggleReply()">Responder</button>
      <button class="abtn mut" onclick="exportar('${id}')">Exportar</button>
      <button class="abtn mut" onclick="eliminarChat('${id}')">Eliminar</button>
    </div>`;
  // resolver productos para los chips
  const ids=new Set();
  for(const it of curItems){ const c=contenidoStr(it.content); if(c.trim().startsWith('{')){ try{ (JSON.parse(c).mostrar_productos||[]).forEach(x=>ids.add(x)); }catch(e){} } }
  prodMap={};
  if(ids.size){ try{ const pr=await api('/productos?ids='+[...ids].join(',')); (pr.productos||[]).forEach(p=>prodMap[p.id]=p); }catch(e){} }
  renderMsgs(curItems);
}
function contenidoStr(c){ if(c==null) return ''; if(Array.isArray(c)) return c.map(x=>(x&&(x.text||x.content))||'').join(' '); if(typeof c!=='string'){ try{ return JSON.stringify(c)||''; }catch(e){ return ''; } } return c; }
function renderMsgs(items){
  const el=$('#msgs');
  if(!items||!items.length){ el.innerHTML='<div class="empty">Sin mensajes.</div>'; return; }
  el.innerHTML=items.map(it=>{
    const tipo=it.type||'';
    if(tipo.indexOf('function_call')>=0||tipo==='tool'||it.role==='tool'){ const n=it.name||it.tool||'acción'; return `<div class="tool">⌁ ${esc(n)} ▾</div>`; }
    const role=it.role||'assistant'; let raw=contenidoStr(it.content);
    if(role==='user') return `<div class="row l"><div class="bub user">${esc(raw)}</div><div class="btime">${fmtTime(it.ts)}</div></div>`;
    let msg=raw,chips=[],escal=false,sup=false;
    if(raw.startsWith('[SUPERVISOR]')){ sup=true; msg=raw.replace('[SUPERVISOR]','').trim(); }
    else if(raw.trim().startsWith('{')){ try{ const o=JSON.parse(raw); if(o&&typeof o.mensaje==='string'){ msg=o.mensaje; chips=o.mostrar_productos||[]; escal=!!o.escalar; } }catch(e){} }
    let ch='';
    if(chips.length){ ch='<div class="pchips">'+chips.map(id=>{ const p=prodMap[id]; return `<div class="pchip"><span class="sku">#${esc(String(id))}</span><span class="pn">${p?esc(p.nombre):'producto '+id}</span>${p?`<span class="pp">RD$ ${Number(p.precio).toFixed(2)}</span>`:''}</div>`; }).join('')+'</div>'; }
    const badge=escal?'<span class="badge-esc">Pasado a un asesor</span>':'';
    return `<div class="row r"><div class="bub ${sup?'super':'bot'}">${badge}${esc(msg)}${ch}</div><div class="btime">${fmtTime(it.ts)}</div></div>`;
  }).join('');
  el.scrollTop=el.scrollHeight;
}
async function toggleBot(id,p){ await api('/chats/'+encodeURIComponent(id)+(p?'/reactivar':'/pausar'),{method:'POST'}); openChat(id); loadChats(); }
async function marcarRevision(id){ await api('/chats/'+encodeURIComponent(id)+'/revisar',{method:'POST'}); alert('Marcado para revisión.'); loadStats(); }
function toggleReply(){ const i=$('#rin'); if(i)i.focus(); }
async function responder(){ if(!selChat)return; const t=$('#rin').value.trim(); if(!t)return; $('#rin').value='';
  const d=await api('/chats/'+encodeURIComponent(selChat)+'/responder',{method:'POST',body:JSON.stringify({texto:t})}); openChat(selChat); }
async function eliminarChat(id){
  if(!confirm('¿Eliminar esta conversación? Se borra el historial del chat y no se puede deshacer.'))return;
  try{ await api('/chats/'+encodeURIComponent(id),{method:'DELETE'}); }catch(e){ alert('No se pudo eliminar.'); return; }
  selChat=null;
  $('#thead').innerHTML='<div class="row1"><div><div class="cn">Elegí una conversación</div></div></div>';
  $('#msgs').innerHTML='<div class="empty">Selecciona un chat de la izquierda.</div>';
  loadChats(); loadStats();
}
function exportar(id){ const txt=curItems.map(it=>{ let c=contenidoStr(it.content); if(c.trim().startsWith('{')){ try{c=JSON.parse(c).mensaje||c;}catch(e){} } return (it.role||'?')+': '+c; }).join('\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([txt],{type:'text/plain'})); a.download='chat_'+id+'.txt'; a.click(); }

// alertas
const MOTIVOS={busqueda_ambigua:{d:'Búsqueda',e:'El cliente pidió algo muy general; el bot no supo qué mostrar.'},foto_ilegible:{d:'Foto',e:'No se pudo leer bien la foto.'},foto_sin_match_claro:{d:'Foto',e:'La foto no coincidió claramente.'},comprobante_sin_pedido:{d:'Pago',e:'Llegó un comprobante pero el pedido no se creó solo.'},claim_pedido_sin_order_id:{d:'Cierre',e:'El bot iba a decir "pedido registrado" sin crearlo; se bloqueó.'},max_turns_excedido:{d:'Agente',e:'El bot dio demasiadas vueltas y se cortó.'},handoff:{d:'Asesor',e:'El bot pasó la conversación a una persona.'},pedido_sin_lineas:{d:'Pedido',e:'Se creó un pedido sin líneas.'},anuncio_sin_mapear:{d:'Anuncio',e:'Anuncio no mapeado a un producto.'},error_agente:{d:'Agente',e:'Error técnico ejecutando el agente.'},repeticion_3x:{d:'Repetición',e:'El cliente preguntó lo mismo 3+ veces; se pasó a una persona.'},takeover_supervisor:{d:'Asesor',e:'Un asesor tomó el control desde YCloud.'},marcado_manual:{d:'Manual',e:'Un asesor lo marcó para revisión.'}};
const ALERTA=['error','revision','handoff','comprobante_sin_pedido'];
function dondeDe(e){ if(e.donde)return e.donde; for(const m of (e.motivos||[]))if(MOTIVOS[m])return MOTIVOS[m].d; return e.kind; }
function porQueDe(e){ return (e.motivos||[]).map(m=>MOTIVOS[m]?MOTIVOS[m].e:m).join(' ')||''; }
async function pollEvents(){ try{ const d=await api('/events?after='+lastEventId);
    if(d.eventos&&d.eventos.length){ lastEventId=d.ultimo_id; let tocaSel=false;
      for(const e of d.eventos){ alerts.unshift(e); if(alerts.length>400)alerts.pop(); if(ALERTA.includes(e.kind))alertCount++; if(e.chat_id===selChat)tocaSel=true; }
      renderBadge(); renderFeed(); if(tocaSel)openChat(selChat); }
    $('#hdot').classList.remove('bad');
  }catch(e){ $('#hdot').classList.add('bad'); } }
function renderBadge(){ const b=$('#badge'); if(alertCount>0){b.style.display='block';b.textContent=alertCount;}else b.style.display='none'; }
const FGRUPO={todos:()=>true, turn:e=>e.kind==='turn', cambios:e=>['control','manual','order'].includes(e.kind),
  revisar:e=>['revision','handoff','comprobante_sin_pedido'].includes(e.kind), error:e=>e.kind==='error'};
const FTABS=[['todos','Todo'],['turn','Respuestas'],['cambios','Cambios'],['revisar','Revisar'],['error','Errores']];
function renderFiltros(){ $('#filtros').innerHTML=FTABS.map(([k,l])=>{ const n=alerts.filter(FGRUPO[k]).length;
  return `<button class="${filtro===k?'on':''}" onclick="setFiltro('${k}')">${l}<b>${n}</b></button>`; }).join(''); }
function setFiltro(t){ filtro=t; renderFiltros(); renderFeed(); }
function nombreDe(e){ return esc(e.user_name)||esc(e.chat_id)||'un cliente'; }
function traducirError(det){ det=det||''; if(/connection|redis|network|memoria|timeout/i.test(det)) return 'El bot perdió conexión con la memoria por unos segundos. Se recuperó solo y siguió atendiendo.'; return 'El bot tuvo un problema técnico y no pudo completar el turno. Ya quedó registrado.'; }
function verConv(id){ document.querySelector('nav.side .it[data-v=conv]').click(); openChat(id); }
function renderFeed(){
  const el=$('#feed');
  let lista=alerts.filter(FGRUPO[filtro]||(()=>true));
  if(!lista.length){ el.innerHTML='<div class="empty">Nada por aquí. Todo marcha según lo esperado.</div>'; return; }
  let html='',dia='';
  for(const e of lista){ let d=fmtDay(e.ts); const hoy=fmtDay(Date.now()/1000);
    let et=(d===hoy?'Hoy · ':(d===fmtDay(Date.now()/1000-86400)?'Ayer · ':''))+d;
    if(d!==dia){dia=d; html+=`<div class="daysep">${et}</div>`;}
    let cuerpo='';
    if(e.kind==='turn'){ cuerpo=`<div class="b">El bot le contestó a ${nombreDe(e)}${e.agente&&e.agente!=='fast-path'?' · '+esc(e.agente):''}</div>`; }
    else if(e.kind==='control'){ cuerpo=`<div>▣ ${esc(e.detalle||'cambio')}</div>`; }
    else if(e.kind==='order'){ cuerpo=`<div>▣ ${esc(e.detalle||'pedido creado')} — ${nombreDe(e)}</div>`; }
    else if(['revision','handoff','comprobante_sin_pedido','manual'].includes(e.kind)){
      const motivo = porQueDe(e) || e.resumen || e.detalle || '';
      const detalle = e.texto ? `${nombreDe(e)}: "${esc(e.texto)}". ${esc(motivo)}` : `${nombreDe(e)}. ${esc(motivo)}`;
      cuerpo=`<div class="card rev"><div class="k revision">Marcada para revisión</div><div style="margin:4px 0">${detalle}</div>
        ${e.chat_id&&e.chat_id!=='-'?`<button class="verconv" onclick="verConv('${e.chat_id}')">Ver conversación</button>`:''}</div>`;
    }
    else if(e.kind==='error'){
      cuerpo=`<div class="card err"><div class="k error">Error del sistema</div><div style="margin:4px 0">${esc(traducirError(e.detalle))}</div>
        ${e.traceback||e.detalle?`<details><summary class="meta">Ver detalle técnico</summary><pre>${esc(e.detalle||'')}\n\n${esc(e.traceback||'')}</pre></details>`:''}
        <div style="margin-top:6px"><button class="btn sec sm" onclick="copiarError(${e.id})">Copiar para Claude</button></div></div>`;
    }
    else{ cuerpo=`<div>${esc(e.detalle||e.resumen||e.kind)}</div>`; }
    html+=`<div class="ev ${e.kind}"><div class="ts">${fmtTime(e.ts)}</div><div>${cuerpo}</div></div>`; }
  el.innerHTML=html;
}
function mejorarDesde(chatId,texto){ document.querySelector('nav.side .it[data-v=aprendizaje]').click(); const s=$('#corsit'); if(s)s.value=texto||''; const r=$('#corresp'); if(r)r.focus(); }
function copiarError(id){ const e=alerts.find(x=>x.id===id); if(!e)return; const rep=`[ERROR pastoriza-bot]\nDónde: ${dondeDe(e)}\nChat: ${e.chat_id||''} (${e.user_name||''})\nCliente dijo: ${e.texto||''}\nError: ${e.detalle||''}\nContexto: ${e.contexto?JSON.stringify(e.contexto):'-'}\nMotivos: ${(e.motivos||[]).join(', ')}\n\nTraceback:\n${e.traceback||'(sin traceback)'}`;
  navigator.clipboard.writeText(rep).then(()=>{const b=event.target,t=b.textContent;b.textContent='✓ Copiado';setTimeout(()=>b.textContent=t,2000);}).catch(()=>alert('Copia manual.')); }

// config
const GRUPOS=[{t:'Envío y entrega',s:'Costo, días y notas de envío.',campos:['precio_envio','dias_envio','hora_corte','nota_envio','info_envio']},{t:'Pagos',s:'Cuentas para transferencia y mensaje de comprobante.',campos:['banco1_nombre','banco1_cuenta','banco2_nombre','banco2_cuenta','titular','cedula','msg_comprobante']},{t:'Negocio',s:'Datos de la tienda.',campos:['direccion','telefono','horario_tienda','website','maps_url']},{t:'Mensajes del bot',s:'Notas y frases que usa el bot.',campos:['nota_botellon','nota_stock','msg_escalar']}];
const LBL={precio_envio:'Precio de envío',dias_envio:'Días de entrega',hora_corte:'Hora de corte',nota_envio:'Notas de envío',info_envio:'Info de envío',banco1_nombre:'Banco',banco1_cuenta:'Número de cuenta',banco2_nombre:'Banco 2',banco2_cuenta:'Número de cuenta 2',titular:'Titular',cedula:'RNC',msg_comprobante:'Mensaje de comprobante',direccion:'Dirección',telefono:'Teléfono',horario_tienda:'Horario',website:'Website',maps_url:'Enlace de Maps',nota_botellon:'Nota de botellón',nota_stock:'Cuando no hay stock',msg_escalar:'Cuando pasa a un asesor'};
const HINTS={precio_envio:'En el chat: "El envío dentro del Gran Santo Domingo son RD$ …"',hora_corte:'Después de esa hora el pedido sale al día siguiente.',msg_comprobante:'Se envía justo después de dar la cuenta.',msg_escalar:'En el chat aparece antes de "Pasado a un asesor".'};
let cfgDirty=0;
async function loadCfg(){ const d=await api('/config'); cfgDirty=0;
  $('#cfg').innerHTML=GRUPOS.map(g=>`<div class="group"><h3>${g.t}</h3><div class="gsub">${g.s}</div><div class="grid">`+g.campos.map(k=>{const v=esc(String(d[k]!=null?d[k]:''));
    const hint=HINTS[k]?`<span class="hint">${HINTS[k]}</span>`:'';
    if(k==='precio_envio')return `<div class="fld"><label>${LBL[k]}</label><div class="prefix"><span>RD$</span><input id="cfg_${k}" value="${v}" oninput="cfgTouch()"/></div>${hint}</div>`;
    const long=['info_envio','msg_comprobante','msg_escalar','nota_envio'].includes(k);
    return `<div class="fld" ${long?'style="grid-column:1/-1"':''}><label>${LBL[k]||k}</label>${long?`<textarea id="cfg_${k}" style="min-height:70px" oninput="cfgTouch()">${v}</textarea>`:`<input id="cfg_${k}" value="${v}" oninput="cfgTouch()"/>`}${hint}</div>`;
  }).join('')+`</div></div>`).join('');
  $('#cfgmsg').textContent='Sin cambios sin guardar.'; }
function cfgTouch(){ cfgDirty++; $('#cfgmsg').innerHTML='<span class="warn">'+cfgDirty+' cambio(s) sin guardar</span>'; }
async function saveCfg(){ const data={}; document.querySelectorAll('[id^=cfg_]').forEach(i=>data[i.id.slice(4)]=i.value); await api('/config',{method:'POST',body:JSON.stringify(data)}); cfgDirty=0; $('#cfgmsg').innerHTML='<span class="ok">Guardado ✓</span>'; }

// prompt
let PROMPTS={};
async function loadPrompt(){ const d=await api('/prompts'); PROMPTS=d.prompts||{}; const sel=$('#pagente'); if(!sel.options.length)sel.innerHTML=(d.agentes||[]).map(a=>`<option value="${a}">${a}</option>`).join(''); mostrarPrompt(); }
function mostrarPrompt(){ const a=$('#pagente').value,p=PROMPTS[a]||{}; $('#pov').value=p.override||''; $('#pbase').value=p.base||'';
  const ov=p.usando_override; $('#pdot').className='d'+(ov?'':' base');
  $('#pstate').textContent=ov?('Usando override · '+((p.override||'').length)+' caracteres'):'Usando prompt base'; $('#pmsg').textContent='';
  syncGutter(); syncGutterBase(); }
function gut(el,ta){ if(!el||!ta)return; const n=(ta.value.match(/\n/g)||[]).length+1; let g=''; for(let i=1;i<=n;i++)g+=i+'\n'; el.textContent=g; el.scrollTop=ta.scrollTop; }
function syncGutter(){ gut($('#pgut'),$('#pov')); }
function syncGutterBase(){ gut($('#pgutb'),$('#pbase')); }
async function savePrompt(){ const a=$('#pagente').value; try{ const d=await api('/prompts/'+a,{method:'POST',body:JSON.stringify({override:$('#pov').value})}); $('#pmsg').innerHTML='<span class="ok">Guardado ✓ ('+(d.usando_override?'override':'base')+')</span>'; await loadPrompt(); }catch(e){ $('#pmsg').innerHTML='<span class="bad">Error (¿mínimo 40 caracteres?)</span>'; } setTimeout(()=>$('#pmsg').textContent='',3500); }
async function resetPrompt(){ if(!confirm('¿Volver al .md base de este agente?'))return; $('#pov').value=''; await savePrompt(); }
function subirMd(ev){ const f=ev.target.files[0]; if(!f)return; const rd=new FileReader(); rd.onload=()=>{ $('#pov').value=rd.result; syncGutter(); $('#pmsg').innerHTML='<span class="meta">Archivo cargado; dale Guardar.</span>'; }; rd.readAsText(f); ev.target.value=''; }

// aprendizaje
function normR(s){ return (s||'').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/[^a-z0-9 ]/g,' ').split(/\s+/).filter(Boolean); }
function simil(a,b){ const A=new Set(normR(a)),B=new Set(normR(b)); if(!A.size||!B.size)return 0; let inter=0; A.forEach(x=>{if(B.has(x))inter++;}); return inter/new Set([...A,...B]).size; }
function detectarDup(reglas){ for(let i=0;i<reglas.length;i++)for(let j=i+1;j<reglas.length;j++){ if(simil(reglas[i].texto,reglas[j].texto)>=0.6) return {i,j}; } return null; }
async function combinar(id){ if(!confirm('¿Eliminar la regla duplicada y quedarte con una sola?'))return; await api('/reglas/'+id,{method:'DELETE'}); loadAprendizaje(); }
async function loadAprendizaje(){ const d=await api('/aprendizaje'); const sugs=d.sugerencias||[],reglas=d.reglas||[],correc=d.correcciones||[];
  const pend=sugs.filter(s=>s.estado==='pendiente'); const bs=$('#badgeSug'); if(pend.length){bs.style.display='block';bs.textContent=pend.length;}else bs.style.display='none';
  $('#hsug').textContent=pend.length; $('#hreg').textContent=reglas.length;
  $('#sugs').innerHTML=sugs.length?sugs.map(s=>{const st=s.estado==='pendiente'?'':(s.estado==='aprobada'?'<span class="ok">✓ aprobada</span>':'<span class="bad">✕ descartada</span>');
    const chip=`<span class="rchip ${s.riesgo==='alto'?'alto':'bajo'}">Riesgo ${s.riesgo}</span>`;
    const btns=s.estado==='pendiente'?`<div style="margin-top:8px"><button class="btn sm" onclick="aprobarSug(${s.id})">Aprobar</button> <button class="btn sec sm" onclick="rechazarSug(${s.id})">Descartar</button></div>`:'';
    return `<div class="group">${chip} <span class="meta">${esc(s.origen||'')}</span> ${st}<div style="margin:6px 0;font-weight:600">${esc(s.contenido)}</div>${btns}</div>`;}).join(''):'<div class="empty">Nada que revisar. El bot responde según lo esperado.</div>';
  const dup=detectarDup(reglas);
  let rhtml=dup?`<div class="dupwarn"><span>2 reglas muy parecidas · las número ${dup.i+1} y ${dup.j+1} dicen lo mismo</span><button class="btn sec sm" onclick="combinar(${reglas[dup.j].id})">Combinar</button></div>`:'';
  rhtml+=reglas.length?reglas.map((r,i)=>{const isdup=dup&&(i===dup.i||i===dup.j);
    return `<div class="rrow ${isdup?'dup':''}"><span class="rn">${String(i+1).padStart(2,'0')}</span><span class="rt">${esc(r.texto)}</span><span class="ro">(${esc(r.origen||'manual')})</span><button class="rx" onclick="delRegla(${r.id})">×</button></div>`;}).join(''):'<div class="empty">Sin reglas.</div>';
  $('#reglas').innerHTML=rhtml;
  $('#correcciones').innerHTML=correc.length?correc.map(c=>`<div class="group" style="padding:10px 12px"><div><b>Si:</b> ${esc(c.situacion)}</div><div><b>Responde:</b> ${esc(c.respuesta_correcta)}</div><button class="btn sec sm" style="margin-top:6px" onclick="delCorreccion(${c.id})">Borrar</button></div>`).join(''):'<div class="empty">Sin correcciones.</div>'; }
async function addRegla(){ const t=$('#regin').value.trim(); if(t.length<5)return; $('#regin').value=''; await api('/reglas',{method:'POST',body:JSON.stringify({texto:t})}); loadAprendizaje(); }
async function delRegla(id){ await api('/reglas/'+id,{method:'DELETE'}); loadAprendizaje(); }
async function addCorreccion(){ const s=$('#corsit').value.trim(),r=$('#corresp').value.trim(); if(!s||!r)return; $('#corsit').value='';$('#corresp').value=''; await api('/correcciones',{method:'POST',body:JSON.stringify({situacion:s,respuesta_correcta:r})}); loadAprendizaje(); }
async function delCorreccion(id){ await api('/correcciones/'+id,{method:'DELETE'}); loadAprendizaje(); }
async function aprobarSug(id){ await api('/sugerencias/'+id+'/aprobar',{method:'POST'}); loadAprendizaje(); }
async function rechazarSug(id){ await api('/sugerencias/'+id+'/rechazar',{method:'POST'}); loadAprendizaje(); }
async function analizar(){ $('#anmsg').textContent='Analizando…'; try{ const d=await api('/sugerencias/analizar',{method:'POST'}); $('#anmsg').innerHTML='<span class="ok">Listo: '+(d.sugeridas||0)+' sugerencia(s), '+(d.auto_aplicadas||0)+' aplicada(s).</span>'; }catch(e){ $('#anmsg').innerHTML='<span class="bad">Error al analizar</span>'; } loadAprendizaje(); }

// boot
async function boot(){ renderFiltros(); tick(); setInterval(tick,20000);
  try{ await fetch('/panel/api/whoami',{headers:headers()}); }catch(e){}
  await loadGlobal(); await loadChats(); await pollEvents();
  clearInterval(window._t1); clearInterval(window._t2); window._t1=setInterval(pollEvents,3000); window._t2=setInterval(loadChats,10000);
}
boot();
</script>
</body>
</html>
"""
