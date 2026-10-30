"""Extract four hero talent layouts, abilities and gamble stock from files only."""
from item_asset_layout import item_layout
from pathlib import Path
import hashlib, json, struct, sys

ROOT = Path(__file__).resolve().parents[1]


def extract(game):
    import UnityPy
    from UnityPy.classes.PPtr import PPtr
    base = game / 'Deskrawl_Data'
    profile = json.loads((ROOT/'data/game-actions-profile.json').read_text(encoding='utf-8'))
    for name, digest in profile['sha256'].items():
        if hashlib.sha256((game/name).read_bytes()).hexdigest() != digest:
            raise ValueError('Unverified installation: '+name)
    scripts = UnityPy.load(str(base/'globalgamemanagers.assets'))
    classes = {o.path_id:o.read().m_ClassName for o in scripts.objects if o.type.name=='MonoScript'}
    env = UnityPy.load(str(base/'resources.assets'))
    assets = next(iter(env.files.values()))
    shared = env.load_file(str(base/'sharedassets0.assets'), is_dependency=True)
    level = env.load_file(str(base/'level0'), is_dependency=True)
    labels = {k:v for table in json.loads((ROOT/'data/game-catalog.json').read_text(encoding='utf-8'))['tables'].values()
              for k,v in table['entries'].items()}
    def text(key):
        row=labels.get(key,{})
        return row.get('Chinese (Simplified)') or row.get('English') or key
    def payload(obj):
        raw=obj.get_raw_data();n=struct.unpack_from('<i',raw,28)[0]
        if not 0 <= n <= 256:raise ValueError('Invalid asset name')
        return raw[32:32+n].decode(), raw[(32+n+3)&~3:]
    class Cursor:
        def __init__(self,obj):self.obj=obj;self.key,self.data=payload(obj);self.pos=0
        def take(self,fmt):
            values=struct.unpack_from('<'+fmt,self.data,self.pos);self.pos+=struct.calcsize('<'+fmt)
            return values[0] if len(values)==1 else values
        def string(self):
            n=self.take('i')
            if not 0 <= n <= 1024:raise ValueError('Invalid string')
            value=self.data[self.pos:self.pos+n].decode();self.pos=(self.pos+n+3)&~3;return value
        def ptr(self):
            fid,pid=self.take('iq')
            return PPtr(m_FileID=fid,m_PathID=pid,assetsfile=self.obj.assets_file).deref() if pid else None
        def count(self):
            value=self.take('i')
            if not 0 <= value <= 512:raise ValueError('Invalid asset array')
            return value
        def align(self):self.pos=(self.pos+3)&~3
        def obscured(self):
            sys.path.insert(0,str(ROOT))
            from deskrawl_assistant.native_reader import decode_obscured_float
            try:value=decode_obscured_float(self.data[self.pos:self.pos+20])
            except RuntimeError as exc:raise ValueError(f'{self.key} float at {self.pos}: {self.data[self.pos:self.pos+28].hex()}') from exc
            if self.data[self.pos+20:self.pos+28] != b'\0'*8:raise ValueError('Unexpected serialized float')
            self.pos+=28;return value
        def leveled(self):return {'base':self.obscured(),'per_rank':self.obscured()}
        def token(self):
            value={'name':self.string(),'format':self.string(),'suffix':self.string()}
            value['highlight']=self.take('?');self.align()
            value['floor']=self.take('?');self.align();return value
        def modifiers(self):
            result={}
            for _ in range(self.count()):
                self.take('ii');value=self.leveled();token=self.token()
                if token['name']:result[token['name']]={**value,**token}
            return result
    objects=list(assets.objects.values())+list(shared.objects.values())+list(level.objects.values())
    grouped={}
    for obj in objects:
        if obj.type.name!='MonoBehaviour':continue
        raw=obj.get_raw_data();fid,sid=struct.unpack_from('<iq',raw,16)
        if fid==1:grouped.setdefault(classes.get(sid),[]).append(obj)
    icons=ROOT/'deskrawl_assistant/web/progression-icons';icons.mkdir(exist_ok=True)
    def icon(sprite,key):
        if not sprite or sprite.type.name!='Sprite':raise ValueError('Missing sprite: '+key)
        path=icons/(key+'.png');sprite.read().image.save(path)
        return '/progression-icons/'+path.name
    talents={}
    for obj in grouped['TalentData']:
        c=Cursor(obj);sprite=c.ptr();maximum,requirement=c.take('ii');parent=c.ptr();exclusive=c.string();demo=c.take('?')
        if not 1 <= maximum <= 20 or not 0 <= requirement <= 1000:raise ValueError('Unknown talent limits')
        talents[c.key]={'id':c.key,'name':text(c.key),'description':text(c.key+'.Desc'),
            'max_points':maximum,'point_requirement':requirement,'prerequisite':payload(parent)[0] if parent else None,
            'exclusive_group':exclusive,'demo_locked':demo,'sprite':sprite}
        c.align();talents[c.key]['description_tokens']=c.modifiers()
    layouts={name:[] for name in ('Warrior','Sorcerer','Hunter','Monk')}
    for obj in grouped['TalentSlotUI']:
        c=Cursor(obj);talent=c.ptr();key=payload(talent)[0]
        fid,pid=struct.unpack_from('<iq',obj.get_raw_data())
        go=PPtr(m_FileID=fid,m_PathID=pid,assetsfile=obj.assets_file).deref().read()
        transform=next(p.component.deref().read() for p in go.m_Component if p.component.deref().type.name=='RectTransform')
        owner=transform.m_Father.deref().read().m_GameObject.deref().read().m_Name
        class_name=owner.removeprefix('TalentLayout')
        if class_name not in layouts or key not in talents:raise ValueError('Unknown talent layout')
        row=dict(talents[key]);row['icon']=icon(row.pop('sprite'),key)
        # The shipped Warrior layout includes six inactive prototype buttons.
        # Keep their identities for reading old ranks, but never offer/learn them.
        row['selectable']=bool(go.m_IsActive)
        row['x'],row['y']=round(transform.m_AnchoredPosition.x,2),round(-transform.m_AnchoredPosition.y,2)
        layouts[class_name].append(row)
    abilities={}
    for obj in grouped['AbilityData']:
        c=Cursor(obj);name_key=c.string();sprite=c.ptr();tags=[c.take('i') for _ in range(c.count())];requirement=c.take('i');demo=c.take('?')
        abilities[c.key]={'id':c.key,'name':text(c.key),'description':text(c.key+'.Desc'),'tags':tags,
            'level_requirement':requirement,'demo_locked':demo,'sprite':sprite}
    heroes={}
    for obj in grouped['HeroData']:
        c=Cursor(obj);c.string();c.ptr();c.take('?');c.pos=(c.pos+3)&~3;c.take('i');mask=c.take('i')
        for _ in range(3):c.ptr()
        # SimpleSound: AudioClip PPtr (above), volume and pitch floats.
        c.take('ff')
        skills=[]
        for _ in range(c.count()):
            ref=c.ptr();key=payload(ref)[0];row=dict(abilities[key]);row['icon']=icon(row.pop('sprite'),key)
            row['slots']=[n for n in range(4) if (0 if n==0 else 1 if n==1 else 2) in row['tags']]
            if not row['slots']:raise ValueError('Hero ability has no assignable slot: '+key)
            skills.append(row)
        name={1:'Warrior',2:'Sorcerer',4:'Hunter',8:'Monk'}.get(mask)
        if name is None:raise ValueError('Unknown hero class')
        nodes=sorted(layouts[name],key=lambda r:(r['y'],r['x'],r['id']))
        if not nodes or len({n['id'] for n in nodes})!=len(nodes):raise ValueError(f'Incomplete talent page: {name}, {mask}, {len(nodes)}, {len({n["id"] for n in nodes})}')
        heroes[str(mask)]={'id':c.key,'class_mask':mask,'name':{'Warrior':'战士','Sorcerer':'法师','Hunter':'猎人','Monk':'武僧'}[name],
            'talents':nodes,'abilities':skills}
    shop=[]
    for obj in grouped['RandomEquipment']:
        key,b=payload(obj);slot=struct.unpack_from('<i',b,len(b)-8)[0]
        if not 1 <= slot <= 10:continue  # Token exchanges are not legacy gamble slots.
        price=struct.unpack_from('<i',b,20)[0]
        sprite=Cursor(obj).ptr()
        shop.append({'id':key,'slot':slot,'name':text(key).removeprefix('神秘'),
            'base_price':price,'icon':icon(sprite,'shop-'+key)})
    soul=next(o for o in grouped['ItemData'] if payload(o)[0]=='SoulShard')
    maximum=struct.unpack_from('<i',payload(soul)[1],item_layout(payload(soul)[1])['max_carry'])[0]
    unlocks=[]
    for obj in grouped['UnlockableObject']:
        fid,pid=struct.unpack_from('<iq',obj.get_raw_data())
        go=PPtr(m_FileID=fid,m_PathID=pid,assetsfile=obj.assets_file).deref().read()
        if go.m_Name!='MysteriousMerchantArea':continue
        c=Cursor(obj);cost,required,difficulty=c.take('iii');materials=[]
        for _ in range(c.count()):
            key=payload(c.ptr())[0];materials.append({'id':key,'name':text(key),'count':c.take('i')})
        unlocks.append({'key':go.m_Name,'level':required,'gold':cost,'difficulty':difficulty,'materials':materials})
    if not unlocks or any(row!=unlocks[0] for row in unlocks):raise ValueError('Merchant unlock requirements differ across towns')
    life=[]
    for obj in grouped['LifeSkillNodeData']:
        c=Cursor(obj);sprite=c.ptr();maximum_level=c.take('i');costs=[c.take('i') for _ in range(c.count())];parent=c.ptr();demo=c.take('?');c.pos=(c.pos+3)&~3
        effects=[c.ptr() for _ in range(c.count())];notable=c.take('?')
        cap=0;effect_keys=[];tokens={}
        for effect in effects:
            effect_keys.append(payload(effect)[0]);raw=effect.get_raw_data();fid,sid=struct.unpack_from('<iq',raw,16)
            if classes.get(sid)=='LifeSkillEffect_MaterialCarryCap':
                e=Cursor(effect);material=e.ptr();added=e.take('i')
                if payload(material)[0]=='SoulShard':cap+=added
            kind=classes.get(sid)
            if kind=='LifeSkillEffect_StatModifier':tokens.update(Cursor(effect).modifiers())
            elif kind in ('LifeSkillEffect_FishingTime','LifeSkillEffect_FishingSuccess','LifeSkillEffect_OfflineTimeCap','LifeSkillEffect_ReviveCooldown'):
                e=Cursor(effect)
                value=e.leveled() if kind=='LifeSkillEffect_ReviveCooldown' else {'base':e.take('f'),'per_rank':0}
                token=e.token()
                if token['name']:tokens[token['name']]={**value,**token}
        description='\n'.join(text(k+'.Desc') for k in effect_keys)
        if cap:description=description.replace('{value}',str(cap))
        life.append({'id':c.key,'name':text(effect_keys[0] if effect_keys else c.key),'description':description,'max_points':maximum_level,'gold_costs':costs,
                     'prerequisite':payload(parent)[0] if parent else None,'demo_locked':demo,'notable':notable,'effect_ids':effect_keys,
                     'soul_capacity_per_rank':cap,'description_tokens':tokens,'icon':icon(sprite,c.key)})
    positions={}
    for obj in grouped['LifeSkillNodeUI']:
        c=Cursor(obj);node=c.ptr();key=payload(node)[0]
        fid,pid=struct.unpack_from('<iq',obj.get_raw_data())
        go=PPtr(m_FileID=fid,m_PathID=pid,assetsfile=obj.assets_file).deref().read()
        transform=next(p.component.deref().read() for p in go.m_Component if p.component.deref().type.name=='RectTransform')
        if key in positions:raise ValueError('Duplicate life node layout: '+key)
        positions[key]={'x':round(transform.m_AnchoredPosition.x,2),'y':round(-transform.m_AnchoredPosition.y,2)}
    if set(positions)!={n['id'] for n in life}:raise ValueError('Incomplete life skill layout')
    for node in life:node.update(positions[node['id']])
    # Sprites referenced by the shipped UI; resources are bundled, not read from
    # another player's installation at runtime.
    ui_ids={'skill-book':(5364,'Spell_book'),'skill-banner':(5294,'name_bar3'),
            'separator':(5569,'Bar_separator'),'talent-background':(5454,'Tile_background_1'),
            'life-background':(5363,'Tile_background_2'),'life-summary':(5457,'big_background'),
            'round-frame':(5539,'icon_frame_round'),'square-frame':(5412,'Button_square_Fr'),
            'small-round-frame':(5520,'lil_roundframe'),
            'talent-rail-frame':(5901,'Options_slider_vert'),
            'talent-rail-track':(5554,'Options_slider_vert_line'),
            'talent-rail-fluid':(5752,'G_Textures_01 Sprite'),
            'talent-milestone':(5581,'lil_roundframe_ready')}
    ui_assets={}
    for name,(pid,expected) in ui_ids.items():
        matches=[o for o in shared.objects.values() if o.type.name=='Sprite' and o.read().m_Name==expected]
        if not matches:raise ValueError('Missing growth UI sprite: '+name)
        images=[o.read().image for o in matches]
        if any((v.size,v.mode,v.tobytes())!=(images[0].size,images[0].mode,images[0].tobytes()) for v in images[1:]):
            from PIL import Image
            with Image.open(icons/('ui-'+name+'.png')) as prior:
                matching=[o for o,v in zip(matches,images) if (v.size,v.convert('RGBA').tobytes())==(prior.size,prior.convert('RGBA').tobytes())]
            if not matching:raise ValueError('Ambiguous growth UI sprite: '+name)
            matches=matching
        sprite=matches[0]
        ui_assets[name]=icon(sprite,'ui-'+name)
    ui_assets['soul-shard']=icon(Cursor(soul).ptr(),'ui-soul-shard')
    return {'schema_version':1,'build':profile['build'],'sha256':profile['sha256'],'soul_maximum':maximum,
            'shop':sorted(shop,key=lambda r:r['slot']),'shop_unlock':unlocks[0],'heroes':heroes,'life_skills':life,'ui_assets':ui_assets}


if __name__=='__main__':
    result=extract(Path(sys.argv[1]) if len(sys.argv)>1 else Path(r'G:\SteamLibrary\steamapps\common\Deskrawl'))
    (ROOT/'data/progression-catalog.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'soul_maximum':result['soul_maximum'],'shop':result['shop'],
        'heroes':{k:{'talents':len(h['talents']),'abilities':len(h['abilities'])} for k,h in result['heroes'].items()}},ensure_ascii=False))
