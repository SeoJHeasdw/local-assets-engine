import {defaults,hex} from '../shared/editing.mjs';
import {escapeHtml,fileUrl,formatBytes} from '../shared/format.mjs';
import {popup,toast,pickFile,uploadBlob,waitJob} from './ui.js';
import {readDraft,writeDraft} from './drafts.js';
const clone=x=>structuredClone(x);
const range=(key,label,min,max,step,value)=>`<label class="edit-range"><span>${label}<output data-value="${key}">${value}</output></span><input type="range" data-key="${key}" aria-label="${label}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
export function createEditor({api,refreshJobs,openAsset,makeMesh,addToPrevizScene,bridge}) {
 let session=null;let opening=0;const drafts=new Map();
 function close(){opening++;session?.dialog.close();}
 async function open(job,asset){
  close();const ticket=++opening;const key=`${job.id}/${asset.id}`,isMesh=asset.kind==='mesh',old=drafts.get(key)||await readDraft(key);
  const dialog=document.createElement('dialog');dialog.id='editor-dialog';dialog.className='editor-dialog';dialog.setAttribute('aria-label','에셋 편집');
  if(ticket!==opening)return;
  const baseFile=asset.meta?.editBaseFile||asset.file;
  let savedStamp=null,savedBlob=null;
  if(!old&&asset.meta?.editStampFile){const response=await fetch(fileUrl(job.id,asset.meta.editStampFile));savedBlob=await response.blob();const bitmap=await createImageBitmap(savedBlob),canvas=document.createElement('canvas'),scale=Math.min(1,1024/Math.max(bitmap.width,bitmap.height));canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();savedStamp={width:canvas.width,height:canvas.height,data:canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data};}
  if(ticket!==opening)return;
  const name=old?.name||job.params?.subject||job.title;
  dialog.innerHTML=`<header class="editor-head"><div><span class="eyebrow">${isMesh?'3D 에셋':'2D 이미지'}</span><h2>${escapeHtml(name)}</h2></div><div class="editor-actions"><button class="secondary" data-e="compare" aria-pressed="false">원본 비교</button><button class="ghost" data-e="info">정보</button><button class="icon-button" data-e="close" aria-label="편집 닫기">✕</button></div></header>
   <div class="editor-body"><div class="editor-canvas-wrap"><div class="editor-canvas">${isMesh?`<model-viewer src="${fileUrl(job.id,baseFile)}" camera-controls camera-orbit="30deg 70deg auto" shadow-intensity=".7" environment-image="neutral" exposure="1.05" interaction-prompt="none" alt="${escapeHtml(name)}"${asset.preview?` poster="${fileUrl(job.id,asset.preview)}"`:''}></model-viewer>`:'<canvas aria-label="이미지 편집 미리보기"></canvas>'}</div><div class="viewport-bar"><span class="editor-status" role="status">불러오는 중…</span><span>${isMesh?'드래그로 회전 · 휠로 확대':'로고 탭에서 이미지를 눌러 위치 지정'}</span></div></div>
   <aside class="editor-panel"><div class="editor-tabs" role="tablist"><button role="tab" aria-selected="true" data-tab="color">색상</button><button role="tab" aria-selected="false" data-tab="logo">로고·문구</button>${!isMesh?'<button role="tab" aria-selected="false" data-tab="frame">구성</button>':''}</div>
    <div class="edit-controls" data-panel="color"><h3>특정 색 바꾸기</h3><p class="hint">원하는 색을 찍고 바꿀 색을 고르세요. 원래의 명암은 유지합니다.</p>
     <label class="check-label"><input type="checkbox" data-key="recolor.enabled">색상 교체</label><div class="color-pair"><label>원래 색<input type="color" data-key="recolor.from" value="#bd8858"></label><span>→</span><label>바꿀 색<input type="color" data-key="recolor.to" value="#5478a3"></label></div>
     <button class="secondary full" data-e="pick-color">에셋에서 색 찍기</button>${range('recolor.tolerance','선택 범위',.01,1,.01,.2)}<details class="edit-section"><summary>밝기·대비·채도</summary>${range('brightness','밝기',.1,2,.05,1)}${range('contrast','대비',.1,2,.05,1)}${range('saturation','채도',0,2,.05,1)}
     </details>
     ${isMesh?`<div class="control-divider"></div><h3>재질 조절</h3>${range('metallic','금속성 유지',0,1,.05,1)}${range('roughness','거칠기 유지',.05,1,.05,1)}`:''}
    </div>
    <div class="edit-controls" data-panel="logo" hidden><h3>표면에 더하기</h3><p class="hint">로고 이미지나 문구를 올려 나만의 에셋으로 바꾸세요.</p>
     <button class="secondary full" data-e="logo-file">로고 이미지 가져오기</button><div class="logo-preview" hidden></div>
     <label class="field"><span class="label">문구</span><input id="edit-text" maxlength="80" placeholder="예: JAVIS"></label><div class="text-tools"><label>문구 색<input type="color" id="edit-text-color" value="#f5e3b4"></label><button class="secondary" data-e="text">문구 만들기</button></div>
     <div class="control-divider"></div><label class="check-label"><input type="checkbox" data-key="overlay.enabled">로고 표시</label>
     <button class="secondary full" data-e="place">${isMesh?'붙일 표면 선택':'위치 선택'}</button><p class="hint" id="placement-hint">${isMesh?'버튼을 누른 뒤 에셋에서 원하는 위치를 클릭하세요.':'이미지에서 원하는 위치를 클릭하세요.'}</p>
     ${range('overlay.size','크기',.03,1,.01,.25)}${range('overlay.rotation','회전',-180,180,1,0)}${range('overlay.opacity','불투명도',0,1,.05,1)}
     <button class="ghost full" data-e="remove-logo">로고 빼기</button>
    </div>
    ${!isMesh?`<div class="edit-controls" data-panel="frame" hidden><h3>구성</h3><div class="control-row"><button class="secondary" data-e="rotate">90° 회전</button><button class="secondary" data-e="flip">좌우 반전</button></div><label class="field"><span class="label">가운데 자르기</span><select data-key="frame.crop"><option value="original">원래 비율</option><option value="square">정사각형 · 1:1</option><option value="portrait">세로 · 3:4</option><option value="landscape">가로 · 4:3</option></select></label></div>`:''}
    <div class="edit-bottom"><div class="control-row"><button class="ghost" data-e="undo" disabled>↶ 되돌리기</button><button class="ghost" data-e="reset">처음 상태</button></div><label class="field"><span class="label">새 버전 이름</span><input id="edit-name" value="${escapeHtml(name)}" maxlength="120"></label><button class="primary full" data-e="save" disabled>새 버전으로 저장</button><p class="hint">원본은 그대로 보관됩니다.</p></div>
   </aside></div><footer class="editor-foot"><div class="review-controls"><span class="review-label">${asset.review==='approved'?'승인됨':asset.review==='rejected'?'거절됨':'검토 대기'}</span><button class="ghost" data-e="approve">승인</button><button class="ghost" data-e="reject">거절</button></div><div class="control-row">${isMesh?'<button class="secondary" data-e="previz">프리비즈에 넣기</button>':'<button class="secondary" data-e="to3d">이 이미지로 3D 만들기</button>'}<a class="secondary button-link" data-e="download" href="${fileUrl(job.id,asset.file)}" download>파일 내보내기</a></div></footer>`;
  document.body.append(dialog);dialog.showModal();
  const s={dialog,job,asset,key,isMesh,plan:clone(old?.plan||(asset.meta?.editBaseFile?asset.meta.editPlan:null)||defaults()),stamp:old?.stamp||savedStamp,stampBlob:old?.stampBlob||savedBlob,stampUpload:old?.stampUpload||null,stampLabel:old?.stampLabel||asset.meta?.editPlan?.overlay?.text||'',history:[],ready:false,loaded:!isMesh,busy:false,pending:false,revision:0,compare:false,pick:null,tab:'color',textures:new Map(),originals:[],closed:false};session=s;
  s.dirty=!!old;
  s.plan.recolor ||= {enabled:false,from:'#bd8858',to:'#5478a3',tolerance:.2};
  s.plan.overlay ||= {enabled:false,x:.5,y:.5,size:.25,rotation:0,opacity:1,position:null,normal:null};
  const $=q=>dialog.querySelector(q),status=text=>$('.editor-status').textContent=text;
  const viewer=$('model-viewer');
  const set=(key,value)=>{const parts=key.split('.');if(parts.length===2)s.plan[parts[0]][parts[1]]=value;else s.plan[key]=value;};
  const get=key=>key.split('.').reduce((obj,p)=>obj?.[p],s.plan);
  function sync(){for(const el of dialog.querySelectorAll('[data-key]')){const value=get(el.dataset.key);if(el.type==='checkbox')el.checked=!!value;else el.value=value;const out=dialog.querySelector(`[data-value="${el.dataset.key}"]`);if(out)out.textContent=Number.isFinite(+value)?(+value).toFixed(el.step==='1'?0:2):value;} $('[data-e="undo"]').disabled=!s.history.length;}
  function snapshot(){s.dirty=true;s.history.push({plan:clone(s.plan),stamp:s.stamp,stampBlob:s.stampBlob,stampUpload:s.stampUpload,stampLabel:s.stampLabel});if(s.history.length>30)s.history.shift();}
  const draftValue=()=>({name:$('#edit-name').value,plan:clone(s.plan),stamp:s.stamp,stampBlob:s.stampBlob,stampUpload:s.stampUpload,stampLabel:s.stampLabel});
  function queue(){clearTimeout(s.draftTimer);if(s.dirty)s.draftTimer=setTimeout(async()=>{if(!await writeDraft(key,draftValue())&&!s.closed)status('임시 저장이 어려워요. 새 버전으로 저장해 주세요.');},700);s.pending=true;$('[data-e="save"]').disabled=true;clearTimeout(s.timer);s.timer=setTimeout(send,140);}
  function send(){if(s.closed||!s.ready||!s.loaded||s.busy||!s.pending)return;s.pending=false;s.busy=true;s.revision++;status('미리보기 적용 중…');s.worker.postMessage({type:'preview',id:s.revision,plan:s.compare?defaults():s.plan,stamp:s.compare?null:s.stamp});}
  async function display(images){
   if(s.closed)return;
   if(isMesh){
    for(const image of images){let texture=s.textures.get(image.index);if(!texture){texture=viewer.createCanvasTexture();s.textures.set(image.index,texture);}
      const canvas=texture.source.element;canvas.width=image.width;canvas.height=image.height;const flipped=new Uint8ClampedArray(image.data.length);for(let y=0;y<image.height;y++)flipped.set(image.data.subarray(y*image.width*4,(y+1)*image.width*4),(image.height-1-y)*image.width*4);canvas.getContext('2d').putImageData(new ImageData(flipped,image.width,image.height),0,0);texture.source.update();
      for(const mi of image.materials)viewer.model.materials[mi].pbrMetallicRoughness.baseColorTexture.setTexture(s.compare?s.originals[mi].texture:texture);
    }
    viewer.model.materials.forEach((m,i)=>{m.pbrMetallicRoughness.setMetallicFactor(s.originals[i].metallic*(s.compare?1:s.plan.metallic));m.pbrMetallicRoughness.setRoughnessFactor(s.originals[i].roughness*(s.compare?1:s.plan.roughness));});
   }else{const image=images[0],canvas=$('canvas');canvas.width=image.width;canvas.height=image.height;canvas.getContext('2d').putImageData(new ImageData(image.data,image.width,image.height),0,0);}
   status(s.compare?'원본을 보고 있습니다':s.plan.overlay.enabled&&!s.plan.overlay.position&&isMesh?'로고를 붙일 표면을 선택하세요':'미리보기');
   $('[data-e="save"]').disabled=!s.ready||!s.loaded||s.saving||!s.dirty||s.pending;
  }
  s.worker=new Worker(new URL('./edit-preview.js',import.meta.url),{type:'module'});
  s.worker.onmessage=async({data})=>{
   if(s.closed)return;
   if(data.type==='ready'){s.ready=true;queue();}
   if(data.type==='preview'){s.busy=false;await display(data.images);if(s.pending)send();}
   if(data.type==='sample'){snapshot();s.plan.recolor.from=data.color;s.plan.recolor.enabled=true;s.pick=null;dialog.classList.remove('is-picking');sync();queue();}
   if(data.type==='error'){s.busy=false;status(data.message);toast(data.message);}
  };
  s.worker.postMessage({type:'init',kind:asset.kind,url:fileUrl(job.id,baseFile)});
  if(viewer){viewer.addEventListener('load',()=>{s.originals=viewer.model.materials.map(m=>({texture:m.pbrMetallicRoughness.baseColorTexture.texture,metallic:m.pbrMetallicRoughness.metallicFactor,roughness:m.pbrMetallicRoughness.roughnessFactor}));s.loaded=true;const d=viewer.getDimensions();s.longest=Math.max(d.x,d.y,d.z);queue();},{once:true});viewer.addEventListener('error',()=>status('3D 파일을 불러오지 못했습니다. 다시 열어 주세요.'));}
  for(const control of dialog.querySelectorAll('[data-key]')){
   control.addEventListener('pointerdown',()=>{s.sliderStart=clone(s.plan);});
   control.addEventListener('input',()=>{s.dirty=true;if(!s.sliderStart)snapshot();if(control.type==='color')s.plan.recolor.enabled=true;set(control.dataset.key,control.type==='checkbox'?control.checked:control.tagName==='SELECT'||control.type==='color'?control.value:Number(control.value));sync();queue();});
   control.addEventListener('change',()=>{if(s.sliderStart){s.history.push({plan:s.sliderStart,stamp:s.stamp,stampBlob:s.stampBlob,stampUpload:s.stampUpload,stampLabel:s.stampLabel});s.sliderStart=null;sync();}});
  }
  async function setStamp(blob,label){const bitmap=await createImageBitmap(blob),canvas=document.createElement('canvas');const scale=Math.min(1,1024/Math.max(bitmap.width,bitmap.height));canvas.width=Math.round(bitmap.width*scale);canvas.height=Math.round(bitmap.height*scale);canvas.getContext('2d').drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
   snapshot();s.stamp={width:canvas.width,height:canvas.height,data:canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data};s.stampBlob=blob;s.stampUpload=null;s.stampLabel=label;s.plan.overlay.enabled=true;
   $('.logo-preview').hidden=false;$('.logo-preview').textContent=label;sync();if(isMesh&&!s.plan.overlay.position){s.pick='place';dialog.classList.add('is-picking');status('로고를 붙일 표면을 클릭하세요.');}else queue();
  }
  let down=null;
  const viewport=$('.editor-canvas');viewport.addEventListener('pointerdown',event=>{down=[event.clientX,event.clientY];});
  viewport.addEventListener('click',event=>{
   if(!s.ready||!s.loaded||!down||Math.hypot(event.clientX-down[0],event.clientY-down[1])>6)return;
   if(!s.pick&&!(s.tab==='logo'&&!isMesh&&s.stamp))return;
   if(isMesh){const hit=viewer.positionAndNormalFromPoint(event.clientX,event.clientY);if(!hit)return toast('에셋 표면을 클릭해 주세요.');
    if(s.pick==='color'&&hit.uv){const material=viewer.materialFromPoint(event.clientX,event.clientY);s.worker.postMessage({type:'sample',material:material?.index??0,uv:[hit.uv.u??hit.uv.x,hit.uv.v??hit.uv.y]});}
    else if(s.stamp){snapshot();s.plan.overlay.position=[hit.position.x,hit.position.y,hit.position.z];s.plan.overlay.normal=[hit.normal.x,hit.normal.y,hit.normal.z];s.plan.overlay.depth=s.longest*.035;s.pick=null;dialog.classList.remove('is-picking');sync();queue();}
   }else{const rect=$('canvas').getBoundingClientRect(),uv=[(event.clientX-rect.left)/rect.width,(event.clientY-rect.top)/rect.height];if(uv.some(v=>v<0||v>1))return;
    if(s.pick==='color')s.worker.postMessage({type:'sample',uv,frame:s.plan.frame});
    else if(s.stamp){snapshot();s.plan.overlay.x=uv[0];s.plan.overlay.y=uv[1];s.pick=null;dialog.classList.remove('is-picking');sync();queue();}
   }
  });
  async function saveVersion(next=null){
     if(s.saving)return;
     if(s.plan.overlay.enabled&&(!s.stamp||(isMesh&&!s.plan.overlay.position)))return toast('로고와 붙일 위치를 선택해 주세요.');
     s.saving=true;$('.editor-panel').inert=true;$('.editor-foot').inert=true;$('[data-e="compare"]').disabled=true;$('[data-e="save"]').disabled=true;status('새 버전을 저장하고 있습니다…');
     const plan=clone(s.plan);if(plan.overlay.enabled){s.stampUpload ||= await uploadBlob(s.stampBlob);plan.overlay.uploadId=s.stampUpload.id;}else plan.overlay=null;
     const result=await api('/api/jobs',{method:'POST',body:{recipe:'edit-asset',params:{source:{jobId:job.id,assetId:asset.id},name:$('#edit-name').value,replaceEdits:!!asset.meta?.editBaseFile,plan}}});
     await refreshJobs();const completed=await waitJob(api,result.id);s.saved=true;clearTimeout(s.draftTimer);drafts.delete(key);await writeDraft(key,null);const wasOpen=!s.closed;if(wasOpen)close();await refreshJobs();if(wasOpen){if(next)await next(completed);else await openAsset(completed.id,completed.assets[0].id);}toast('편집본을 새 버전으로 저장했습니다.');
  }
  dialog.addEventListener('click',async event=>{
   const tab=event.target.closest('[data-tab]');if(tab){s.tab=tab.dataset.tab;for(const b of dialog.querySelectorAll('[data-tab]'))b.setAttribute('aria-selected',String(b===tab));for(const p of dialog.querySelectorAll('[data-panel]'))p.hidden=p.dataset.panel!==s.tab;return;}
   const button=event.target.closest('[data-e]');if(!button)return;
   try{switch(button.dataset.e){
    case 'close':close();break;
    case 'pick-color':s.pick='color';dialog.classList.add('is-picking');status('바꾸고 싶은 색을 클릭하세요.');break;
    case 'place':if(!s.stamp)return toast('로고 이미지나 문구를 먼저 추가해 주세요.');s.pick='place';dialog.classList.add('is-picking');status('붙일 위치를 클릭하세요.');break;
    case 'logo-file':{const file=await pickFile();if(file)await setStamp(file,file.name);break;}
    case 'text':{const text=$('#edit-text').value.trim();if(!text)return toast('문구를 적어 주세요.');const canvas=document.createElement('canvas'),ctx=canvas.getContext('2d');ctx.font='bold 160px sans-serif';canvas.width=Math.min(4096,Math.ceil(ctx.measureText(text).width+50));canvas.height=240;ctx.font='bold 160px sans-serif';ctx.fillStyle=$('#edit-text-color').value;ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(text,canvas.width/2,120,canvas.width-30);const blob=await new Promise(r=>canvas.toBlob(r));await setStamp(blob,text);s.plan.overlay.text=text;s.plan.overlay.textColor=$('#edit-text-color').value;break;}
    case 'remove-logo':snapshot();s.plan.overlay.enabled=false;sync();queue();break;
    case 'rotate':snapshot();s.plan.frame.turns=(s.plan.frame.turns+1)%4;sync();queue();break;
    case 'flip':snapshot();s.plan.frame.flipX=!s.plan.frame.flipX;sync();queue();break;
    case 'undo':{const prev=s.history.pop();if(prev){Object.assign(s,prev);sync();queue();}break;}
    case 'reset':snapshot();s.plan={...defaults(),recolor:{enabled:false,from:'#bd8858',to:'#5478a3',tolerance:.2},overlay:{enabled:false,x:.5,y:.5,size:.25,rotation:0,opacity:1,position:null,normal:null}};sync();queue();break;
    case 'compare':s.compare=!s.compare;button.setAttribute('aria-pressed',String(s.compare));button.textContent=s.compare?'편집본 보기':'원본 비교';queue();break;
    case 'save':await saveVersion();break;
    case 'approve':case 'reject':{const review=button.dataset.e==='approve'?'approved':'rejected';await api(`/api/jobs/${job.id}/assets/${asset.id}/review`,{method:'POST',body:{status:review}});$('.review-label').textContent=review==='approved'?'승인됨':'거절됨';await refreshJobs();break;}
    case 'to3d':if(s.dirty)await saveVersion(j=>makeMesh(j.id,j.assets[0].id));else{close();makeMesh(job.id,asset.id);}break;
    case 'previz':if(s.dirty)await saveVersion(j=>addToPrevizScene(j.id,j.assets[0].id));else{close();addToPrevizScene(job.id,asset.id);}break;
    case 'download':if(s.dirty){event.preventDefault();await saveVersion(async j=>{await openAsset(j.id,j.assets[0].id);const link=document.createElement('a');link.href=fileUrl(j.id,j.assets[0].file);link.download='';link.click();});}break;
    case 'info':{
     const meta=asset.meta||{},stats=meta.stats||{};
     const info=popup('에셋 정보',`<dl class="meta"><dt>파일</dt><dd>${escapeHtml(asset.file)}</dd><dt>크기</dt><dd>${isMesh?formatBytes(stats.bytes):`${meta.width} × ${meta.height}px`}</dd>${isMesh?`<dt>면 수</dt><dd>${(stats.facesOut||0).toLocaleString()}</dd><dt>텍스처</dt><dd>${meta.textureSize||'–'}px</dd>`:''}<dt>시드</dt><dd>${meta.seed??'–'}</dd></dl>${meta.inspectionFile?`<a class="secondary button-link" href="${fileUrl(job.id,meta.inspectionFile)}" target="_blank" rel="noopener">여섯 방향 보기</a>`:''}${meta.source?'<button class="secondary" data-parent>이전 버전 열기</button>':''}${stats.topology?.warnings?.length?`<p class="notice">${escapeHtml(stats.topology.warnings.join(' '))}</p>`:''}${meta.sourceStateFile?'<div class="control-divider"></div><h3>원본에서 다시 구성</h3><p class="hint">생성한 원본으로 표면과 재질을 다시 만듭니다.</p><button class="secondary" data-refine>고품질 원본 다시 구성</button>':''}`);
     info.querySelector('[data-parent]')?.addEventListener('click',()=>{info.close();close();openAsset(meta.source.jobId,meta.source.assetId);});
     info.querySelector('[data-refine]')?.addEventListener('click',async e=>{e.target.disabled=true;try{await api('/api/jobs',{method:'POST',body:{recipe:'refine-mesh',params:{source:{jobId:job.id,assetId:asset.id},gameFaces:0}}});info.close();toast('원본 재구성을 시작했습니다.');refreshJobs();}catch(error){toast(error.message);e.target.disabled=false;}});break;
    }
   }}catch(error){s.saving=false;$('.editor-panel').inert=false;$('.editor-foot').inert=false;$('[data-e="compare"]').disabled=false;status(error.message);toast(error.message);if(s.ready)$('[data-e="save"]').disabled=false;}
  });
  $('#edit-name').addEventListener('input',()=>{s.dirty=true;clearTimeout(s.draftTimer);s.draftTimer=setTimeout(()=>writeDraft(key,draftValue()),700);if(s.ready&&!s.busy)$('[data-e="save"]').disabled=false;});
  dialog.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='z'&&!e.target.matches('input,textarea')){e.preventDefault();$('[data-e="undo"]').click();}});
  dialog.addEventListener('close',()=>{clearTimeout(s.draftTimer);if(!s.saved&&s.dirty){const draft=draftValue();drafts.set(key,draft);writeDraft(key,draft);}s.closed=true;s.worker.terminate();clearTimeout(s.timer);dialog.remove();if(session===s)session=null;});sync();if(s.stamp){$('.logo-preview').hidden=false;$('.logo-preview').textContent=s.stampLabel||'로고 이미지';$('#edit-text').value=s.plan.overlay?.text||'';let color=s.plan.overlay?.textColor;if(!color&&s.plan.overlay?.text){for(let i=0;i<s.stamp.data.length;i+=4){if(s.stamp.data[i+3]>245){color=hex(s.stamp.data.subarray(i,i+3));break;}}}$('#edit-text-color').value=color||'#f5e3b4';}
 }
 return {open,close};
}
