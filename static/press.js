const $=s=>document.querySelector(s);let current=null,result=null,channel='proof';
const fields=[['width_mm','纸宽mm',10,300,.1],['height_mm','纸高mm',10,300,.1],['dpi','输出DPI',36,300,1],['lpi','网线LPI',3,80,.5],['black','黑版替代',0,1,.01],['ink_limit','总墨量上限',1,4,.1],['gamma','整体色阶',.2,4,.05],['saturation','色彩饱和',0,2,.05],['gain','网点扩大',-.3,.5,.01],['proof_density','打样墨密度',.2,2.5,.05],['paper','纸色','color'],['fit','图像适配',['contain','cover','stretch']],['screened','使用网点','checkbox']];
async function api(path,options={}){const r=await fetch('/press'+path,options);if(!r.ok){let e=await r.json();throw Error(e.error||r.status)}return r.json()}
const data=(method,obj)=>({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(obj)});
function toast(text){$('#toast').textContent=text;$('#toast').style.display='block';setTimeout(()=>$('#toast').style.display='none',4000)}
async function safe(fn){try{await fn()}catch(e){toast(e.message);console.error(e)}}
async function listing(){let ps=await api('/projects');$('#list').replaceChildren();for(let p of ps){let b=document.createElement('button');b.textContent=p.name;b.onclick=()=>safe(()=>selectProject(p.id));$('#list').append(b)}}
function form(){const root=$('#fields');root.replaceChildren();for(let [key,title,a,b,step] of fields){let l=document.createElement('label');l.textContent=title;let e;if(Array.isArray(a)){e=document.createElement('select');for(let v of a){let o=document.createElement('option');o.value=v;o.textContent=v;e.append(o)}}else{e=document.createElement('input');e.type=typeof a==='string'?a:'number';if(typeof a==='number'){e.min=a;e.max=b;e.step=step}}e.name=key;if(a==='checkbox')e.checked=current.settings[key];else e.value=current.settings[key];l.append(e);root.append(l)}$('#inks').replaceChildren();for(let i=0;i<4;i++){let row=document.createElement('div');row.className='inkrow';let l=document.createElement('label');let cb=document.createElement('input');cb.type='checkbox';cb.name='enabled-'+i;cb.checked=current.settings.enabled[i];l.append(cb,document.createTextNode('CMYK'[i]));row.append(l);for(let key of ['angles','phase_x','phase_y','offset_x','offset_y']){let e=document.createElement('input');e.type='number';e.name=key+'-'+i;e.value=current.settings[key][i];e.step=key==='angles'?1:.05;e.setAttribute('aria-label','CMYK'[i]+' '+key);row.append(e)}$('#inks').append(row)}}
function display(){if(!current)return;$('#picture').hidden=!result;$('#status').textContent=result?`${result.report.size.join(' × ')} / REV ${result.revision} / ${channel}`:`REV ${current.revision} · 设置已变更，请生成`;if(result)$('#picture').src=`/press/projects/${current.id}/result/${channel}.png?result=${result.id}`;$('#reportbox').hidden=true}
async function selectProject(id){current=await api('/projects/'+id);result=current.results.find(r=>r.id===current.current_result)||null;$('#title').textContent=current.name;$('#meta').textContent='物理页面 '+current.settings.width_mm+' × '+current.settings.height_mm+' mm / REV '+current.revision;$('#name').value=current.name;$('#empty').hidden=true;$('#editor').hidden=false;form();display();$('#versions').replaceChildren();for(let v of current.versions){let b=document.createElement('button');b.textContent='↶ '+v.label;b.onclick=()=>safe(async()=>{await api(`/projects/${id}/versions/${v.id}/restore`,data('POST',{revision:current.revision}));await selectProject(id)});$('#versions').append(b)}$('#results').replaceChildren();for(let r of [...current.results].reverse()){let b=document.createElement('button');b.textContent='REV '+r.revision+' / '+r.report.size.join('×');b.onclick=()=>{result=r;display()};$('#results').append(b)}await listing()}
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
$('#import').onclick=()=>$('#projectfile').click();$('#projectfile').onchange=()=>safe(async()=>{let f=$('#projectfile').files[0];if(!f)return;let d=new FormData();d.append('file',f);let p=await api('/import',{method:'POST',body:d});await selectProject(p.id);$('#projectfile').value=''});

/* ---------- 网花诊断：真实印版频谱 + 采样混叠/拍频归因 ---------- */
let diag=null,diagChoice=null;
const KIND_NAME={pair_beat:'通道拍频',self_alias:'采样混叠',content:'图像内容',unexplained:'未解释'};
function diagRect(){return ['di-left','di-top','di-right','di-bottom'].map(k=>Number($('#'+k).value))}
function diagList(el,items,renderer){el.replaceChildren();if(!items.length){let p=document.createElement('p');p.className='muted';p.textContent='无';el.append(p);return}for(let item of items){let row=document.createElement('div');row.className='diag-row';renderer(row,item);el.append(row)}}
$('#opendiagnose').onclick=()=>{if(!current)return;$('#diagnosis-panel').hidden=false;$('#diagnosis-panel').scrollIntoView({behavior:'smooth'})};
$('#diagnose').onclick=()=>safe(async()=>{
  if(!current)return;
  diag=null;diagChoice=null;$('#diag-apply').hidden=true;$('#diag-result').hidden=true;
  $('#diag-status').textContent='正在渲染当前印版并分析低频频谱…';
  let rep=await api(`/projects/${current.id}/diagnose`,data('POST',{revision:current.revision,rect:diagRect()}));
  diag=rep;renderDiagnosis(rep);
});
function renderDiagnosis(rep){
  $('#diag-status').textContent=`REV ${rep.revision} · 输入指纹 ${rep.input_fingerprint} · 区域 ${rep.context.pixel_rect.join('×')}px · 每网胞 ${rep.context.pixels_per_cell} 像素`;
  $('#diag-result').hidden=false;
  $('#diag-level').textContent='网花等级：'+rep.current.level;
  $('#diag-level').className='badge level-'+rep.current.level;
  $('#diag-score').textContent=`评分 ${rep.current.moire_score}/100 · 仿真低频能量 ${rep.current.sim_low_band??'—'}`;
  let reasons=$('#diag-reasons');reasons.replaceChildren();
  let note=document.createElement('p');note.className=rep.reliable?'muted':'warn';
  note.textContent=rep.reliable?'本次分析满足分辨率与信号条件，结论可靠。':'无法可靠判断：'+rep.reasons.join('；');
  reasons.append(note);
  let notes=$('#diag-notes');notes.replaceChildren();
  for(let text of rep.notes){let p=document.createElement('p');p.className='warn';p.textContent='⚠ '+text;notes.append(p)}
  diagList($('#diag-peaks'),rep.current.peaks,(row,pk)=>{
    row.append(Object.assign(document.createElement('span'),{textContent:KIND_NAME[pk.kind]||pk.kind,className:'badge'}));
    let detail=`${pk.channels?pk.channels.join('/'):''} 周期 ${pk.period_mm}mm（${pk.period_cells} 网胞）角度 ${pk.angle_deg}° 显著性 ${pk.prominence}${pk.content_related?' · 连续调中也存在（含图像内容）':''}`;
    row.append(document.createTextNode(detail));
  });
  diagList($('#diag-pairs'),rep.current.pair_contributions,(row,pr)=>{
    row.append(Object.assign(document.createElement('span'),{textContent:pr.channels.join('–'),className:'badge'}));
    row.append(document.createTextNode(`贡献 ${pr.score} · 最低拍频 ${pr.min_beat_ratio} f₀（约 ${pr.beat_period_cells} 网胞一拍）`));
  });
  diagList($('#diag-alias'),rep.current.channel_alias,(row,ch)=>{
    let observed=ch.observed_low_peaks.filter(p=>p.kind==='self_alias').length;
    row.append(Object.assign(document.createElement('span'),{textContent:ch.channel,className:'badge'}));
    row.append(document.createTextNode(`最低混叠 ${ch.min_alias_ratio??'—'} f₀ · 实测混叠峰 ${observed} 个`));
  });
  renderCandidates(rep);
  renderPreviews();
}
function renderCandidates(rep){
  let box=$('#diag-candidates');box.replaceChildren();diagChoice=null;$('#diag-apply').hidden=true;
  let currentCard=document.createElement('div');currentCard.className='candidate current';
  currentCard.innerHTML=`<strong>当前手动网角</strong><div>${rep.current.angles.map(a=>a+'°').join(' / ')}</div><div class="muted">仿真低频能量 ${rep.current.sim_low_band??'—'}</div>`;
  box.append(currentCard);
  if(!rep.candidates.length){let p=document.createElement('p');p.className='warn';p.textContent='当前条件下没有可仿真验证的替代网角（启用通道不足或已关闭网点）。';box.append(p);return}
  rep.candidates.forEach((cand,idx)=>{
    let card=document.createElement('button');card.type='button';card.className='candidate';
    card.innerHTML=`<strong>${cand.label}</strong><div>${cand.angles.map(a=>a+'°').join(' / ')}</div><div class="muted">仿真低频 ${cand.sim_low_band} · 预期改善 ${Math.round(cand.expected_improvement*100)}% · 解析评分 ${cand.analytic_score}</div>`;
    if(cand.warnings&&cand.warnings.length){let w=document.createElement('div');w.className='warn';w.textContent='⚠ '+cand.warnings.join('；');card.append(w)}
    card.onclick=()=>{diagChoice={...cand,index:idx};document.querySelectorAll('.candidate').forEach(el=>el.classList.remove('selected'));card.classList.add('selected');$('#diag-apply').hidden=false;renderPreviews()};
    box.append(card);
  });
}
function previewUrl(angles){
  let q=new URLSearchParams();q.set('revision',diag.revision);q.set('view',$('#diag-view').value);
  for(let v of diag.context.rect)q.append('rect',v);
  for(let a of angles)q.append('angle',a);
  return `/press/projects/${current.id}/diagnose/preview.png?${q.toString()}`;
}
function renderPreviews(){
  let box=$('#diag-previews');box.replaceChildren();
  let make=(title,angles)=>{let wrap=document.createElement('div');wrap.className='preview';let h=document.createElement('p');h.textContent=title;let img=document.createElement('img');img.alt=title+' 同区域预览';img.src=previewUrl(angles);wrap.append(h,img);return wrap};
  box.append(make('当前（左） vs 推荐（右）：'+$('#diag-view').value,diagChoice?diagChoice.angles:diag.current.angles));
}
$('#diag-view').onchange=()=>{if(diag)renderPreviews()};
$('#diag-apply').onclick=()=>safe(async()=>{
  if(!diag||!diagChoice)return;
  if(!confirm(`确认将网角写为 ${diagChoice.angles.join(' / ')}°？\n该操作绑定诊断修订 REV ${diag.revision} 与输入指纹 ${diag.input_fingerprint}，写回后需重新生成印版。`))return;
  let p=await api(`/projects/${current.id}/diagnose/apply`,data('POST',{revision:diag.revision,input_fingerprint:diag.input_fingerprint,angles:diagChoice.angles,label:diagChoice.label}));
  await selectProject(current.id);
  diag=null;diagChoice=null;$('#diag-result').hidden=true;$('#diag-status').textContent='网角已写回（REV '+p.revision+'），请重新生成印版并可再次诊断确认。';
  toast('推荐网角已写回，手动角度仍可随时修改');
});
safe(listing);
