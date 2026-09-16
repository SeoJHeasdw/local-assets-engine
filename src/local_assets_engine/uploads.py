"""Validated local image imports. IDs never encode filesystem paths."""
import io
import re
import secrets
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from .presets import PresetError

MAX_UPLOAD = 20 * 1024 * 1024
ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')


def upload_dir(store): return store.root.parent / 'uploads'


def resolve_upload(store, upload_id):
    if not isinstance(upload_id, str) or not ID_PATTERN.fullmatch(upload_id):
        raise PresetError('이미지를 다시 골라 주세요.')
    path = upload_dir(store) / f'{upload_id}.png'
    if not path.is_file(): raise PresetError('가져온 이미지를 찾을 수 없습니다.')
    return path


def save_upload(store, data):
    if not data or len(data) > MAX_UPLOAD: raise PresetError('20MB 이하의 이미지를 골라 주세요.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {'PNG','JPEG','WEBP'}: raise ValueError()
            if image.width * image.height > 16_777_216: raise PresetError('이미지는 1,677만 픽셀 이하로 골라 주세요.')
            image.load()
            from PIL import ImageOps
            image = ImageOps.exif_transpose(image).convert('RGBA')
            upload_id=secrets.token_hex(16); path=upload_dir(store)/f'{upload_id}.png'
            path.parent.mkdir(parents=True,exist_ok=True)
            image.save(path)
            return {'id':upload_id,'width':image.width,'height':image.height,'url':f'/uploads/{upload_id}'}
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError) as error:
        raise PresetError('유효한 PNG·JPG·WEBP 이미지를 골라 주세요.') from error
