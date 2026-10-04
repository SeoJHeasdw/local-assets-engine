// Local drafts survive closing the editor or restarting the app. Assets stay immutable.
let database;
function db(){database ||= new Promise((resolve,reject)=>{const request=indexedDB.open('assets-studio',1);request.onupgradeneeded=()=>request.result.createObjectStore('drafts');request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});return database;}
export async function readDraft(key){try{const database=await db();return await new Promise((resolve,reject)=>{const request=database.transaction('drafts').objectStore('drafts').get(key);request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);});}catch{return null;}}
export const sameDraftVersion=(a,b)=>Boolean(a&&b&&typeof a.generation==='string'&&a.generation&&Number.isSafeInteger(a.revision)&&a.generation===b.generation&&a.revision===b.revision);

// A reopened editor claims a new generation. Delayed writes from its predecessor
// may neither overwrite it nor move the revision of the same generation backwards.
export function canWriteDraft(current,value,previous=null){
  if(!current)return true;
  const before=current.draftVersion,after=value.draftVersion;
  if(!before)return previous===null;
  if(before.generation===after.generation)return before.revision<=after.revision;
  return Boolean(previous&&before.generation===previous.generation&&before.revision<=previous.revision);
}

async function changeDraft(key,change){
  try{
    const database=await db();
    return await new Promise((resolve,reject)=>{
      const tx=database.transaction('drafts','readwrite'),store=tx.objectStore('drafts');
      let changed=false;
      const request=store.get(key);
      request.onsuccess=()=>{try{changed=change(store,request.result);}catch{tx.abort();}};
      tx.oncomplete=()=>resolve(changed);
      tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error);
    });
  }catch{return false;}
}

export async function writeDraft(key,value,options){
  return changeDraft(key,(store,current)=>{
    if(options&&value&&!canWriteDraft(current,value,options.previous))return false;
    if(value)store.put(value,key);else store.delete(key);
    return true;
  });
}

// Read and delete in one transaction: completing an older save cannot discard
// a newer draft even when another editor has already persisted it.
export async function deleteDraft(key,expected){
  return changeDraft(key,(store,current)=>{
    if(!sameDraftVersion(current?.draftVersion,expected))return false;
    store.delete(key);return true;
  });
}
