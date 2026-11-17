const $=s=>document.querySelector(s);let current=null,result=null,channel='proof';
let cmpState=null;
const fields=[['width_mm','纸宽mm',10,300,.1],['height_mm','纸高mm',10,300,.1],['dpi','输出DPI',36,300,1],['lpi','网线LPI',3,80,.5],['black','黑版替代',0,1,.01],['ink_limit','总墨量上限',1,4,.1],['gamma','整体色阶',.2,4,.05],['saturation','色彩饱和',0,2,.05],['gain','网点扩大',-.3,.5,.01],['proof_density','打样墨密度',.2,2.5,.05],['paper','纸色','color'],['fit','图像适配',['contain','cover','stretch']],['screened','使用网点','checkbox']];
async function api(path,options={}){const r=await fetch('/press'+path,options);if(!r.ok){let e=await r.json();throw Error(e.error||r.status)}return r.json()}
const data=(method,obj)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
function toast(text){$('#toast').textContent=text;$('#toast').style.display='block';setTimeout(()=>$('#toast').style.display='none',4000)}
async function safe(fn){try{await fn()}catch(e){toast(e.message);console.error(e)}}
async function listing(){let ps=await api('/projects');$('#list').replaceChildren();for(let p of ps){let b=document.createElement('button');b.textContent=p.name;b.onclick=()=>safe(()=>selectProject(p.id));$('#list').append(b)}}
function form(){const root=$('#fields');root.replaceChildren();for(let [key,title,a,b,step] of fields){let l=document.createElement('label');l.textContent=title;let e;if(Array.isArray(a)){e=document.createElement('select');for(let v of a){let o=document.createElement('option');o.value=v;o.textContent=v;e.append(o)}}else{e=document.createElement('input');e.type=typeof a==='string'?a:'number';if(typeof a==='number'){e.min=a;e.max=b;e.step=step}}e.name=key;if(a==='checkbox')e.checked=current.settings[key];else e.value=current.settings[key];l.append(e);root.append(l)}$('#inks').replaceChildren();for(let i=0;i<4;i++){let row=document.createElement('div');row.className='inkrow';let l=document.createElement('label');let cb=document.createElement('input');cb.type='checkbox';cb.name='enabled-'+i;cb.checked=current.settings.enabled[i];l.append(cb,document.createTextNode('CMYK'[i]));row.append(l);for(let key of ['angles','phase_x','phase_y','offset_x','offset_y']){let e=document.createElement('input');e.type='number';e.name=key+'-'+i;e.value=current.settings[key][i];e.step=key==='angles'?1:.05;e.setAttribute('aria-label','CMYK'[i]+' '+key);row.append(e)}$('#inks').append(row)}}
function display(){if(!current)return;$('#picture').hidden=!result;$('#status').textContent=result?`${result.report.size.join(' × ')} / REV ${result.revision} / ${channel}`:`REV ${current.revision} · 设置已变更，请生成`;if(result)$('#picture').src=`/press/projects/${current.id}/result/${channel}.png?result=${result.id}`;$('#reportbox').hidden=true}
async function selectProject(id){current=await api('/projects/'+id);result=current.results.find(r=>r.id===current.current_result)||null;cmpState=null;$('#cmpView').hidden=true;$('#cmpReadout').hidden=true;$('#cmpDownload').disabled=true;clearCrosshairs();$('#title').textContent=current.name;$('#meta').textContent='物理页面 '+current.settings.width_mm+' × '+current.settings.height_mm+' mm / REV '+current.revision;$('#name').value=current.name;$('#empty').hidden=true;$('#editor').hidden=false;form();display();$('#versions').replaceChildren();for(let v of current.versions){let b=document.createElement('button');b.textContent='↶ '+v.label;b.onclick=()=>safe(async()=>{await api(`/projects/${id}/versions/${v.id}/restore`,data('POST',{revision:current.revision}));await selectProject(id)});$('#versions').append(b)}$('#results').replaceChildren();for(let r of [...current.results].reverse()){let b=document.createElement('button');b.textContent='REV '+r.revision+' / '+r.report.size.join('×');b.onclick=()=>{result=r;display()};$('#results').append(b)}fillCompareSelectors();await listing()}
function resultLabel(r){let size=r.report?r.report.size:r.size_px;return `REV ${r.revision} · ${size.join('×')}px · ${r.settings.dpi}dpi · ${r.settings.width_mm}×${r.settings.height_mm}mm · ${r.id.slice(0,8)}`}
function fillCompareSelectors(){let keep={a:$('#cmpA').value,b:$('#cmpB').value};for(let [sel,val] of [['#cmpA',keep.a],['#cmpB',keep.b]]){let e=$(sel);e.replaceChildren();let blank=document.createElement('option');blank.value='';blank.textContent='— 选择固定结果 —';e.append(blank);for(let r of current.results){let o=document.createElement('option');o.value=r.id;o.textContent=resultLabel(r);e.append(o)}e.value=current.results.some(r=>r.id===val)?val:''}$('#cmpDownload').disabled=true}
function fmt(v){return typeof v==='number'?v.toFixed(4):JSON.stringify(v)}
function channelName(c){let map={proof:'纸墨软打样',source:'纸上源图',continuous:'连续调分色预览',C:'C 青版',M:'M 品版',Y:'Y 黄版',K:'K 黑版','C-tone':'C 连续调','M-tone':'M 连续调','Y-tone':'Y 连续调','K-tone':'K 连续调'};return map[c]||c}
$('#cmpRun').onclick=()=>safe(runCompare);
async function runCompare(){
  let a=$('#cmpA').value,b=$('#cmpB').value,ch=$('#cmpChannel').value;
  if(!current||!a||!b){toast('请先选择两份固定结果');return}
  if(a===b){toast('请选择两份不同的结果');return}
  let d=await api(`/projects/${current.id}/compare?a=${a}&b=${b}`);
  cmpState={d,a,b,ch};
  $('#cmpView').hidden=false;$('#cmpReadout').hidden=true;
  let q=`a=${a}&b=${b}`,base=`/press/projects/${current.id}/compare`;
  $('#cmpImgA').src=`${base}/grid/${ch}.png?${q}&side=a`;
  $('#cmpImgB').src=`${base}/grid/${ch}.png?${q}&side=b`;
  $('#cmpImgD').src=`${base}/diff/${ch}.png?${q}`;
  $('#cmpPageA').src=`/press/projects/${current.id}/result/proof.png?result=${a}`;
  $('#cmpPageB').src=`/press/projects/${current.id}/result/proof.png?result=${b}`;
  $('#cmpOutA').src=`${base}/outside/a.png?${q}`;
  $('#cmpOutB').src=`${base}/outside/b.png?${q}`;
  $('#capA').textContent=`A · ${resultLabel(d.a)}`;
  $('#capB').textContent=`B · ${resultLabel(d.b)}`;
  $('#capD').textContent=`差异 · ${channelName(ch)} · 共同域同网格逐单元绝对差`;
  $('#pageCapA').textContent=`A 全页（红=超出共同覆盖域，不参与比较）`;
  $('#pageCapB').textContent=`B 全页（红=超出共同覆盖域，不参与比较）`;
  $('#cmpMeta').innerHTML=`对照锁定：A <b>${d.a.id}</b>（修订 ${d.a.revision}） 与 B <b>${d.b.id}</b>（修订 ${d.b.revision}）。两幅图的 URL 均带固定 result 参数；之后继续编辑或重新生成当前工程，都不会更换这两份结果图像。`;
  $('#cmpSampling').innerHTML=`采样尺度：${d.sampling.description}<br>共同覆盖域 mm：[${d.common_mm.join(', ')}]；对照网格 ${d.grid.size.join(' × ')} 单元、间距 ${d.grid.pitch_mm[0].toFixed(4)} mm（A 像素间距 ${d.sampling.pixel_pitch_mm.a.toFixed(4)} mm / B ${d.sampling.pixel_pitch_mm.b.toFixed(4)} mm，取较粗者）。`;
  let out=[];
  for(let side of ['a','b']){let rects=d.outside_mm[side];out.push(rects.length?`结果${side.toUpperCase()} 有 ${rects.length} 块超出共同域、未参与比较（mm）：${rects.map(r=>'['+r.map(n=>n.toFixed(2)).join(', ')+']').join('；')}`:`结果${side.toUpperCase()} 页面完全落在共同覆盖域内，无超出区域。`)}
  $('#cmpOutside').textContent=out.join(' ');
  let st=d.channels[ch],rows=[['平均绝对差',st.mean_abs],['均方根差 RMSE',st.rmse],['最大绝对差',st.max_abs],['完全相同单元占比',st.identical_fraction]];
  let html=`<table><tr><th>指标（${channelName(ch)}，仅共同域 ${st.cell_count} 个网格单元）</th><th>数值（0..1）</th></tr>`;
  for(let [k,v] of rows)html+=`<tr><td>${k}</td><td class="${k.includes('相同')&&v===1?'diffzero':''}">${fmt(v)}</td></tr>`;
  if(st.per_channel)for(let i=0;i<st.per_channel.length;i++){let c=st.per_channel[i];html+=`<tr><td>RGB 通道 ${'RGB'[i]} 平均/RMSE/最大</td><td>${fmt(c.mean_abs)} / ${fmt(c.rmse)} / ${fmt(c.max_abs)}</td></tr>`}
  html+='</table>';$('#cmpStats').innerHTML=html;
  let sd=d.settings_diff;
  $('#cmpSettings').innerHTML=`<table><tr><th>参数</th><th>A（修订 ${d.a.revision}）</th><th>B（修订 ${d.b.revision}）</th></tr>`+sd.map(r=>`<tr><td>${r.key}</td><td>${fmt(r.a)}</td><td>${fmt(r.b)}</td></tr>`).join('')+'</table>';
  $('#cmpDownload').disabled=false;
  for(let img of document.querySelectorAll('#cmpStage img')){img.onload=applyRulers}
  applyRulers();clearCrosshairs();
}
$('#cmpChannel').onchange=()=>{if(cmpState){cmpState.ch=$('#cmpChannel').value;safe(runCompare)}};
function applyRulers(){if(!cmpState)return;let common=cmpState.d.common_mm,wMm=common[2]-common[0],hMm=common[3]-common[1];for(let w of document.querySelectorAll('#cmpStage .cmp-imgwrap')){let img=w.querySelector('img');if(!img.clientWidth)continue;w.style.setProperty('--rx',(10/wMm*img.clientWidth)+'px');w.style.setProperty('--ry',(10/hMm*img.clientHeight)+'px')}}
function clearCrosshairs(){for(let c of document.querySelectorAll('#cmpStage .cross'))c.hidden=true}
for(let wrap of document.querySelectorAll('#cmpStage .cmp-imgwrap')){
  wrap.addEventListener('click',e=>safe(async()=>{
    if(!cmpState)return;
    let img=wrap.querySelector('img');
    let rect=img.getBoundingClientRect();
    let fx=Math.min(1,Math.max(0,(e.clientX-rect.left)/rect.width));
    let fy=Math.min(1,Math.max(0,(e.clientY-rect.top)/rect.height));
    let c=cmpState.d.common_mm,x=c[0]+fx*(c[2]-c[0]),y=c[1]+fy*(c[3]-c[1]);
    for(let w2 of document.querySelectorAll('#cmpStage .cmp-imgwrap')){
      let cross=w2.querySelector('.cross'),iv=w2.querySelector('i'),bv=w2.querySelector('b'),em=w2.querySelector('em');
      cross.hidden=false;iv.style.left=(fx*100)+'%';bv.style.top=(fy*100)+'%';
      em.textContent=`${x.toFixed(3)}, ${y.toFixed(3)} mm`;
    }
    let p=await api(`/projects/${current.id}/compare/probe?a=${cmpState.a}&b=${cmpState.b}&x=${x}&y=${y}`);
    let ch=cmpState.ch;
    let va=p.sides.a.values[ch],vb=p.sides.b.values[ch];
    let row=(name,v,px)=>`<tr><td>${name}</td><td>${Array.isArray(v.grid)?v.grid.map(n=>n.toFixed(3)).join(', '):fmt(v.grid)}</td><td>${Array.isArray(v.native_pixel)?v.native_pixel.map(n=>n.toFixed(3)).join(', '):fmt(v.native_pixel)}</td><td>(${px[0]}, ${px[1]})</td></tr>`;
    $('#cmpReadout').hidden=false;
    $('#cmpReadout').innerHTML=`点位 <b>(${x.toFixed(3)}, ${y.toFixed(3)}) mm</b> · 网格单元 mm [${p.grid_cell_mm.map(n=>n.toFixed(3)).join(', ')}] · 单元编号 (${p.grid_cell_xy.join(', ')}) · 通道 ${channelName(ch)}<table><tr><th>侧 / 固定结果</th><th>同网格重采样值</th><th>该点原像素值</th><th>原像素列,行</th></tr>${row('A '+p.sides.a.id.slice(0,8),va,va.native_pixel_xy)}${row('B '+p.sides.b.id.slice(0,8),vb,vb.native_pixel_xy)}</table><p style="margin:6px 0 0">单版/连续调为墨量 0..1（黑=墨）；RGB 通道为 sRGB 0..1 三分量。</p>`;
  }));
}
$('#cmpDownload').onclick=()=>{if(cmpState)location.href=`/press/projects/${current.id}/compare.zip?a=${cmpState.a}&b=${cmpState.b}`};
$('#new').onclick=()=>safe(async()=>{let p=await api('/projects',data('POST',{name:'新印刷作业'}));await selectProject(p.id)});
$('#demo').onclick=$('#start').onclick=()=>safe(async()=>{let p=await api('/demo',data('POST',{}));await selectProject(p.id)});
$('#settings').onsubmit=e=>{e.preventDefault();safe(async()=>{let s={...current.settings};for(let [key,title,a] of fields){let el=$(`[name="${key}"]`);s[key]=a==='checkbox'?el.checked:typeof a==='number'?Number(el.value):el.value}for(let key of ['angles','phase_x','phase_y','offset_x','offset_y'])s[key]=[0,1,2,3].map(i=>Number($(`[name="${key}-${i}"]`).value));s.enabled=[0,1,2,3].map(i=>$(`[name="enabled-${i}"]`).checked);await api('/projects/'+current.id,data('PATCH',{revision:current.revision,name:$('#name').value,settings:s}));await selectProject(current.id);toast('设置已保存，需重新生成印版')})};
$('#render').onclick=()=>safe(async()=>{if(!current)return;$('#status').textContent='生成中…';result=await api(`/projects/${current.id}/render`,data('POST',{revision:current.revision}));await selectProject(current.id)});
$('#channel').onchange=e=>{channel=e.target.value;display()};
$('#report').onclick=()=>{if(!result)return;$('#reportbox').textContent=JSON.stringify(result.report,null,2);$('#reportbox').hidden=false};
$('#measure').onclick=()=>safe(async()=>{let r=await api(`/projects/${current.id}/measure`,data('POST',{revision:current.revision,rect:['left','top','right','bottom'].map(k=>Number($('#'+k).value))}));$('#measurement').textContent=JSON.stringify(r,null,2)});
$('#version').onclick=()=>safe(async()=>{if(!current)return;await api(`/projects/${current.id}/versions`,data('POST',{revision:current.revision,label:$('#versionname').value||'印版方案'}));await selectProject(current.id);toast('版本已保存')});
$('#upload').onclick=()=>$('#sourcefile').click();$('#sourcefile').onchange=()=>safe(async()=>{let f=$('#sourcefile').files[0];if(!f||!current)return;let d=new FormData();d.append('revision',current.revision);d.append('file',f);await api(`/projects/${current.id}/source`,{method:'POST',body:d});await selectProject(current.id);$('#sourcefile').value=''});
$('#archive').onclick=()=>{if(current)location.href=`/press/projects/${current.id}/project.zip`};$('#download').onclick=()=>{if(current&&result)location.href=`/press/projects/${current.id}/plates.zip?result=${result.id}`;else toast('请先生成印版')};
$('#import').onclick=()=>$('#projectfile').click();$('#projectfile').onchange=()=>safe(async()=>{let f=$('#projectfile').files[0];if(!f)return;let d=new FormData();d.append('file',f);let p=await api('/import',{method:'POST',body:d});await selectProject(p.id);$('#projectfile').value=''});safe(listing);
