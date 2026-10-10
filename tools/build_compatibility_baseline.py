"""Generate a reviewed baseline; never run automatically for unknown builds."""
import argparse,hashlib,json,sys
from pathlib import Path
R=Path(__file__).resolve().parents[1];sys.path.insert(0,str(R))
from deskrawl_assistant.game_compatibility import ASSET_FILES,enum_contract,native_contract,profiles
from deskrawl_assistant.il2cpp_metadata import MetadataInspector
def main():
    p=argparse.ArgumentParser();p.add_argument('--game-dir',type=Path,default=Path(r'G:\SteamLibrary\steamapps\common\Deskrawl'));a=p.parse_args()
    known=profiles()['runtime-type-hints.json'];g=a.game_dir
    meta=g/'Deskrawl_Data/il2cpp_data/Metadata/global-metadata.dat';dll=g/'GameAssembly.dll'
    if hashlib.sha256(meta.read_bytes()).hexdigest()!=known['metadataSha256'] or hashlib.sha256(dll.read_bytes()).hexdigest()!=known['gameAssemblySha256']:
        raise RuntimeError('Only the manually reviewed build may generate a baseline')
    i=MetadataInspector(meta,dll)
    print('Parsing reviewed native signatures...',flush=True)
    methods,layout=native_contract(i,progress=lambda t:print(t,flush=True))
    result={'schema_version':1,'game_version':known['gameVersion'],'game_build':known['steamBuildId'],
        'metadata_sha256':known['metadataSha256'],'assembly_sha256':known['gameAssemblySha256'],
        'assets':{n:hashlib.sha256((g/'Deskrawl_Data'/n).read_bytes()).hexdigest() for n in ASSET_FILES},
        'enums':enum_contract(i,('StatType','EquipSlotType','AttributeCategory','ModifierType','ItemRarity','ItemType',
            'WorldDifficulty','PrimaryStat','AbilityTag','PlayerState')),
        'native_layout':layout,'native_methods':methods}
    target=R/'data/compatibility-baseline.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'methods':len(methods),'bytes':target.stat().st_size,'native_layout':layout}),flush=True)
if __name__=='__main__':main()
