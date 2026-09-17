import {escapeHtml} from "../shared/format.mjs";
export function toast(message) {
  let item=document.querySelector('#studio-toast');
  if(!item){item=document.createElement('div');item.id='studio-toast';item.role='status';item.setAttribute('popover','manual');document.body.append(item);}
  item.textContent=message;
  if(item.showPopover&&!item.matches(':popover-open'))item.showPopover();
  item.classList.add('is-visible');clearTimeout(item.timer);
  item.timer=setTimeout(()=>{item.classList.remove('is-visible');if(item.hidePopover&&item.matches(':popover-open'))item.hidePopover();},4500);
}
export function popup(title,body,{wide=false,className=''}={}) {
  const dialog=document.createElement('dialog');dialog.className=`studio-dialog${wide?' is-wide':''}${className?` ${className}`:''}`;
  dialog.innerHTML=`<header class="dialog-head"><h2>${escapeHtml(title)}</h2><button class="icon-button" type="button" aria-label="닫기">✕</button></header><div class="dialog-body">${body}</div>`;
  dialog.setAttribute('aria-label',title);dialog.querySelector('.icon-button').onclick=()=>dialog.close();
  dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
  dialog.addEventListener('close',()=>dialog.remove());document.body.append(dialog);dialog.showModal();return dialog;
}
export async function uploadBlob(blob) {
  if(blob.size>20*1024*1024)throw Error('20MB 이하의 이미지를 골라 주세요.');
  const response=await fetch('/api/uploads',{method:'POST',headers:{'Content-Type':blob.type||'application/octet-stream'},body:blob});
  const data=await response.json();if(!response.ok)throw Error(data.detail||'이미지를 가져오지 못했습니다.');return data;
}
export function pickFile(){return new Promise(resolve=>{const input=document.createElement('input');input.type='file';input.accept='image/png,image/jpeg,image/webp';
 input.onchange=()=>resolve(input.files?.[0]||null);input.oncancel=()=>resolve(null);input.click();});}
// 앞선 3D 생성이 길면 대기가 수십 분일 수 있다. 시간 제한 없이 기다리고 상태가 바뀔 때마다 알린다.
export async function waitJob(api,id,{onUpdate=null,interval=800}={}){
 for(;;){const job=await api(`/api/jobs/${id}`);
  if(!['queued','running','cancelling'].includes(job.state)){if(job.state!=='done')throw Error(job.error||'작업이 중지됐습니다.');return job;}
  onUpdate?.(job);await new Promise(r=>setTimeout(r,interval));}
}
