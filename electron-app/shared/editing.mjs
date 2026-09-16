// One pixel/projection implementation for interactive preview and measured saves.
export const clamp = (v, a=0, b=1) => Math.max(a, Math.min(b, v));
export const defaults = () => ({brightness:1, contrast:1, saturation:1, recolor:null,
  frame:{turns:0, flipX:false, crop:"original"}, overlay:null, metallic:1, roughness:1});
export const rgb = (hex) => (hex.match(/[a-f\d]{2}/gi) || []).slice(0,3).map(x=>parseInt(x,16));
export const hex = (p) => `#${Array.from(p).slice(0,3).map(x=>Math.round(x).toString(16).padStart(2,"0")).join("")}`;
export function adjustPixels(data, plan) {
  const result = new Uint8ClampedArray(data);
  const from = plan.recolor && rgb(plan.recolor.from), to = plan.recolor && rgb(plan.recolor.to);
  const radius = Math.max(1, (plan.recolor?.tolerance ?? .2)*441.673);
  for(let i=0;i<result.length;i+=4) {
    let r=data[i],g=data[i+1],b=data[i+2];
    if(from && plan.recolor.enabled) {
      const distance=Math.hypot(r-from[0],g-from[1],b-from[2]);
      const blend=clamp((radius-distance)/(radius*.3));
      const light=clamp((.2126*r+.7152*g+.0722*b)/Math.max(20,.2126*from[0]+.7152*from[1]+.0722*from[2]),0,2);
      r=r*(1-blend)+clamp(to[0]*light,0,255)*blend;
      g=g*(1-blend)+clamp(to[1]*light,0,255)*blend;
      b=b*(1-blend)+clamp(to[2]*light,0,255)*blend;
    }
    const l=.2126*r+.7152*g+.0722*b;
    const sat=plan.saturation??1, contrast=plan.contrast??1, bright=plan.brightness??1;
    for(const [c,v] of [r,g,b].entries()) result[i+c]=clamp(((l+(v-l)*sat)-127.5)*contrast*bright+127.5*bright,0,255);
  }
  return result;
}
export function frameImage(image, frame={}) {
  const turns=((frame.turns||0)%4+4)%4, ow=image.width, oh=image.height;
  const rw=turns%2?oh:ow, rh=turns%2?ow:oh;
  const ratio={square:1,portrait:3/4,landscape:4/3}[frame.crop];
  let width=rw,height=rh;
  if(ratio) {if(rw/rh>ratio) width=Math.round(rh*ratio); else height=Math.round(rw/ratio);}
  const offx=Math.floor((rw-width)/2),offy=Math.floor((rh-height)/2);
  const data=new Uint8ClampedArray(width*height*4);
  for(let y=0;y<height;y++)for(let x=0;x<width;x++){
    const tx=frame.flipX?rw-1-(x+offx):x+offx,ty=y+offy;
    const [sx,sy]=turns===1?[ty,oh-1-tx]:turns===2?[ow-1-tx,oh-1-ty]:turns===3?[ow-1-ty,tx]:[tx,ty];
    data.set(image.data.subarray((sy*ow+sx)*4,(sy*ow+sx)*4+4),(y*width+x)*4);
  }
  return {data,width,height};
}
export function composite(data,index,stamp,u,v,opacity=1) {
  if(u<0||u>1||v<0||v>1)return;
  const x=clamp(u*stamp.width-.5,0,stamp.width-1),y=clamp(v*stamp.height-.5,0,stamp.height-1);
  const ix=Math.floor(x),iy=Math.floor(y),fx=x-ix,fy=y-iy;
  const c=[0,0,0];let a=0;
  for(let j=0;j<2;j++)for(let i=0;i<2;i++){
    const weight=(i?fx:1-fx)*(j?fy:1-fy),s=(Math.min(iy+j,stamp.height-1)*stamp.width+Math.min(ix+i,stamp.width-1))*4;
    const alpha=stamp.data[s+3]/255*weight; a+=alpha;
    for(let k=0;k<3;k++)c[k]+=stamp.data[s+k]*alpha;
  }
  if(a===0)return;
  const alpha=a*opacity,back=data[index+3]/255,oa=alpha+back*(1-alpha);
  for(let k=0;k<3;k++)data[index+k]=(c[k]*opacity+data[index+k]*back*(1-alpha))/oa;
  data[index+3]=oa*255;
}
export function overlayImage(image,stamp,placement) {
  if(!stamp||!placement?.enabled)return image;
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
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const dot=(a,b)=>a.reduce((s,x,i)=>s+x*b[i],0);
const sub=(a,b)=>a.map((v,i)=>v-b[i]);
const norm=a=>{const l=Math.hypot(...a);return a.map(v=>v/l);};
export function projectOverlay(image,stamp,placement,primitives) {
  if(!stamp||!placement?.enabled||!placement.position)return image;
  const n=norm(placement.normal),ref=Math.abs(n[1])>.95?[0,0,-1]:[0,1,0];
  const right=norm(cross(ref,n)),up=cross(n,right);
  const a=placement.rotation*Math.PI/180,c=Math.cos(a),s=Math.sin(a);
  const r=right.map((v,i)=>v*c+up[i]*s),t=up.map((v,i)=>v*c-right[i]*s);
  const width=placement.size,height=width*stamp.height/stamp.width,depth=placement.depth??width*.2;
  for(const primitive of primitives){
    const {positions,uv,indices}=primitive;if(!uv)continue;
    for(let f=0;f<indices.length;f+=3){
      const ids=[indices[f],indices[f+1],indices[f+2]];
      const p=ids.map(i=>Array.from(positions.subarray(i*3,i*3+3)));
      const normal=cross(sub(p[1],p[0]),sub(p[2],p[0]));
      if(dot(normal,n)<.2*Math.hypot(...normal))continue;
      const projected=p.map(v=>{const d=sub(v,placement.position);return [dot(d,r)/width+.5,.5-dot(d,t)/height,dot(d,n)];});
      if([0,1].some(k=>projected.every(v=>v[k]<0)||projected.every(v=>v[k]>1))||projected.every(v=>v[2]>depth)||projected.every(v=>v[2]<-depth))continue;
      const q=ids.map(i=>[uv[i*2]*image.width-.5,uv[i*2+1]*image.height-.5]);
      const [a0,b0,c0]=q, dx=b0[0]-a0[0],dy=b0[1]-a0[1],ex=c0[0]-a0[0],ey=c0[1]-a0[1],det=dx*ey-dy*ex;
      if(Math.abs(det)<1e-9)continue;
      const xmin=Math.max(0,Math.ceil(Math.min(...q.map(v=>v[0])))),xmax=Math.min(image.width-1,Math.floor(Math.max(...q.map(v=>v[0]))));
      const ymin=Math.max(0,Math.ceil(Math.min(...q.map(v=>v[1])))),ymax=Math.min(image.height-1,Math.floor(Math.max(...q.map(v=>v[1]))));
      for(let y=ymin;y<=ymax;y++)for(let x=xmin;x<=xmax;x++){
        const b=((x-a0[0])*ey-(y-a0[1])*ex)/det,d=(dx*(y-a0[1])-dy*(x-a0[0]))/det,a1=1-b-d;
        if(a1<-.00001||b<-.00001||d<-.00001)continue;
        const v=[0,1,2].map(k=>a1*projected[0][k]+b*projected[1][k]+d*projected[2][k]);
        if(Math.abs(v[2])<=depth)composite(image.data,(y*image.width+x)*4,stamp,v[0],v[1],placement.opacity);
      }
    }
  }
  return image;
}
