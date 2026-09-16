import test from 'node:test';
import assert from 'node:assert/strict';
import {defaults,adjustPixels,frameImage,overlayImage,projectOverlay} from '../../electron-app/shared/editing.mjs';
import {buildJobRequest} from '../../electron-app/shared/format.mjs';

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
});
test('transparent stamp composites over transparent pixels without dark fringes',()=>{
 const im={width:4,height:4,data:new Uint8ClampedArray(64)},stamp={width:1,height:1,data:new Uint8ClampedArray([255,0,0,128])};
 overlayImage(im,stamp,{enabled:true,x:.5,y:.5,size:.5,rotation:0,opacity:1});
 assert.deepEqual(Array.from(im.data.slice(20,24)),[255,0,0,128]);assert.equal(im.data[3],0);
});
test('projected logo follows the front surface without painting its reverse side',()=>{
 const im={width:32,height:16,data:new Uint8ClampedArray(32*16*4).fill(255)};
 const stamp={width:1,height:1,data:new Uint8ClampedArray([255,0,0,255])};
 const front={positions:new Float64Array([-.5,-.5,0,.5,-.5,0,.5,.5,0,-.5,.5,0]),uv:new Float64Array([0,1,.5,1,.5,0,0,0]),indices:new Float64Array([0,1,2,0,2,3])};
 const back={...front,uv:new Float64Array([.5,1,1,1,1,0,.5,0]),indices:new Float64Array([0,2,1,0,3,2])};
 projectOverlay(im,stamp,{enabled:true,position:[0,0,0],normal:[0,0,1],size:.5,rotation:0,opacity:1,depth:.02},[front,back]);
 assert.deepEqual(Array.from(im.data.slice((8*32+8)*4,(8*32+8)*4+4)),[255,0,0,255]);
 assert.deepEqual(Array.from(im.data.slice((8*32+24)*4,(8*32+24)*4+4)),[255,255,255,255]);
});
test('concept-first is explicit even with one candidate, imported sources work',()=>{
 const form={kind:'3d',source:'text',preset:'prop-3d',subject:'chest',count:1,workflow:'concept',gameFaces:0};
 assert.equal(buildJobRequest(form).recipe,'image');
 const direct=buildJobRequest({...form,count:4,workflow:'direct'});assert.equal(direct.recipe,'text-to-3d');assert.equal(direct.params.count,1);assert.equal(direct.params.gameFaces,0);
 assert.deepEqual(buildJobRequest({...form,source:'image',uploadId:'abc'}).params.uploadId,'abc');
});
