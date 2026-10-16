"""Verify installed PE/metadata on disk. Does not open or invoke a game process."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deskrawl_assistant.il2cpp_metadata import MetadataInspector
from deskrawl_assistant.native_signatures import Signatures


def verify(game):
    profile = json.loads((ROOT/'data/game-actions-profile.json').read_text(encoding='utf-8'))
    catalog = json.loads((ROOT/'data/progression-catalog.json').read_text(encoding='utf-8'))
    assert catalog['sha256'] == profile['sha256']
    for name, expected in profile['sha256'].items():
        assert hashlib.sha256((game/name).read_bytes()).hexdigest() == expected, name
    metadata = MetadataInspector(game/'Deskrawl_Data/il2cpp_data/Metadata/global-metadata.dat', game/'GameAssembly.dll')
    signatures = object.__new__(Signatures); signatures.pe = metadata.pe
    module = signatures.find_module('Assembly-CSharp.dll')
    image = next(row for row in metadata.images() if row['name']=='Assembly-CSharp.dll')
    classes = {metadata.defs[index]['name']:index for index in range(image['start'],image['start']+image['count'])}
    for name, expected in profile['types'].items():
        index = classes[expected.get('name', name)]
        assert expected['typeDefinitionIndex'] == index, name
        actual = metadata.fields(index)
        field_signature = lambda rows:[(f['name'],f['rawRegistrationOffset'],f['token']) for f in rows]
        assert field_signature(actual) == field_signature(expected['fields']), name
    for key, expected in profile['methods'].items():
        name, method = key.rsplit('.',1)
        actual = next(m for m in metadata.methods(classes[expected.get('declaring_type', name)]) if m['token']==expected['token'])
        for field in ('name','isStatic','returnType','parameters'): assert actual[field] == expected[field], (key, field)
        rid = int(actual['token'],16) & 0xffffff
        pointer = metadata.pe.unpack('<Q',module['methods']+(rid-1)*8)[0]
        assert pointer-metadata.pe.base == expected['rva'], key
        raw = metadata.pe.raw(pointer); prefix = bytes.fromhex(expected['prefix'])
        assert metadata.pe.data[raw:raw+len(prefix)] == prefix, key
    assert catalog['soul_maximum'] == 2000
    assert catalog['soul_maximum']+sum(n['soul_capacity_per_rank']*n['max_points'] for n in catalog['life_skills']) == 3000
    print(json.dumps({'build':profile['build'],'types':len(profile['types']),'methods':len(profile['methods']),
                      'file_only':True,'game_process_opened':False},ensure_ascii=False))


if __name__=='__main__':verify(Path(sys.argv[1]) if len(sys.argv)>1 else Path(r'G:\SteamLibrary\steamapps\common\Deskrawl'))
