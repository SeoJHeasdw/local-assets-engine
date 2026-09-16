import {escapeHtml} from "../shared/format.mjs";
export function toast(message) {
  let item=document.querySelector('#studio-toast');
  if(!item){item=document.createElement('div');item.id='studio-toast';item.role='status';item.setAttribute('popover','manual');document.body.append(item);}
  item.textContent=message;
  if(item.showPopover&&!item.matches(':popover-open'))item.showPopover();
  item.classList.add('is-visible');clearTimeout(item.timer);
  item.timer=setTimeout(()=>{item.classList.remove('is-visible');if(item.hidePopover&&item.matches(':popover-open'))item.hidePopover();},4500);
}
export function popup(title,body,{wide=false}={}) {
  const dialog=document.createElement('dialog');dialog.className=`studio-dialog${wide?' is-wide':''}`;
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
export async function waitJob(api,id){for(let i=0;i<150;i++){const job=await api(`/api/jobs/${id}`);if(!['queued','running','cancelling'].includes(job.state)){if(job.state!=='done')throw Error(job.error||'작업이 중지됐습니다.');return job;}await new Promise(r=>setTimeout(r,800));}throw Error('작업이 계속 진행 중입니다. 만들기에서 결과를 확인해 주세요.');}
