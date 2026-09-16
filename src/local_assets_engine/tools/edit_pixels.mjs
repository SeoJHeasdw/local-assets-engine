import fs from "node:fs/promises";
import {adjustPixels,frameImage,overlayImage,projectOverlay} from "../../../electron-app/shared/editing.mjs";
import {parseGlb,primitives,baseImages} from "../../../electron-app/shared/glb.mjs";
const manifest=JSON.parse(await fs.readFile(process.argv[2],"utf8"));
const readImage=async x=>({...x,data:new Uint8ClampedArray(await fs.readFile(x.file))});
const stamp=manifest.stamp?await readImage(manifest.stamp):null;
let geometry=[],materials=[];
if(manifest.glb){const b=await fs.readFile(manifest.glb);const glb=parseGlb(b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength));
 if(manifest.plan.overlay?.enabled)geometry=primitives(glb);materials=baseImages(glb);}
for(const t of manifest.textures){
 let image=await readImage(t);
 if(!manifest.glb)image=frameImage(image,manifest.plan.frame);
 image.data=adjustPixels(image.data,manifest.plan);
 if(manifest.glb){const entry=materials.find(x=>x.index===t.index);projectOverlay(image,stamp,manifest.plan.overlay,geometry.filter(p=>entry?.materials.includes(p.material)));}
 else overlayImage(image,stamp,manifest.plan.overlay);
 await fs.writeFile(t.file,image.data);t.width=image.width;t.height=image.height;
}
await fs.writeFile(process.argv[2],JSON.stringify(manifest));
