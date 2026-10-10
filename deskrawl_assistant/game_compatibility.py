"""Local, conservative adaptation of ordinary name/address changes.

Only unchanged layouts, static assets and native logic can pass. Generated
profiles are process-local; disk reports are diagnostics, never trusted code.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import time

from .il2cpp_metadata import MetadataInspector
from .native_memory import MemoryReadError
from .paths import RESOURCE_ROOT, runtime_dir

PROFILE_FILES=('runtime-type-hints.json','ui-runtime-hints.json','container-transfer-types.json','carriage-types.json')
ASSET_FILES=('resources.assets','sharedassets0.assets','globalgamemanagers.assets')
EXPORTS=('il2cpp_class_get_name','il2cpp_class_get_namespace','il2cpp_class_get_parent','il2cpp_class_get_fields',
    'il2cpp_field_get_name','il2cpp_field_get_offset','il2cpp_class_get_static_field_data',
    'il2cpp_class_get_element_class','il2cpp_array_element_size','il2cpp_gc_wbarrier_set_field')


class CompatibilityError(MemoryReadError):
    def __init__(self,reason,detail=''):
        self.reason,self.detail=reason,detail
        super().__init__('需要维护适配：'+reason+('（'+detail+'）' if detail else ''))


def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def profiles():
    return {name:json.loads((RESOURCE_ROOT/'data'/name).read_text(encoding='utf-8')) for name in PROFILE_FILES}


def type_rows(items):return items['types'] if isinstance(items,dict) else items


def field_shape(fields,canonical=lambda x:x):
    return [(f['rawRegistrationOffset'],canonical(f['resolvedType']),f['typeKind'],
        f['isStatic'],f['isLiteral'],f['isInitOnly']) for f in fields]


def match_profiles(inspector,originals):
    """Keep exact field types, offsets and ordering; update names and tokens."""
    output=deepcopy(originals);defs=inspector.defs;indices={}
    aliases={}
    for source in originals.values():
        for t in type_rows(source):
            key=(t.get('namespace',''),t['name'])
            if key in indices:continue
            found=[n for n,d in enumerate(defs) if (d['namespace'],d['name'])==key]
            if not found and key==('','gj'):
                # This registry type is obfuscated; match its entire unique shape.
                shape=field_shape(t['fields'])
                found=[n for n,d in enumerate(defs) if not d['namespace'] and field_shape(inspector.fields(n))==shape]
                if len(found)==1:aliases[defs[found[0]]['name']]='gj'
            if len(found)!=1:raise CompatibilityError('类型缺失或存在歧义',key[1])
            n=found[0];fields=inspector.fields(n)
            canonical=lambda name:aliases.get(name,name)
            if field_shape(fields,canonical)!=field_shape(t['fields']):raise CompatibilityError('装备或容器字段布局改变',key[1])
            indices[key]=n
    for source in output.values():
        for t in type_rows(source):
            key=(t.get('namespace',''),t['name']);n=indices[key]
            t.update({'name':defs[n]['name'],'logicalName':key[1],'typeDefinitionIndex':n,
                'fields':deepcopy(inspector.fields(n)),'methods':deepcopy(inspector.methods(n))})
    runtime=output['runtime-type-hints.json']
    runtime['metadataSha256']=hashlib.sha256(inspector.meta).hexdigest()
    runtime['gameAssemblySha256']=hashlib.sha256(inspector.pe.data).hexdigest()
    runtime['source']='Locally verified automatic adaptation of unchanged layouts and native logic.'
    # Do not label an unknown version as the previous game version.
    runtime['gameVersion']='自动适配版本';runtime['steamBuildId']=''
    return output,aliases


def enum_contract(inspector,names):
    off,_,count=inspector.section(92,10);data,size,_=inspector.section(104)
    defaults={field:(typ,index) for field,typ,index in struct.iter_unpack('<iHi',inspector.meta[off:off+count*10])}
    result={}
    for n,d in enumerate(inspector.defs):
        if d['namespace'] or d['name'] not in names:continue
        values=[]
        for f in inspector.fields(n):
            if not f['isLiteral']:continue
            typ,index=defaults[f['fieldIndex']]
            if inspector.type_at(typ)['resolvedType']!='System.Int32' or not 0<=index<size:
                raise CompatibilityError('词条枚举常量格式改变',d['name'])
            pos=data+index;first=inspector.meta[pos];pos+=1
            if first<0x80:unsigned=first
            elif first==0xf0:unsigned=struct.unpack_from('<I',inspector.meta,pos)[0];pos+=4
            elif first==0xff:unsigned=0xffffffff
            elif first==0xfe:unsigned=0xfffffffe
            elif first&0xc0==0xc0:unsigned=((first&0x3f)<<24)|int.from_bytes(inspector.meta[pos:pos+3],'big');pos+=3
            else:unsigned=((first&0x7f)<<8)|inspector.meta[pos];pos+=1
            if pos>data+size:raise CompatibilityError('枚举常量越界')
            value=-(1<<31) if unsigned==0xffffffff else -(unsigned//2+1) if unsigned&1 else unsigned//2
            values.append([f['name'],value])
        result[d['name']]=values
    if set(result)!=set(names):raise CompatibilityError('词条或物品枚举缺失')
    return result


def native_contract(inspector,aliases=None,progress=lambda text:None):
    from .native_signatures import Signatures
    signatures=Signatures(inspector,aliases)
    progress('正在比对装备数值、锁定、保存和搬运逻辑…')
    # All game/ACTk methods detect changes through callbacks and indirect calls.
    # Addresses and obfuscated names are normalized; significant constants remain.
    bodies={}
    for key,pointer in sorted(signatures.method_pointers.items()):
        try:bodies[key]=signatures.body(pointer)
        except ValueError as exc:raise ValueError(key+' @ '+hex(pointer-inspector.pe.base)+': '+str(exc)) from exc
    for name in EXPORTS:
        if name not in signatures.exports:raise CompatibilityError('运行时接口缺失',name)
        bodies['export:'+name]=signatures.body(signatures.exports[name])
    position=0
    while position<len(signatures.helpers):
        pointer=list(signatures.helpers)[position]
        try:bodies['native-helper:'+str(position)]=signatures.body(pointer)
        except ValueError as exc:raise ValueError('helper '+hex(pointer-inspector.pe.base)+': '+str(exc)) from exc
        position+=1
    return bodies,signatures.native_layout()


def bindings(profile):
    types={t.get('logicalName',t['name']):t for t in profile['types']}
    def name(cls,offset,typ):
        matches=[f['name'] for f in types[cls]['fields'] if not f['isStatic'] and f['rawRegistrationOffset']==offset and f['resolvedType']==typ]
        if len(matches)!=1:raise CompatibilityError('关键状态字段无法唯一定位',cls)
        return matches[0]
    return {'save_dirty_field':name('SaveSystem',92,'System.Boolean'),
            'lock_field':name('GeneratedItemData',73,'System.Boolean')}


def check_assets(game,baseline):
    root=game/'Deskrawl_Data'
    if {p.name for p in root.glob('*.assets')}!=set(baseline['assets']):
        raise CompatibilityError('游戏资源文件有增减，需要维护')
    for name,expected in baseline['assets'].items():
        path=root/name
        if not path.is_file() or file_hash(path)!=expected:
            raise CompatibilityError('游戏资源已变化，可能有新增物品或效果，需要维护',name)


def inspect_changes(inspector,baseline,originals,progress=lambda text:None):
    progress('正在核验字段类型、布局和词条枚举…')
    adapted,aliases=match_profiles(inspector,originals)
    actual=enum_contract(inspector,baseline['enums'])
    if actual!=baseline['enums']:raise CompatibilityError('词条、物品或部位枚举改变')
    bodies,layout=native_contract(inspector,aliases,progress)
    if bodies!=baseline['native_methods']:
        changed=len(set(bodies)^set(baseline['native_methods']))+sum(bodies[k]!=baseline['native_methods'][k] for k in set(bodies)&set(baseline['native_methods']))
        raise CompatibilityError('游戏操作逻辑或编译结构改变',str(changed)+' 项代码特征不一致')
    return {'profiles':adapted,'native_layout':layout,'bindings':bindings(adapted['runtime-type-hints.json'])}


def detect_game_build(game):
    parent=game.parent.parent
    for manifest in parent.glob('appmanifest_*.acf'):
        import re
        raw=manifest.read_text(encoding='utf-8',errors='replace')
        installed=re.search(r'"installdir"\s+"([^\"]+)"',raw)
        build=re.search(r'"buildid"\s+"(\d+)"',raw)
        if installed and installed[1]==game.name and build:return build[1]
    return ''


def resolve(game,assembly,progress=lambda state:None,*,force_inspection=False):
    """Return an in-memory profile only after complete static verification."""
    originals=profiles()
    baseline=json.loads((RESOURCE_ROOT/'data/compatibility-baseline.json').read_text(encoding='utf-8'))
    def stage(message):progress({'phase':'checking','message':message})
    stage('正在检测游戏文件和资源…')
    paths=[assembly,game/'Deskrawl_Data/il2cpp_data/Metadata/global-metadata.dat',*[game/'Deskrawl_Data'/n for n in baseline['assets']]]
    try:
        stamps={str(p):(p.stat().st_size,p.stat().st_mtime_ns) for p in paths}
        check_assets(game,baseline)
        meta=paths[1].read_bytes();binary=assembly.read_bytes()
    except OSError as exc:
        raise CompatibilityError('无法读取游戏文件，请完成游戏更新后重新连接',str(exc)[:120]) from exc
    hashes={'metadata':hashlib.sha256(meta).hexdigest(),'assembly':hashlib.sha256(binary).hexdigest()}
    known=(hashes['metadata']==baseline['metadata_sha256'] and hashes['assembly']==baseline['assembly_sha256'])
    try:
        if known and not force_inspection:
            result={'profiles':originals,'native_layout':baseline['native_layout'],
                'bindings':bindings(originals['runtime-type-hints.json'])}
        else:
            stage('检测到游戏版本变化，正在自动检查普通变化…')
            inspector=MetadataInspector(meta,binary)
            result=inspect_changes(inspector,baseline,originals,stage)
        if any((Path(p).stat().st_size,Path(p).stat().st_mtime_ns)!=stamp for p,stamp in stamps.items()):
            raise CompatibilityError('检测期间游戏文件正在更新，请完成更新后重试')
    except CompatibilityError:raise
    except (ValueError,KeyError,IndexError,struct.error,UnicodeError,RecursionError) as exc:
        raise CompatibilityError('无法确认新版本结构或逻辑',str(exc)[:180]) from exc
    build=detect_game_build(game)
    result.update({'metadata':meta,'file_stamps':stamps,'status':{
        'phase':'verified' if known else 'adapted','auto_adapted':not known,'game_build':build,
        'game_version':baseline['game_version'] if known else '',
        'message':('已验证游戏兼容性' if known else '普通变化自动适配通过')+(' · build '+build if build else '')}})
    # Saving a diagnostic report does not grant compatibility on future runs.
    try:
        report={'schema_version':1,**result['status'],'hashes':hashes,'native_layout':result['native_layout'],
            'checked_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}
        folder=runtime_dir();folder.mkdir(parents=True,exist_ok=True)
        (folder/'game-compatibility.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    except OSError:pass
    progress(result['status'])
    return result


def native_rva(reader,key,default):return getattr(reader,'native_layout',{}).get(key,default)


def assert_current(reader):
    check=getattr(reader,'check_game_files',None)
    if check:check()
