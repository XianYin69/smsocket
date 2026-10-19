"""Embedded dashboard page (plain HTML + fetch, no build step).

Aero 拟物化改造：4 套可切换皮肤，右上角拟物拨杆切换，选择存 localStorage，默认 A。
  A = Win7 Aero Glass（深蓝毛玻璃 + 蓝光晕）
  B = Win11 Fluent Mica（磨砂灰白 + 柔和阴影）
  C = 金属拉丝面板 + 玻璃窗框混搭（保留工业感）
  D = 仿成品简洁（ChatGPT/OpenWebUI 式浅色双栏）
零依赖 / 零构建 / 无外链字体与 CDN；元素 id 与 fetch 数据逻辑保持不变。
"""

_BASE = """
*{box-sizing:border-box;max-width:100%}
body{font:14px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;margin:0;min-height:100vh;max-width:100%;overflow-x:hidden;
  background:var(--bg);color:var(--fg);background-attachment:fixed}
header{padding:12px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 14px 0;
 background:var(--glass);backdrop-filter:blur(var(--blur)) saturate(var(--sat));-webkit-backdrop-filter:blur(var(--blur)) saturate(var(--sat));
 border:1px solid var(--edge);border-radius:var(--rad);box-shadow:var(--drop),var(--bevel)}
h1{font-size:17px;margin:0;font-weight:600;white-space:nowrap;text-shadow:var(--glow)}
main{padding:14px 18px 34px;width:100%;max-width:100%;margin:0 auto;display:grid;grid-template-columns:1fr;gap:12px;align-items:start;overflow-x:hidden}
h2{font-size:12px;letter-spacing:.08em;margin:18px 0 6px;font-weight:700;color:var(--h2);text-shadow:var(--glow)}
h3{font-size:11px;letter-spacing:.07em;margin:14px 0 5px;font-weight:600;color:var(--muted)}
.extnote{font-size:11px;color:var(--muted);margin:0 0 4px}
table{border-collapse:separate;border-spacing:0;width:100%;overflow-x:auto;display:block;
  background:var(--glass);backdrop-filter:blur(var(--blur)) saturate(var(--sat));-webkit-backdrop-filter:blur(var(--blur)) saturate(var(--sat));
  border:1px solid var(--edge);border-radius:var(--rad);box-shadow:var(--drop),var(--bevel)}
th,td{padding:6px 10px;text-align:left;font-size:13px;white-space:nowrap}
th{background:var(--th);color:var(--thfg);font-weight:700}
td{border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
tr:hover td{background:var(--hover)}
"""
_BASE += """
.cards{display:flex;gap:10px;flex-wrap:wrap}
.card{padding:10px 14px;min-width:120px;background:var(--glass);border:1px solid var(--edge);border-radius:var(--rad);
 backdrop-filter:blur(var(--blur)) saturate(var(--sat));-webkit-backdrop-filter:blur(var(--blur)) saturate(var(--sat));
 box-shadow:var(--drop),var(--bevel)}
.card b{display:block;font-size:22px;font-weight:700;color:var(--num);text-shadow:var(--glow)}
.card i{font-style:normal;font-size:11px;letter-spacing:.05em;opacity:.85}
pre{padding:12px;overflow-x:auto;overflow-y:auto;max-width:100%;font:12px/1.6 ui-monospace,Consolas,monospace;white-space:pre;
  background:var(--well);border:1px solid var(--edge);border-radius:var(--rad);box-shadow:var(--inset)}
input,button{font:inherit;color:var(--fg);border-radius:var(--rad);padding:6px 11px;border:1px solid var(--btnedge);
 background:var(--btn);box-shadow:var(--btnsh);cursor:pointer}
#key{min-width:210px;background:var(--well);color:var(--fg);box-shadow:var(--inset),var(--bevel)}
#key::placeholder{color:var(--muted)}
button:hover{filter:brightness(1.08)}
button:active{transform:translateY(1px);box-shadow:var(--pressed)}
.skinbar{margin-left:auto;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.skinbar em{font-style:normal;font-size:11px;opacity:.7;margin-right:2px}
.skinbtn{font-size:12px;font-weight:600;padding:5px 11px;line-height:1.35}
.skinbtn.on{background:var(--accent);color:var(--accentfg);box-shadow:var(--lit),var(--bevel)}
#msg{display:none;font-size:12px;font-weight:600;padding:7px 12px;border-radius:var(--rad);
 background:var(--warn);color:var(--warnfg);border:1px solid var(--warnedge);box-shadow:var(--drop),var(--bevel)}
#msg:not(:empty){display:block}
td.empty{text-align:center;font-weight:600;padding:16px;color:var(--muted);background:var(--empty)}
tr.pmhead{cursor:pointer}
tr.pmhead:hover td{background:var(--hover,rgba(127,127,127,.12))}
tr.pmhead.stale td{box-shadow:inset 3px 0 0 var(--warn)}
.pmcaret{display:inline-block;width:14px;font-weight:700;opacity:.7}
table.pmsub{margin:0;width:100%}
tr.pmsub td,tr.pmcols td{padding:0 0 10px 22px;background:transparent;border:0}
.pmchip{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;margin:1px 3px 1px 0;
 border-radius:9px;border:1px solid var(--edge);background:var(--empty);color:var(--muted)}
.pmchip.ok{background:var(--ok,rgba(60,180,110,.22));color:var(--okfg,inherit)}
.pmchip.no{background:var(--bad,rgba(200,80,80,.22));color:var(--badfg,inherit)}
.pmchip.warn{background:var(--warn,rgba(220,170,60,.22));color:var(--warnfg,inherit)}
.pmchip.off{opacity:.55;border-style:dashed}
.pmchips{max-width:420px}
.pmact{font-size:11px;padding:2px 8px}
tr.pmadd td{padding:4px 22px 8px;background:transparent;border:0;border-top:1px dashed var(--edge)}
.pmf{font:inherit;font-size:12px;padding:3px 6px;width:auto;min-width:120px;
 background:var(--well);box-shadow:var(--inset);border:1px solid var(--edge);border-radius:var(--rad)}
.pmemb{font-size:11px;display:inline-flex;align-items:center;gap:4px;margin-left:8px;opacity:.85}
.side{display:contents}
::-webkit-scrollbar{width:15px;height:15px}
::-webkit-scrollbar-track{background:var(--track);border-radius:8px;box-shadow:var(--inset)}
::-webkit-scrollbar-thumb{background:var(--thumb);border:1px solid var(--edge);border-radius:8px;box-shadow:var(--bevel)}
::-webkit-scrollbar-thumb:active{background:var(--accent)}
@media(max-width:780px){.skinbar{width:100%;margin-left:0}header{margin:10px 8px 0}}
"""
_A = """
body.skin-a{
 --bg:radial-gradient(1300px 700px at 18% -12%,#2f68b4 0%,#14305a 42%,#070f1e 100%),#0a1526;
 --fg:#eaf3ff;--h2:#bcdcff;--muted:#9fc2e8;--num:#dbeeff;
 --glass:linear-gradient(180deg,rgba(255,255,255,.18),rgba(122,178,255,.11) 44%,rgba(6,16,34,.34));
 --edge:rgba(190,226,255,.5);--rad:12px;--blur:14px;--sat:170%;
 --drop:0 10px 28px rgba(0,8,26,.55);--bevel:inset 0 1px 0 rgba(255,255,255,.72),inset 0 -1px 0 rgba(120,180,255,.32);
 --inset:inset 0 2px 7px rgba(0,10,30,.6);--glow:0 0 14px rgba(120,190,255,.75);
 --th:linear-gradient(180deg,rgba(164,214,255,.32),rgba(60,120,200,.14));--thfg:#dceeff;
 --line:rgba(150,200,255,.18);--hover:rgba(140,200,255,.15);--well:rgba(4,14,32,.55);
 --btn:linear-gradient(180deg,rgba(255,255,255,.9),rgba(192,226,255,.5) 47%,rgba(70,132,212,.55));
 --btnedge:rgba(212,236,255,.85);--btnsh:0 3px 9px rgba(0,10,30,.55),inset 0 1px 0 #fff,inset 0 -2px 4px rgba(0,30,70,.25);
 --pressed:inset 0 3px 9px rgba(0,20,55,.7);
 --accent:linear-gradient(180deg,#93d2ff,#1f6fc4);--accentfg:#fff;--lit:0 0 16px rgba(90,180,255,.95);
 --warn:linear-gradient(180deg,rgba(255,214,124,.92),rgba(188,110,20,.88));--warnfg:#2a1600;--warnedge:#ffd98a;
 --empty:linear-gradient(180deg,rgba(255,255,255,.06),rgba(0,0,0,.12));
 --track:linear-gradient(180deg,rgba(8,22,46,.75),rgba(0,8,22,.8));--thumb:linear-gradient(180deg,#c6e6ff,#3f86d6)}
body.skin-a header{box-shadow:var(--drop),var(--bevel),0 0 30px rgba(70,150,255,.3)}
body.skin-a .card b{font-size:24px}
"""
_B = """
body.skin-b{
 --bg:radial-gradient(900px 500px at 85% -10%,rgba(120,180,255,.35),transparent 60%),
     radial-gradient(700px 420px at 5% 10%,rgba(200,160,255,.22),transparent 60%),
     linear-gradient(180deg,#eef1f5,#dfe4ea 60%,#d6dce4);
 --fg:#1b2028;--h2:#3a4658;--muted:#6b7686;--num:#0b57a4;
 --glass:linear-gradient(180deg,rgba(255,255,255,.72),rgba(240,244,250,.58));
 --edge:rgba(255,255,255,.85);--rad:9px;--blur:26px;--sat:130%;
 --drop:0 8px 22px rgba(20,30,55,.14),0 1px 2px rgba(20,30,55,.1);
 --bevel:inset 0 1px 0 rgba(255,255,255,.95),inset 0 0 0 1px rgba(255,255,255,.35);
 --inset:inset 0 2px 6px rgba(60,75,100,.16);--glow:none;
 --th:linear-gradient(180deg,rgba(255,255,255,.85),rgba(228,235,245,.7));--thfg:#2b3546;
 --line:rgba(120,140,170,.22);--hover:rgba(0,90,180,.07);--well:rgba(255,255,255,.78);
 --btn:linear-gradient(180deg,#fdfdfe,#e6ebf2);--btnedge:rgba(140,160,190,.5);
 --btnsh:0 2px 6px rgba(20,30,55,.14),inset 0 1px 0 #fff;
 --pressed:inset 0 3px 8px rgba(60,80,110,.28);
 --accent:linear-gradient(180deg,#3b9ae8,#0067c9);--accentfg:#fff;--lit:0 3px 10px rgba(0,103,201,.35);
 --warn:linear-gradient(180deg,#fff4d6,#ffe2a8);--warnfg:#7a4a00;--warnedge:#e8bf6a;
 --empty:linear-gradient(180deg,rgba(255,255,255,.6),rgba(225,232,242,.5));
 --track:rgba(210,218,230,.6);--thumb:linear-gradient(180deg,#f6f8fb,#b9c6d8)}
body.skin-b header{border-bottom-color:rgba(255,255,255,.9)}
body.skin-b .card{border-left:3px solid rgba(0,103,201,.55)}
"""
_C = """
body.skin-c{
 --bg:linear-gradient(180deg,#2a2e33,#1b1e22 55%,#14171a),#171a1d;
 --fg:#e8ecef;--h2:#c9d4dd;--muted:#98a3ad;--num:#ffd98a;
 --glass:linear-gradient(180deg,rgba(150,160,170,.30),rgba(70,78,86,.34) 40%,rgba(30,34,38,.46)),
   repeating-linear-gradient(90deg,rgba(255,255,255,.055) 0 1px,rgba(0,0,0,.06) 1px 3px);
 --edge:rgba(226,234,240,.42);--rad:6px;--blur:9px;--sat:120%;
 --drop:0 10px 24px rgba(0,0,0,.6),0 2px 0 rgba(255,255,255,.12);
 --bevel:inset 0 1px 0 rgba(255,255,255,.55),inset 0 -2px 4px rgba(0,0,0,.5),inset 0 0 0 1px rgba(0,0,0,.35);
 --inset:inset 0 3px 9px rgba(0,0,0,.7),inset 0 0 0 1px rgba(255,255,255,.12);--glow:0 0 8px rgba(255,200,110,.35);
 --th:linear-gradient(180deg,rgba(210,220,230,.26),rgba(60,68,76,.4));--thfg:#dfe8ef;
 --line:rgba(220,230,240,.14);--hover:rgba(255,214,138,.1);--well:rgba(6,10,14,.72);
 --btn:linear-gradient(180deg,#dfe4e9,#a8b1ba 48%,#7b848d 52%,#c3ccd4);--btnedge:rgba(20,24,28,.8);
 --btnsh:0 3px 7px rgba(0,0,0,.6),inset 0 1px 0 #fff,inset 0 -2px 3px rgba(0,0,0,.35);
 --pressed:inset 0 4px 10px rgba(0,0,0,.7);
"""
_C += """
 --accent:linear-gradient(180deg,#ffd98a,#c98b1f);--accentfg:#241703;
 --lit:0 0 14px rgba(255,200,90,.7),inset 0 1px 0 rgba(255,255,255,.7);
 --warn:linear-gradient(180deg,#ffd28a,#b96f14);--warnfg:#2b1600;--warnedge:#7a4a08;
 --empty:linear-gradient(180deg,rgba(255,255,255,.05),rgba(0,0,0,.25));
 --track:linear-gradient(180deg,#101316,#22262b);--thumb:linear-gradient(180deg,#e2e7ec,#8b949d 50%,#6a7178)}
body.skin-c input,body.skin-c button{color:#161a1e;text-shadow:0 1px 0 rgba(255,255,255,.55)}
body.skin-c #key{color:#ffe9b8;text-shadow:none}
body.skin-c header{border:1px solid rgba(226,234,240,.5);border-top-color:rgba(255,255,255,.75)}
body.skin-c .card,body.skin-c table,body.skin-c header,body.skin-c pre{position:relative}
body.skin-c .card::before,body.skin-c table::before,body.skin-c header::before{
 content:"";position:absolute;left:5px;top:5px;width:7px;height:7px;border-radius:50%;
 background:radial-gradient(circle at 30% 30%,#f2f6f9,#6b737b 60%,#23282d);box-shadow:inset 0 -1px 1px rgba(0,0,0,.6)}
body.skin-c .card::after,body.skin-c table::after,body.skin-c header::after{
 content:"";position:absolute;right:5px;bottom:5px;width:7px;height:7px;border-radius:50%;
 background:radial-gradient(circle at 30% 30%,#f2f6f9,#6b737b 60%,#23282d);box-shadow:inset 0 -1px 1px rgba(0,0,0,.6)}
body.skin-c .skinbtn{border-width:2px}
"""
_D = """
body.skin-d{
 --bg:linear-gradient(180deg,#f7f8fa,#eef0f4);
 --fg:#1f2328;--h2:#55606d;--muted:#8a94a2;--num:#0f6b4f;
 --glass:linear-gradient(180deg,rgba(255,255,255,.92),rgba(248,250,253,.8));
 --edge:rgba(255,255,255,.95);--rad:12px;--blur:18px;--sat:120%;
 --drop:0 6px 18px rgba(25,35,55,.09),0 1px 2px rgba(25,35,55,.07);
 --bevel:inset 0 1px 0 #fff,inset 0 0 0 1px rgba(180,195,215,.28);
 --inset:inset 0 2px 5px rgba(70,85,110,.12);--glow:none;
 --th:linear-gradient(180deg,#fbfcfe,#eef1f6);--thfg:#3a4453;
 --line:rgba(150,165,185,.2);--hover:rgba(15,107,79,.06);--well:#fff;
 --btn:linear-gradient(180deg,#ffffff,#eef1f5);--btnedge:rgba(150,165,185,.45);
 --btnsh:0 1px 3px rgba(25,35,55,.12),inset 0 1px 0 #fff;
 --pressed:inset 0 3px 7px rgba(70,85,110,.22);
 --accent:linear-gradient(180deg,#25b384,#0f6b4f);--accentfg:#fff;--lit:0 3px 10px rgba(15,107,79,.3);
 --warn:linear-gradient(180deg,#fff2f0,#ffd8d3);--warnfg:#8a2b18;--warnedge:#f0b3a6;
 --empty:linear-gradient(180deg,#fbfcfd,#f1f4f8);
 --track:#eef1f5;--thumb:linear-gradient(180deg,#f4f6f9,#c3ccd8)}
body.skin-d main{grid-template-columns:290px 1fr;gap:18px;align-items:start}
body.skin-d .side{display:block;position:sticky;top:12px}
body.skin-d .side .cards{flex-direction:column;gap:8px}
body.skin-d .card{min-width:0;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
body.skin-d .card b{font-size:19px}
body.skin-d header{margin:10px 12px 0;border-radius:14px}
body.skin-d h2{margin-top:14px}
"""

_NAV = """
.nav{display:flex;gap:6px;flex-wrap:wrap}
.navbtn{font-size:13px;font-weight:600;padding:6px 13px}
.navbtn.on{background:var(--accent);color:var(--accentfg);box-shadow:var(--lit),var(--bevel)}
.page{display:none}
.page.on{display:block}
.cred{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px}
.cred em{font-style:normal;opacity:.7}
.cred code{font:12px/1.5 ui-monospace,Consolas,monospace;padding:3px 8px;border-radius:var(--rad);
 background:var(--well);border:1px solid var(--edge);box-shadow:var(--inset);word-break:break-all}
"""
_NAV += """
.btns{display:flex;gap:6px;align-items:end;flex-wrap:wrap}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;align-items:end;
 background:var(--glass);border:1px solid var(--edge);border-radius:var(--rad);padding:12px;
 box-shadow:var(--drop),var(--bevel)}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:600;color:var(--h2)}
label input,label select,label textarea{font:inherit;font-weight:400;color:var(--fg)}
label.wide{grid-column:1/-1}
textarea{font:12px/1.5 ui-monospace,Consolas,monospace;padding:6px 8px;background:var(--well);
 color:var(--fg);border:1px solid var(--edge);border-radius:var(--rad);box-shadow:var(--inset);resize:vertical}
td button{font-size:11px;padding:3px 8px;margin-right:4px}
@media(max-width:780px){.cred{width:100%}.nav{width:100%}}
"""
_NET = """
/* discreet net plane: footer glyph + slide-over panel, skin-agnostic (vars only) */
.netglyph{position:fixed;right:12px;bottom:10px;z-index:40;font-size:11px;opacity:.45;
 background:transparent;border:0;color:var(--fg);cursor:pointer;padding:3px 6px}
.netglyph:hover{opacity:.95}
.netglyph.off{display:none}
.hide{display:none}
.reach{display:block;overflow-x:auto;width:100%}
.reach td{padding:2px 6px;font-size:12px;white-space:nowrap}
.reach th{white-space:nowrap}
.v-healthy{background:rgba(46,160,67,.22)}.v-slow{background:rgba(212,160,23,.22)}
.v-blocked{background:rgba(226,68,68,.26)}.v-unstable{background:rgba(120,140,255,.22)}
.v-unknown{opacity:.5}
.netmask{position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:41;display:none}
.netmask.on{display:block}
.netpanel{position:fixed;top:0;right:0;bottom:0;width:min(430px,94vw);z-index:42;
 background:var(--bg);border-left:1px solid var(--edge);box-shadow:var(--drop);
 padding:12px 14px;overflow:auto;transform:translateX(100%);transition:transform .18s ease}
.netpanel.on{transform:translateX(0)}
.netpanel h2{margin:0 0 8px;font-size:15px}
.netrow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}
.netpanel table{width:100%;font-size:12px}
.netpanel .muted{opacity:.65;font-size:11px}
"""
_EXT = """
/* 扩展程序：导入区 + 已挂载条目（自带面板 iframe） */
.extitem{border:1px solid var(--edge);border-radius:var(--rad);padding:10px 12px;margin:10px 0;
 background:var(--glass);box-shadow:var(--drop),var(--bevel)}
.extitem h4{margin:0 0 4px;font-size:13px;font-weight:700;display:flex;gap:7px;
 align-items:center;flex-wrap:wrap;color:var(--h2)}
.extchip{font:11px/1.5 ui-monospace,Consolas,monospace;font-weight:600;padding:1px 7px;
 border-radius:9px;border:1px solid var(--edge);background:var(--empty);color:var(--muted)}
.extchip.ok{background:var(--ok,rgba(60,180,110,.22));color:var(--okfg,inherit)}
.extchip.bad{background:var(--bad,rgba(200,80,80,.22));color:var(--badfg,inherit)}
.extpath{font:11px/1.5 ui-monospace,Consolas,monospace;color:var(--muted);word-break:break-all}
iframe.extpanel{display:block;width:100%;min-height:380px;border:1px solid var(--edge);
 border-radius:var(--rad);background:var(--bg);margin-top:8px}
#x_path{min-width:320px;background:var(--well);box-shadow:var(--inset)}
"""
CSS = _BASE + _A + _B + _C + _D + _NAV + _NET + _EXT
JS = """
const CUR=__CUR__;const SYM=__SYM__;
const K=()=>document.getElementById('key').value.trim();
function saveKey(){try{localStorage.setItem('SMSocket.key',K())}catch(e){}}
const hdr=()=>K()?{Authorization:'Bearer '+K()}:{};
const L={alias:'别名',calls:'调用数',tokens:'词元数',cost:'费用',provider:'提供方',key:'密钥',
 ok:'可用',fails:'失败数',cooldown_left:'冷却剩余',ts:'时间',status:'状态',ms:'耗时',
 total:'总词元',stream:'流式',d:'日期',errors:'错误数'};
const th=c=>L[c]||c;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gv=id=>document.getElementById(id).value.trim();
function toast(s){document.getElementById('msg').textContent=s||''}
function tbl(id,rows,cols){const t=document.getElementById(id);
 if(!rows||!rows.length){t.innerHTML='<tr><td class=empty colspan='+cols.length+'>暂无数据 · 等待网关产生记录</td></tr>';return}
 t.innerHTML='<tr>'+cols.map(c=>'<th>'+th(c)+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+
  cols.map(c=>'<td>'+esc(r[c]==null?'-':r[c])+'</td>').join('')+'</tr>').join('')}
async function j(u){const r=await fetch(u,{headers:hdr()});
 if(!r.ok)throw new Error(u+' -> '+r.status);return r.json()}
async function send(u,m,b){const r=await fetch(u,{method:m,headers:Object.assign(
 {'Content-Type':'application/json'},hdr()),body:JSON.stringify(b||{})});
 let d={};try{d=await r.json()}catch(e){}
 if(!r.ok){const e=d&&d.detail;const msg=(e&&e.error&&e.error.message)||(typeof e==='string'?e:u+' -> '+r.status);
  throw new Error(msg)}return d}
"""
JS += """
const PAGES=['overview','usage','providers','socket','settings'];
const PKEY='SMSocket.page';
function go(p){if(PAGES.indexOf(p)<0)p='overview';
 document.querySelectorAll('.page').forEach(el=>el.classList.toggle('on',el.id==='page-'+p));
 document.querySelectorAll('.navbtn').forEach(b=>b.classList.toggle('on',b.dataset.p===p));
 try{localStorage.setItem(PKEY,p)}catch(e){}
 if(location.hash!=='#'+p)try{history.replaceState(null,'','#'+p)}catch(e){}
 if(p==='providers'){loadConfig();loadPM(true)}
 if(p==='socket'){loadSocket();renderStack({});loadReach();tickSocket()}

 if(p==='settings'){loadSelf();loadSettings();loadClash();loadTune();loadTuneScores();loadExts()}}
let SELF={},REVEAL=false;
function paintSelf(){document.getElementById('baseurl').textContent=SELF.base_url||'-';
 document.getElementById('apikey').textContent=REVEAL?(SELF.key||''):(SELF.key_masked||'-');
 document.getElementById('reveal').textContent=REVEAL?'隐藏':'显示';
 document.getElementById('modelcount').textContent=(SELF.models||[]).length;
 const h=document.getElementById('hint');
 if(h)h.textContent='base_url = '+(SELF.base_url||location.origin+'/v1')+
  String.fromCharCode(10)+'model    = <取自 /v1/models 的别名>'+
  String.fromCharCode(10)+'api_key  = '+(REVEAL?(SELF.key||''):(SELF.key_masked||''))+
  String.fromCharCode(10)+'对话 API = '+(SELF.base_url||location.origin+'/v1')+'/chat/completions'+
  String.fromCharCode(10)+'响应 API = '+(SELF.base_url||location.origin+'/v1')+'/responses'+
  String.fromCharCode(10)+'密钥文件 = '+(SELF.key_file||'-')+
  String.fromCharCode(10)+'提示：本机浏览器打开控制台自动免密钥（会话 Cookie），'+
  '外部程序仍需用 api_key；主题与设置自动保存在服务端'}
async function loadSelf(){try{SELF=await j('/admin/self');REVEAL=false;paintSelf()}
 catch(e){SELF={};document.getElementById('baseurl').textContent='需主密钥';
  document.getElementById('apikey').textContent='需主密钥'}}
async function revealKey(){try{SELF=await j('/admin/self?reveal=1');REVEAL=true;paintSelf();
 toast('已显示明文密钥，请勿外泄')}catch(e){toast('取明文失败 · '+e.message)}}
function copyText(txt,tag){if(!txt||txt==='-'){toast('没有可复制的内容');return}
 const done=()=>toast('已复制'+tag);
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(txt).then(done,
  ()=>fallback(txt,done))}else fallback(txt,done)}
function fallback(txt,done){const ta=document.createElement('textarea');ta.value=txt;
 ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);ta.select();
 let ok=false;try{ok=document.execCommand('copy')}catch(e){}
 document.body.removeChild(ta);ok?done():toast('浏览器拒绝复制，请手动选取')}
"""
JS += """
async function refresh(){toast('');
 try{const [s,p,mods,x]=await Promise.all([j('/stats'),j('/pool'),j('/v1/models'),j('/concurrency')]);
  document.getElementById('calls').textContent=s.calls;
  document.getElementById('tokens').textContent=s.tokens;
  document.getElementById('cost').textContent=(+s.cost||0).toFixed(4)+' '+(s.currency||'');
  document.getElementById('slots').textContent=p.slots.length;
  document.getElementById('models').textContent=mods.data.length;
  document.getElementById('ucalls').textContent=s.calls;
  document.getElementById('utokens').textContent=s.tokens;
  document.getElementById('ucost').textContent=(+s.cost||0).toFixed(4)+' '+(s.currency||'');
  tbl('bymodel',s.by_model,['alias','calls','tokens','cost']);
  tbl('pool',p.slots,['provider','key','ok','fails','cooldown_left']);
  tbl('byprov',s.by_provider,['provider','calls','tokens','cost','avg_ms','errors']);
  tbl('recent',s.recent.map(r=>({...r,ts:new Date(r.ts*1000).toLocaleTimeString(),
   stream:r.stream?'是':'否'})),['ts','alias','provider','status','ms','total','cost','stream']);
  try{const u=await j('/v1/usage?days=14');tbl('daily',u.daily,['d','calls','tokens','cost','errors'])}
  catch(e){tbl('daily',[],['d','calls','tokens','cost','errors'])}
  document.getElementById('xactive').textContent=x.active;
  document.getElementById('xpeak').textContent=x.peak;
  document.getElementById('xcurrency').textContent=(s.currency||'-');
  document.getElementById('strategy').textContent=p.strategy;
  toast('已刷新 · '+new Date().toLocaleTimeString())
 }catch(e){toast('数据加载失败 · '+e.message)}}
"""
JS += """
let CFG={providers:[]},EDIT='',DER={};
function provRow(p){const b=(a,t)=>'<button data-a='+a+'>'+t+'</button>';
 return '<tr data-n='+esc(p.name)+'><td><b>'+esc(p.name)+'</b></td><td>'+esc(p.base_url)+
  '</td><td>'+esc(p.style)+'</td><td>'+keyCell(p)+
  '</td><td>'+provRank(p)+'</td><td>'+esc(p.timeout)+' / '+esc(p.max_rpm||0)+
  '</td><td>'+(p.enabled===false?'停用':'启用')+'</td><td>'+b('edit','编辑')+b('key','加密钥')+
  b('del','删除')+'</td></tr>'}
function provRank(p){const d=DER[p.name];
 if(!d)return esc(p.priority)+' / '+esc(p.weight);
 return '<b>'+esc(d.priority)+' / '+esc(d.weight)+'</b>'+
  ' <span class=muted title="由实测推导（日常对话 + 定时探测）">自动</span>'}
async function loadConfig(){try{const [c,p]=await Promise.all([j('/admin/config'),j('/pool')]);
  CFG=c;const rows=c.providers||[];DER={};
  (p.slots||[]).forEach(x=>{if(x.auto)DER[x.provider]={priority:x.priority,weight:x.weight}});
  const hd='<tr><th>名称</th><th>基础URL</th><th>协议</th><th>密钥（已掩码）</th><th>优先级/权重</th>'+
   '<th>超时/RPM</th><th>状态</th><th>操作</th></tr>';
  document.getElementById('provs').innerHTML=hd+
   (rows.length?rows.map(provRow).join(''):'<tr><td class=empty colspan=8>暂无提供方</td></tr>');
  tbl('poolprov',p.slots,['provider','key','ok','fails','cooldown_left']);
 }catch(e){toast('配置加载失败 · '+e.message)}}
function mapParse(s){const o={};String(s||'').split(/[,;]+/).forEach(t=>{const i=t.indexOf('=');
 if(i>0)o[t.slice(0,i).trim()]=t.slice(i+1).trim()});return o}
const lines=s=>String(s||'').split(String.fromCharCode(10)).map(x=>x.trim()).filter(Boolean);
 const PRESETS={
  opencode:{name:'opencode',url:'https://opencode.ai/zen/v1',style:'opencode',
   models:'*',keyless:true,note:'OpenCode · 免费模型无需 Key；网关会伪装成 OpenCode CLI 的身份头以通过免费档门禁'},
  kilocode:{name:'kilocode',url:'https://api.kilo.ai/api/gateway/v1',style:'openai',
   models:'*',note:'Kilocode · Key 取自环境变量 KILOCODE_API_KEY；模型名 kilocode/<id>'},
  openai:{name:'openai',url:'https://api.openai.com/v1',style:'openai',models:'',note:'OpenAI · Key 格式 sk-...'},
  anthropic:{name:'anthropic',url:'https://api.anthropic.com',style:'anthropic',models:'',note:'Anthropic · Key 格式 sk-ant-...'}}
 function applyPreset(k){const p=PRESETS[k];if(!p)return;
  if(gv('p_name')&&!confirm('预置会覆盖当前名称/URL/协议，继续？'))return;
  document.getElementById('p_name').value=p.name;
  document.getElementById('p_url').value=p.url;
  document.getElementById('p_style').value=p.style;
  if(p.models)document.getElementById('p_models').value=p.models;
  document.getElementById('p_preset_note').textContent=p.note||''}
function formBody(){return {name:gv('p_name'),base_url:gv('p_url'),style:gv('p_style'),
  models:mapParse(gv('p_models')),embeddings:mapParse(gv('p_emb')),
  priority:+gv('p_prio')||0,weight:+gv('p_w')||1,timeout:+gv('p_to')||120,max_rpm:+gv('p_rpm')||0,
  enabled:document.getElementById('p_on').checked,
  auto:document.getElementById('p_auto').checked,
  keyless:document.getElementById('p_keyless').checked}}
function fillForm(p){['name|p_name','base_url|p_url','style|p_style'].forEach(x=>{const [k,id]=x.split('|');
  document.getElementById(id).value=p[k]||''});
  document.getElementById('p_models').value=Object.keys(p.models||{}).map(k=>k+'='+p.models[k]).join(',');
  document.getElementById('p_emb').value=Object.keys(p.embeddings||{}).map(k=>k+'='+p.embeddings[k]).join(',');
  document.getElementById('p_prio').value=p.priority;document.getElementById('p_w').value=p.weight;
  document.getElementById('p_to').value=p.timeout;document.getElementById('p_rpm').value=p.max_rpm||0;
  document.getElementById('p_on').checked=p.enabled!==false;
  document.getElementById('p_auto').checked=p.auto!==false;
  document.getElementById('p_keyless').checked=p.keyless===true;
  document.getElementById('p_keys').value=''}
function cancelEdit(){EDIT='';document.getElementById('pf').reset();
  document.getElementById('p_on').checked=true;
  document.getElementById('p_auto').checked=true;
  document.getElementById('p_keyless').checked=false;
  document.getElementById('p_submit').textContent='新增提供方';
  document.getElementById('p_note').textContent=''}
async function submitP(){const b=formBody();
  if(!b.name||!b.base_url){toast('名称与基础URL必填');return}
  if(b.keyless&&!b.keys){b.keys=['']}
  try{if(EDIT){await send('/admin/providers/'+encodeURIComponent(EDIT),'PATCH',b);
    toast('已更新提供方 '+EDIT);cancelEdit()}
  else{b.keys=lines(gv('p_keys'));await send('/admin/providers','POST',b);
    toast('已新增提供方 '+b.name+' · 网关正在自动探测其模型');cancelEdit()}
  await loadConfig();await loadPM(true)}catch(e){toast('保存失败 · '+e.message)}}
"""
JS += """
function keyCell(p){const ks=p.keys||[];
 return '<b>'+ks.length+'</b> 把 '+ks.map((k,i)=>'<span>'+esc(k)+
  '<button data-a=delkey data-i='+i+'>×</button></span>').join(' ')}
async function addKeyTo(n){const v=window.prompt('为提供方 '+n+' 追加一把 API Key（sk-…）：');
 if(!v)return;try{await send('/admin/providers/'+encodeURIComponent(n)+'/keys','POST',{key:v.trim()});
  toast('已为 '+n+' 追加密钥');await loadConfig();await loadPM(true)}catch(e){toast('追加失败 · '+e.message)}}
async function delKey(n,i){try{await send('/admin/providers/'+encodeURIComponent(n)+'/keys/'+i,'DELETE');
  toast('已删除 '+n+' 的第 '+(+i+1)+' 把密钥');await loadConfig();await loadPM(false)}catch(e){toast('删除密钥失败 · '+e.message)}}
async function delProv(n){if(!window.confirm('确认删除提供方 '+n+' ？此操作写回 config.yaml'))return;
 try{await send('/admin/providers/'+encodeURIComponent(n),'DELETE');toast('已删除提供方 '+n);
  if(EDIT===n)cancelEdit();await loadConfig();await loadPM(false)}catch(e){toast('删除失败 · '+e.message)}}
function editProv(n){const p=(CFG.providers||[]).filter(x=>x.name===n)[0];if(!p)return;
 EDIT=n;fillForm(p);document.getElementById('p_submit').textContent='保存修改';
 document.getElementById('p_note').textContent='正在编辑：'+n+'（密钥请用下方按钮增删）';
 document.getElementById('pf').scrollIntoView({block:'center'})}
async function saveGlobals(){const body={listen:gv('g_listen'),strategy:gv('g_strategy'),
 cooldown:+gv('g_cd')||0,currency:gv('g_cur'),
 max_concurrency:+gv('g_maxc')||0,queue_wait:+gv('g_qw')||0,
 per_provider_concurrency:+gv('g_ppc')||0};
 const pr={};lines(gv('g_pricing')).forEach(t=>{const a=t.split('=');if(a.length===2){
  const v=(a[1]||'').split('/');pr[a[0].trim()]={prompt:+v[0]||0,completion:+(v[1]||0)||0}}});
 body.pricing=pr;
 try{const d=await send('/admin/config','PUT',body);CFG=d.config||CFG;
  document.getElementById('strategy').textContent=body.strategy;
  toast('全局设置已保存并热加载 · '+JSON.stringify(d.applied))}catch(e){toast('保存失败 · '+e.message)}}
"""
JS += """
const SKIN_KEY='SMSocket.skin';
function setSkin(k,save){if(!/[abcd]/.test(k))k='a';
 document.body.className='skin-'+k;
 try{localStorage.setItem(SKIN_KEY,k)}catch(e){}
 document.querySelectorAll('.skinbtn').forEach(b=>b.classList.toggle('on',b.dataset.k===k));
 if(save!==false){try{send('/admin/ui','PUT',{skin:k}).catch(()=>{})}catch(e){}}}
async function loadSettings(){try{const c=await j('/admin/config');CFG=c;
 document.getElementById('g_listen').value=c.listen||'';
 document.getElementById('g_strategy').value=c.strategy||'priority';
 document.getElementById('g_cd').value=c.cooldown;
 document.getElementById('g_cur').value=c.currency||'USD';
 document.getElementById('g_maxc').value=c.max_concurrency||0;
 document.getElementById('g_qw').value=c.queue_wait==null?30:c.queue_wait;
 document.getElementById('g_ppc').value=c.per_provider_concurrency||0;
 fillBilling(c.billing||{});
 document.getElementById('g_pricing').value=Object.keys(c.pricing||{}).map(k=>{
  const r=c.pricing[k]||{};return k+'='+r.prompt+'/'+r.completion}).join(String.fromCharCode(10));
 document.getElementById('g_path').textContent=c.path||'-';
 document.getElementById('g_slots').textContent=c.slots;
 document.getElementById('g_models').textContent=(c.models||[]).join(', ')||'无';
 }catch(e){toast('设置加载失败 · '+e.message)}}
document.addEventListener('click',ev=>{const h=ev.target.closest('tr.pmhead');
 if(h&&!ev.target.closest('button')){togglePM(h.dataset.n);return}
 const b=ev.target.closest('button[data-a]');
 if(!b)return;const a=b.dataset.a;
// 模型树里的按钮挂在 tr[data-p] 上（提供方名），不在 tr[data-n] 上
if(a==='addmodel'||a==='delmodel'||a==='undeny'){const tr=b.closest('tr[data-p]');
    if(!tr)return;const n=tr.dataset.p;
    if(a==='addmodel')addModel(n);
    else if(a==='undeny')undenyModel(n,b.dataset.u);
    else delModel(n,b.dataset.m,b.dataset.u);return}
 const tr=b.closest('tr[data-n]');if(!tr)return;const n=tr.dataset.n;
 if(a==='edit')editProv(n);
 else if(a==='key')addKeyTo(n);
 else if(a==='del')delProv(n);
 else if(a==='delkey')delKey(n,b.dataset.i)});
"""
JS += """
// ---- 并发与批量 ----------------------------------------------------------
let AUTO=false,TMR=null,LASTJOB='';
function num(x,d){return (x==null||x===''||isNaN(x))?(d===undefined?'-':d):x}
async function loadSocket(){try{const x=await j('/concurrency');
 [['active','s_active'],['queued','s_queued'],['peak','s_peak'],['total','s_total'],
  ['rejected','s_rej'],['rps','s_rps'],['errors','s_err'],['avg_wait_ms','s_wait']]
  .forEach(([k,id])=>{const el=document.getElementById(id);if(el)el.textContent=num(x[k],0)});
 document.getElementById('s_max').textContent=x.max_concurrency||'不限';
 document.getElementById('s_ppc').textContent=x.per_provider_concurrency||'不限';
 document.getElementById('s_qw').textContent=(x.queue_wait||0)+'s';
 document.getElementById('s_sat').textContent=x.saturated?'已饱和（新请求排队/429）':'有余量';
 renderStack(x);
 const rows=x.by_provider||{},keys=Object.keys(rows),bp=document.getElementById('sprov');
 bp.innerHTML=keys.length?('<tr><th>提供方</th><th>在途</th><th>峰值</th><th>累计</th><th>失败</th></tr>'+
  keys.map(k=>'<tr><td><b>'+esc(k)+'</b></td><td>'+rows[k].active+'</td><td>'+rows[k].peak+
   '</td><td>'+rows[k].total+'</td><td>'+rows[k].errors+'</td></tr>').join(''))
  :'<tr><td class=empty colspan=5>暂无在途数据</td></tr>';
 }catch(e){toast('并发数据加载失败 · '+e.message)}}
const ASSESS_ON=__ASSESS_ON__;
function renderStack(x){const st=(x&&x.stack)||null,blk=document.getElementById('stackblk');
 if(!blk)return; if(!st||!st.enabled){blk.classList.add('hide');return}
 blk.classList.remove('hide');
 const set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v};
 set('k_policy',st.policy);set('k_depth',st.depth);set('k_peak',st.peak);
 set('k_popped',st.popped);set('k_expired',st.expired);
 set('k_avg',num(st.avg_parked_ms,0));set('k_pmax',num(st.parked_ms_peak,0));
 const es=st.entries||[],t=document.getElementById('kentries');
 t.innerHTML=es.length?('<tr><th>别名</th><th>提供方</th><th>原因</th><th>已等待ms</th></tr>'+
  es.map(e=>'<tr><td><b>'+esc(e.alias||'-')+'</b></td><td>'+esc(e.provider||'-')+
   '</td><td>'+esc(e.reason)+'</td><td>'+e.waited_ms+'</td></tr>').join(''))
  :'<tr><td class=empty colspan=4>栈内没有等待中的请求</td></tr>'}
async function loadReach(){const blk=document.getElementById('reachblk');
 if(!blk||!ASSESS_ON){return}
 try{const d=await j('/assess'),rows=d.rows||[];
  blk.classList.remove('hide');
  document.getElementById('r_window').textContent=Math.round((d.window_s||0)/60)+' 分钟';
  document.getElementById('r_rows').textContent=(d.counts||{}).rows||0;
  const eg=[...new Set(rows.map(r=>r.egress))].sort();
  const cell=(m,e)=>rows.filter(r=>r.model===m&&r.egress===e)[0];
  const t=document.getElementById('reach');
  t.innerHTML=rows.length?('<tr><th>模型</th>'+eg.map(e=>'<th>'+esc(e)+'</th>').join('')+
   '</tr>'+[...new Set(rows.map(r=>r.model))].sort().map(m=>'<tr><td><b>'+esc(m)+'</b>'+
   eg.map(e=>{const c=cell(m,e);if(!c)return '<td class=v-unknown>-</td>';
    return '<td class=v-'+c.verdict+' title="p50 '+c.p50_ms+'ms · 成功率 '+
     Math.round(c.ok_ratio*100)+'% · '+(c.last_error||'ok')+'">'+
     c.p50_ms+'ms</td>'}).join('')+'</tr>').join(''))
   :'<tr><td class=empty colspan=6>暂无评估数据（等待定时探测或点“立即探测”）</td></tr>';
  try{const st=await j('/assess/status');
   const nx=st.next_run?new Date(st.next_run*1000).toLocaleTimeString():'未排程';
   document.getElementById('r_next').textContent=nx}catch(e){}}
 catch(e){blk.classList.add('hide')}}
async function runReach(){try{toast('探测中…');const d=await send('/assess/run','POST',
 {async:true});toast(d.accepted?'已排入探测':'探测未启动：'+(d.reason||''))}
 catch(e){toast('探测失败 · '+e.message)}}
function tickSocket(){if(!AUTO)return;loadSocket();TMR=setTimeout(tickSocket,1000)}
function toggleAuto(){AUTO=!AUTO;document.getElementById('s_auto').textContent='自动刷新：'+(AUTO?'开':'关');
 clearTimeout(TMR);if(AUTO)tickSocket()}
function batchItems(){return lines(gv('q_body')).map((ln,i)=>{
 if(ln[0]==='{'){try{const o=JSON.parse(ln);o.custom_id=o.custom_id||('c'+i);return o}catch(e){return null}}
 return {custom_id:'c'+i,model:(CFG.models||[])[0]||'demo',
  messages:[{role:'user',content:ln}]}}).filter(Boolean)}
function resRows(rs){return (rs||[]).map(r=>({index:r.index,custom_id:r.custom_id,model:r.model,
 status:r.status,ms:r.ms,tokens:(r.usage||{}).total_tokens||0,
 cost:r.cost?(+r.cost.display).toFixed(6)+' '+r.cost.display_currency:'-',
 err:(r.error||{}).message||(r.cancelled?'已取消':(r.skipped?'已跳过':''))}))}
async function runBatch(){const items=batchItems();
 if(!items.length){toast('请求列表为空（每行一条提示词或 JSON）');return}
 const body={requests:items,concurrency:+gv('q_conc')||8,fail_fast:gv('q_ff')==='1'};
 const note=document.getElementById('q_note');note.textContent='发送中 '+items.length+' 条…';
 try{
  if(gv('q_mode')==='async'){const d=await send('/v1/batch?async=1','POST',body);LASTJOB=d.job;
   note.textContent='作业已受理 '+d.job+' · 点「轮询作业」看进度';
   tbl('qres',[],['index','custom_id','model','status','ms','tokens','cost','err']);showJobs();return}
  const d=await send('/v1/batch','POST',body),sm=d.summary||{};
  note.textContent='完成：成功 '+sm.ok+' / 失败 '+sm.failed+' / 跳过 '+(sm.skipped||0)+
   ' · 词元 '+sm.tokens+' · 费用 '+(+sm.cost||0).toFixed(6)+' '+(sm.currency||'')+
   ' · 墙钟 '+sm.wall_ms+'ms（平均单条 '+sm.avg_ms+'ms）';
  tbl('qres',resRows(d.results),
   ['index','custom_id','model','status','ms','tokens','cost','err']);
 }catch(e){note.textContent='失败 · '+e.message}}
async function showJobs(){try{const d=await j('/v1/batches');
 tbl('qjobs',d.jobs.map(x=>({job:x.job,status:x.status,done:x.completed+' / '+x.total,
  failed:x.failed,concurrency:x.concurrency,elapsed:x.elapsed+'s'})),
  ['job','status','done','failed','concurrency','elapsed'])}catch(e){}}
async function pollJob(){if(!LASTJOB){toast('还没有异步作业，先用「异步作业」模式发送');return}
 try{const d=await j('/v1/batches/'+LASTJOB+'?wait=5');
  document.getElementById('q_note').textContent=d.status+' · '+d.completed+'/'+d.total+
   ' · 进度 '+(d.progress*100).toFixed(0)+'%';
  tbl('qres',resRows(d.results),
   ['index','custom_id','model','status','ms','tokens','cost','err']);
  showJobs()}catch(e){toast('轮询失败 · '+e.message)}}
"""
JS += """
// ---- 计费货币 ------------------------------------------------------------
function optList(id,arr,cur){const s=document.getElementById(id);if(!s)return;
 const list=(arr&&arr.length)?arr:CUR;
 s.innerHTML=list.map(c=>'<option value='+c+'>'+c+(SYM[c]?' '+SYM[c]:'')+'</option>').join('');
 if(cur&&list.indexOf(cur)>=0)s.value=cur}
function fillBilling(b){b=b||{};let cs=Object.keys(b.rates||{}).sort();
 if(!cs.length)cs=CUR;                 // an unauthenticated or empty table
 optList('b_cur',cs,b.currency||'USD');optList('b_base',cs,b.base||'USD');
 optList('c_from',cs,'USD');optList('c_to',cs,b.currency||'USD');
 optList('g_cur',cs,b.currency||'USD');
 const g=(id,v)=>{const el=document.getElementById(id);if(el)el.value=v};
 g('b_prec',b.precision==null?6:b.precision);g('b_url',b.rates_url||'');
 g('b_rates',Object.keys(b.rates||{}).map(k=>k+'='+b.rates[k]).join(String.fromCharCode(10)))}
async function loadBilling(){try{const d=await j('/admin/billing');fillBilling(d.billing);
  const n=document.getElementById('b_note');
  if(n)n.textContent='当前显示币种 '+d.currency+' '+d.symbol}catch(e){toast('计费设置加载失败 · '+e.message)}}
async function saveBilling(){const rates={};
 lines(gv('b_rates')).forEach(x=>{const a=x.split('=');
  if(a.length===2)rates[a[0].trim().toUpperCase()]=+a[1]||0});
 const body={billing:{currency:gv('b_cur'),base:gv('b_base'),precision:+gv('b_prec')||6,
  rates_url:gv('b_url'),rates:rates}};
 try{const d=await send('/admin/billing','PUT',body);fillBilling(d.billing);
  document.getElementById('b_note').textContent='已保存 · 显示币种 '+d.billing.currency+
   ' · '+Object.keys(d.billing.rates).length+' 条汇率';refresh()}
 catch(e){document.getElementById('b_note').textContent='保存失败 · '+e.message}}
async function refreshRates(){try{const d=await send('/admin/billing/rates/refresh','POST',
  gv('b_url')?{url:gv('b_url')}:{});fillBilling(d.billing);
  document.getElementById('b_note').textContent='已更新 '+d.updated+' 条汇率（'+d.source+'）'}
 catch(e){document.getElementById('b_note').textContent='拉取失败 · '+e.message}}
async function convertTry(){try{const d=await j('/admin/billing/convert?amount='+
   encodeURIComponent(gv('c_amt')||1)+'&frm='+gv('c_from')+'&to='+gv('c_to'));
  document.getElementById('c_out').textContent=d.converted+' '+d.to+
   '（1 '+d.from+' = '+d.rate+' '+d.to+'）'}
 catch(e){document.getElementById('c_out').textContent='换算失败'}}
"""
JS += """
// ---- 提供商与大模型：一级=提供商，二级=其模型，参数自动探测 ------------------
let PM=[],OPEN={};
function pmFind(n){for(const x of PM)if(x.name===n)return x;return null}
function pmChip(t,c){return '<span class="pmchip '+(c||'')+'">'+esc(t)+'</span>'}
function pmParams(m){
 if(!m.probed)return pmChip('未探测','off');
 const P=m.params||{},ks=Object.keys(P);
 if(!ks.length)return pmChip('无参数记录','muted');
 const sup=ks.filter(k=>P[k]==='supported'),rej=ks.filter(k=>P[k]==='rejected');
 let out=sup.slice(0,5).map(k=>pmChip(k,'ok')).join('');
 if(sup.length>5)out+=pmChip('+'+(sup.length-5),'muted');
 out+=rej.slice(0,3).map(k=>pmChip(k,'no')).join('');
 if(m.stream==='supported')out+=pmChip('流式','ok');
 else if(m.stream==='rejected')out+=pmChip('无流式','no');
 return out}
function pmModelRow(n,m){
  const st=m.ok?pmChip('可用','ok'):(m.probed?pmChip('不可用','no'):pmChip('未探测','off'));
  const lat=m.probed?(Math.round(m.latency_ms||0)+' ms'):'-';
  const ctx=m.context?esc(m.context):'-';
const al=m.alias?esc(m.alias)+' → '+esc(m.upstream):esc(m.upstream);
    const tag=(m.embed?pmChip('嵌入','muted'):'')+(m.published?'':pmChip('未发布','warn'))+
      (m.denied?pmChip('已拒绝','no'):'');
    // 删除按钮：已拒绝的可取消拒绝；已发布的别名走 config 删除；
    // 未发布（未拒绝）的探测缓存走缓存删除（同时记录拒绝，下次探测不再问）
    const act=m.denied?'<button class=pmact data-a=undeny data-m='+(m.alias||m.upstream)+
      ' data-u='+esc(m.upstream)+'>取消拒绝</button>':
      (m.published?'<button class=pmact data-a=delmodel data-m='+(m.alias||m.upstream)+
      '>删除别名</button>':'<button class=pmact data-a=delmodel data-m='+esc(m.upstream)+
      ' data-u='+esc(m.upstream)+'>删除缓存</button>');
  return '<tr class="pmrow" data-p='+esc(n)+'><td>'+al+' '+tag+'</td><td>'+st+'</td>'+
   '<td>'+esc(lat)+'</td><td>'+esc(ctx)+'</td><td class="pmchips">'+pmParams(m)+'</td>'+
   '<td class="muted">'+esc((m.error||'').slice(0,60))+'</td><td>'+act+'</td></tr>'}
function pmProvRow(x){
  const on=!OPEN[x.name];
  const hd=x.model_count+' 个模型 · 可用 '+x.ok_models+
   (x.probed_at?' · 探测于 '+new Date(x.probed_at*1000).toLocaleString():' · 尚未探测');
  const st=x.enabled?pmChip('启用','ok'):pmChip('停用','off');
  const err=x.error?' '+pmChip('探测报错','no'):'';
  const why=x.probe_note?' '+pmChip(x.probe_note,'off'):'';
  return '<tr class="pmhead'+(x.stale?' stale':'')+'" data-n='+esc(x.name)+'>'+
   '<td><span class="pmcaret">'+(on?'▸':'▾')+'</span><b>'+esc(x.name)+'</b>'+st+err+'</td>'+
   '<td>'+esc(x.base_url)+'</td><td>'+esc(x.style)+'</td><td>'+esc(x.key_count)+'</td>'+
   '<td>'+esc(x.priority)+' / '+esc(x.weight)+'</td><td class="muted">'+esc(hd)+why+
   '</td></tr>'}
function paintPM(){
  const t=document.getElementById('pmtree');if(!t)return;
  if(!PM.length){t.innerHTML='<tr><td class=empty colspan=7>还没有提供方：在下方「新增 / 编辑提供方」添加，网关会自动探测其模型与参数</td></tr>';return}
  const hd='<tr><th>提供商</th><th>基础URL</th><th>协议</th><th>密钥</th><th>优先级/权重</th><th>模型概况</th><th>操作</th></tr>';
  let out='';
  PM.forEach(x=>{out+=pmProvRow(x);
   if(OPEN[x.name]){
    if(!x.models.length)out+='<tr class="pmsub"><td colspan=7 class=empty>未列出模型 · 网关会自动探测</td></tr>';
    else out+='<tr class="pmcols"><td colspan=7><table class="pmsub"><tr><th>模型（别名 → 真实名）</th><th>状态</th><th>延迟</th><th>上下文</th><th>参数（自动探测）</th><th>错误</th><th>操作</th></tr>'+
      x.models.map(m=>pmModelRow(x.name,m)).join('')+
      pmAddRow(x.name)+'</table></td></tr>'}})
  t.innerHTML=hd+out}
// 单条新增模型别名的小表单（挂在该提供方的模型子表末尾）
function pmAddRow(n){return '<tr class="pmadd" data-p='+esc(n)+'>'+
  '<td><input class="pmf" placeholder="别名" id="pm_a_'+esc(n)+'"></td>'+
  '<td colspan=5><input class="pmf" placeholder="真实模型名（upstream）" id="pm_u_'+esc(n)+'">'+
   '<label class="pmemb"><input type="checkbox" id="pm_e_'+esc(n)+'">嵌入模型</label></td>'+
  '<td><button class=pmact data-a=addmodel>添加</button></td></tr>'}
async function loadPM(auto){
 try{const d=await j('/admin/provider-models');PM=d.data||[];paintPM();
  const el=document.getElementById('pm_seen');
  if(el)el.textContent=(d.models||0)+' 个模型 · 最近探测 '+
   (d.last_seen?new Date(d.last_seen*1000).toLocaleString():'无')+
   ' · 新鲜度 '+(d.fresh_seconds||900)+'s';
  if(auto)autoProbe()}catch(e){toast('模型树加载失败 · '+e.message)}}
async function autoProbe(){
 try{const d=await send('/admin/provider-models/refresh','POST',{});
  const el=document.getElementById('pm_note');
  if(d.queued&&d.queued.length){if(el)el.textContent='自动探测中：'+d.queued.join(', ');pollPM()}
  else if(el)el.textContent=d.note||'全部提供方均为最新'}catch(e){}}
function pollPM(){
 setTimeout(async()=>{try{const s=await j('/admin/discover/status');
  const el=document.getElementById('pm_prog');
  if(s&&s.running){if(el)el.textContent='探测中 · 已完成 '+s.done+' 个模型 · '+s.elapsed+'s';pollPM();return}
  if(el)el.textContent='空闲';await loadPM(false);await loadConfig()}catch(e){await loadPM(false)}}
 ,1500)}
function togglePM(n){OPEN[n]=!OPEN[n];paintPM()}
async function clearPM(n){try{await send('/admin/models?provider='+encodeURIComponent(n||''),'DELETE');
  await loadPM(true)}catch(e){toast('清空失败 · '+e.message)}}
async function addModel(n){const a=(document.getElementById('pm_a_'+n)||{}).value||'',
  u=(document.getElementById('pm_u_'+n)||{}).value||'',
  e=document.getElementById('pm_e_'+n);
 if(!a||!u){toast('别名与真实名都必填');return}
 try{await send('/admin/providers/'+encodeURIComponent(n)+'/models','POST',
   {alias:a.trim(),upstream:u.trim(),embed:!!(e&&e.checked)});
  toast('已为 '+n+' 添加模型 '+a.trim()+' · 网关正在探测');
  await loadPM(true);await loadConfig()}catch(e){toast('添加模型失败 · '+e.message)}}
async function delModel(n,m,u){
  if(!m){toast('缺少模型标识');return}
  if(u){
    // 未发布的探测缓存：按 (provider, upstream) 删除缓存行并记录拒绝，
    // 下一次自动探测不会再问这个 id（可由「取消拒绝」恢复）
    if(!window.confirm('从 '+n+' 的探测缓存中删除模型 '+u+' ？\\n\\n'
        +'删除后网关不会再探测这个模型；如需恢复请点「取消拒绝」'))return;
    try{const r=await send('/admin/models/'+encodeURIComponent(n)+'/'+encodeURIComponent(u),'DELETE');
      toast('已删除缓存 '+n+'/'+u+' · '+JSON.stringify(r.deleted)+' 行 · 已加入拒绝列表');
      await loadPM(false);await loadConfig()}catch(e){toast('删除缓存失败 · '+e.message)}
    return
  }
  if(!window.confirm('从 '+n+' 删除模型别名 '+m+' ？此操作写回 config.yaml'))return;
  try{await send('/admin/providers/'+encodeURIComponent(n)+'/models/'+encodeURIComponent(m),'DELETE');
   toast('已从 '+n+' 删除模型 '+m);await loadPM(false);await loadConfig()}
  catch(e){toast('删除模型失败 · '+e.message)}}
async function undenyModel(n,u){
  if(!u){toast('缺少模型标识');return}
  if(!window.confirm('取消对 '+n+'/'+u+' 的拒绝？\\n\\n取消后下一次探测会重新列出并探测这个模型'))return;
  try{const r=await send('/admin/models/'+encodeURIComponent(n)+'/'+encodeURIComponent(u)+'/undeny','POST');
   toast('已取消拒绝 '+n+'/'+u+' · '+JSON.stringify(r.cleared)+' 条');await loadPM(false)}
  catch(e){toast('取消拒绝失败 · '+e.message)}}
"""
JS += """
 (function(){let k='__SKIN__';try{k=localStorage.getItem(SKIN_KEY)||k}catch(e){}
 setSkin(k,false);
 try{const sv=localStorage.getItem('SMSocket.key');
  if(sv&&!document.getElementById('key').value)document.getElementById('key').value=sv}catch(e){}
 optList('b_cur',CUR,'USD');optList('b_base',CUR,'USD');
 optList('c_from',CUR,'USD');optList('c_to',CUR,'USD');optList('g_cur',CUR,'USD');
 const h=(location.hash||'').replace('#','');let p=h;
 if(PAGES.indexOf(p)<0){try{p=localStorage.getItem(PKEY)||'overview'}catch(e){p='overview'}}
 go(p);loadSelf();refresh()})();
"""
JS += """
const NET_ON=__NET_ON__;
const NET='/internal/net';
let netOpen=false;
function netHdr(){return Object.assign({'content-type':'application/json'},hdr())}
function netRow(k,v){return '<div class=netrow><em>'+k+'</em><code>'+(v==null?'-':esc(v))+'</code></div>'}
async function netGet(u){const r=await fetch(NET+u,{headers:netHdr()});
 const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error((d.detail&&d.detail.message)||u);return d}
async function netSend(u,m,b){const r=await fetch(NET+u,{method:m,headers:netHdr(),
 body:JSON.stringify(b||{})});const d=await r.json().catch(()=>({}));
 if(!r.ok)throw new Error((d.detail&&d.detail.message)||u+' -> '+r.status);return d}
function netPanel(on){netOpen=on;
 document.getElementById('net_mask').classList.toggle('on',on);
 document.getElementById('net_panel').classList.toggle('on',on);
 if(on)netRefresh()}
async function netRefresh(){const box=document.getElementById('net_body');
 try{const [s,sc]=await Promise.all([netGet('/state'),netGet('/scores')]);
  box.innerHTML=netRow('模式',s.mode)+netRow('控制器',s.controller)+netRow('在线',s.alive)+
   netRow('出口',((s.paths||[]).join(' '))||'direct')+netRow('打分',s.scores)+
   netRow('上次探测',s.last_refresh)+netRow('错误',s.error||'')+
   '<h2>出口打分</h2><table id=net_t></table>'
  const rows=(sc.scores||[]).map(x=>'<tr><td>'+esc(x.provider)+'</td><td>'+esc(x.egress)+
   '</td><td>'+(x.ok?'通':'败')+'</td><td>'+x.delay_ms+'</td><td>'+x.fail_ratio+'</td></tr>');
  document.getElementById('net_t').innerHTML=rows.length?
   ('<tr><th>提供方</th><th>出口</th><th>状态</th><th>ms</th><th>失败率</th></tr>'+rows.join(''))
   :'<tr><td class=empty colspan=5>尚无数据，点探测</td></tr>';
  document.getElementById('net_note').textContent=''
 }catch(e){box.innerHTML='<div class=muted>不可用：'+esc(e.message)+'</div>'}}
async function netProbe(){document.getElementById('net_note').textContent='探测中…';
 try{const d=await netSend('/probes','POST',{});
  document.getElementById('net_note').textContent='探测 '+d.probes+' 次';netRefresh()}
 catch(e){document.getElementById('net_note').textContent=e.message}}
async function netMode(m){try{await netSend('/mode','POST',{mode:m});netRefresh()}
 catch(e){document.getElementById('net_note').textContent=e.message}}
async function netSelect(){const g=document.getElementById('net_g').value.trim(),
 n=document.getElementById('net_n').value.trim();if(!g||!n)return;
 try{await netSend('/select','PUT',{group:g,node:n});netRefresh()}
 catch(e){document.getElementById('net_note').textContent=e.message}}
async function netFlush(){try{await netSend('/flush','POST',{dns:true});
 document.getElementById('net_note').textContent='DNS 已刷新'}
 catch(e){document.getElementById('net_note').textContent=e.message}}
function netEgress(){const p=document.getElementById('net_p').value.trim();if(!p)return;
 netGet('/egress/'+encodeURIComponent(p)).then(d=>{document.getElementById('net_note').textContent=
  p+' -> '+d.pick}).catch(e=>{document.getElementById('net_note').textContent=e.message})}
if(NET_ON){document.getElementById('net_glyph').classList.remove('off');
 document.addEventListener('keydown',e=>{if(e.key==='Escape'&&netOpen)netPanel(false)})}
"""
JS += """
// ---- SMSC 网络平面（总设置 · 扩展程序条目下） ------------------------------
function gv_set(id,v){const e=document.getElementById(id);if(e&&v!=null&&v!==undefined)e.value=v}
function boolOf(id){const e=document.getElementById(id);return !!e&&e.value==='1'}
async function loadClash(){try{const c=await j('/admin/config');const k=c.clash||{};
 gv_set('c_on',k.enabled?'1':'0');gv_set('c_ctl',k.controller);gv_set('c_port',k.mixed_port);
 gv_set('c_mode',k.mode||'auto');gv_set('c_int',k.interval);gv_set('c_health',k.health_url);
 gv_set('c_smart',(k.smart===false?'0':'1'));gv_set('c_fail',k.fail_ratio);
 gv_set('c_min',k.min_delay_ms);gv_set('c_groups',(k.groups||[]).join(','));
 const pp=k.provider_proxy||{};
 gv_set('c_pp',Object.keys(pp).map(x=>x+'='+pp[x]).join(String.fromCharCode(10)));
 const sec=document.getElementById('c_secret');
 if(sec){sec.value='';sec.placeholder=(k.secret&&k.secret!=='***')?
  '已设置 · 留空＝保持原值':'留空＝不使用密钥'}
 }catch(e){}}
async function saveClash(){const body={clash:{enabled:boolOf('c_on'),controller:gv('c_ctl'),
 mixed_port:+gv('c_port')||0,mode:gv('c_mode'),interval:+gv('c_int')||60,
 health_url:gv('c_health'),smart:boolOf('c_smart'),fail_ratio:+gv('c_fail')||0.5,
 min_delay_ms:+gv('c_min')||0,groups:gv('c_groups'),provider_proxy:gv('c_pp')}};
 const sec=gv('c_secret');if(sec)body.clash.secret=sec;
 try{const d=await send('/admin/config','PUT',body);
 document.getElementById('c_note').textContent='已保存 · 出口平面 '+
  ((d.applied||{}).net_plane?'已重建在线':'未启用');loadClash()}
 catch(e){document.getElementById('c_note').textContent='保存失败 · '+e.message}}
// ---- 自动调权 --------------------------------------------------------------
async function loadTune(){try{const c=await j('/admin/config');const t=c.tune||{};
 gv_set('t_on',(t.enabled===false?'0':'1'));gv_set('t_win',t.window_s);
 gv_set('t_min',t.min_samples);gv_set('t_int',t.interval_s);
 gv_set('t_persist',t.persist?'1':'0');gv_set('t_pspread',t.priority_spread);
 gv_set('t_wspread',t.weight_spread)}catch(e){}}
async function saveTune(){const body={tune:{enabled:boolOf('t_on'),
 window_s:+gv('t_win')||86400,min_samples:+gv('t_min')||3,interval_s:+gv('t_int')||600,
 persist:boolOf('t_persist'),priority_spread:+gv('t_pspread')||10,
 weight_spread:+gv('t_wspread')||10}};
 try{await send('/admin/config','PUT',body);
 document.getElementById('t_note').textContent='已保存 · 按实测窗口自动重算';
 loadTuneScores()}catch(e){document.getElementById('t_note').textContent='保存失败 · '+e.message}}
async function applyTune(){try{const d=await send('/tune/apply','POST',{});
 document.getElementById('t_note').textContent='已重算 · 生效 '+d.applied+' 个提供方';
 loadTuneScores();loadConfig()}catch(e){document.getElementById('t_note').textContent='重算失败 · '+e.message}}
async function loadTuneScores(){const t=document.getElementById('tunet');if(!t)return;
 try{const d=await j('/tune');const rows=d.scores||[];
 t.innerHTML=rows.length?('<tr><th>提供方</th><th>样本</th><th>成功率</th><th>p50(ms)</th>'+
  '<th>词元/秒</th><th>优先级</th><th>权重</th><th>依据</th></tr>'+rows.map(x=>'<tr><td><b>'+
  esc(x.provider)+'</b></td><td>'+x.samples+'</td><td>'+Math.round((x.ok_ratio||0)*100)+
  '%</td><td>'+x.p50_ms+'</td><td>'+x.tok_s+'</td><td>'+x.priority+'</td><td>'+x.weight+
  '</td><td>'+esc(x.note||'')+'</td></tr>').join(''))
  :'<tr><td class=empty colspan=8>暂无实测数据（日常对话与定时探测都会写进这里）</td></tr>';
 }catch(e){t.innerHTML=''}}
"""
JS += """
// ---- 扩展程序：导入 · 识别码验证 · 自带面板挂载 --------------------------
function smscUrl(){let c='http://127.0.0.1:9090';const e=document.getElementById('c_ctl');
 if(e&&e.value.trim())c=e.value.trim();if(c.slice(0,4)!=='http')c='http://'+c;
 while(c.length>1&&c.charAt(c.length-1)==='/')c=c.slice(0,-1);return c+'/ui/'}
function openSmsc(){const u=smscUrl();let w=null;try{w=window.open(u,'_blank')}catch(e){}
 const n=document.getElementById('c_note');if(n)n.textContent=
  w?('已打开 SMSC 控制台 · '+u):('浏览器拦住了新窗口，请手动打开 '+u)}
function xnote(s){const e=document.getElementById('x_note');if(e)e.textContent=s||''}
function extPath(){const e=document.getElementById('x_path');return e?e.value.trim():''}
async function loadExts(){try{renderExts(await j('/admin/extensions'))}
 catch(e){const b=document.getElementById('x_list');
  if(b)b.innerHTML='<p class="extchip bad">扩展程序读取失败 · '+esc(e.message)+'</p>'}}
var XPATH={};
function renderExts(d){const box=document.getElementById('x_list');if(!box)return;
 const code=document.getElementById('x_code');if(code)code.textContent=(d&&d.identity)||'-'
 ;const items=(d&&d.items)||[];
 if(!items.length){box.innerHTML='<p class="extnote">尚无已导入的扩展程序</p>';return}
 box.innerHTML=items.map(it=>{const ok=it.verified&&it.panel_ready;
  return '<div class="extitem" data-name="'+esc(it.name)+'"><h4>'+esc(it.title||it.name)+
   '<span class="extchip '+(ok?'ok':'bad')+'">'+(it.verified?(ok?'识别码验证通过 · 面板已挂载':'面板缺失'):'未验证')+'</span>'+
   (it.version?'<span class="extchip">v'+esc(it.version)+'</span>':'')+'</h4>'+
   '<div class="extpath">'+esc(it.path)+'</div>'+
   (it.reason?'<p class="extchip bad">'+esc(it.reason)+'</p>':'')+
    '<div class="btns"><button type="button" onclick="mountPanel(&#39;'+esc(it.name)+'&#39;)" '+(ok?'':'disabled title="面板缺失，无法挂载"')+'>刷新面板</button>'+
    (it.verified?'':'<button type="button" onclick="pairPath(&#39;'+esc(it.name)+'&#39;)">配对并导入</button>')+
    '<button type="button" onclick="removeExt(&#39;'+esc(it.name)+'&#39;)">卸载</button></div>'+
    (ok?'<iframe id="pf-'+esc(it.name)+'" class="extpanel" title="'+esc(it.title||it.name)+' 自带面板"></iframe>':'')+
    (it.verified&&!ok?'<p class="extchip bad">自带面板缺失或过大，刷新无效</p>':'')+
    '</div>'
  }).join('');
 items.forEach(it=>{XPATH[it.name]=it.path;if(it.verified&&it.panel_ready)mountPanel(it.name)})}
async function mountPanel(name){const f=document.getElementById('pf-'+name);if(!f)return;
  try{const r=await fetch('/admin/extensions/'+encodeURIComponent(name)+'/panel',{headers:hdr()});
   if(!r.ok)throw new Error('面板 '+r.status);
   const txt=await r.text();
   if(!/^[ \t]*(<!doctype|<html|<div|<style)/i.test(txt))throw new Error('面板内容不是 HTML');
   f.srcdoc=txt}
  catch(e){f.removeAttribute('srcdoc');xnote('自带面板挂载失败 · '+e.message)}}
async function importExt(){const p=extPath();if(!p){xnote('请先填扩展程序路径');return}
 xnote('导入中…');try{const d=await send('/admin/extensions/import','POST',{path:p});
  xnote('识别码验证通过 · '+(d.item&&d.item.title||p)+' 的自带面板已挂到扩展程序条目');renderExts(d)}
 catch(e){xnote('导入失败 · '+e.message);loadExts()}}
async function pairExt(){const p=extPath();if(!p){xnote('请先填扩展程序路径');return}
 xnote('配对中…');try{const d=await send('/admin/extensions/pair','POST',{path:p});
  xnote('识别码已写入 '+(d.paired&&d.paired.file||'asset')+' · 验证通过，自带面板已挂载');renderExts(d)}
 catch(e){xnote('配对失败 · '+e.message);loadExts()}}
function pairPath(n){const e=document.getElementById('x_path');if(e)e.value=XPATH[n]||'';pairExt()}
async function removeExt(name){try{renderExts(await send('/admin/extensions/remove','POST',{name:name}));
  xnote('已卸载 '+name)}catch(e){xnote('卸载失败 · '+e.message)}}
"""
BODY = """
<body class="skin-__SKIN__">
<header>
<h1>SMSocket 控制台</h1>
<div class="nav">
<button class="navbtn" data-p="overview" onclick="go('overview')">概览</button>
<button class="navbtn" data-p="usage" onclick="go('usage')">流量与费用</button>
<button class="navbtn" data-p="providers" onclick="go('providers')">提供商与大模型</button>
<button class="navbtn" data-p="socket" onclick="go('socket')">并发与批量</button>
<button class="navbtn" data-p="settings" onclick="go('settings')">总设置</button>
</div>
<div class="cred"><em>基础URL</em><code id="baseurl">-</code>
<button onclick="copyText(document.getElementById('baseurl').textContent,' 基础URL')">复制</button>
<em>API Key</em><code id="apikey">-</code>
<button id="reveal" onclick="revealKey()">显示</button>
<button onclick="copyText(document.getElementById('apikey').textContent,' API Key')">复制</button>
</div>
<input id="key" type="password" size="20" placeholder="主密钥（本机浏览器自动免填）"
 oninput="saveKey()">
<button onclick="refresh()">刷新</button>
<span>调度策略：<b id="strategy">-</b></span>
<span id="msg"></span>
</header>
"""
BODY += """
<main>
<div class="page" id="page-overview">
<div class="cards">
<div class="card"><b id="calls">-</b><i>调用总数</i></div>
<div class="card"><b id="tokens">-</b><i>词元总量</i></div>
<div class="card"><b id="cost">-</b><i>累计费用</i></div>
<div class="card"><b id="slots">-</b><i>密钥槽位</i></div>
<div class="card"><b id="models">-</b><i>可用模型</i></div>
<div class="card"><b id="xactive">-</b><i>当前并发</i></div>
<div class="card"><b id="xpeak">-</b><i>峰值并发</i></div>
<div class="card"><b id="xcurrency">-</b><i>计费币种</i></div>
</div>
<h2>按模型统计</h2><table id="bymodel"></table>
<h2>密钥池摘要</h2><table id="pool"></table>
</div>
<div class="page" id="page-usage">
<div class="cards">
<div class="card"><b id="ucalls">-</b><i>调用总数</i></div>
<div class="card"><b id="utokens">-</b><i>词元总量</i></div>
<div class="card"><b id="ucost">-</b><i>累计费用</i></div>
</div>
<h2>按提供方统计</h2><table id="byprov"></table>
<h2>近 14 天用量</h2><table id="daily"></table>
<h2>最近调用记录</h2><table id="recent"></table>
</div>
"""
BODY += """
<div class="page" id="page-providers">
<h2>提供商与大模型（点一行展开其模型；参数由网关自动探测）</h2>
<div class="cred"><em>自动探测</em><code id="pm_prog">空闲</code><em>缓存</em><code id="pm_seen">-</code>
<em>说明</em><code id="pm_note">进入本页自动补探陈旧项</code>
<button onclick="clearPM('')">清空全部缓存</button></div>
<table id="pmtree"></table>
<h2>提供方操作（编辑 · 密钥增删 · 删除）</h2><table id="provs"></table>
<h2>新增 / 编辑提供方</h2>
 <div class="btns wide"><em>预置提供方（点按钮自动填入 URL/协议）</em>
 <button type="button" onclick="applyPreset('opencode')">OpenCode</button>
 <button type="button" onclick="applyPreset('kilocode')">Kilocode</button>
 <button type="button" onclick="applyPreset('openai')">OpenAI</button>
 <button type="button" onclick="applyPreset('anthropic')">Anthropic</button>
 <span id="p_preset_note" class=muted></span></div>
 <form id="pf" onsubmit="submitP();return false">
<label>名称<input id="p_name" placeholder="my-provider"></label>
<label>基础URL<input id="p_url" placeholder="https://api.xx.com/v1"></label>
<label>协议<select id="p_style"><option value="openai">openai · 对话API</option>
<option value="openai-responses">openai-responses · 响应API</option>
<option value="anthropic">anthropic</option></select></label>
<label>优先级<input id="p_prio" type="number" value="0" title="留默认即可：网关按实测自动调权"></label>
<label>权重<input id="p_w" type="number" value="1" title="留默认即可：网关按实测自动调权"></label>
<label>参与自动调权<input id="p_auto" type="checkbox" checked></label>
<label>超时(秒)<input id="p_to" type="number" value="120"></label>
<label>最大RPM（0=不限）<input id="p_rpm" type="number" value="0"></label>
<label>启用<input id="p_on" type="checkbox" checked></label>
 <label>无密钥提供方<input id="p_keyless" type="checkbox"></label>
<label class="wide">模型别名（别名=真实名,别名2=真实2）<input id="p_models" placeholder="gpt-x=gpt-4o-mini"></label>
<label class="wide">嵌入模型（同上格式）<input id="p_emb"></label>
<label class="wide">密钥（每行一个，仅新增时生效）<textarea id="p_keys" rows="3" placeholder="sk-..."></textarea></label>
<div class="btns wide"><button id="p_submit" type="submit">新增提供方</button>
<button type="button" onclick="cancelEdit()">清空</button>
<span id="p_note"></span></div>
</form>
<h2>密钥池实时状态</h2><table id="poolprov"></table>
</div>
"""
BODY += """
<div class="page" id="page-settings">
<h2>界面皮肤</h2>
<div class="skinbar"><em>选择皮肤（自动保存到服务端 + 本机浏览器）</em>
<button class="skinbtn" data-k="a" onclick="setSkin('a')">A · Aero 玻璃</button>
<button class="skinbtn" data-k="b" onclick="setSkin('b')">B · Fluent 云母</button>
<button class="skinbtn" data-k="c" onclick="setSkin('c')">C · 金属玻璃</button>
<button class="skinbtn" data-k="d" onclick="setSkin('d')">D · 简洁双栏</button>
</div>
<h2>全局设置</h2>
<form id="gf" onsubmit="saveGlobals();return false">
<label>监听地址<input id="g_listen"></label>
<label>调度策略<select id="g_strategy"><option value="priority">priority</option>
<option value="round_robin">round_robin</option><option value="weighted">weighted</option></select></label>
<label>冷却秒数<input id="g_cd" type="number"></label>
<label>最大并发（0=不限）<input id="g_maxc" type="number" value="0"></label>
<label>排队等待(秒)<input id="g_qw" type="number" value="30"></label>
<label>单提供方并发<input id="g_ppc" type="number" value="0"></label>
<label>计费币种<select id="g_cur"></select></label>
<label class="wide">定价（别名=每百万prompt/每百万completion，每行一条，* 为默认）
<textarea id="g_pricing" rows="4"></textarea></label>
<div class="btns wide"><button type="submit">保存并热加载</button>
<button type="button" onclick="loadSettings()">重新读取</button>
<span>配置文件：<code id="g_path">-</code></span></div>
</form>
<h2>扩展程序</h2>
<p class="extnote">SMS 的可选外部能力：关掉不影响主链路，开启后按需接管。</p>
<h3>导入扩展程序</h3>
<p class="extnote">扩展程序＝自带面板的目录（内含 <code>asset/</code> 文件夹）；
<code>asset/SMSocket.identity</code> 里的识别码与本机一致才算验证通过，通过后其自带面板
自动挂到下方「扩展程序条目」。本机识别码：<code id="x_code">-</code></p>
<div class="btns wide">
<label class="wide" style="flex:1 1 340px">扩展程序路径<input id="x_path"
 placeholder="C:/UserSpace/EthanYan/Developin/smsc"></label>
<button type="button" onclick="importExt()">导入扩展程序</button>
<button type="button" onclick="pairExt()">配对并导入（写入识别码）</button>
<button type="button" onclick="loadExts()">重新扫描</button>
<span id="x_note"></span>
</div>
<h3>SMSC 网络平面（智能分流）</h3>
<p class="extnote">SMSC = 本机 smsc 控制面（原「Clash 设置」）；按大陆/香港可达性决定直连或代理。</p>
<form id="cf" onsubmit="saveClash();return false">
<label>启用<select id="c_on"><option value="0">关</option><option value="1">开</option></select></label>
<label>控制器 URL<input id="c_ctl" placeholder="http://127.0.0.1:9090"></label>
<label>控制器密钥<input id="c_secret" type="password" placeholder="留空＝保持原值"></label>
<label>混合端口<input id="c_port" type="number" value="7890"></label>
<label>分流模式<select id="c_mode"><option value="auto">auto · 按实测选路</option>
<option value="direct">direct · 只直连</option><option value="proxy">proxy · 只走代理</option></select></label>
<label>探测间隔(秒)<input id="c_int" type="number" value="60"></label>
<label>健康检查 URL<input id="c_health" placeholder="https://www.google.com/generate_204"></label>
<label>智能分流<select id="c_smart"><option value="1">开</option><option value="0">关</option></select></label>
<label>失败率阈值<input id="c_fail" type="number" step="0.05" value="0.5"></label>
<label>最小延迟差(ms)<input id="c_min" type="number" value="0"></label>
<label class="wide">代理分组（逗号分隔）<input id="c_groups" placeholder="PROXY,AI"></label>
<label class="wide">固定出口（每行 provider=egress）<textarea id="c_pp" rows="3"></textarea></label>
<div class="btns wide"><button type="submit">保存并重建出口平面</button>
<button type="button" onclick="loadClash()">重新读取</button>
<button type="button" onclick="openSmsc()">打开 SMSC</button><span id="c_note"></span></div></form>
<h3>扩展程序条目</h3>
<div id="x_list"><p class="extnote">尚无已导入的扩展程序</p></div>
<h2>自动调权（权重与优先级由实测推导，无需手填）</h2>
<form id="tf" onsubmit="saveTune();return false">
<label>启用<select id="t_on"><option value="1">开</option><option value="0">关</option></select></label>
<label>统计窗口(秒)<input id="t_win" type="number" value="86400"></label>
<label>最少样本<input id="t_min" type="number" value="3"></label>
<label>重算间隔(秒)<input id="t_int" type="number" value="600"></label>
<label>写回配置文件<select id="t_persist"><option value="0">否 · 仅运行时生效</option>
<option value="1">是</option></select></label>
<label>优先级跨度<input id="t_pspread" type="number" value="10"></label>
<label>权重跨度<input id="t_wspread" type="number" value="10"></label>
<div class="btns wide"><button type="submit">保存调权设置</button>
<button type="button" onclick="applyTune()">立即重算</button>
<button type="button" onclick="loadTune()">重新读取</button><span id="t_note"></span></div></form>
<h2>当前实测排名</h2><table id="tunet"></table>
<h2>计费货币与汇率</h2>
<form id="bf" onsubmit="saveBilling();return false">
<label>显示币种<select id="b_cur"></select></label>
<label>汇率基准<select id="b_base"></select></label>
<label>小数位<input id="b_prec" type="number" value="6"></label>
<label>汇率源URL<input id="b_url" placeholder="https://...（留空=手工汇率）"></label>
<label class="wide">汇率表（币种=每1基准的数量，每行一条）
<textarea id="b_rates" rows="6" placeholder="USD=1"></textarea></label>
<div class="btns wide"><button type="submit">保存计费设置</button>
<button type="button" onclick="refreshRates()">拉取在线汇率</button>
<button type="button" onclick="loadBilling()">重新读取</button>
<span id="b_note"></span></div></form>
<h2>换算试算</h2>
<div class="btns">
<input id="c_amt" type="number" value="1" size="6">
<select id="c_from"></select><em>→</em><select id="c_to"></select>
<button onclick="convertTry()">试算</button><b id="c_out">-</b></div>
<h2>客户端接入信息</h2>
<pre id="hint">-</pre>
<div class="cards">
<div class="card"><b id="modelcount">-</b><i>已索引模型</i></div>
<div class="card"><b id="g_slots">-</b><i>密钥槽位</i></div>
</div>
<h2>可用模型别名</h2><pre id="g_models">-</pre>
</div>
<div class="page" id="page-socket">
<div class="cards">
<div class="card"><b id="s_active">-</b><i>在途请求</i></div>
<div class="card"><b id="s_queued">-</b><i>排队中</i></div>
<div class="card"><b id="s_peak">-</b><i>历史峰值</i></div>
<div class="card"><b id="s_total">-</b><i>累计请求</i></div>
<div class="card"><b id="s_rej">-</b><i>拒绝(429)</i></div>
<div class="card"><b id="s_rps">-</b><i>每秒请求</i></div>
<div class="card"><b id="s_wait">-</b><i>平均排队ms</i></div>
<div class="card"><b id="s_err">-</b><i>失败数</i></div>
</div>
<h2>并发闸门</h2>
<div class="cred"><em>上限</em><code id="s_max">-</code><em>单提供方</em><code id="s_ppc">-</code>
<em>排队等待</em><code id="s_qw">-</code><em>状态</em><code id="s_sat">-</code>
<button onclick="loadSocket()">刷新</button>
<button id="s_auto" onclick="toggleAuto()">自动刷新：关</button></div>
<h2>按提供方并发</h2><table id="sprov"></table>
<div id="stackblk" class="hide">
<h2>压栈调度</h2>
<div class="cred"><em>策略</em><code id="k_policy">-</code><em>深度</em><code id="k_depth">-</code>
<em>峰值</em><code id="k_peak">-</code><em>唤醒</em><code id="k_popped">-</code>
<em>超时</em><code id="k_expired">-</code><em>平均滞留</em><code id="k_avg">-</code>
<em>最长滞留</em><code id="k_pmax">-</code></div>
<table id="kentries"></table></div>
<div id="reachblk" class="hide">
<h2>模型通达性</h2>
<div class="cred"><em>窗口</em><code id="r_window">-</code><em>下次探测</em><code id="r_next">-</code>
<em>样本</em><code id="r_rows">-</code>
<button onclick="loadReach()">刷新</button>
<button onclick="runReach()">立即探测</button></div>
  <table id="reach" class="reach"></table>
  <div class="muted" style="overflow-x:auto;white-space:normal;word-break:break-all">绿=健康 · 黄=慢 · 红=不通 · 蓝=抖动；行=模型，列=出口路径</div></div>
<h2>并行批量发送</h2>
<form id="bf2" onsubmit="runBatch();return false">
<label>并发数<input id="q_conc" type="number" value="8"></label>
<label>模式<select id="q_mode"><option value="sync">同步等待</option>
<option value="async">异步作业（可轮询）</option></select></label>
<label>失败即停<select id="q_ff"><option value="0">否</option><option value="1">是</option></select></label>
<label class="wide">请求列表（每行一个提示词，或一行一个 JSON 对象）
<textarea id="q_body" rows="5">你好
介绍一下你自己</textarea></label>
<div class="btns wide"><button type="submit">发送</button>
<button type="button" onclick="pollJob()">轮询作业</button>
<span id="q_note"></span></div></form>
<h2>批量结果</h2><table id="qres"></table>
<h2>作业列表</h2><table id="qjobs"></table>
</div>
</main>
<div class="netglyph off" id="net_glyph" title="net plane" onclick="netPanel(true)">⌁ net</div>
<div class="netmask" id="net_mask" onclick="netPanel(false)"></div>
<div class="netpanel" id="net_panel">
<h2>网络出口（内部）</h2>
<div class="netrow"><button onclick="netRefresh()">状态</button>
<button onclick="netProbe()">探测</button><button onclick="netMode('auto')">auto</button>
<button onclick="netMode('direct')">direct</button><button onclick="netMode('proxy')">proxy</button>
<button onclick="netFlush()">flush dns</button><span id="net_note" class="muted"></span></div>
<div class="netrow"><em>分组</em><input id="net_g" placeholder="PROXY-GRP">
<em>节点</em><input id="net_n" placeholder="node"><button onclick="netSelect()">固定</button></div>
<div class="netrow"><em>预览出口</em><input id="net_p" placeholder="provider">
<button onclick="netEgress()">查看</button></div>
<div id="net_body" class="muted">点“状态”读取 /internal/net/state</div>
</div>
"""
PAGE = ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>SMSocket 控制台</title><style>" + CSS + "</style></head>"
        + BODY + "<script>" + JS + "</script></body></html>")
