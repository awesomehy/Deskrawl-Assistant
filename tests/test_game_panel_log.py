"""No game calls/writes: copied notification-log layouts and rolling cursor."""
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_panel_log import read_panel_log, parse_disposal, DisposalLogCursor
from deskrawl_assistant.native_dispatcher import load_profile
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError


def packet(*records, source='log'):
    return {'available':True,'complete':True,'id':source,'records':list(records),'capacity':1000}


def event(id_, category=2):
    return {'id':str(id_),'category':category}


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.templates = load_profile()['panel_log']['templates']
        self.colors = {'E8E8E8':0,'6680FF':1,'FFFF00':2,'FF8000':3,'FFDB9A':4}
        self.names = {'简易木弓':{'CommonBow1'}, 'Simple Bow':{'CommonBow1'},
                      '红木猎弓':{'UncommonBow1'}}

    def parse(self, text):
        return parse_disposal(text,self.colors,self.templates,self.names)

    def test_verified_sell_and_salvage_templates_and_gold(self):
        sale = self.parse('出售 <color=#E8E8E8>[简易木弓]</color> 获得 273 金币')
        self.assertEqual((sale['action'],sale['item']['rarity'],sale['quantity'],sale['gold']),('sell',0,1,273))
        salvage = self.parse('分解 <color=#6680ff>[红木猎弓]</color>')
        self.assertEqual((salvage['action'],salvage['item']['rarity']),('salvage',1))
        self.assertIsNone(salvage['gold'])
        self.assertEqual(self.parse('Sold <color=#E8E8E8>[Simple Bow]</color> for 273 gold')['gold'],273)
        self.assertEqual(self.parse('出售 <color=#E8E8E8>[简易木弓]</color> 獲得 273 金幣')['gold'],273)

    def test_no_fuzzy_matching_gem_guessing_or_multiple_items(self):
        for text in ('出售 [简易木弓] 获得 273 金币','出售 <color=#123456>[简易木弓]</color> 获得 273 金币',
                     '出售 <color=#E8E8E8>[红宝石]</color> 获得 273 金币',
                     '出售 <color=#E8E8E8>[简易木弓]</color> x2 获得 273 金币',
                     '出售 <color=#E8E8E8>[简易木弓]</color> 获得 -1 金币','灰烬之冠: 4 通关，用时01:25'):
            with self.subTest(text=text): self.assertIsNone(self.parse(text))
        self.names['简易木弓'].add('CommonBow2')
        self.assertIsNone(self.parse('分解 <color=#E8E8E8>[简易木弓]</color>'))


class CursorTests(unittest.TestCase):
    def test_baseline_repeat_polls_and_identical_texts_with_distinct_events(self):
        cursor = DisposalLogCursor()
        self.assertEqual(cursor.observe(packet(event('old')),baseline=True),[])
        text = 'same item and amount'
        records = [event('old'),dict(event('new1'),text=text),dict(event('new2'),text=text),event('progress',3)]
        self.assertEqual([r['id'] for r in cursor.observe(packet(*records))],['new1','new2'])
        self.assertEqual(cursor.observe(packet(*records)),[])
        self.assertFalse(cursor.incomplete)

    def test_rolling_overlap_is_complete_but_lost_anchor_is_partial(self):
        cursor = DisposalLogCursor();cursor.observe(packet(event(1),event(2)),baseline=True)
        self.assertEqual([r['id'] for r in cursor.observe(packet(event(2),event(3)))],['3'])
        self.assertFalse(cursor.incomplete)
        self.assertEqual([r['id'] for r in cursor.observe(packet(event(4),event(5)))],['4','5'])
        self.assertTrue(cursor.incomplete)
        self.assertEqual(cursor.seen,{'4','5'})

    def test_read_failure_can_recover_events_without_rebaselining(self):
        cursor = DisposalLogCursor();cursor.observe(packet(event(1)),baseline=True)
        self.assertEqual(cursor.observe({'available':False,'reason':'busy'}),[])
        self.assertFalse(cursor.available)
        self.assertEqual([r['id'] for r in cursor.observe(packet(event(1),event(2)))],['2'])
        self.assertFalse(cursor.incomplete)

    def test_late_attachment_reset_and_bad_ids_never_backfill_history(self):
        cursor = DisposalLogCursor();self.assertEqual(cursor.observe(packet(event(1))),[])
        self.assertTrue(cursor.incomplete)
        self.assertEqual(cursor.observe(packet(event(1),event(1))),[])
        self.assertFalse(cursor.available)
        self.assertEqual(cursor.observe(packet(event(10),source='new-list')),[])
        self.assertEqual([r['id'] for r in cursor.observe(packet(event(10),event(11),source='new-list'))],['11'])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.read = Mock(), Mock()
        self.reader.module={'base':0};self.reader.session_id='game'
        self.profile = load_profile()
        self.read.klass.side_effect=lambda p,n:p
        self.read.verify.side_effect=lambda p,n:p
        pointers={self.profile['panel_log']['class_rva']:1000,1184:2000,2000:3000,5040:6000}
        self.read.ptr.side_effect=lambda a:pointers[a]
        self.read.list.return_value=[7000]
        self.raw=struct.pack('<Qffffi4xQ',8000,1,1,1,1,2,638000000000000000 | (2<<62))
        self.read.read.side_effect=self.raw_read
        self.reader.string.return_value='出售 <color=#E8E8E8>[简易木弓]</color> 获得 273 金币'

    def raw_read(self,address,size):
        if address==7016:return self.raw
        colors={6216:(232/255,232/255,232/255,1),6232:(.4,.5,1,1),6248:(1,1,0,1),
                6264:(1,.5,0,1),6280:(1,219/255,154/255,1)}
        return struct.pack('<ffff',*colors[address])

    def read_log(self,cache=None):
        with patch('deskrawl_assistant.game_actions._Read',return_value=self.read), \
                patch('deskrawl_assistant.game_panel_log.equipment_names',return_value={'简易木弓':{'CommonBow1'}}):
            return read_panel_log(self.reader,self.profile,5000,cache=cache)

    def test_copies_guarded_log_and_cache_does_not_invoke_or_write(self):
        cache={};first=self.read_log(cache);second=self.read_log(cache)
        self.assertEqual(first,second)
        self.assertEqual(first['records'][0]['receipt']['item']['rarity'],0)
        self.reader.string.assert_called_once()
        self.assertEqual(self.read.finish.call_count,2)
        self.reader.memory.write.assert_not_called();self.reader.memory.invoke.assert_not_called()
        self.raw=struct.pack('<Qffffi4xQ',8000,1,1,1,1,2,638000000000000001 | (2<<62))
        third=self.read_log(cache)
        self.assertNotEqual(first['records'][0]['id'],third['records'][0]['id'])
        self.assertEqual(len(cache),1)

    def test_changing_log_does_not_commit_a_cache_snapshot(self):
        cache={};self.read.finish.side_effect=SnapshotChangedError('changing')
        with self.assertRaises(SnapshotChangedError):self.read_log(cache)
        self.assertEqual(cache,{})

    def test_oversize_list_and_invalid_category_fail_closed(self):
        self.read.list.return_value=[7000]*1001
        with self.assertRaisesRegex(MemoryReadError,'条目数'):self.read_log()
        self.read.list.return_value=[7000]
        self.raw=struct.pack('<Qffffi4xQ',8000,1,1,1,1,9,638000000000000000)
        with self.assertRaisesRegex(MemoryReadError,'事件格式'):self.read_log()


if __name__=='__main__':unittest.main()
