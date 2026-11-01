const fmt=n=>n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);
const LV={yellow_at:50,red_at:90};  // colour thresholds, loaded from Settings
const col=p=>p>=LV.red_at?'var(--bad)':p>=LV.yellow_at?'var(--yel)':'var(--ok)';
fetch('/api/settings').then(r=>r.json()).then(c=>{for(const k in LV)if(c[k])LV[k]=c[k]}).catch(()=>{});
const rs=iso=>{if(!iso)return'—';const m=Math.max(0,Math.round((new Date(iso)-new Date())/60000));return 'in '+(m>=1440?Math.floor(m/1440)+'d '+Math.floor(m%1440/60)+'h':Math.floor(m/60)+'h '+m%60+'m')};
const act=n=>fetch('/api/action',{method:'POST',body:JSON.stringify({name:n})});
function ring(p,size,w,label){
  const r=(size-w)/2,c=2*Math.PI*r,v=Math.min(100,Math.max(0,p)),k=col(v);
  return `<svg width="${size}" height="${size}"><circle class="trk" cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke-width="${w}"/><circle class="val" cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke-width="${w}" stroke-linecap="round" stroke="${k}" style="color:${k}" stroke-dasharray="${c}" stroke-dashoffset="${c*(1-v/100)}"/></svg><div class="txt"><b style="font-size:${size*.24}px">${v.toFixed(0)}<span style="font-size:.5em">%</span></b><small>${label||''}</small></div>`;
}
function hourChart(h,height){
  const mx=Math.max(1,...h),pk=mx>1?h.indexOf(mx):-1,lab=i=>(i%12||12)+(i<12?'a':'p');
  const bars=h.map((v,i)=>`<div title="${lab(i)}: ${fmt(v)}" style="flex:1;display:flex;align-items:flex-end;height:100%"><div style="width:100%;height:${Math.max(3,v/mx*100)}%;border-radius:2px;background:${i===pk?'linear-gradient(180deg,#ffb08f,var(--accent))':'var(--track)'};${i===pk?'box-shadow:0 0 10px rgba(217,119,87,.6)':''}"></div></div>`).join('');
  const axis=[0,6,12,18].map(i=>`<span style="position:absolute;left:${i/24*100}%">${lab(i)}</span>`).join('');
  return `<div style="display:flex;gap:2px;height:${height}px">${bars}</div><div style="position:relative;height:14px;margin-top:4px;font-size:10px;color:var(--mute)">${axis}</div><div class="mute" style="font-size:11px;margin-top:2px">${pk<0?'No usage yet':'Busiest: '+lab(pk).replace('a',' AM').replace('p',' PM')+' · '+fmt(mx)+' tokens'}</div>`;
}
function seg(p,n,color,h){  // segmented vertical-bar meter, like a level gauge
  const on=Math.round(Math.min(100,p)/100*n);
  return `<div style="display:flex;gap:2px;height:${h}px;align-items:flex-end">`+Array.from({length:n},(_,i)=>`<div style="flex:1;height:${i<on?100:14}%;border-radius:2px;background:${i<on?color:'var(--track)'};${i<on?`box-shadow:0 0 6px ${color}`:''}"></div>`).join('')+'</div>';
}
function spark(vals,w,h,color){  // area line chart
  const mx=Math.max(1,...vals),st=w/(vals.length-1),pts=vals.map((v,i)=>[i*st,h-4-(v/mx)*(h-10)]);
  const line=pts.map(p=>p.join(',')).join(' ');
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none"><defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs><polygon points="0,${h} ${line} ${w},${h}" fill="url(#sg)"/><polyline points="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/></svg>`;
}

// "Sessions" tabs show how many Claude sessions are running, with a pulsing dot while one is working
async function sessionBadge(){
  const tabs=document.querySelectorAll('[data-sessions]');if(!tabs.length)return;
  try{
    const s=await fetch('/api/terms',{headers:{'X-Claude-Usage':'1'}}).then(r=>r.json());
    const n=s.running.length,busy=s.running.some(r=>r.status==='busy');
    tabs.forEach(a=>a.innerHTML='Sessions'+(n?` <span class="cnt${busy?' busy':''}" title="${n} Claude session${n>1?'s':''}${busy?', one working':''}">${n}</span>`:''));
  }catch(e){}
}
addEventListener('DOMContentLoaded',()=>{sessionBadge();setInterval(sessionBadge,10000)});
