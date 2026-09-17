import fs from "node:fs/promises";
import {renderEdit,meshContext,needsGeometry} from "../../../electron-app/shared/editing.mjs";
import {parseGlb,primitives,baseImages} from "../../../electron-app/shared/glb.mjs";
const manifest=JSON.parse(await fs.readFile(process.argv[2],"utf8"));
const readImage=async x=>({...x,data:new Uint8ClampedArray(await fs.readFile(x.file))});
const stamps={};
for(const [id,file] of Object.entries(manifest.stamps||{}))stamps[id]=await readImage(file);
let context=null,materials=[];
if(manifest.glb){const b=await fs.readFile(manifest.glb);const glb=parseGlb(b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength));
 if(needsGeometry(manifest.plan))context=meshContext(primitives(glb));materials=baseImages(glb);}
for(const t of manifest.textures){
 const image=await readImage(t);
 // 형상이 필요 없는 3D 편집(색·보정만)도 같은 함수를 지난다. 브러시 영역이 없으면 형상을 읽지 않는다.
 const texture=manifest.glb?{materials:materials.find(x=>x.index===t.index)?.materials||[]}:null;
 const output=renderEdit(image,manifest.plan,{stamps,context:manifest.glb?context||meshContext([]):null,texture});
 await fs.writeFile(t.file,output.data);t.width=output.width;t.height=output.height;
}
await fs.writeFile(process.argv[2],JSON.stringify(manifest));
