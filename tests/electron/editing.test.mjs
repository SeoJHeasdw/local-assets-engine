import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {
  defaults,adjustPixels,frameImage,overlayImage,projectOverlay,migratePlan,renderEdit,colorLayer,stampLayer,
  outputToSource,sourceToOutput,frameLayout,imageRegionMask,meshContext,texturedFaces,surfaceRegionMask,stampFaces,needsGeometry,
} from '../../electron-app/shared/editing.mjs';
import {buildJobRequest} from '../../electron-app/shared/format.mjs';

const solid=(width,height,color)=>({width,height,data:new Uint8ClampedArray(width*height*4).map((_,i)=>color[i%4])});
const pixel=(image,x,y)=>Array.from(image.data.slice((y*image.width+x)*4,(y*image.width+x)*4+4));

test('identity preserves pixels and color replacement keeps alpha and unmatched colors',()=>{
 const p=new Uint8ClampedArray([180,80,40,128,20,20,20,0]);
 assert.deepEqual(adjustPixels(p,defaults()),p);
 const plan={...defaults(),recolor:{enabled:true,from:'#b45028',to:'#2850b4',tolerance:.05}};
 const out=adjustPixels(p,plan);assert.deepEqual(Array.from(out),[40,80,180,128,20,20,20,0]);assert.equal(p[0],180);
});
test('rotation, reflection and crop preserve coordinate order',()=>{
 const im={width:2,height:1,data:new Uint8ClampedArray([255,0,0,255,0,0,255,255])};
 const v=frameImage(im,{turns:1});assert.equal(v.width,1);assert.equal(v.height,2);assert.deepEqual(v.data,im.data);
 const f=frameImage(im,{flipX:true});assert.deepEqual(Array.from(f.data.slice(0,4)),[0,0,255,255]);
 assert.equal(frameImage(solid(8,4,[1,2,3,255]),{crop:'square'}).width,4);
});
test('screen positions map back to the original through rotation, flip, free crop and margins',()=>{
 for (const turns of [0,1,2,3]) for (const flipX of [false,true]) {
  const frame={turns,flipX,crop:{x:.1,y:.2,w:.6,h:.5},padding:.1};
  for (const [u,v] of [[.3,.4],[.55,.6],[.2,.35]]) {
   const [x,y]=sourceToOutput(frame,200,100,u,v),[bu,bv]=outputToSource(frame,200,100,x,y);
   assert.ok(Math.abs(bu-u)<1e-9&&Math.abs(bv-v)<1e-9,`turns ${turns} flip ${flipX}`);
  }
 }
 const layout=frameLayout({crop:{x:0,y:0,w:.5,h:1},padding:.25,width:100},40,20);
 assert.deepEqual([layout.crop.w,layout.crop.h,layout.pad,layout.cw,layout.ch,layout.ow,layout.oh],[20,20,5,30,30,100,100]);
});
test('free crop adds a colored margin and resizes without dark fringes',()=>{
 const image=solid(4,4,[200,10,10,255]);
 const out=frameImage(image,{crop:{x:0,y:0,w:.5,h:.5},padding:.5,background:'#00ff00',width:12});
 assert.equal(out.width,12);assert.equal(out.height,12);
 assert.deepEqual(pixel(out,0,0),[0,255,0,255]);assert.deepEqual(pixel(out,6,6),[200,10,10,255]);
 const transparent=frameImage(solid(2,2,[255,0,0,255]),{padding:.5,width:2});
 assert.deepEqual(pixel(transparent,0,0).slice(0,3),[255,0,0]);
});
test('transparent stamp composites over transparent pixels without dark fringes',()=>{
 const im={width:4,height:4,data:new Uint8ClampedArray(64)},stamp={width:1,height:1,data:new Uint8ClampedArray([255,0,0,128])};
 overlayImage(im,stamp,{enabled:true,x:.5,y:.5,size:.5,rotation:0,opacity:1});
 assert.deepEqual(Array.from(im.data.slice(20,24)),[255,0,0,128]);assert.equal(im.data[3],0);
});
test('a brushed region limits color changes and erasing restores the rest',()=>{
 const image=solid(20,10,[180,80,40,255]);
 const strokes=[[.25,.5,.2,1],[.35,.5,.05,0]];
 const mask=imageRegionMask(20,10,{strokes});
 assert.equal(mask[5*20+4],255);assert.equal(mask[5*20+15],0);assert.ok(mask[5*20+7]<10);
 const plan={...defaults(),layers:[colorLayer({from:'#b45028',to:'#2850b4',tolerance:.05,region:{strokes}})]};
 const out=renderEdit(image,plan);
 assert.deepEqual(pixel(out,4,5),[40,80,180,255]);assert.deepEqual(pixel(out,15,5),[180,80,40,255]);assert.ok(pixel(out,7,5)[0]>170);
});
test('fill mode paints a region regardless of its colors and keeps relative shading',()=>{
 const image={width:2,height:1,data:new Uint8ClampedArray([100,100,100,255,200,200,200,255])};
 const out=renderEdit(image,{...defaults(),layers:[colorLayer({mode:'fill',to:'#ff0000'})]});
 assert.ok(out.data[0]<out.data[4]&&out.data[1]===0&&out.data[5]===0);
});
test('layers apply in order, hidden layers are skipped and legacy plans render the same',()=>{
 const image=solid(8,8,[255,255,255,255]),stamp={width:1,height:1,data:new Uint8ClampedArray([0,0,255,255])};
 const legacy={...defaults(),recolor:{enabled:true,from:'#ffffff',to:'#ff0000',tolerance:.1},overlay:{enabled:true,x:.5,y:.5,size:.25,rotation:0,opacity:1}};
 const migrated=migratePlan(legacy);
 assert.deepEqual(migrated.layers.map(l=>l.type),['color','stamp']);assert.equal(migrated.recolor,undefined);
 const out=renderEdit(image,legacy,{stamps:{[migrated.layers[1].id]:stamp}});
 assert.deepEqual(pixel(out,4,4),[0,0,255,255]);assert.deepEqual(pixel(out,0,0),[255,0,0,255]);
 migrated.layers[0].visible=false;
 assert.deepEqual(pixel(renderEdit(image,migrated,{stamps:{[migrated.layers[1].id]:stamp}}),0,0),[255,255,255,255]);
 assert.equal(migratePlan({frame:{crop:'square'}}).frame.crop,'square');
 assert.equal(needsGeometry(migrated),true);assert.equal(needsGeometry(defaults()),false);
});
test('each color layer selects and shades from the original while later layers composite above it',()=>{
 const image=solid(1,1,[255,0,0,128]);
 const blue=colorLayer({id:'blue',from:'#ff0000',to:'#0000ff',tolerance:.01});
 const green=colorLayer({id:'green',from:'#ff0000',to:'#00ff00',tolerance:.01});
 assert.deepEqual(pixel(renderEdit(image,{...defaults(),layers:[blue,green]}),0,0),[0,255,0,128]);
 assert.deepEqual(pixel(renderEdit(image,{...defaults(),layers:[blue,{...green,from:'#0000ff'}]}),0,0),[0,0,255,128]);
 const gray={width:2,height:1,data:new Uint8ClampedArray([60,60,60,255,180,180,180,255])};
 const fillBlue=colorLayer({id:'fill-blue',mode:'fill',to:'#0000ff'}),fillGreen=colorLayer({id:'fill-green',mode:'fill',to:'#00ff00'});
 assert.deepEqual(renderEdit(gray,{...defaults(),layers:[fillBlue,fillGreen]}).data,renderEdit(gray,{...defaults(),layers:[fillGreen]}).data);
});
test('region highlighting is applied after all color selection and tone corrections',()=>{
 const image=solid(1,1,[255,0,0,123]);
 const plan={...defaults(),brightness:.7,layers:[colorLayer({id:'a',from:'#ff0000',to:'#0000ff',tolerance:.01}),colorLayer({id:'b',from:'#ff0000',to:'#00ff00',tolerance:.01})]};
 const ordinary=renderEdit(image,plan),expected=new Uint8ClampedArray(ordinary.data);
 for(let k=0;k<3;k++)expected[k]=expected[k]*.55+[255,179,92][k]*.45;
 assert.deepEqual(renderEdit(image,plan,{highlight:'a'}).data,expected);
 assert.deepEqual(image.data,new Uint8ClampedArray([255,0,0,123]));
});

// 20×20cm 판 앞에 3cm 튀어나온 10cm 자물쇠. 옆면이 판과 정점을 공유한다.
function chestWithLock() {
 const positions=[],uv=[],indices=[];
 const add=(p,t)=>{positions.push(...p);uv.push(...t);return positions.length/3-1;};
 const front=(x,y)=>[(x+.5)*.5,.5-y];
 const outer=[[-.5,-.5],[.5,-.5],[.5,.5],[-.5,.5]].map(([x,y])=>add([x,y,0],front(x,y)));
 const inner=[[-.05,-.05],[.05,-.05],[.05,.05],[-.05,.05]].map(([x,y])=>add([x,y,0],front(x,y)));
 for (let i=0;i<4;i++){const j=(i+1)%4;indices.push(outer[i],outer[j],inner[j],outer[i],inner[j],inner[i]);}
 const lockUv=(x,y)=>[.5+(x+.05)*5,(.05-y)*5];
 const top=[[-.05,-.05],[.05,-.05],[.05,.05],[-.05,.05]].map(([x,y])=>add([x,y,.03],lockUv(x,y)));
 indices.push(top[0],top[1],top[2],top[0],top[2],top[3]);
 for (let i=0;i<4;i++){const j=(i+1)%4;const a=add([...positions.slice(inner[i]*3,inner[i]*3+2),0],[.6,.6]),b=add([...positions.slice(inner[j]*3,inner[j]*3+2),0],[.9,.6]),c=add([...positions.slice(top[j]*3,top[j]*3+2),.03],[.9,.9]),d=add([...positions.slice(top[i]*3,top[i]*3+2),.03],[.6,.9]);indices.push(a,b,c,a,c,d);}
 return {positions:new Float64Array(positions),uv:new Float64Array(uv),indices:new Float64Array(indices),material:0};
}
test('projected logo follows the front surface without painting its reverse side',()=>{
 const im={width:32,height:16,data:new Uint8ClampedArray(32*16*4).fill(255)};
 const stamp={width:1,height:1,data:new Uint8ClampedArray([255,0,0,255])};
 const front={positions:new Float64Array([-.5,-.5,0,.5,-.5,0,.5,.5,0,-.5,.5,0]),uv:new Float64Array([0,1,.5,1,.5,0,0,0]),indices:new Float64Array([0,1,2,0,2,3])};
 const back={...front,uv:new Float64Array([.5,1,1,1,1,0,.5,0]),indices:new Float64Array([0,2,1,0,3,2])};
 projectOverlay(im,stamp,{enabled:true,position:[0,0,0],normal:[0,0,1],size:.5,rotation:0,opacity:1,depth:.02},[front,back]);
 assert.deepEqual(Array.from(im.data.slice((8*32+8)*4,(8*32+8)*4+4)),[255,0,0,255]);
 assert.deepEqual(Array.from(im.data.slice((8*32+24)*4,(8*32+24)*4+4)),[255,255,255,255]);
});
test('a logo on a small raised part stays on that part when clipped to the connected surface',()=>{
 const context=meshContext([chestWithLock()]),faces=texturedFaces(context,[0]),stamp={width:1,height:1,data:new Uint8ClampedArray([255,0,0,255])};
 const layer=stampLayer({position:[0,0,.03],normal:[0,0,1],size:.4,depth:.05});
 const projection=stampFaces(context,faces,{...layer,clip:'projection'},stamp),connected=stampFaces(context,faces,layer,stamp);
 assert.ok(projection.size>2);assert.deepEqual([...connected.keys()].sort(),[8,9]);
 const texture=solid(64,64,[255,255,255,255]);
 const render=(clip)=>renderEdit(texture,{...defaults(),layers:[{...layer,clip}]},{stamps:{[layer.id]:stamp},context,texture:{materials:[0]}});
 const chestTexel=[19,32],lockTexel=[48,12];
 assert.deepEqual(pixel(render('projection'),...chestTexel),[255,0,0,255]);
 const clipped=render('connected');
 assert.deepEqual(pixel(clipped,...chestTexel),[255,255,255,255]);assert.deepEqual(pixel(clipped,...lockTexel),[255,0,0,255]);
});
test('a surface brush masks only nearby texels and bleeds into the UV margin',()=>{
 const context=meshContext([chestWithLock()]),faces=texturedFaces(context,[0]);
 const mask=surfaceRegionMask(context,faces,64,64,{strokes:[[-.35,0,0,.1,1]]});
 const at=(x,y)=>mask[Math.floor(.5-y)*0+Math.floor((.5-y)*64)*64+Math.floor((x+.5)*.5*64)];
 assert.equal(at(-.35,0),255);assert.equal(at(.35,0),0);
 const plan={...defaults(),layers:[colorLayer({mode:'fill',to:'#0000ff',region:{strokes:[[-.35,0,0,.1,1]]}})]};
 const out=renderEdit(solid(64,64,[200,200,200,255]),plan,{context,texture:{materials:[0]}});
 assert.equal(pixel(out,Math.floor(.075*64),32)[0],0);assert.equal(pixel(out,Math.floor(.425*64),32)[0],200);
});
test('sparse brush lookup preserves the pre-optimization paint, erase and UV margin pixels',()=>{
 const plane={positions:new Float64Array([-.5,-.5,0,.5,-.5,0,.5,.5,0,-.5,.5,0]),uv:new Float64Array([.1,.1,.9,.1,.9,.9,.1,.9]),indices:new Float64Array([0,1,2,0,2,3]),material:0};
 const strokes=[[0,0,0,.3,1],[.1,0,0,.1,0],[-.25,.1,0,.15,1],[0,-.2,0,.12,0]];
 // These full mask digests were recorded from the former dense lookup before
 // changing candidate enumeration; pixel weights and UV dilation must stay exact.
 const expected=new Map([[16,'008771fbf2bbcdc7adaa849414ecd4651ce42e40b3654545f6b40febbd7fadb2'],[32,'f7b93358baee8bf5a672c5bc92f8c850abe28851b68a4ecf721a12bcf9461687'],[64,'b8ef756ff1b27acc972b309e3feefb320860f9c12cbbc5f0a5c57763b02cbb28']]);
 for(const [size,digest] of expected){const c=meshContext([plane]),mask=surfaceRegionMask(c,texturedFaces(c,[0]),size,size,{strokes});assert.equal(createHash('sha256').update(mask).digest('hex'),digest);}
});
test('one fine stroke on one large triangle finishes without scanning empty grid cells',()=>{
 const url=new URL('../../electron-app/shared/editing.mjs',import.meta.url).href;
 const script=`import {meshContext,texturedFaces,surfaceRegionMask} from ${JSON.stringify(url)};
 const c=meshContext([{positions:new Float64Array([0,0,0,1,0,0,0,1,0]),uv:new Float64Array([0,0,1,0,0,1]),indices:new Float64Array([0,1,2]),material:0}]);
 const mask=surfaceRegionMask(c,texturedFaces(c,[0]),16,16,{strokes:[[.21875,.21875,0,.0001,1]]});console.log(mask[3*16+3]);`;
 const result=spawnSync(process.execPath,['--input-type=module','-e',script],{encoding:'utf8',timeout:1500});
 assert.ifError(result.error);assert.equal(result.status,0,result.stderr);assert.equal(result.stdout.trim(),'255');
});
test('concept-first is explicit even with one candidate, imported sources work',()=>{
 const form={kind:'3d',source:'text',preset:'prop-3d',subject:'chest',count:1,workflow:'concept',gameFaces:0};
 assert.equal(buildJobRequest(form).recipe,'image');
 const direct=buildJobRequest({...form,count:4,workflow:'direct'});assert.equal(direct.recipe,'text-to-3d');assert.equal(direct.params.count,1);assert.equal(direct.params.gameFaces,0);
 assert.deepEqual(buildJobRequest({...form,source:'image',uploadId:'abc'}).params.uploadId,'abc');
});
