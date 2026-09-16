import {adjustPixels,frameImage,overlayImage,projectOverlay,hex} from '../shared/editing.mjs';
import {parseGlb,primitives,baseImages} from '../shared/glb.mjs';
let images=[],geometry=[],mesh=false;
async function decode(blob,max=1024){const bitmap=await createImageBitmap(blob),ratio=Math.min(1,max/Math.max(bitmap.width,bitmap.height));
 const canvas=new OffscreenCanvas(Math.round(bitmap.width*ratio),Math.round(bitmap.height*ratio)),ctx=canvas.getContext('2d');ctx.drawImage(bitmap,0,0,canvas.width,canvas.height);bitmap.close();
 return {width:canvas.width,height:canvas.height,data:ctx.getImageData(0,0,canvas.width,canvas.height).data};}
self.onmessage=async ({data:message})=>{try{
 if(message.type==='init'){
  mesh=message.kind==='mesh';const response=await fetch(message.url);if(!response.ok)throw Error('파일을 불러오지 못했습니다.');
  if(mesh){const glb=parseGlb(await response.arrayBuffer());const entries=baseImages(glb);if(!entries.length)throw Error('색상 텍스처가 없는 메시입니다.');
    images=await Promise.all(entries.map(async entry=>({...await decode(new Blob([entry.bytes],{type:entry.mime})),materials:entry.materials,index:entry.index})));
    geometry=primitives(glb);
  }else images=[await decode(await response.blob())];
  self.postMessage({type:'ready'});
 }else if(message.type==='preview'){
  const stamp=message.stamp;
  const output=images.map(original=>{
    const image=mesh?{...original,data:new Uint8ClampedArray(original.data)}:frameImage(original,message.plan.frame);
    image.data=adjustPixels(image.data,message.plan);
    if(mesh)projectOverlay(image,stamp,message.plan.overlay,geometry.filter(p=>original.materials.includes(p.material)));
    else overlayImage(image,stamp,message.plan.overlay);
    return image;
  });self.postMessage({type:'preview',id:message.id,images:output},output.map(x=>x.data.buffer));
 }else if(message.type==='sample'){
   const image=mesh?images.find(i=>i.materials.includes(message.material)):frameImage(images[0],message.frame);
   if(!image)throw Error('색상을 읽지 못했습니다.');const [u,v]=message.uv;
   const x=Math.max(0,Math.min(image.width-1,Math.floor(u*image.width))),y=Math.max(0,Math.min(image.height-1,Math.floor(v*image.height)));
   self.postMessage({type:'sample',color:hex(image.data.subarray((y*image.width+x)*4))});
 }
}catch(error){self.postMessage({type:'error',message:error.message});}};
