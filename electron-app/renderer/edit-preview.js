// 편집 미리보기 계산을 화면 밖 스레드에서 한다. 저장과 같은 renderEdit를 부른다.
import {renderEdit,meshContext,needsGeometry} from '../shared/editing.mjs';
import {parseGlb,primitives,baseImages} from '../shared/glb.mjs';
let images=[],mesh=false,glb=null,context=null;
const stamps={},cache=new Map();
async function decode(blob,max=1024){const bitmap=await createImageBitmap(blob),ratio=Math.min(1,max/Math.max(bitmap.width,bitmap.height));
 const canvas=new OffscreenCanvas(Math.round(bitmap.width*ratio),Math.round(bitmap.height*ratio)),ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
 return {width:canvas.width,height:canvas.height,data:ctx.getImageData(0,0,canvas.width,canvas.height).data};}
// 1백만 면 메시의 형상은 로고나 브러시 영역을 처음 쓸 때 한 번만 푼다(약 0.3초).
function geometry(){if(!context)context=meshContext(primitives(glb));return context;}
self.onmessage=async ({data:message})=>{try{
 if(message.type==='init'){
  mesh=message.kind==='mesh';const response=await fetch(message.url);if(!response.ok)throw Error('파일을 불러오지 못했습니다.');
  if(mesh){glb=parseGlb(await response.arrayBuffer());const entries=baseImages(glb);if(!entries.length)throw Error('색상 텍스처가 없는 메시입니다.');
    images=await Promise.all(entries.map(async entry=>({...await decode(new Blob([entry.bytes],{type:entry.mime})),materials:entry.materials,index:entry.index})));
    primitives(glb);
  }else images=[await decode(await response.blob())];
  self.postMessage({type:'ready',width:images[0].width,height:images[0].height,materials:images.map(i=>i.materials)});
 }else if(message.type==='stamp'){
  if(message.image)stamps[message.id]=message.image;else delete stamps[message.id];
 }else if(message.type==='preview'){
  const plan=message.plan,highlight=message.highlight||null;
  const ctx=mesh&&needsGeometry(plan)?geometry():mesh?meshContext([]):null;
  const output=images.map(original=>renderEdit(original,plan,{stamps,context:ctx,texture:mesh?{materials:original.materials}:null,highlight,resize:false,cache}));
  self.postMessage({type:'preview',id:message.id,images:output.map((image,i)=>({...image,index:images[i].index,materials:images[i].materials}))},output.map(x=>x.data.buffer));
 }else if(message.type==='sample'){
   // 색은 편집 전 원본에서 읽는다. 2D uv는 원본 좌표다.
   const image=mesh?images.find(i=>i.materials.includes(message.material)):images[0];
   if(!image)throw Error('색상을 읽지 못했습니다.');const [u,v]=message.uv;
   const x=Math.max(0,Math.min(image.width-1,Math.floor(u*image.width))),y=Math.max(0,Math.min(image.height-1,Math.floor(v*image.height)));
   const i=(y*image.width+x)*4;self.postMessage({type:'sample',color:'#'+[0,1,2].map(k=>image.data[i+k].toString(16).padStart(2,'0')).join('')});
 }
}catch(error){self.postMessage({type:'error',message:error.message});}};
