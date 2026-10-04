import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile,mkdtemp,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';

const root=path.resolve(fileURLToPath(new URL('../../',import.meta.url)));
const require=createRequire(import.meta.url),electron=require('electron');
const red=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAJUlEQVR4nGP8z8Dwn4ECwESJ5lEDIICJgULANGoAw2gYMFAeBgBYbQIe5lO8/AAAAABJRU5ErkJggg==','base64');
const thin=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAEAAAAAABCAYAAABeIWt8AAAAKUlEQVR4nO3BMQEAAAjAoNk/tMbwAWZrAwAAAAAAAAAAAAAAAAAAAPp0BIUCAAEs7YsAAAAASUVORK5CYII=','base64');

// Exercise the actual Worker, editor and IndexedDB transactions in an isolated,
// hidden window. The server serves only synthetic images and source modules.
const scenario=String.raw`(async()=>{
 const pause=ms=>new Promise(r=>setTimeout(r,ms));
 async function until(predicate){for(let n=0;n<100;n++){if(predicate())return;await pause(20);}throw Error('editor did not become ready');}
 const {createEditor}=await import('/electron-app/renderer/editor.js');
 const {readDraft,writeDraft,deleteDraft}=await import('/electron-app/renderer/drafts.js');
 const job={id:'20261004-000000-aaaa',title:'fixture',params:{subject:'fixture'}};
 const asset={id:'a01',kind:'image',file:'source.png',meta:{width:16,height:16}};
 const key=job.id+'/'+asset.id;
 function change(value){const input=document.querySelector('[data-prop="brightness"]');input.value=value;input.dispatchEvent(new Event('input',{bubbles:true}));}
 function setup(){
   let complete;
   const saved={id:'20261004-000000-bbbb',title:'saved',state:'done',assets:[{id:'a01',file:'source.png'}]};
   const opened=[];
   const api=async(url,options)=>{
     if(url==='/api/jobs'&&options?.method==='POST')return {id:saved.id};
     if(url==='/api/jobs/'+saved.id)return new Promise(r=>complete=r);
     throw Error('unexpected API '+url);
   };
   const editor=createEditor({api,refreshJobs:async()=>{},openAsset:async(...args)=>opened.push(args),makeMesh:()=>{},addToPrevizScene:()=>{}});
   return {editor,opened,async start(){document.querySelector('[data-e="save"]').click();await until(()=>complete);},finish(){complete(saved);}};
 }
 const worker=await new Promise((resolve,reject)=>{
   const w=new Worker('/electron-app/renderer/edit-preview.js',{type:'module'});
   const timer=setTimeout(()=>{w.terminate();reject(Error('worker timeout'));},2000);
   w.onmessage=({data})=>{clearTimeout(timer);w.terminate();resolve(data);};w.onerror=e=>reject(Error(e.message));
   w.postMessage({type:'init',kind:'image',url:'/files/'+job.id+'/thin.png'});
 });

 await writeDraft(key,null);
 const older=setup();await older.editor.open(job,asset);change('1.2');await until(()=>!document.querySelector('[data-e="save"]').disabled);
 await older.start();older.editor.close();await pause(30);await older.editor.open(job,asset);change('1.4');await pause(500);
 const before=await readDraft(key);older.finish();await pause(100);const after=await readDraft(key);
 older.editor.close();await pause(30);

 await writeDraft(key,null);
 const same=setup();await same.editor.open(job,asset);change('1.2');await until(()=>!document.querySelector('[data-e="save"]').disabled);
 await same.start();change('1.6');same.finish();await pause(100);
 const sameOpen=Boolean(document.querySelector('#editor-dialog')),sameDraft=await readDraft(key);
 same.editor.close();await pause(30);

 await writeDraft(key,null);
 const unchanged=setup();await unchanged.editor.open(job,asset);change('1.2');await until(()=>!document.querySelector('[data-e="save"]').disabled);
 await unchanged.start();unchanged.finish();await until(()=>unchanged.opened.length);
 const unchangedDraft=await readDraft(key),unchangedOpen=Boolean(document.querySelector('#editor-dialog'));

 const a={generation:'A',revision:0},b={generation:'B',revision:0};
 const casKey='cas';await writeDraft(casKey,{draftVersion:a},{previous:null});
 const claim=await writeDraft(casKey,{draftVersion:b},{previous:a});
 const oldWrite=await writeDraft(casKey,{draftVersion:{...a,revision:99}},{previous:null});
 const oldDelete=await deleteDraft(casKey,a);
 const next=await writeDraft(casKey,{draftVersion:{...b,revision:1}},{previous:a});
 const rollback=await writeDraft(casKey,{draftVersion:b},{previous:a});
 const exactDelete=await deleteDraft(casKey,{...b,revision:1});
 const {showVersionHistory}=await import('/electron-app/renderer/versions.js');
 const nodes=[['v','video'],['m','mesh'],['i','image']].map(([id,kind],index)=>({jobId:id,assetId:'a01',key:id+'/a01',kind,depth:index?1:0,relation:index?'변환':'영상 변환',missingParent:!index,preview:null,summary:[],title:'<img id="untrusted-title">',createdAt:'2026-10-04T00:00:00+09:00'}));
 const versionApi=async()=>({current:'v/a01',root:'v/a01',versions:nodes}),openedVersions=[];
 const history=await showVersionHistory(versionApi,'v','a01',(...args)=>openedVersions.push(args));
 const missingParent=history.querySelector('.version-text strong').textContent;
 const kinds=[...history.querySelectorAll('.version-text small:first-of-type')].map(el=>el.textContent.split(' · ')[0]);
 const titleInjected=Boolean(history.querySelector('#untrusted-title'));
 history.querySelector('[data-version-index="1"]').click();await pause(20);
 const failure=await showVersionHistory(versionApi,'v','a01',()=>{throw Error('synthetic version open error');});
 failure.querySelector('[data-version-index="2"]').click();await pause(20);
 const errorToast=document.querySelector('#studio-toast').textContent;
 return {worker,before:before?.plan.brightness,after:after?.plan.brightness,sameOpen,sameBrightness:sameDraft?.plan.brightness,unchangedDraft:unchangedDraft??null,unchangedOpen,cas:{claim,oldWrite,oldDelete,next,rollback,exactDelete},versionUI:{missingParent,kinds,titleInjected,openedVersions,errorToast}};
})()`;

test('preview dimensions and draft generations survive delayed save completion',{timeout:20000},async()=>{
 const directory=await mkdtemp(path.join(tmpdir(),'lae-editor-test-'));
 const server=createServer(async(req,res)=>{
   try{
     if(req.url==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html><body></body></html>');return;}
     if(req.url.startsWith('/files/')){res.setHeader('Content-Type','image/png');res.end(req.url.endsWith('/thin.png')?thin:red);return;}
     const filename=path.resolve(root,'.'+new URL(req.url,'http://localhost').pathname);
     if(!filename.startsWith(root+path.sep)){res.writeHead(404).end();return;}
     res.setHeader('Content-Type',filename.endsWith('.js')||filename.endsWith('.mjs')?'text/javascript':'text/plain');
     res.end(await readFile(filename));
   }catch{res.writeHead(404).end();}
 });
 let child;
 try{
   await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
   const url='http://127.0.0.1:'+server.address().port+'/';
   const main=path.join(directory,'main.cjs');
   await writeFile(main,`const {app,BrowserWindow}=require('electron');app.setPath('userData',${JSON.stringify(path.join(directory,'profile'))});
 app.whenReady().then(async()=>{const w=new BrowserWindow({show:false,webPreferences:{sandbox:true,nodeIntegration:false}});try{await w.loadURL(${JSON.stringify(url)});console.log('RESULT '+JSON.stringify(await w.webContents.executeJavaScript(${JSON.stringify(scenario)})));}catch(e){console.error(e.stack);process.exitCode=1;}finally{w.destroy();app.quit();}});`);
   const env={...process.env};delete env.ELECTRON_RUN_AS_NODE;
   child=spawn(electron,[main],{env,stdio:['ignore','pipe','pipe']});
   let stdout='',stderr='';child.stdout.on('data',x=>stdout+=x);child.stderr.on('data',x=>stderr+=x);
   const timer=setTimeout(()=>child.kill('SIGKILL'),15000);
   const status=await new Promise((resolve,reject)=>{child.on('error',reject);child.on('exit',resolve);});clearTimeout(timer);
   assert.equal(status,0,stderr||stdout);
   const line=stdout.split('\n').find(x=>x.startsWith('RESULT '));assert.ok(line,stdout);
   const result=JSON.parse(line.slice(7));
   assert.deepEqual(result.worker,{type:'ready',width:1024,height:1,materials:[null]});
   assert.equal(result.before,1.4);assert.equal(result.after,1.4);
   assert.equal(result.sameOpen,true);assert.equal(result.sameBrightness,1.6);
   assert.equal(result.unchangedDraft,null);assert.equal(result.unchangedOpen,false);
   assert.deepEqual(result.cas,{claim:true,oldWrite:false,oldDelete:false,next:true,rollback:false,exactDelete:true});
   assert.equal(result.versionUI.missingParent,'영상 변환 · 이전 기록 없음');
   assert.deepEqual(result.versionUI.kinds,['영상','3D','2D']);
   assert.equal(result.versionUI.titleInjected,false);
   assert.deepEqual(result.versionUI.openedVersions,[['m','a01']]);
   assert.equal(result.versionUI.errorToast,'synthetic version open error');
 }finally{
   if(child&&child.exitCode===null)child.kill('SIGKILL');
   await new Promise(resolve=>server.close(resolve));
   await rm(directory,{recursive:true,force:true});
 }
});
