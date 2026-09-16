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


def color(value):
    if not isinstance(value,str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',value): raise PresetError('올바른 색상 값이 필요합니다.')
    return value


def vector(value):
    if not isinstance(value,list) or len(value)!=3: raise PresetError('표면 위치를 다시 선택해 주세요.')
    return [float_param({'v':x},'v',0,-10000,10000) for x in value]


def normalize_plan(raw, kind, store):
    if not isinstance(raw,dict): raise PresetError('편집 설정이 필요합니다.')
    plan={k:float_param(raw,k,1,*bounds) for k,bounds in {
        'brightness':(.1,2),'contrast':(.1,2),'saturation':(0,2),'metallic':(0,1),'roughness':(.05,1)}.items()}
    frame=raw.get('frame') or {}
    if not isinstance(frame,dict): raise PresetError('자르기 설정을 확인해 주세요.')
    plan['frame']={'turns':int_param(frame,'turns',0,0,3),'flipX':bool_param(frame,'flipX',False),
                   'crop':choice_param(frame,'crop','original',['original','square','portrait','landscape'])} if kind=='image' else {}
    recolor=raw.get('recolor')
    if recolor:
        if not isinstance(recolor,dict): raise PresetError('색상 교체 설정을 확인해 주세요.')
        plan['recolor']={'enabled':bool_param(recolor,'enabled',False),'from':color(recolor.get('from')),
                         'to':color(recolor.get('to')),'tolerance':float_param(recolor,'tolerance',.2,.01,1)}
    overlay=raw.get('overlay'); stamp=None
    if overlay and not isinstance(overlay,dict): raise PresetError('로고 설정을 확인해 주세요.')
    if overlay and overlay.get('enabled'):
        stamp=resolve_upload(store,overlay.get('uploadId'))
        item={'enabled':True,'uploadId':overlay['uploadId'],'opacity':float_param(overlay,'opacity',1,0,1),
              'rotation':float_param(overlay,'rotation',0,-180,180),'size':float_param(overlay,'size',.25,.001,1000),
              'text':str(overlay.get('text',''))[:160]}
        if overlay.get('textColor'): item['textColor']=color(overlay['textColor'])
        if kind=='mesh':
            item.update(position=vector(overlay.get('position')),normal=vector(overlay.get('normal')),
                        depth=float_param(overlay,'depth',.03,.0001,100))
            if sum(v*v for v in item['normal'])<.001: raise PresetError('표면 방향을 다시 선택해 주세요.')
        else: item.update(x=float_param(overlay,'x',.5,0,1),y=float_param(overlay,'y',.5,0,1))
        plan['overlay']=item
    return plan,stamp


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
    plan,stamp=normalize_plan(params.get('plan'),asset['kind'],store)
    title=' '.join(str(params.get('name') or job['params'].get('subject') or job['title']).split())[:120]
    return {'source':{'jobId':job['id'],'assetId':asset['id']},'inputPath':str(path),'kind':asset['kind'],
            'sourceMeta':copy.deepcopy(asset['meta']),'plan':plan,'stampPath':str(stamp) if stamp else None,
            'subject':title},f'{title} · 편집본'


def run(ctx):
    directory=ctx.dir/'edit';directory.mkdir()
    is_mesh=ctx.params['kind']=='mesh'
    source=directory/('source.glb' if is_mesh else 'source.png')
    shutil.copyfile(ctx.params['inputPath'],source)
    output=directory/('asset.glb' if is_mesh else 'asset.png')
    stamp=None
    if ctx.params.get('stampPath'):
        stamp=directory/'logo.png';shutil.copyfile(ctx.params['stampPath'],stamp)
    request=directory/'request.json';stats=directory/'stats.json'
    request.write_text(json.dumps({'input':str(source),'output':str(output),'stats':str(stats),
                                  'plan':ctx.params['plan'],'stamp':str(stamp) if stamp else None}), 'utf-8')
    with ctx.stage('edit','편집 내용 저장') as stage:
        stage.run([sys.executable,'-m','local_assets_engine.tools.asset_edit','--request',request])
    result=json.loads(stats.read_text())
    meta=copy.deepcopy(ctx.params['sourceMeta'])
    for key in ('sourceStateFile','optimizedFile','optimizedBytes','rawFile','inspectionFile','error'):meta.pop(key,None)
    meta.update(source=ctx.params['source'],editPlan=ctx.params['plan'],label='편집본',variant='edited')
    meta['editBaseFile']=ctx.rel(source)
    meta['editStampFile']=ctx.rel(stamp) if stamp else None
    if is_mesh:
        meta.setdefault('stats',{}).update(bytes=result['bytes'])
        preview=directory/'inspection/edited-0.png'
        with ctx.stage('inspect','편집 결과 확인') as stage:
            stage.run([sys.executable,'-m','local_assets_engine.tools.mesh_inspect','--output',directory/'inspection','--mesh',f'edited={output}'])
        meta['inspectionFile']=ctx.rel(directory/'inspection/edited-contact.png')
    else:
        meta.update(width=result['width'],height=result['height'],checks=result.get('checks',{}));preview=output
    ctx.add_asset(kind=ctx.params['kind'],role='final' if is_mesh else 'candidate',file=output,preview=preview,meta=meta)


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
