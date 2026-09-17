"""Create an immutable, independently reviewable image or mesh edit version."""
import copy
import json
import re
import shutil
import sys
from pathlib import Path
from ..jobs import JobNotFound
from ..presets import PresetError
from ..uploads import resolve_upload
from .base import Recipe, float_param, int_param, bool_param, choice_param

MAX_LAYERS = 32
MAX_STROKES = 20000
LAYER_ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


def color(value):
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value): raise PresetError('올바른 색상 값이 필요합니다.')
    return value


def vector(value):
    if not isinstance(value,list) or len(value)!=3: raise PresetError('표면 위치를 다시 선택해 주세요.')
    return [float_param({'v':x},'v',0,-10000,10000) for x in value]


def text(value, limit):
    return ' '.join(str(value or '').split())[:limit]


def frame_plan(raw):
    frame=raw.get('frame') or {}
    if not isinstance(frame,dict): raise PresetError('자르기 설정을 확인해 주세요.')
    crop=frame.get('crop')
    if isinstance(crop,dict):
        crop={k:float_param(crop,k,0 if k in 'xy' else 1,0,1) for k in ('x','y','w','h')}
        if crop['w']<.01 or crop['h']<.01 or crop['x']+crop['w']>1.0001 or crop['y']+crop['h']>1.0001:
            raise PresetError('자르기 영역을 다시 지정해 주세요.')
    elif crop not in (None,''):
        crop=choice_param(frame,'crop','original',['original','square','portrait','landscape','wide'])
        crop=None if crop=='original' else crop
    else: crop=None
    width=frame.get('width')
    return {'turns':int_param(frame,'turns',0,0,3),'flipX':bool_param(frame,'flipX',False),'crop':crop,
            'ratio':choice_param(frame,'ratio','original',['original','free','square','portrait','landscape','wide']),
            'padding':float_param(frame,'padding',0,0,.5),
            'background':color(frame['background']) if frame.get('background') else None,
            'width':int_param(frame,'width',0,16,4096) if width not in (None,'',0) else None}


def strokes(region, kind):
    if region in (None,{}): return None
    if not isinstance(region,dict) or not isinstance(region.get('strokes'),list): raise PresetError('칠한 영역을 확인해 주세요.')
    items=region['strokes']
    if len(items)>MAX_STROKES: raise PresetError('칠한 영역이 너무 복잡합니다. 영역을 비우고 다시 칠해 주세요.')
    size=4 if kind=='image' else 5
    cleaned=[]
    for item in items:
        if not isinstance(item,list) or len(item)!=size: raise PresetError('칠한 영역을 확인해 주세요.')
        values=[float_param({'v':v},'v',0,-10000,10000) for v in item]
        if not 0<values[-2]<=1000 or values[-1] not in (0,1): raise PresetError('칠한 영역을 확인해 주세요.')
        cleaned.append(values[:-1]+[int(values[-1])])
    return {'strokes':cleaned}


def legacy_layers(raw):
    layers=[]
    recolor=raw.get('recolor')
    if recolor:
        if not isinstance(recolor,dict): raise PresetError('색상 교체 설정을 확인해 주세요.')
        color(recolor.get('from'));color(recolor.get('to'))
        if bool_param(recolor,'enabled',False):
            layers.append({'id':'color-1','type':'color','name':'색 바꾸기','mode':'match',**{k:recolor[k] for k in ('from','to','tolerance') if k in recolor}})
    overlay=raw.get('overlay')
    if overlay and not isinstance(overlay,dict): raise PresetError('로고 설정을 확인해 주세요.')
    if overlay and overlay.get('enabled'):
        layers.append({**overlay,'id':'stamp-1','type':'stamp','name':overlay.get('text') or '로고','clip':'projection'})
    return layers


def layer_plan(item, kind, store, stamps):
    if not isinstance(item,dict): raise PresetError('레이어 설정을 확인해 주세요.')
    layer_id=item.get('id')
    if not isinstance(layer_id,str) or not LAYER_ID.fullmatch(layer_id): raise PresetError('레이어 이름표가 올바르지 않습니다.')
    base={'id':layer_id,'name':text(item.get('name'),60),'visible':bool_param(item,'visible',True),'locked':bool_param(item,'locked',False)}
    if item.get('type')=='color':
        return {**base,'type':'color','mode':choice_param(item,'mode','match',['match','fill']),
                'from':color(item.get('from','#bd8858')),'to':color(item.get('to')),
                'tolerance':float_param(item,'tolerance',.2,.01,1),'region':strokes(item.get('region'),kind)}
    if item.get('type')!='stamp': raise PresetError('알 수 없는 레이어입니다.')
    stamps[layer_id]=resolve_upload(store,item.get('uploadId'))
    layer={**base,'type':'stamp','uploadId':item['uploadId'],'opacity':float_param(item,'opacity',1,0,1),
           'rotation':float_param(item,'rotation',0,-180,180),'size':float_param(item,'size',.25,.001,1000),
           'text':str(item.get('text') or '')[:160]}
    if item.get('textColor'): layer['textColor']=color(item['textColor'])
    if kind=='mesh':
        layer.update(position=vector(item.get('position')),normal=vector(item.get('normal')),
                     depth=float_param(item,'depth',.03,.0001,100),clip=choice_param(item,'clip','connected',['connected','projection']))
        if sum(v*v for v in layer['normal'])<.001: raise PresetError('표면 방향을 다시 선택해 주세요.')
    else: layer.update(x=float_param(item,'x',.5,-1,2),y=float_param(item,'y',.5,-1,2))
    return layer


def normalize_plan(raw, kind, store):
    """Validate an edit plan. Returns (plan, {stamp layer id: upload path})."""
    if not isinstance(raw,dict): raise PresetError('편집 설정이 필요합니다.')
    plan={k:float_param(raw,k,1,*bounds) for k,bounds in {
        'brightness':(.1,2),'contrast':(.1,2),'saturation':(0,2),'metallic':(0,1),'roughness':(.05,1)}.items()}
    plan['frame']=frame_plan(raw) if kind=='image' else {}
    items=raw.get('layers')
    if items is None or (items==[] and (raw.get('recolor') or raw.get('overlay'))): items=legacy_layers(raw)
    if not isinstance(items,list): raise PresetError('레이어 설정을 확인해 주세요.')
    if len(items)>MAX_LAYERS: raise PresetError(f'레이어는 {MAX_LAYERS}개까지 쓸 수 있습니다.')
    stamps={}
    plan['layers']=[layer_plan(item,kind,store,stamps) for item in items]
    if len({layer['id'] for layer in plan['layers']})!=len(plan['layers']): raise PresetError('레이어 이름표가 겹칩니다.')
    return plan,stamps


def prepare(params, presets, store):
    source=params.get('source')
    if not isinstance(source,dict): raise PresetError('편집할 에셋을 선택해 주세요.')
    try:
        job=store.load(str(source.get('jobId','')))
        asset=next(a for a in job['assets'] if a['id']==source.get('assetId') and a['kind'] in ('image','mesh'))
        replace_edits = bool_param(params, 'replaceEdits', False)
        base = asset['meta'].get('editBaseFile') if replace_edits else None
        path=store.resolve_file(job['id'],base or asset['file'])
    except (JobNotFound,StopIteration) as error: raise PresetError('편집할 에셋을 찾을 수 없습니다.') from error
    plan,stamps=normalize_plan(params.get('plan'),asset['kind'],store)
    title=' '.join(str(params.get('name') or job['params'].get('subject') or job['title']).split())[:120]
    library={k:copy.deepcopy(asset[k]) for k in ('tags','collection') if asset.get(k)}
    return {'source':{'jobId':job['id'],'assetId':asset['id']},'inputPath':str(path),'kind':asset['kind'],
            'sourceMeta':copy.deepcopy(asset['meta']),'plan':plan,'stampPaths':{k:str(v) for k,v in stamps.items()},
            'library':library,'subject':title},f'{title} · 편집본'


def run(ctx):
    directory=ctx.dir/'edit';directory.mkdir()
    is_mesh=ctx.params['kind']=='mesh'
    source=directory/('source.glb' if is_mesh else 'source.png')
    shutil.copyfile(ctx.params['inputPath'],source)
    output=directory/('asset.glb' if is_mesh else 'asset.png')
    # 레이어마다 로고·문구 그림을 작업 폴더에 복사한다. 다시 열어 고칠 때 이 파일을 읽는다.
    stamps={}
    for layer_id,path in (ctx.params.get('stampPaths') or {}).items():
        stamps[layer_id]=directory/f'logo-{layer_id}.png'
        shutil.copyfile(path,stamps[layer_id])
    request=directory/'request.json';stats=directory/'stats.json'
    request.write_text(json.dumps({'input':str(source),'output':str(output),'stats':str(stats),
                                  'plan':ctx.params['plan'],'stamps':{k:str(v) for k,v in stamps.items()}}), 'utf-8')
    with ctx.stage('edit','편집 내용 저장') as stage:
        stage.run([sys.executable,'-m','local_assets_engine.tools.asset_edit','--request',request])
    result=json.loads(stats.read_text())
    meta=copy.deepcopy(ctx.params['sourceMeta'])
    for key in ('sourceStateFile','optimizedFile','optimizedBytes','rawFile','inspectionFile','error','editStampFile'):meta.pop(key,None)
    meta.update(source=ctx.params['source'],editPlan=ctx.params['plan'],label='편집본',variant='edited')
    meta['editBaseFile']=ctx.rel(source)
    meta['editStampFiles']={k:ctx.rel(v) for k,v in stamps.items()}
    if is_mesh:
        meta.setdefault('stats',{}).update(bytes=result['bytes'])
        preview=directory/'inspection/edited-0.png'
        with ctx.stage('inspect','편집 결과 확인') as stage:
            stage.run([sys.executable,'-m','local_assets_engine.tools.mesh_inspect','--output',directory/'inspection','--mesh',f'edited={output}'])
        meta['inspectionFile']=ctx.rel(directory/'inspection/edited-contact.png')
    else:
        meta.update(width=result['width'],height=result['height'],checks=result.get('checks',{}));preview=output
    record=ctx.add_asset(kind=ctx.params['kind'],role='final' if is_mesh else 'candidate',file=output,preview=preview,meta=meta)
    library=ctx.params.get('library') or {}
    if library:
        # 같은 에셋의 새 버전이므로 컬렉션과 태그는 이어받는다. 즐겨찾기는 사람이 버전마다 고른다.
        ctx.mutate(lambda job: next(a for a in job['assets'] if a['id']==record['id']).update(copy.deepcopy(library)))


def prepare_import(params,presets,store):
    path=resolve_upload(store,params.get('uploadId'))
    name=' '.join(str(params.get('name') or '가져온 이미지').split())[:120]
    return {'imagePath':str(path),'subject':name},name


def run_import(ctx):
    output=ctx.dir/'image.png';stats=ctx.dir/'stats.json';request=ctx.dir/'request.json'
    request.write_text(json.dumps({'import':True,'input':ctx.params['imagePath'],'output':str(output),'stats':str(stats)}))
    with ctx.stage('import','이미지 가져오기') as stage:
        stage.run([sys.executable,'-m','local_assets_engine.tools.asset_edit','--request',request])
    ctx.add_asset(kind='image',role='candidate',file=output,preview=output,meta=json.loads(stats.read_text()))

EDIT_ASSET=Recipe('edit-asset','에셋 편집',prepare,run)
IMPORT_IMAGE=Recipe('import-image','이미지 가져오기',prepare_import,run_import)
