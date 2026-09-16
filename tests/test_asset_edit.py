import hashlib
import io
import json
from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from local_assets_engine.jobs import JobStore
from local_assets_engine.presets import PresetError
from local_assets_engine.recipes.edit_asset import EDIT_ASSET, IMPORT_IMAGE, normalize_plan
from local_assets_engine.runner import Runner
from local_assets_engine.uploads import save_upload,resolve_upload
from local_assets_engine.tools.asset_edit import read_glb
from local_assets_engine.workers.gltf_export import export_glb_with_texture


def image_bytes(color='red',size=(16,16)):
    b=io.BytesIO();Image.new('RGBA',size,color).save(b,format='PNG');return b.getvalue()


def test_uploads_only_accept_bounded_images_and_opaque_ids(tmp_path):
    store=JobStore(tmp_path/'jobs')
    upload=save_upload(store,image_bytes());assert resolve_upload(store,upload['id']).is_file()
    with pytest.raises(PresetError):resolve_upload(store,'../../secret')
    with pytest.raises(PresetError):save_upload(store,b'<script>bad</script>')
    with pytest.raises(PresetError):normalize_plan({'brightness':float('nan')},'image',store)
    with pytest.raises(PresetError):normalize_plan({'overlay':['bad']},'mesh',store)
    with pytest.raises(PresetError):normalize_plan({'recolor':{'from':'oops','to':'#000000'}},'image',store)


def test_import_and_edit_keep_original_and_record_measured_versions(tmp_path):
    store=JobStore(tmp_path/'jobs');runner=Runner(store,recipes={r.id:r for r in (IMPORT_IMAGE,EDIT_ASSET)})
    upload=save_upload(store,image_bytes())
    original=runner.run_job(runner.create('import-image',{'uploadId':upload['id'],'name':'red tile'})['id'])
    assert original['state']=='done',original['error']
    asset=original['assets'][0];source=store.resolve_file(original['id'],asset['file']);before=source.read_bytes()
    store.update(original['id'],lambda j:j['assets'][0].update(review='approved'))
    edited=runner.run_job(runner.create('edit-asset',{'source':{'jobId':original['id'],'assetId':asset['id']},
        'plan':{'recolor':{'enabled':True,'from':'#ff0000','to':'#0000ff','tolerance':.1},'frame':{'crop':'square'}}})['id'])
    assert edited['state']=='done',edited['error']
    result=edited['assets'][0];assert result['review']=='pending'
    with Image.open(store.resolve_file(edited['id'],result['file'])) as im:assert im.getpixel((5,5))==(0,0,255,255)
    assert source.read_bytes()==before and store.load(original['id'])['assets'][0]['review']=='approved'
    assert edited['stages'][0]['peakMemoryBytes']>0 and edited['stages'][0]['seconds']>0
    assert result['meta']['source']=={'jobId':original['id'],'assetId':asset['id']}


def test_mesh_edit_preserves_geometry_uv_and_pbr_while_replacing_texture(tmp_path):
    store=JobStore(tmp_path/'jobs');job=store.create('fixture',{'subject':'plane'},'plane')
    source=store.job_dir(job['id'])/'asset.glb'
    export_glb_with_texture(np.array([[0.,0.,0.],[1.,0.,0.],[0.,1.,0.]]),np.array([[0,1,2]]),
         np.array([[0.,0.],[1.,0.],[0.,1.]]),np.full((8,8,4),[255,0,0,255],dtype=np.uint8),
         np.full((8,8,3),[0,120,90],dtype=np.uint8),source,native_pbr=True)
    before=source.read_bytes();old_doc,old_bin=read_glb(source)
    store.update(job['id'],lambda j:j.update(state='done',assets=[{'id':'a01','kind':'mesh','role':'final','file':'asset.glb','review':'approved','meta':{'processingVersion':2,'sourceStateFile':'mesh/source.npz','stats':{'facesOut':1},'seed':7}}]))
    runner=Runner(store,recipes={'edit-asset':EDIT_ASSET})
    edited=runner.run_job(runner.create('edit-asset',{'source':{'jobId':job['id'],'assetId':'a01'},'plan':{'recolor':{'enabled':True,'from':'#ff0000','to':'#0000ff','tolerance':.1}}})['id'])
    assert edited['state']=='done',edited['error']
    asset=edited['assets'][0];doc,binary=read_glb(store.resolve_file(edited['id'],asset['file']))
    assert doc['accessors']==old_doc['accessors'] and doc['meshes']==old_doc['meshes']
    for accessor in doc['accessors']:
        a=old_doc['bufferViews'][accessor['bufferView']];b=doc['bufferViews'][accessor['bufferView']]
        assert old_bin[a['byteOffset']:a['byteOffset']+a['byteLength']]==binary[b['byteOffset']:b['byteOffset']+b['byteLength']]
    assert doc['materials']==old_doc['materials']
    assert source.read_bytes()==before and asset['review']=='pending'
    assert 'sourceStateFile' not in asset['meta']  # rebuilding old voxels would discard the edit
    assert store.resolve_file(edited['id'],asset['meta']['inspectionFile']).is_file()


def test_reopening_a_saved_edit_replays_settings_from_its_base(tmp_path):
    store=JobStore(tmp_path/'jobs');runner=Runner(store,recipes={r.id:r for r in (IMPORT_IMAGE,EDIT_ASSET)})
    uploaded=save_upload(store,image_bytes())
    a=runner.run_job(runner.create('import-image',{'uploadId':uploaded['id']})['id'])
    request={'source':{'jobId':a['id'],'assetId':'a01'},'plan':{'recolor':{'enabled':True,'from':'#ff0000','to':'#0000ff','tolerance':.1}}}
    b=runner.run_job(runner.create('edit-asset',request)['id'])
    request['source']={'jobId':b['id'],'assetId':'a01'};request['replaceEdits']=True
    request['plan']['recolor']['to']='#00ff00'
    c=runner.run_job(runner.create('edit-asset',request)['id'])
    assert c['state']=='done',c['error']
    with Image.open(store.resolve_file(c['id'],c['assets'][0]['file'])) as im: assert im.getpixel((5,5))==(0,255,0,255)
    with Image.open(store.resolve_file(b['id'],b['assets'][0]['file'])) as im: assert im.getpixel((5,5))==(0,0,255,255)
    assert c['assets'][0]['meta']['source']=={'jobId':b['id'],'assetId':'a01'}
