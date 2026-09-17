// One pixel/projection implementation for interactive preview and measured saves.
// 편집 설정(plan)은 전체 보정 + 레이어 목록이다. 색 레이어가 먼저 에셋 자체의 색을 바꾸고,
// 로고·문구 레이어가 그 위에 목록 순서대로 쌓인다. 미리보기(Web Worker)와 저장(Node)이 같은
// renderEdit를 부르므로 화면에서 본 결과와 파일이 어긋나지 않는다.
export const clamp = (v, a=0, b=1) => Math.max(a, Math.min(b, v));
export const MAX_LAYERS = 32;
export const MAX_OUTPUT = 4096;
export const CROP_RATIOS = {square:1, portrait:3/4, landscape:4/3, wide:16/9};
export const defaultFrame = () => ({turns:0, flipX:false, crop:null, ratio:"original", padding:0, background:null, width:null});
export const defaults = () => ({brightness:1, contrast:1, saturation:1, metallic:1, roughness:1, frame:defaultFrame(), layers:[]});
export const rgb = (hex) => (String(hex).match(/[a-f\d]{2}/gi) || []).slice(0,3).map(x=>parseInt(x,16));
export const hex = (p) => `#${Array.from(p).slice(0,3).map(x=>Math.round(x).toString(16).padStart(2,"0")).join("")}`;
const luminance = (r,g,b) => .2126*r+.7152*g+.0722*b;

// ---- 설정 ---------------------------------------------------------------------

let idCounter = 0;
export function layerId(type, existing=[]) {
  const taken = new Set(existing.map(layer=>layer.id));
  let id;
  do id = `${type}-${Date.now().toString(36).slice(-5)}${(idCounter++).toString(36)}`; while (taken.has(id));
  return id;
}
export const colorLayer = (fields={}) => ({id:"color-1", type:"color", name:"색 바꾸기", visible:true, locked:false,
  mode:"match", from:"#bd8858", to:"#5478a3", tolerance:.2, region:null, ...fields});
export const stampLayer = (fields={}) => ({id:"stamp-1", type:"stamp", name:"로고", visible:true, locked:false,
  size:.25, rotation:0, opacity:1, x:.5, y:.5, position:null, normal:null, depth:null, clip:"connected", ...fields});

// 예전 편집 설정(recolor 하나 + overlay 하나)을 레이어 목록으로 옮긴다. 저장된 편집본을
// 다시 열거나 예전 임시 편집을 복원할 때 같은 결과가 나와야 한다.
export function migratePlan(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const plan = {...defaults(), ...source};
  const frame = source.frame && typeof source.frame === "object" ? source.frame : {};
  plan.frame = {...defaultFrame(), ...frame};
  if (typeof frame.crop === "string") {
    plan.frame.ratio = frame.crop;
    plan.frame.crop = frame.crop === "original" ? null : frame.crop;
  }
  if (!Array.isArray(source.layers) || (!source.layers.length && (source.recolor || source.overlay))) {
    plan.layers = [];
    const recolor = source.recolor;
    if (recolor?.enabled) plan.layers.push(colorLayer({from:recolor.from, to:recolor.to, tolerance:recolor.tolerance ?? .2}));
    const overlay = source.overlay;
    if (overlay?.enabled) {
      const {enabled, ...rest} = overlay;
      plan.layers.push(stampLayer({...rest, name:overlay.text || "로고", clip:"projection"}));
    }
  } else plan.layers = source.layers.map(layer => ({...layer}));
  delete plan.recolor; delete plan.overlay;
  return plan;
}

// ---- 2D 구성: 회전·반전 → 자르기 → 여백 → 출력 크기 ------------------------------------

export function frameLayout(frame, width, height) {
  const f = {...defaultFrame(), ...(frame || {})};
  const turns = ((f.turns||0)%4+4)%4, rw = turns%2 ? height : width, rh = turns%2 ? width : height;
  let crop;
  if (f.crop && typeof f.crop === "object") {
    const x = Math.round(clamp(f.crop.x)*rw), y = Math.round(clamp(f.crop.y)*rh);
    crop = {x, y, w:Math.max(1, Math.min(rw-x, Math.round(clamp(f.crop.w)*rw))), h:Math.max(1, Math.min(rh-y, Math.round(clamp(f.crop.h)*rh)))};
  } else {
    const ratio = CROP_RATIOS[f.crop];
    let w = rw, h = rh;
    if (ratio) { if (rw/rh > ratio) w = Math.round(rh*ratio); else h = Math.round(rw/ratio); }
    crop = {x:Math.floor((rw-w)/2), y:Math.floor((rh-h)/2), w, h};
  }
  const pad = Math.round(clamp(f.padding||0, 0, .5)*Math.max(crop.w, crop.h));
  const cw = crop.w+2*pad, ch = crop.h+2*pad;
  let ow = cw, oh = ch;
  if (f.width) {
    ow = Math.max(1, Math.round(Math.min(MAX_OUTPUT, f.width)));
    oh = Math.max(1, Math.round(ow*ch/cw));
    if (oh > MAX_OUTPUT) { oh = MAX_OUTPUT; ow = Math.max(1, Math.round(oh*cw/ch)); }
  }
  return {turns, flipX:!!f.flipX, rw, rh, crop, pad, cw, ch, ow, oh};
}

// 회전된 공간의 정규화 좌표 ↔ 원본 정규화 좌표. 화면에서 찍은 위치를 원본에 남긴다.
export function rotatedToSource(turns, flipX, a, b) {
  const x = flipX ? 1-a : a;
  return turns===1 ? [b, 1-x] : turns===2 ? [1-x, 1-b] : turns===3 ? [1-b, x] : [x, b];
}
export function sourceToRotated(turns, flipX, u, v) {
  const [x, b] = turns===1 ? [1-v, u] : turns===2 ? [1-u, 1-v] : turns===3 ? [v, 1-u] : [u, v];
  return [flipX ? 1-x : x, b];
}
export function outputToSource(frame, width, height, x, y) {
  const l = frameLayout(frame, width, height);
  const a = (l.crop.x + x*l.cw - l.pad)/l.rw, b = (l.crop.y + y*l.ch - l.pad)/l.rh;
  return rotatedToSource(l.turns, l.flipX, a, b);
}
export function sourceToOutput(frame, width, height, u, v) {
  const l = frameLayout(frame, width, height), [a, b] = sourceToRotated(l.turns, l.flipX, u, v);
  return [(a*l.rw - l.crop.x + l.pad)/l.cw, (b*l.rh - l.crop.y + l.pad)/l.ch];
}

export function frameImage(image, frame={}, {resize=true}={}) {
  const l = frameLayout(frame, image.width, image.height), ow = image.width, oh = image.height;
  const data = new Uint8ClampedArray(l.cw*l.ch*4);
  const background = frame?.background ? [...rgb(frame.background), 255] : null;
  if (background) for (let i=0;i<data.length;i+=4) data.set(background, i);
  for (let y=0;y<l.crop.h;y++) for (let x=0;x<l.crop.w;x++) {
    const rx = x+l.crop.x, tx = l.flipX ? l.rw-1-rx : rx, ty = y+l.crop.y;
    const [sx, sy] = l.turns===1 ? [ty, oh-1-tx] : l.turns===2 ? [ow-1-tx, oh-1-ty] : l.turns===3 ? [ow-1-ty, tx] : [tx, ty];
    const s = (sy*ow+sx)*4, d = ((y+l.pad)*l.cw+x+l.pad)*4;
    if (background) blendOver(data, d, image.data[s], image.data[s+1], image.data[s+2], image.data[s+3]/255);
    else data.set(image.data.subarray(s, s+4), d);
  }
  const framed = {data, width:l.cw, height:l.ch};
  return resize && (l.ow !== l.cw || l.oh !== l.ch) ? resample(framed, l.ow, l.oh) : framed;
}

// 줄일 때는 칸 평균, 늘릴 때는 정수배면 최근접(픽셀 아트 보존), 아니면 쌍선형. 알파는 곱해서 섞는다.
export function resample(image, width, height) {
  const {width:sw, height:sh, data:src} = image, out = new Uint8ClampedArray(width*height*4);
  const fx = sw/width, fy = sh/height;
  if (fx >= 1 && fy >= 1) {
    for (let y=0;y<height;y++) for (let x=0;x<width;x++) {
      const x0 = Math.floor(x*fx), x1 = Math.max(x0+1, Math.floor((x+1)*fx)), y0 = Math.floor(y*fy), y1 = Math.max(y0+1, Math.floor((y+1)*fy));
      let r=0,g=0,b=0,a=0,n=0;
      for (let yy=y0;yy<y1;yy++) for (let xx=x0;xx<x1;xx++) { const s=(yy*sw+xx)*4, al=src[s+3]; r+=src[s]*al; g+=src[s+1]*al; b+=src[s+2]*al; a+=al; n++; }
      const d = (y*width+x)*4;
      if (a > 0) { out[d]=r/a; out[d+1]=g/a; out[d+2]=b/a; }
      out[d+3] = a/n;
    }
    return {data:out, width, height};
  }
  const integer = Number.isInteger(1/fx) && Number.isInteger(1/fy);
  for (let y=0;y<height;y++) for (let x=0;x<width;x++) {
    const d = (y*width+x)*4;
    if (integer) { const s=(Math.floor(y*fy)*sw+Math.floor(x*fx))*4; out.set(src.subarray(s, s+4), d); continue; }
    const px = clamp((x+.5)*fx-.5, 0, sw-1), py = clamp((y+.5)*fy-.5, 0, sh-1), ix = Math.floor(px), iy = Math.floor(py), ax = px-ix, ay = py-iy;
    let r=0,g=0,b=0,a=0;
    for (let j=0;j<2;j++) for (let i=0;i<2;i++) {
      const w = (i?ax:1-ax)*(j?ay:1-ay), s = (Math.min(iy+j, sh-1)*sw+Math.min(ix+i, sw-1))*4, al = src[s+3]*w;
      r+=src[s]*al; g+=src[s+1]*al; b+=src[s+2]*al; a+=al;
    }
    if (a > 0) { out[d]=r/a; out[d+1]=g/a; out[d+2]=b/a; }
    out[d+3] = a;
  }
  return {data:out, width, height};
}

// ---- 색 ------------------------------------------------------------------------

export function tonePixels(data, plan) {
  const sat=plan.saturation??1, contrast=plan.contrast??1, bright=plan.brightness??1;
  if (sat===1 && contrast===1 && bright===1) return data;
  for (let i=0;i<data.length;i+=4) {
    const r=data[i],g=data[i+1],b=data[i+2],l=luminance(r,g,b);
    data[i]=clamp(((l+(r-l)*sat)-127.5)*contrast*bright+127.5*bright,0,255);
    data[i+1]=clamp(((l+(g-l)*sat)-127.5)*contrast*bright+127.5*bright,0,255);
    data[i+2]=clamp(((l+(b-l)*sat)-127.5)*contrast*bright+127.5*bright,0,255);
  }
  return data;
}
// 예전 설정(recolor)도 받는다. 색 교체는 원래 색에서 고르므로 밝기·대비보다 먼저 적용한다.
export function adjustPixels(data, plan) {
  const result = new Uint8ClampedArray(data);
  if (plan.recolor?.enabled) recolorPixels(result, colorLayer(plan.recolor), null);
  return tonePixels(result, plan);
}

// match: 고른 색과 가까운 픽셀만, fill: 영역 전체. 둘 다 원래 명암을 유지한다. mask(0..255)가 있으면 그 안에서만 바꾼다.
export function recolorPixels(data, layer, mask) {
  const from = rgb(layer.from), to = rgb(layer.to), fill = layer.mode === "fill";
  const radius = Math.max(1, (layer.tolerance ?? .2)*441.673);
  let reference = Math.max(20, luminance(...from));
  if (fill) {
    let sum = 0, weight = 0;
    for (let i=0, p=0;i<data.length;i+=4, p++) { const w = (mask ? mask[p]/255 : 1)*data[i+3]/255; sum += luminance(data[i],data[i+1],data[i+2])*w; weight += w; }
    reference = weight > 0 ? Math.max(20, sum/weight) : reference;
  }
  for (let i=0, p=0;i<data.length;i+=4, p++) {
    const area = mask ? mask[p]/255 : 1;
    if (area <= 0) continue;
    const r=data[i],g=data[i+1],b=data[i+2];
    const blend = area*(fill ? 1 : clamp((radius-Math.hypot(r-from[0],g-from[1],b-from[2]))/(radius*.3)));
    if (blend <= 0) continue;
    const light = clamp(luminance(r,g,b)/reference, 0, 2);
    data[i]=r*(1-blend)+clamp(to[0]*light,0,255)*blend;
    data[i+1]=g*(1-blend)+clamp(to[1]*light,0,255)*blend;
    data[i+2]=b*(1-blend)+clamp(to[2]*light,0,255)*blend;
  }
}

// 브러시 획: 2D [u, v, 반지름(원본 가로 비율), 1=칠하기/0=지우기]. 획 순서대로 칠하고 지운다.
// region이 없으면 에셋 전체, 획 목록이 비어 있으면 아무 데도 바꾸지 않는다(칠한 곳만 선택 직후).
const FEATHER = .3;
const strokeWeight = (distance, radius) => clamp((radius-distance)/(radius*FEATHER));
export function imageRegionMask(width, height, region) {
  const strokes = region?.strokes;
  if (!Array.isArray(strokes)) return null;
  const mask = new Uint8Array(width*height);
  for (const [u, v, r, mode] of strokes) {
    const cx = u*width, cy = v*height, radius = Math.max(.5, r*width);
    for (let y=Math.max(0, Math.floor(cy-radius));y<Math.min(height, Math.ceil(cy+radius));y++)
      for (let x=Math.max(0, Math.floor(cx-radius));x<Math.min(width, Math.ceil(cx+radius));x++) {
        const w = strokeWeight(Math.hypot(x+.5-cx, y+.5-cy), radius)*255;
        if (w <= 0) continue;
        const p = y*width+x;
        mask[p] = mode ? Math.max(mask[p], w) : Math.min(mask[p], 255-w);
      }
  }
  return mask;
}

// ---- 로고·문구 합성 ------------------------------------------------------------------

export function sampleStamp(stamp, u, v, out) {
  if (u<0||u>1||v<0||v>1) return 0;
  const x=clamp(u*stamp.width-.5,0,stamp.width-1),y=clamp(v*stamp.height-.5,0,stamp.height-1);
  const ix=Math.floor(x),iy=Math.floor(y),fx=x-ix,fy=y-iy;
  let r=0,g=0,b=0,a=0;
  for(let j=0;j<2;j++)for(let i=0;i<2;i++){
    const weight=(i?fx:1-fx)*(j?fy:1-fy),s=(Math.min(iy+j,stamp.height-1)*stamp.width+Math.min(ix+i,stamp.width-1))*4;
    const alpha=stamp.data[s+3]/255*weight; a+=alpha;
    r+=stamp.data[s]*alpha; g+=stamp.data[s+1]*alpha; b+=stamp.data[s+2]*alpha;
  }
  if (a>0) { out[0]=r/a; out[1]=g/a; out[2]=b/a; }
  out[3]=a;
  return a;
}
export function blendOver(data, index, r, g, b, alpha) {
  if (alpha<=0) return;
  const back=data[index+3]/255,oa=alpha+back*(1-alpha);
  data[index]=(r*alpha+data[index]*back*(1-alpha))/oa;
  data[index+1]=(g*alpha+data[index+1]*back*(1-alpha))/oa;
  data[index+2]=(b*alpha+data[index+2]*back*(1-alpha))/oa;
  data[index+3]=oa*255;
}
export function composite(data,index,stamp,u,v,opacity=1) {
  const color=[0,0,0,0];
  if (sampleStamp(stamp,u,v,color)>0) blendOver(data,index,color[0],color[1],color[2],color[3]*opacity);
}
export function overlayImage(image,stamp,placement) {
  if(!stamp||!placement||placement.enabled===false||placement.visible===false)return image;
  const width=placement.size*image.width,height=width*stamp.height/stamp.width;
  const cx=placement.x*image.width,cy=placement.y*image.height,a=placement.rotation*Math.PI/180;
  const c=Math.cos(a),s=Math.sin(a),radius=Math.hypot(width,height)/2;
  for(let y=Math.max(0,Math.floor(cy-radius));y<Math.min(image.height,cy+radius);y++)
    for(let x=Math.max(0,Math.floor(cx-radius));x<Math.min(image.width,cx+radius);x++){
      const dx=x+.5-cx,dy=y+.5-cy;
      composite(image.data,(y*image.width+x)*4,stamp,(c*dx+s*dy)/width+.5,(-s*dx+c*dy)/height+.5,placement.opacity);
    }
  return image;
}

// ---- 3D 표면 ----------------------------------------------------------------------

const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
const norm=a=>{const l=Math.hypot(...a)||1;return a.map(v=>v/l);};

// glb.primitives()를 하나의 정점·면 배열로 합친다. 면 이웃·UV 덮개 같은 무거운 값은 필요할 때 한 번만 만든다.
export function meshContext(primitives) {
  let vertices=0, faces=0;
  for (const p of primitives) { vertices += p.positions.length/3; faces += p.indices.length/3; }
  const positions=new Float32Array(vertices*3), uvs=new Float32Array(vertices*2).fill(NaN), faceList=new Uint32Array(faces*3), materials=new Uint32Array(faces);
  let v=0, f=0;
  for (const p of primitives) {
    positions.set(p.positions, v*3);
    if (p.uv) uvs.set(p.uv, v*2);
    for (let i=0;i<p.indices.length;i+=3,f++) { faceList[f*3]=p.indices[i]+v; faceList[f*3+1]=p.indices[i+1]+v; faceList[f*3+2]=p.indices[i+2]+v; materials[f]=p.material; }
    v += p.positions.length/3;
  }
  // 면 법선(정규화 안 함)과 경계 상자. 1백만 면을 도는 반복에서 배열을 새로 만들지 않도록 미리 둔다.
  const normals=new Float32Array(faces*3), bounds=new Float32Array(faces*6);
  for (let i=0;i<faces;i++) {
    const a=faceList[i*3]*3,b=faceList[i*3+1]*3,c=faceList[i*3+2]*3;
    const e1x=positions[b]-positions[a],e1y=positions[b+1]-positions[a+1],e1z=positions[b+2]-positions[a+2];
    const e2x=positions[c]-positions[a],e2y=positions[c+1]-positions[a+1],e2z=positions[c+2]-positions[a+2];
    normals[i*3]=e1y*e2z-e1z*e2y; normals[i*3+1]=e1z*e2x-e1x*e2z; normals[i*3+2]=e1x*e2y-e1y*e2x;
    for (let k=0;k<3;k++) {
      bounds[i*6+k]=Math.min(positions[a+k],positions[b+k],positions[c+k]);
      bounds[i*6+3+k]=Math.max(positions[a+k],positions[b+k],positions[c+k]);
    }
  }
  return {positions, uvs, faces:faceList, materials, normals, bounds, vertexCount:vertices, faceCount:faces, cache:new Map()};
}
function cached(context, key, build) {
  if (!context.cache.has(key)) context.cache.set(key, build());
  return context.cache.get(key);
}
export function texturedFaces(context, materials) {
  return cached(context, `faces:${materials.join(",")}`, () => {
    const set=new Set(materials), list=[];
    for (let f=0;f<context.faceCount;f++) if (set.has(context.materials[f]) && !Number.isNaN(context.uvs[context.faces[f*3]*2])) list.push(f);
    return Uint32Array.from(list);
  });
}
const facesKey=(faceList)=>`${faceList.length}:${faceList[0]}:${faceList[faceList.length-1]}`;

// 텍셀 중심이 삼각형 안에 들 때 (x, y, 무게중심 좌표)로 부른다.
function rasterize(context, face, width, height, visit) {
  const {faces, uvs}=context, i0=faces[face*3], i1=faces[face*3+1], i2=faces[face*3+2];
  const ax=uvs[i0*2]*width-.5, ay=uvs[i0*2+1]*height-.5, bx=uvs[i1*2]*width-.5, by=uvs[i1*2+1]*height-.5, cx=uvs[i2*2]*width-.5, cy=uvs[i2*2+1]*height-.5;
  const dx=bx-ax, dy=by-ay, ex=cx-ax, ey=cy-ay, det=dx*ey-dy*ex;
  if (!(Math.abs(det)>=1e-9)) return;
  const xmin=Math.max(0,Math.ceil(Math.min(ax,bx,cx))), xmax=Math.min(width-1,Math.floor(Math.max(ax,bx,cx)));
  const ymin=Math.max(0,Math.ceil(Math.min(ay,by,cy))), ymax=Math.min(height-1,Math.floor(Math.max(ay,by,cy)));
  for (let y=ymin;y<=ymax;y++) for (let x=xmin;x<=xmax;x++) {
    const b=((x-ax)*ey-(y-ay)*ex)/det, c=(dx*(y-ay)-dy*(x-ax))/det, a=1-b-c;
    if (a<-1e-5||b<-1e-5||c<-1e-5) continue;
    visit(x, y, a, b, c);
  }
}

// 삼각형이 덮는 텍셀. 편집을 UV 섬 밖 여백으로 조금 번지게 해야 필터링 때 섬 경계에 원래 색 선이 보이지 않는다.
export function uvCoverage(context, faceList, width, height) {
  return cached(context, `cover:${facesKey(faceList)}:${width}x${height}`, () => {
    const cover=new Uint8Array(width*height);
    for (const face of faceList) rasterize(context, face, width, height, (x,y)=>{cover[y*width+x]=1;});
    return cover;
  });
}
// 칠한 텍셀 옆의 여백(덮이지 않은) 텍셀을 이웃 평균으로 채운다. 칠한 곳 둘레만 본다.
function dilate(values, channels, cover, width, height, filled, iterations=2) {
  let frontier=[];
  for (let p=0;p<filled.length;p++) if (filled[p]) frontier.push(p);
  for (let step=0;step<iterations && frontier.length;step++) {
    const candidates=new Set();
    for (const p of frontier) {
      const x=p%width, y=(p-x)/width;
      if (x>0) candidates.add(p-1); if (x<width-1) candidates.add(p+1);
      if (y>0) candidates.add(p-width); if (y<height-1) candidates.add(p+width);
    }
    const next=[], sum=new Float64Array(channels);
    for (const p of candidates) {
      if (cover[p] || filled[p]) continue;
      const x=p%width, y=(p-x)/width;
      sum.fill(0); let count=0;
      for (const q of [x>0?p-1:-1, x<width-1?p+1:-1, y>0?p-width:-1, y<height-1?p+width:-1]) {
        if (q<0 || !filled[q]) continue;
        for (let k=0;k<channels;k++) sum[k]+=values[q*channels+k];
        count++;
      }
      if (count) next.push([p, Array.from(sum, v=>v/count)]);
    }
    for (const [p, value] of next) { filled[p]=1; for (let k=0;k<channels;k++) values[p*channels+k]=value[k]; }
    frontier=next.map(([p])=>p);
  }
}

// 브러시 획: 3D [x, y, z, 반지름(m), 1=칠하기/0=지우기]. 모델 좌표의 구 안에 든 표면 텍셀만 남긴다.
export function surfaceRegionMask(context, faceList, width, height, region) {
  const strokes = region?.strokes;
  if (!Array.isArray(strokes)) return null;
  if (!strokes.length) return new Uint8Array(width*height);
  let maxRadius=1e-4;
  const low=[Infinity,Infinity,Infinity], high=[-Infinity,-Infinity,-Infinity];
  for (const s of strokes) {
    maxRadius=Math.max(maxRadius, s[3]);
    for (let k=0;k<3;k++) { low[k]=Math.min(low[k], s[k]-s[3]); high[k]=Math.max(high[k], s[k]+s[3]); }
  }
  const cell=maxRadius*2, grid=new Map(), key=(x,y,z)=>`${x},${y},${z}`;
  strokes.forEach((s, index) => {
    const id=key(Math.floor(s[0]/cell), Math.floor(s[1]/cell), Math.floor(s[2]/cell));
    if (!grid.has(id)) grid.set(id, []);
    grid.get(id).push(index);
  });
  const {positions, faces, bounds}=context, mask=new Float32Array(width*height), filled=new Uint8Array(width*height);
  for (const face of faceList) {
    const o=face*6;
    if (bounds[o+3]<low[0]||bounds[o]>high[0]||bounds[o+4]<low[1]||bounds[o+1]>high[1]||bounds[o+5]<low[2]||bounds[o+2]>high[2]) continue;
    const near=[];
    for (let x=Math.floor(bounds[o]/cell)-1;x<=Math.floor(bounds[o+3]/cell)+1;x++)
      for (let y=Math.floor(bounds[o+1]/cell)-1;y<=Math.floor(bounds[o+4]/cell)+1;y++)
        for (let z=Math.floor(bounds[o+2]/cell)-1;z<=Math.floor(bounds[o+5]/cell)+1;z++)
          for (const index of grid.get(key(x,y,z)) || []) {
            const s=strokes[index];
            if (s[0]+s[3]>=bounds[o]&&s[0]-s[3]<=bounds[o+3]&&s[1]+s[3]>=bounds[o+1]&&s[1]-s[3]<=bounds[o+4]&&s[2]+s[3]>=bounds[o+2]&&s[2]-s[3]<=bounds[o+5]) near.push(index);
          }
    if (!near.length) continue;
    const order=near.sort((a,b)=>a-b).map(i=>strokes[i]);
    const i0=faces[face*3]*3, i1=faces[face*3+1]*3, i2=faces[face*3+2]*3;
    rasterize(context, face, width, height, (x,y,a,b,c) => {
      const px=positions[i0]*a+positions[i1]*b+positions[i2]*c;
      const py=positions[i0+1]*a+positions[i1+1]*b+positions[i2+1]*c;
      const pz=positions[i0+2]*a+positions[i1+2]*b+positions[i2+2]*c;
      let w=0;
      for (const s of order) { const f=strokeWeight(Math.hypot(px-s[0],py-s[1],pz-s[2]), s[3]); w = s[4] ? Math.max(w,f) : Math.min(w,1-f); }
      const p=y*width+x; mask[p]=Math.max(mask[p], w); filled[p]=1;
    });
  }
  dilate(mask, 1, uvCoverage(context, faceList, width, height), width, height, filled);
  return Uint8Array.from(mask, v=>Math.round(v*255));
}

// 같은 위치의 정점(UV 이음매에서 갈라진 정점)을 하나로 보고, 정점마다 닿은 면 목록을 만든다.
function weldedNeighbors(context) {
  return cached(context, "neighbors", () => {
    const {positions, faces, vertexCount, faceCount}=context;
    let size=1; while (size<vertexCount*2) size*=2;
    const table=new Int32Array(size).fill(-1), weld=new Uint32Array(vertexCount);
    let lo=Infinity, hi=-Infinity;
    for (let i=0;i<positions.length;i++) { if (positions[i]<lo) lo=positions[i]; if (positions[i]>hi) hi=positions[i]; }
    const step=Math.max(1e-9, (hi-lo)*1e-6), q=new Int32Array(vertexCount*3);
    for (let v=0;v<vertexCount;v++) {
      const x=Math.round(positions[v*3]/step), y=Math.round(positions[v*3+1]/step), z=Math.round(positions[v*3+2]/step);
      q[v*3]=x; q[v*3+1]=y; q[v*3+2]=z;
      let slot=(Math.imul(x,73856093)^Math.imul(y,19349663)^Math.imul(z,83492791))&(size-1);
      while (table[slot]>=0) {
        const other=table[slot];
        if (q[other*3]===x&&q[other*3+1]===y&&q[other*3+2]===z) break;
        slot=(slot+1)&(size-1);
      }
      if (table[slot]<0) table[slot]=v;
      weld[v]=table[slot];
    }
    const starts=new Uint32Array(vertexCount+1);
    for (let f=0;f<faceCount*3;f++) starts[weld[faces[f]]+1]++;
    for (let v=0;v<vertexCount;v++) starts[v+1]+=starts[v];
    const offsets=starts.slice(), list=new Uint32Array(faceCount*3);
    for (let f=0;f<faceCount;f++) for (let k=0;k<3;k++) list[offsets[weld[faces[f*3+k]]]++]=f;
    return {weld, starts, list};
  });
}

export function pointTriangleDistance(p, a, b, c) {
  const sub=(u,v)=>[u[0]-v[0],u[1]-v[1],u[2]-v[2]], ab=sub(b,a), ac=sub(c,a), ap=sub(p,a);
  const d1=dot(ab,ap), d2=dot(ac,ap);
  if (d1<=0&&d2<=0) return Math.hypot(...ap);
  const bp=sub(p,b), d3=dot(ab,bp), d4=dot(ac,bp);
  if (d3>=0&&d4<=d3) return Math.hypot(...bp);
  const vc=d1*d4-d3*d2;
  if (vc<=0&&d1>=0&&d3<=0) { const t=d1/(d1-d3); return Math.hypot(...sub(p,[a[0]+ab[0]*t,a[1]+ab[1]*t,a[2]+ab[2]*t])); }
  const cp=sub(p,c), d5=dot(ab,cp), d6=dot(ac,cp);
  if (d6>=0&&d5<=d6) return Math.hypot(...cp);
  const vb=d5*d2-d1*d6;
  if (vb<=0&&d2>=0&&d6<=0) { const t=d2/(d2-d6); return Math.hypot(...sub(p,[a[0]+ac[0]*t,a[1]+ac[1]*t,a[2]+ac[2]*t])); }
  const va=d3*d6-d5*d4;
  if (va<=0&&d4-d3>=0&&d5-d6>=0) { const t=(d4-d3)/((d4-d3)+(d5-d6)); return Math.hypot(...sub(p,[b[0]+(c[0]-b[0])*t,b[1]+(c[1]-b[1])*t,b[2]+(c[2]-b[2])*t])); }
  const n=cross(ab,ac), length=Math.hypot(...n)||1;
  return Math.abs(dot(ap,n))/length;
}
function boxDistance(bounds, o, p) {
  const dx=Math.max(bounds[o]-p[0],0,p[0]-bounds[o+3]), dy=Math.max(bounds[o+1]-p[1],0,p[1]-bounds[o+4]), dz=Math.max(bounds[o+2]-p[2],0,p[2]-bounds[o+5]);
  return Math.hypot(dx,dy,dz);
}
function nearestFace(context, point, faceList) {
  const {positions, faces, bounds}=context, vertex=i=>[positions[i*3],positions[i*3+1],positions[i*3+2]];
  let best=-1, bestDistance=Infinity;
  for (const face of faceList) {
    if (boxDistance(bounds, face*6, point)>=bestDistance) continue;
    const distance=pointTriangleDistance(point, vertex(faces[face*3]), vertex(faces[face*3+1]), vertex(faces[face*3+2]));
    if (distance<bestDistance) { bestDistance=distance; best=face; }
  }
  return best;
}

function stampFrame(layer, stamp) {
  const n=norm(layer.normal), ref=Math.abs(n[1])>.95?[0,0,-1]:[0,1,0];
  const right=norm(cross(ref,n)), up=cross(n,right);
  const a=(layer.rotation||0)*Math.PI/180, c=Math.cos(a), s=Math.sin(a);
  const width=layer.size, height=width*stamp.height/stamp.width, depth=layer.depth ?? width*.2;
  return {n, r:right.map((v,i)=>v*c+up[i]*s), t:up.map((v,i)=>v*c-right[i]*s), width, height, depth, reach:Math.hypot(width,height)/2+depth};
}
// 표면에 로고를 평면 투영할 때 한 면이 받을지: 로고 방향을 보고, 사각형과 겹치고, 깊이 범위 안.
function stampAccepts(context, face, layer, frame) {
  const {positions, faces, normals, bounds}=context, o=layer.position;
  if (boxDistance(bounds, face*6, o)>frame.reach) return null;
  const nx=normals[face*3], ny=normals[face*3+1], nz=normals[face*3+2];
  if (nx*frame.n[0]+ny*frame.n[1]+nz*frame.n[2] < .2*Math.hypot(nx,ny,nz)) return null;
  const projected=[0,1,2].map(k=>{const i=faces[face*3+k]*3, d=[positions[i]-o[0],positions[i+1]-o[1],positions[i+2]-o[2]];
    return [dot(d,frame.r)/frame.width+.5, .5-dot(d,frame.t)/frame.height, dot(d,frame.n)];});
  if ([0,1].some(k=>projected.every(v=>v[k]<0)||projected.every(v=>v[k]>1))) return null;
  if (projected.every(v=>v[2]>frame.depth)||projected.every(v=>v[2]<-frame.depth)) return null;
  return projected;
}

// connected: 찍은 면에서 이어진 표면만 받는다. 자물쇠에 붙인 로고가 뒤의 상자 앞판으로 번지지 않는다.
// projection: 투영 범위 안의 모든 앞면(예전 방식).
export function stampFaces(context, faceList, layer, stamp) {
  const frame=stampFrame(layer, stamp), accepted=new Map();
  if (layer.clip !== "connected") {
    for (const face of faceList) { const projected=stampAccepts(context, face, layer, frame); if (projected) accepted.set(face, projected); }
    return accepted;
  }
  const allowed=cached(context, `allowed:${facesKey(faceList)}`, () => { const set=new Uint8Array(context.faceCount); for (const f of faceList) set[f]=1; return set; });
  const seed=nearestFace(context, layer.position, faceList);
  if (seed<0) return accepted;
  const {weld, starts, list}=weldedNeighbors(context), seen=new Uint8Array(context.faceCount), queue=[seed];
  seen[seed]=1;
  while (queue.length) {
    const face=queue.pop(), projected=stampAccepts(context, face, layer, frame);
    if (!projected && face!==seed) continue;
    if (projected) accepted.set(face, projected);
    for (let k=0;k<3;k++) {
      const v=weld[context.faces[face*3+k]];
      for (let i=starts[v];i<starts[v+1];i++) { const next=list[i]; if (!seen[next] && allowed[next]) { seen[next]=1; queue.push(next); } }
    }
  }
  return accepted;
}

export function projectStamp(image, stamp, layer, context, faceList) {
  if (!stamp||!layer?.position||!layer.normal) return image;
  const {width, height}=image, faces=stampFaces(context, faceList, layer, stamp);
  if (!faces.size) return image;
  const frame=stampFrame(layer, stamp), {uvs}=context;
  let ux0=Infinity, uy0=Infinity, ux1=-Infinity, uy1=-Infinity;
  for (const face of faces.keys()) for (let k=0;k<3;k++) {
    const i=context.faces[face*3+k]*2;
    ux0=Math.min(ux0,uvs[i]); ux1=Math.max(ux1,uvs[i]); uy0=Math.min(uy0,uvs[i+1]); uy1=Math.max(uy1,uvs[i+1]);
  }
  // 로고가 닿는 UV 범위만 버퍼로 잡는다. 4K 텍스처 전체를 복사하지 않는다.
  const bx0=Math.max(0,Math.floor(ux0*width)-3), by0=Math.max(0,Math.floor(uy0*height)-3);
  const bw=Math.min(width,Math.ceil(ux1*width)+3)-bx0, bh=Math.min(height,Math.ceil(uy1*height)+3)-by0;
  if (bw<=0||bh<=0) return image;
  const paint=new Float32Array(bw*bh*4), filled=new Uint8Array(bw*bh), color=[0,0,0,0];
  let painted=false;
  for (const [face, projected] of faces) rasterize(context, face, width, height, (x,y,a,b,c) => {
    const u=a*projected[0][0]+b*projected[1][0]+c*projected[2][0], v=a*projected[0][1]+b*projected[1][1]+c*projected[2][1], d=a*projected[0][2]+b*projected[1][2]+c*projected[2][2];
    if (Math.abs(d)>frame.depth || sampleStamp(stamp,u,v,color)<=0) return;
    const q=(y-by0)*bw+(x-bx0);
    paint.set(color, q*4); filled[q]=1; painted=true;
  });
  if (!painted) return image;
  // UV 섬 경계의 여백까지 번져 두어 필터링 때 로고 가장자리가 끊겨 보이지 않게 한다.
  const cover=uvCoverage(context, faceList, width, height), boxCover=new Uint8Array(bw*bh);
  for (let y=0;y<bh;y++) boxCover.set(cover.subarray((y+by0)*width+bx0, (y+by0)*width+bx0+bw), y*bw);
  dilate(paint, 4, boxCover, bw, bh, filled);
  const opacity=layer.opacity ?? 1;
  for (let y=0;y<bh;y++) for (let x=0;x<bw;x++) {
    const q=y*bw+x;
    if (filled[q]) blendOver(image.data, ((y+by0)*width+x+bx0)*4, paint[q*4], paint[q*4+1], paint[q*4+2], paint[q*4+3]*opacity);
  }
  return image;
}

// 예전 호출(로고 하나, primitives 목록)과 테스트를 위한 얇은 감싸기.
export function projectOverlay(image,stamp,placement,primitives) {
  if(!stamp||!placement||placement.enabled===false||!placement.position)return image;
  const context=meshContext(primitives);
  return projectStamp(image, stamp, {clip:"projection", ...placement}, context, texturedFaces(context, [...new Set(primitives.map(p=>p.material??0))]));
}

// ---- 한 번에 적용 -------------------------------------------------------------------

const HIGHLIGHT=[255,179,92];
function highlightMask(data, mask) {
  for (let i=0,p=0;i<data.length;i+=4,p++) {
    const w=(mask ? mask[p]/255 : 1)*.45;
    if (w<=0) continue;
    for (let k=0;k<3;k++) data[i+k]=data[i+k]*(1-w)+HIGHLIGHT[k]*w;
  }
}

// image: 2D는 원본, 3D는 색 텍스처({width,height,data}). texture는 3D에서 {materials}.
// stamps: 레이어 id → {width,height,data}. highlight: 영역을 주황으로 보여 줄 색 레이어 id.
// cache: 미리보기에서 브러시 영역을 다시 계산하지 않도록 넘기는 Map.
// 순서: 색 레이어(원래 색에서 고름) → 밝기·대비·채도 → 2D 구성 → 로고·문구 레이어.
export function renderEdit(image, rawPlan, {stamps={}, context=null, texture=null, highlight=null, resize=true, cache=null}={}) {
  const plan=migratePlan(rawPlan), mesh=Boolean(context);
  const base={width:image.width, height:image.height, data:new Uint8ClampedArray(image.data)};
  const faceList=mesh ? texturedFaces(context, texture?.materials || [0]) : null;
  const used=new Set();
  for (const layer of plan.layers) {
    if (layer.type!=="color" || layer.visible===false) continue;
    let mask=null;
    if (Array.isArray(layer.region?.strokes)) {
      const key=`${mesh ? facesKey(faceList) : "image"}:${base.width}x${base.height}:${JSON.stringify(layer.region.strokes)}`;
      used.add(key);
      mask=cache?.get(key) || (mesh ? surfaceRegionMask(context, faceList, base.width, base.height, layer.region) : imageRegionMask(base.width, base.height, layer.region));
      cache?.set(key, mask);
    }
    recolorPixels(base.data, layer, mask);
    if (layer.id===highlight) highlightMask(base.data, mask);
  }
  if (cache) for (const key of cache.keys()) if (!used.has(key) && key.startsWith(mesh ? facesKey(faceList) : "image")) cache.delete(key);
  tonePixels(base.data, plan);
  const output=mesh ? base : frameImage(base, plan.frame, {resize});
  for (const layer of plan.layers) {
    if (layer.type!=="stamp" || layer.visible===false || !stamps[layer.id]) continue;
    if (mesh) projectStamp(output, stamps[layer.id], layer, context, faceList);
    else overlayImage(output, stamps[layer.id], layer);
  }
  return output;
}

// 레이어가 표면 형상을 읽어야 하는지(3D 저장 때 형상 버퍼를 풀지 결정).
export function needsGeometry(rawPlan) {
  return migratePlan(rawPlan).layers.some(layer => layer.visible!==false && (layer.type==="stamp" || layer.region?.strokes?.length));
}
