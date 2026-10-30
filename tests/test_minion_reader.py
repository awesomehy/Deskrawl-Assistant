import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.minion_reader import read_minions
from deskrawl_assistant.native_memory import MemoryReadError, SnapshotChangedError


class MinionReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader, self.copy = Mock(), Mock()
        self.copy.verify.side_effect = lambda obj, name: obj
        self.copy.singleton.side_effect = lambda rva, name: {'SlaveManager':1000, 'GameManager':2000}[name]
        pointers = {2032:3000, 3088:4000, 1080:5000, 1088:6000, 1096:7000,
                    1128:8000, 1136:7000, 9032:10000, 9040:11000}
        self.copy.ptr.side_effect = pointers.__getitem__
        self.copy.integer.side_effect = {4024:11, 4096:4, 4100:7, 9016:3,
            7024:1, 7028:3, 7124:2, 7128:2, 1144:1, 1148:0}.__getitem__
        self.copy.dictionary.return_value = {7000:9000}
        self.copy.list.return_value = [struct.pack('<Q', n) for n in (7000,7100)]
        arrays = {8000:[b'\1', b'\0'], 10000:[struct.pack('<i',2)], 11000:[struct.pack('<i',1)]}
        self.copy.array.side_effect = lambda obj,*a,**k: arrays[obj]
        self.copy.read.return_value = struct.pack('<q',1234)
        self.reader.asset_name.side_effect = {7000:'one',7100:'two'}.__getitem__
        self.enterContext(patch('deskrawl_assistant.minion_reader._Copy', return_value=self.copy))
        self.enterContext(patch('deskrawl_assistant.minion_reader._labels', return_value={'one':'仆从一'}))
        self.enterContext(patch('deskrawl_assistant.game_compatibility.resolved_profile',
                                return_value={'minion_manager_rva':123}))

    def test_progress_default_levels_inheritance_and_no_native_calls(self):
        result = read_minions(self.reader)
        self.assertEqual(result['max_level'],11)  # Use the actual config, not an announcement constant.
        self.assertEqual(result['minions'][0], {'id':'one', 'name':'仆从一', 'active':True,
            'species':1,'rarity':3,'level':3,'xp':1234,'passive_levels':[2],'ability_levels':[1]})
        self.assertEqual(result['minions'][1]['level'],1)
        self.assertEqual(result['minions'][1]['xp'],0)
        self.assertEqual(result['slot_unlocks'],[True,False])
        self.assertEqual(result['borrowed_skill'],{'minion_id':'one','kind':1,'index':0})
        self.copy.finish.assert_called_once()
        self.reader.memory.write.assert_not_called()
        self.assertNotIn('_', result)

    def test_unstable_read_cannot_publish_partial_progress(self):
        self.copy.finish.side_effect = SnapshotChangedError('changing')
        with self.assertRaises(SnapshotChangedError): read_minions(self.reader)

    def test_duplicate_ownership_and_invalid_xp_fail_closed(self):
        self.copy.list.return_value *= 2
        with self.assertRaisesRegex(MemoryReadError,'重复'): read_minions(self.reader)
        self.copy.list.return_value = self.copy.list.return_value[:2]
        self.copy.read.return_value = struct.pack('<q',-1)
        with self.assertRaisesRegex(MemoryReadError,'成长'): read_minions(self.reader)
