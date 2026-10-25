const D=JSON.parse(document.getElementById('cd').textContent);
const tip=document.getElementById('tip'),won=n=>Math.round(n).toLocaleString('ko-KR')+'원',NS='http://www.w3.org/2000/svg';
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p&&p.appendChild(e);return e}
function hover(e,h){e.addEventListener('mousemove',v=>{tip.innerHTML=h;tip.style.opacity=1;tip.style.left=v.clientX+12+'px';tip.style.top=v.clientY+12+'px'});e.addEventListener('mouseleave',()=>tip.style.opacity=0)}
(function(){const s=document.getElementById('bars'),d=D.bars,mx=Math.max(...d.map(x=>x[1]))||1,mc=Math.max(...d.map(x=>x[2]))||1;
 d.forEach(([n,sv,co],i)=>{const y=6+i*46;el('text',{x:0,y:y+19},s).textContent=n;
  const w1=Math.max(3,sv/mx*300),w2=Math.max(3,co/mc*300);
  const a=el('rect',{x:110,y,width:w1,height:16,fill:'var(--s1)',rx:3},s);hover(a,`${n} 월 절감 ${won(sv)}`);
  el('text',{x:116+w1,y:y+12,style:'fill:var(--text)'},s).textContent=won(sv);
  const b=el('rect',{x:110,y:y+19,width:w2,height:16,fill:'var(--s2)',rx:3},s);hover(b,`${n} 탄소 감축 ${co.toLocaleString()} kg CO₂e`);
  el('text',{x:116+w2,y:y+31,style:'fill:var(--text2)'},s).textContent=co.toLocaleString()+' kg'});
})();
// 금액 표기: 1억 이상 'n.n억', 그 아래 'n,nnn만' (끝자리 0 은 뺌)
const krw=v=>v>=1e8?(+(v/1e8).toFixed(1)).toLocaleString('ko-KR')+'억':Math.round(v/1e4).toLocaleString('ko-KR')+'만';
// 눈금: 5칸 안팎이 되게 1·2·2.5·5 × 10^n 간격, 위쪽은 간격 단위로 올림
function niceAxis(max){const raw=max/5||1,p=10**Math.floor(Math.log10(raw)),step=[1,2,2.5,5,10].map(k=>k*p).find(v=>v>=raw);
 return {step,top:Math.ceil(max*1.05/step)*step}}
(function(){const s=document.getElementById('trend'),d=D.trend,ax=niceAxis(Math.max(...d.map(x=>x[1]))),mx=ax.top||1,H=160,T=20,x0=70,W=700,X=i=>x0+(d.length==1?W/2:i*W/(d.length-1)),Y=v=>T+H-v/mx*H;
 for(let v=0;v<=mx+1;v+=ax.step){el('line',{x1:x0,x2:x0+W,y1:Y(v),y2:Y(v),stroke:'var(--grid)'},s);el('text',{x:x0-8,y:Y(v)+4,'text-anchor':'end'},s).textContent=v?krw(v):'0'}
 if(d.length>1)el('polyline',{points:d.map((p,i)=>`${X(i)},${Y(p[1])}`).join(' '),fill:'none',stroke:'var(--s1)','stroke-width':2},s);
 d.forEach((p,i)=>{const c=el('circle',{cx:X(i),cy:Y(p[1]),r:6,fill:'var(--s1)',stroke:'var(--surface)','stroke-width':2},s);hover(c,`${p[0]} 절감 ${won(p[1])} · 탄소 ${Math.round(p[2]).toLocaleString()} kg`);
  el('text',{x:X(i),y:Y(p[1])-12,'text-anchor':'middle',style:'fill:var(--text);font-weight:600'},s).textContent=krw(p[1]);
  el('text',{x:X(i),y:T+H+18,'text-anchor':'middle'},s).textContent=p[0]});
})();
