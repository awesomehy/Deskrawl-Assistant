"""Coverage and verified examples for bundled non-equipment item assets."""
import json
from pathlib import Path
import unittest
from deskrawl_assistant.web_service import BASE


class ItemAssetTests(unittest.TestCase):
    def test_every_shipped_gem_and_rune_has_icon_and_effects(self):
        data=json.loads((BASE/'data/item-ui.json').read_text(encoding='utf-8'))
        catalog=json.loads((BASE/'data/game-catalog.json').read_text(encoding='utf-8'))
        groups=catalog['indexes']['equipment_root_key_groups']
        expected=set().union(*(groups[k] for k in ['gem_names','generic_rune_names','skill_rune_names','set_rune_piece_names']))-set(data['unused_localization_roots'])
        self.assertEqual(set(data['items']),expected)
        for name,item in data['items'].items():
            with self.subTest(name=name):
                icon=BASE/'deskrawl_assistant/web'/item['icon'].lstrip('/')
                self.assertTrue(icon.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
                self.assertTrue(item['effect_groups']);self.assertTrue(any(g['values'] or g['texts'] for g in item['effect_groups']))
    def test_verified_polished_gems_and_ambush_effects(self):
        data=json.loads((BASE/'data/item-ui.json').read_text(encoding='utf-8'))['items']
        diamond=data['GemDiamond4']['effect_groups']
        self.assertEqual([g['values'][0]['display_value'] for g in diamond],['+2.5%','+8.5%','+25%'])
        emerald=data['GemEmerald4']['effect_groups']
        self.assertEqual([g['values'][0]['display_value'] for g in emerald],['+80','+11%','+25%'])
        value=data['UncommonRune_DamageVsHealthy']['effect_groups'][0]['values'][0]
        self.assertEqual(value['display_value'],'+30%');self.assertEqual(value['growth'],'每级 +3%')


if __name__=='__main__':unittest.main()
