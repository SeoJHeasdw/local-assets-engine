"""Measured, lossless-geometry surface editing. Pixel math is shared with UI."""
from __future__ import annotations
import argparse
import io
import json
import struct
import shutil
import subprocess
from pathlib import Path
from PIL import Image
from ..paths import find_tool


def read_glb(path):
    raw = Path(path).read_bytes()
    if raw[:4] != b'glTF':
        raise ValueError('GLB 파일이 필요합니다.')
    document, binary = None, None
    offset = 12
    while offset < len(raw):
        length, kind = struct.unpack_from('<II', raw, offset)
        offset += 8
        chunk = raw[offset:offset + length]
        if kind == 0x4e4f534a: document = json.loads(chunk)
        if kind == 0x004e4942: binary = chunk
        offset += length
    if document is None or binary is None: raise ValueError('GLB 데이터가 없습니다.')
    return document, binary


def write_glb(path, doc, binary, replacements):
    chunks = bytearray()
    for index, view in enumerate(doc['bufferViews']):
        data = replacements.get(index, binary[view.get('byteOffset', 0):view.get('byteOffset', 0) + view['byteLength']])
        view['byteOffset'], view['byteLength'] = len(chunks), len(data)
        chunks.extend(data)
        chunks.extend(b'\0' * (-len(chunks) % 4))
    doc['buffers'][0]['byteLength'] = len(chunks)
    header = json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode()
    header += b' ' * (-len(header) % 4)
    Path(path).write_bytes(struct.pack('<III', 0x46546c67, 2, 28+len(header)+len(chunks)) +
                          struct.pack('<II', len(header), 0x4e4f534a) + header +
                          struct.pack('<II', len(chunks), 0x004e4942) + chunks)


def rgba_file(image, file, **extras):
    image = image.convert('RGBA')
    file.write_bytes(image.tobytes())
    return {'file': str(file), 'width': image.width, 'height': image.height, **extras}


def edit(source, output, plan, work, stamps=None):
    work.mkdir(parents=True, exist_ok=True)
    is_mesh = source.suffix.lower() == '.glb'
    manifest = {'plan': plan, 'textures': [], 'stamps': {}}
    for layer_id, stamp in (stamps or {}).items():
        with Image.open(stamp) as image: manifest['stamps'][layer_id] = rgba_file(image, work / f'stamp-{layer_id}.rgba')
    if is_mesh:
        doc, binary = read_glb(source)
        if doc.get('animations') or doc.get('skins'):
            raise ValueError('애니메이션·리깅 메시는 표면 편집을 지원하지 않습니다.')
        indices = set()
        for material in doc.get('materials', []):
            pbr = material.get('pbrMetallicRoughness', {})
            info = pbr.get('baseColorTexture')
            if info: indices.add(doc['textures'][info['index']]['source'])
            pbr['metallicFactor'] = pbr.get('metallicFactor', 1) * plan['metallic']
            pbr['roughnessFactor'] = pbr.get('roughnessFactor', 1) * plan['roughness']
        if not indices: raise ValueError('색상 텍스처가 있는 GLB를 선택해 주세요.')
        manifest['glb'] = str(source)
        for index in sorted(indices):
            image = doc['images'][index]
            view = doc['bufferViews'][image['bufferView']]
            start = view.get('byteOffset', 0)
            with Image.open(io.BytesIO(binary[start:start+view['byteLength']])) as decoded:
                manifest['textures'].append(rgba_file(decoded, work / f'{index}.rgba', index=index))
    else:
        with Image.open(source) as image:
            manifest['textures'].append(rgba_file(image, work / 'image.rgba'))
    manifest_path = work / 'pixels.json'
    manifest_path.write_text(json.dumps(manifest), 'utf-8')
    node = find_tool('node')
    if not node: raise ValueError('Node.js가 필요합니다. 앱 설치 상태를 확인해 주세요.')
    subprocess.run([node, Path(__file__).with_name('edit_pixels.mjs'), manifest_path], check=True)
    manifest = json.loads(manifest_path.read_text())
    replacements = {}
    sizes = []
    for texture in manifest['textures']:
        image = Image.frombytes('RGBA', (texture['width'], texture['height']), Path(texture['file']).read_bytes())
        sizes.append(image.size)
        if is_mesh:
            stream = io.BytesIO(); image.save(stream, format='PNG')
            record = doc['images'][texture['index']]
            replacements[record['bufferView']] = stream.getvalue()
            record['mimeType'] = 'image/png'
        else: image.save(output)
    if is_mesh: write_glb(output, doc, binary, replacements)
    # Pixel scratch files are temporary copies, not user assets or checkpoints.
    shutil.rmtree(work, ignore_errors=True)
    return {'width': sizes[0][0], 'height': sizes[0][1], 'bytes': output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    source, output = Path(request['input']), Path(request['output'])
    if request.get('import'):
        with Image.open(source) as image: image.convert('RGBA').save(output)
        with Image.open(output) as image: stats={'width': image.width, 'height': image.height, 'bytes': output.stat().st_size}
    else:
        stats=edit(source, output, request['plan'], output.parent / 'scratch', request.get('stamps'))
    if output.suffix == '.png':
        from ..imaging import cutout_checks, has_transparency
        with Image.open(output) as image:
            stats['checks'] = cutout_checks(image) if has_transparency(image) else {}
    Path(request['stats']).write_text(json.dumps(stats), 'utf-8')
    print('@@progress '+json.dumps({'done':1,'total':1,'detail':'새 버전 저장 완료'}), flush=True)

if __name__ == '__main__': main()
