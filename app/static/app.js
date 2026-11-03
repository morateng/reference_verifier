const $ = (s) => document.querySelector(s);
const labels = {matched:'주요 정보 일치',review:'불일치 의심',candidate:'검색 후보 · 확인 필요',unverified:'미확인',error:'조회 실패'};
const verdicts = {supported_by_abstract:'초록에 근거 있음',potential_mismatch:'내용 불일치 가능성',insufficient_evidence:'근거 부족'};
let currentJob = null, filter = 'all';
function el(tag, text, className) {const node=document.createElement(tag); if(text!==undefined)node.textContent=text; if(className)node.className=className; return node;}
function error(message){$('#error').textContent=message;$('#error').hidden=!message;}
function fileChanged(){const f=$('#pdf').files[0];$('#file-name').textContent=f?f.name:'논문 PDF를 여기에 놓아 주세요';error('');}
$('#pdf').addEventListener('change',fileChanged);
for(const event of ['dragenter','dragover']) $('#dropzone').addEventListener(event,()=>$('#dropzone').classList.add('drag'));
for(const event of ['dragleave','drop']) $('#dropzone').addEventListener(event,()=>$('#dropzone').classList.remove('drag'));
fetch('/api/health').then(r=>r.json()).then(data=>{$('#use-ai').disabled=!data.ai_available;$('#ai-note').textContent=data.ai_available?'초록과 본문 인용 문장을 비교합니다. 외부 AI API 비용이 발생할 수 있습니다.':'AI API 키 미설정 · 서지정보 검증은 키 없이 사용할 수 있습니다.';}).catch(()=>{$('#use-ai').disabled=true;$('#ai-note').textContent='서버 연결을 확인해 주세요.';});
$('#upload-form').addEventListener('submit',async(event)=>{
  event.preventDefault();error('');const file=$('#pdf').files[0];if(!file)return;
  if(file.size>20*1024*1024){error('PDF는 최대 20MB까지 업로드할 수 있습니다.');return;}
  $('#submit').disabled=true;$('#submit').textContent='PDF를 읽고 있습니다…';
  const data=new FormData();data.append('file',file);data.append('use_ai',$('#use-ai').checked);
  try{const response=await fetch('/api/jobs',{method:'POST',body:data});const payload=await response.json();if(!response.ok)throw new Error(typeof payload.detail==='string'?payload.detail:'업로드에 실패했습니다.');
    $('#results').hidden=false;$('#download').hidden=true;$('#results').scrollIntoView({behavior:'smooth'});await poll(payload.id);
  }catch(e){error(e.message);}finally{$('#submit').disabled=false;$('#submit').replaceChildren(document.createTextNode('참고문헌 검증 시작 '),el('span','↗'));}
});
async function poll(id){
  let retries=0;
  for(;;){
    let response;
    try{response=await fetch(`/api/jobs/${id}`);}catch(e){if(++retries>3)throw new Error('연결이 끊겼습니다. 서버 상태를 확인해 주세요.');await new Promise(r=>setTimeout(r,1500));continue;}
    if(!response.ok)throw new Error('결과를 가져오지 못했습니다. 작업이 만료되었거나 서버가 재시작되었을 수 있습니다.');
    currentJob=await response.json();retries=0;render();
    if(currentJob.state==='done'||currentJob.state==='failed'){
      $('#download').href=`/api/jobs/${id}/report`;$('#download').hidden=false;
      if(currentJob.state==='failed')error(currentJob.error);return;
    }
    $('#submit').textContent=`검증 중 · ${currentJob.completed}/${currentJob.total}`;
    await new Promise(r=>setTimeout(r,1200));
  }
}
document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('active',b===button));renderList();}));
function render(){
  const j=currentJob;$('#progress-text').textContent=`${j.filename} · ${j.page_count}페이지 · 추출 ${j.total}개 / 처리 ${j.completed}개${j.state==='done'?' · 처리 완료':''}`;
  $('#progress').value=j.total?j.completed/j.total*100:0;
  $('#warnings').replaceChildren(...j.warnings.map(w=>el('div',w)));
  const count=(statuses)=>j.results.filter(r=>statuses.includes(r.status)).length;
  const statistics=[['추출된 참고문헌',j.total],['주요 정보 일치',count(['matched'])],['확인 필요',count(['review','candidate'])],['미확인 / 조회 실패',count(['unverified','error'])]];
  $('#stats').replaceChildren(...statistics.map(([label,n])=>{const node=el('div',undefined,'stat');node.append(el('strong',String(n)),el('span',label));return node;}));
  $('#scope').textContent=j.scope;renderList();
}
function renderList(){
  if(!currentJob)return;
  const open=new Set([...document.querySelectorAll('details[open]')].map(n=>n.dataset.id));
  const rows=[...currentJob.results].sort((a,b)=>a.id-b.id).filter(r=>filter==='all'||(filter==='attention'&&['review','candidate'].includes(r.status))||(filter==='unverified'&&['unverified','error'].includes(r.status))||r.status===filter);
  $('#reference-list').replaceChildren(...rows.map(r=>renderReference(r,open.has(String(r.id)))));
  if(!rows.length)$('#reference-list').append(el('p',currentJob.state==='running'?'항목별 근거를 확인하고 있습니다…':'이 필터에 해당하는 항목이 없습니다.','empty'));
}
function renderReference(r,isOpen){
  const node=el('details',undefined,'reference');node.dataset.id=r.id;node.open=isOpen;
  const summary=el('summary'), main=el('div',undefined,'ref-main');
  main.append(el('div',r.record?.title||r.raw,'ref-title'),el('div',r.record?`${r.record.authors.slice(0,2).join(', ')}${r.record.authors.length>2?' 외':''} · ${r.record.years.join('/')} · ${r.record.journal}`:'등록정보 대조 결과를 확인해 주세요.','ref-meta'));
  summary.append(el('span',`[${r.label}]`,'ref-number'),main,el('span',labels[r.status],`badge ${r.status}`));node.append(summary);
  const detail=el('div',undefined,'ref-detail');detail.append(el('h3','PDF에서 추출한 원래 표기'),el('div',r.raw,'raw'));
  for(const issue of r.issues)detail.append(el('p','• '+issue,'issue'));
  if(r.checks.length){detail.append(el('h3','항목별 대조'));for(const check of r.checks){const line=el('div',undefined,'check');line.append(el('strong',check.field),el('span',check.status==='match'?'✓':check.status==='review'?'△':'—','check-mark'),el('span',check.detail));detail.append(line);}}
  if(r.record){const source=el('a','Crossref 등록 DOI로 원문 확인 ↗');source.href=r.record.source_url;source.target='_blank';source.rel='noopener noreferrer';detail.append(el('h3','조회 근거'),source,el('p',`매칭 방식: ${r.matched_by==='doi'?'DOI 직접 조회':'서지정보 검색'} · 제목 유사도 ${r.title_similarity}/100 (정확도 확률이 아님)`));
    detail.append(el('h3','등록정보 기반 표기 제안'),el('div',r.suggested_citation,'citation'),el('p','일반 서지 표기입니다. 투고처의 APA·IEEE 등 세부 양식에 맞게 최종 조정해 주세요.'));
  }
  detail.append(el('h3',`본문 인용 문맥 · ${r.contexts.length}곳`));
  if(!r.contexts.length)detail.append(el('p','인용 위치를 자동으로 찾지 못했습니다. 미인용이라고 단정하지 않습니다.'));
  r.contexts.forEach((text,i)=>detail.append(el('p',`${i+1}. ${text}`,'context')));
  detail.append(el('h3','AI 내용 검토'),el('p',r.content_check.reason));
  for(const item of r.content_check.items||[]){detail.append(el('p',`문맥 ${item.index} · ${verdicts[item.verdict]} — ${item.reason}`));if(item.evidence_quote)detail.append(el('blockquote',item.evidence_quote,'evidence'));}
  if(r.content_check.status==='abstract_only')detail.append(el('p',`검토한 문맥 ${r.content_check.checked_count}/${r.content_check.total_contexts}개 · 초록은 논문 전체를 대체하지 않습니다.`));
  node.append(detail);return node;
}
