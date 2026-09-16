// Read-only geometry/image access. Saving retains every geometry buffer verbatim.
export function parseGlb(buffer) {
  const view=new DataView(buffer);
  if(view.getUint32(0,true)!==0x46546c67||view.getUint32(4,true)!==2)throw Error("GLB 2 파일이 필요합니다.");
  let doc,bin;for(let p=12;p<buffer.byteLength;){const len=view.getUint32(p,true),type=view.getUint32(p+4,true);p+=8;
    if(type===0x4e4f534a)doc=JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,p,len)));
    if(type===0x004e4942)bin=new Uint8Array(buffer,p,len);p+=len;}
  if(!doc||!bin)throw Error("메시 데이터가 없는 GLB입니다.");return {doc,bin};
}
export function accessor(glb,id) {
  const a=glb.doc.accessors[id],v=glb.doc.bufferViews[a.bufferView];
  if(a.sparse)throw Error("희소 메시 속성은 표면 편집을 지원하지 않습니다.");
  const widths={SCALAR:1,VEC2:2,VEC3:3,VEC4:4},types={5126:[4,"getFloat32"],5125:[4,"getUint32"],5123:[2,"getUint16"],5121:[1,"getUint8"]};
  const [bytes,read]=types[a.componentType]||[];if(!read)throw Error("지원하지 않는 메시 좌표 형식입니다.");
  const n=widths[a.type],out=new Float64Array(a.count*n),data=new DataView(glb.bin.buffer,glb.bin.byteOffset,glb.bin.byteLength);
  for(let i=0;i<a.count;i++)for(let j=0;j<n;j++)out[i*n+j]=data[read]((v.byteOffset||0)+(a.byteOffset||0)+i*(v.byteStride||bytes*n)+j*bytes,true);
  return out;
}
const identity=()=>[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
function multiply(a,b){const out=new Array(16).fill(0);for(let c=0;c<4;c++)for(let r=0;r<4;r++)for(let k=0;k<4;k++)out[c*4+r]+=a[k*4+r]*b[c*4+k];return out;}
function matrix(node){if(node.matrix)return node.matrix;const [x,y,z,w]=node.rotation||[0,0,0,1],s=node.scale||[1,1,1],t=node.translation||[0,0,0];
  return [(1-2*y*y-2*z*z)*s[0],(2*x*y+2*z*w)*s[0],(2*x*z-2*y*w)*s[0],0,
    (2*x*y-2*z*w)*s[1],(1-2*x*x-2*z*z)*s[1],(2*y*z+2*x*w)*s[1],0,
    (2*x*z+2*y*w)*s[2],(2*y*z-2*x*w)*s[2],(1-2*x*x-2*y*y)*s[2],0,...t,1];}
export function primitives(glb) {
  const list=[];
  function walk(id,parent){const node=glb.doc.nodes[id],m=multiply(parent,matrix(node));
    if(node.skin!==undefined)throw Error("리깅된 메시는 표면 편집을 지원하지 않습니다.");
    for(const p of (glb.doc.meshes?.[node.mesh]?.primitives||[])){
      if((p.mode??4)!==4)continue;
      if(p.extensions?.KHR_draco_mesh_compression)throw Error("압축 전 품질본을 선택해 주세요.");
      const positions=accessor(glb,p.attributes.POSITION);
      for(let i=0;i<positions.length;i+=3){const v=Array.from(positions.subarray(i,i+3));for(let k=0;k<3;k++)positions[i+k]=m[k]*v[0]+m[4+k]*v[1]+m[8+k]*v[2]+m[12+k];}
      const uv=p.attributes.TEXCOORD_0===undefined?null:accessor(glb,p.attributes.TEXCOORD_0);
      const indices=p.indices===undefined?Float64Array.from({length:positions.length/3},(_,i)=>i):accessor(glb,p.indices);
      list.push({positions,uv,indices,material:p.material??0});
    }
    for(const child of node.children||[])walk(child,m);
  }
  for(const node of glb.doc.scenes?.[glb.doc.scene??0]?.nodes||[])walk(node,identity());return list;
}
export function baseImages(glb) {
  const list=new Map();
  for(const [material,m] of (glb.doc.materials||[]).entries()){
    const info=m.pbrMetallicRoughness?.baseColorTexture;if(!info)continue;
    if(info.texCoord>0||info.extensions?.KHR_texture_transform)throw Error("변형된 UV는 표면 편집을 지원하지 않습니다.");
    const index=glb.doc.textures[info.index].source,image=glb.doc.images[index],view=glb.doc.bufferViews[image.bufferView];
    if(!view)throw Error("텍스처가 포함된 GLB를 선택해 주세요.");
    if(!list.has(index))list.set(index,{index,materials:[],bytes:glb.bin.slice(view.byteOffset||0,(view.byteOffset||0)+view.byteLength),mime:image.mimeType});
    list.get(index).materials.push(material);
  }
  return [...list.values()];
}
