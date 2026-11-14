"""Exercise real snapshot guards and mailbox encoding without a game process."""
from copy import deepcopy
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import Mock, patch

from deskrawl_assistant.game_actions import _Read
from deskrawl_assistant.native_dispatcher import compile_mailbox, load_profile, MAX_CALLS, MAX_GUARDS
from deskrawl_assistant.native_memory import SnapshotChangedError
from deskrawl_assistant.progression_runtime import _GrowthCopy
from deskrawl_assistant.synthesis_runtime import SynthesisRuntime, SynthesisReceiptPending
from tests.test_equipment_synthesis import gear, recipe
from tests import test_synthesis_runtime as fixtures
from tests.test_synthesis_runtime import gem, gem_recipes


class Memory:
    def __init__(self):
        self.cells = {}

    def store(self, address, raw):
        self.cells.update((address + index, value) for index, value in enumerate(raw))

    def read(self, address, size):
        return bytes(self.cells.get(address + index, 0) for index in range(size))


class EquipmentMailboxTests(unittest.TestCase):
    def fixture(self, warehouse_count=1000):
        memory, profile = Memory(), load_profile()
        base, inventory, cube, array = 0x180000000, 0x30000, 0x31000, 0x40000
        classes = {name: 0x10000 + i * 0x1000 for i, name in enumerate(
            ('Inventory', 'InventorySlot', 'GeneratedItemData', 'CubePanelUI', 'CubeSlotUI'))}
        reader = Mock(memory=memory, module={'base': base}, snapshot_stamps=[])
        reader.class_name.side_effect = {address: name for name, address in classes.items()}.__getitem__
        reader.fields.side_effect = lambda address: [
            {'name': row['name'], 'offset': row['rawRegistrationOffset'], 'token': int(row['token'], 16)}
            for row in profile['types'][reader.class_name(address)]['fields']]
        for i, (name, obj) in enumerate((('Inventory', inventory), ('CubePanelUI', cube))):
            static = 0x20000 + i * 0x1000
            memory.store(base + profile['singletons'][name], struct.pack('<Q', classes[name]))
            memory.store(classes[name] + 184, struct.pack('<Q', static))
            memory.store(static, struct.pack('<Q', obj))
            memory.store(obj, struct.pack('<Q', classes[name]))
        memory.store(inventory + 56, struct.pack('<Q', array))
        memory.store(array + 24, struct.pack('<Q', 60))
        rows = [gear(i, 'AncientLegendary') for i in range(9)]
        registry, strings, assets, bag_stamps = {}, {}, {}, []
        for i in range(60):
            slot = 0x50000 + i * 0x100
            memory.store(array + 32 + i * 8, struct.pack('<Q', slot))
            memory.store(slot, struct.pack('<Q', classes['InventorySlot']))
            if i < len(rows):
                generated, asset, uid = 0x60000 + i * 0x100, 0x70000 + i * 0x100, 0x80000 + i * 0x100
                strings[uid], assets[asset] = rows[i]['item_uid'], rows[i]['internal_name']
                registry[rows[i]['item_uid']] = generated
                memory.store(slot + 16, struct.pack('<Qi4xQ8x', asset, 1, uid))
                memory.store(generated, struct.pack('<Q', classes['GeneratedItemData']))
                for offset, value in ((16, 845), (20, 3), (32, 0)):
                    memory.store(generated + offset, struct.pack('<i', value))
                memory.store(generated + 72, b'\1\0')
                bag_stamps.append((generated, memory.read(generated, 128)))
            bag_stamps.append((slot + 16, memory.read(slot + 16, 32)))
        bag_stamps.append((array + 32, memory.read(array + 32, 60 * 8)))
        cube_slots = [0xA0000 + i * 0x100 for i in range(9)]
        memory.store(cube + 64, struct.pack('<Q', 0x90000))
        memory.store(0x90000 + 24, struct.pack('<Q', 9))
        memory.store(0x90000 + 32, b''.join(struct.pack('<Q', obj) for obj in cube_slots))
        for obj in cube_slots:
            memory.store(obj, struct.pack('<Q', classes['CubeSlotUI']))
        reader.array.return_value = ([struct.pack('<Q', obj) for obj in cube_slots],
                                     memory.read(0x90000 + 32, 9 * 8))
        reader.string.side_effect = lambda pointer: strings.get(pointer, '')
        reader.asset_name.side_effect = assets.__getitem__
        entries, root, static, registry_class = 0xB0000, 0x210000, 0x220000, 0x230000
        header = struct.pack('<Qiiii', entries, warehouse_count, -1, 0, 17)
        registry_stamps = [(registry_class + 184, struct.pack('<Q', static)),
                           (static, struct.pack('<Q', root)), (root + 24, header),
                           (entries + 32, b'R' * (warehouse_count * 24))]
        warehouse = (0x100000, b'S' * (warehouse_count * 32))
        for address, raw in registry_stamps + [warehouse]:
            memory.store(address, raw)

        def read_registry():
            reader.snapshot_stamps.extend(registry_stamps)
            return registry, {'object': root, 'entries': entries, 'header': header}

        def container(name, *_):
            reader.snapshot_stamps.extend(bag_stamps if name == 'Inventory' else [warehouse])
            return {'complete': True, 'slots': deepcopy(rows) if name == 'Inventory' else [],
                    'slot_count': 60 if name == 'Inventory' else warehouse_count}

        reader.registry.side_effect, reader.container.side_effect = read_registry, container
        read = _Read(reader, profile)
        # Use the real full-snapshot finish(), without loading a process profile.
        copy = object.__new__(_GrowthCopy)
        copy.reader, copy.memory, copy.guards, copy.action_guards, copy.guarding = reader, memory, [], [], True
        copy.read(0x250000, 128)  # other live recipe/currency context predicates
        actions = SimpleNamespace(reader=reader, profile=profile, verified_kinds=set(), dispatcher=Mock())
        runtime = SynthesisRuntime(actions)
        runtime._context = Mock(return_value=(read, 0x260000, 0x270000))
        runtime._copy = Mock(return_value=copy)
        runtime._singleton = Mock(return_value=0x240000)
        rec = recipe('ancient_to_black_mist')
        runtime._equipment = Mock(return_value=dict(recipes=[rec], online=True, gold=10000000,
            sequence=53, _recipe_objects={rec['recipe_id']: 0x280000}))
        runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed', uncertain=True))
        runtime._guard_bag_identities = Mock(wraps=runtime._guard_bag_identities)
        captured = {}

        def encode(calls, guards, **_):
            captured.update(calls=calls, guards=guards)
            captured['mailbox'] = compile_mailbox(0x100000, base, profile, 1, 2, 3, calls, guards)
            return {'sequence': 1}

        actions.dispatcher.invoke.side_effect = encode
        payload = dict(recipe_id=rec['recipe_id'], count=1, input_uids=[r['item_uid'] for r in rows],
                       inputs=[dict(r, quantity=1) for r in rows])
        return runtime, payload, memory, captured, warehouse

    def test_large_warehouse_stays_out_of_mailbox_but_selected_identities_remain_guarded(self):
        counts = []
        for warehouse_count in (100, 2000):
            runtime, payload, _, captured, warehouse = self.fixture(warehouse_count)
            with patch('deskrawl_assistant.synthesis_runtime.record'):
                self.assertTrue(runtime.execute('synthesize_equipment', payload)['synthesis_pending'])
            self.assertEqual(len(captured['calls']), 14)
            self.assertLessEqual(len(captured['calls']), MAX_CALLS)
            words = sum((len(raw) + 7) // 8 for _, raw in captured['guards'])
            self.assertLessEqual(words, MAX_GUARDS)
            counts.append(words)
            self.assertIn(warehouse, runtime._copy.return_value.guards)
            self.assertNotIn(warehouse, captured['guards'])
            runtime._guard_bag_identities.assert_not_called()
            # Native-thread guards retain selected location, UID, counts, locks,
            # generated quality/level/upgrade flags, and registry root/version.
            covered = {address + i for address, raw in captured['guards'] for i in range(len(raw))}
            for i in range(9):
                for start, size in ((0x40000 + 32 + i * 8, 8), (0x50000 + i * 0x100 + 16, 32),
                                    (0x60000 + i * 0x100, 8), (0x60000 + i * 0x100 + 16, 94)):
                    self.assertTrue(set(range(start, start + size)) <= covered)
            for start, size in ((0x230000 + 184, 8), (0x220000, 8), (0x210000 + 24, 24)):
                self.assertTrue(set(range(start, start + size)) <= covered)
        self.assertEqual(counts[0], counts[1])

    def test_warehouse_change_after_snapshot_still_blocks_before_dispatch(self):
        runtime, payload, memory, _, warehouse = self.fixture()
        state = runtime._equipment.return_value
        def changed(*_, **__):
            memory.store(warehouse[0] + 8, b'X')
            return state
        runtime._equipment.side_effect = changed
        with self.assertRaises(SnapshotChangedError):
            runtime.execute('synthesize_equipment', payload)
        runtime.actions.dispatcher.invoke.assert_not_called()
        self.assertIsNone(runtime._pending)

    def test_receipt_reads_still_recheck_full_warehouse_at_end(self):
        runtime, _, memory, _, warehouse = self.fixture(100)
        read = runtime._context.return_value[0]
        previous_stamps = runtime.reader.snapshot_stamps
        runtime._bag(read, include_storage=True)
        self.assertIs(runtime.reader.snapshot_stamps, previous_stamps)
        self.assertIn(warehouse, read.guards)
        memory.store(warehouse[0] + 8, b'X')
        with self.assertRaises(SnapshotChangedError):
            read.finish()
        runtime.actions.dispatcher.invoke.assert_not_called()

    def test_selected_identity_lock_quality_and_registry_changes_still_block(self):
        for address in (0x50000 + 16, 0x50000 + 32, 0x50000 + 40,
                        0x60000 + 16, 0x60000 + 20, 0x60000 + 32,
                        0x60000 + 72, 0x60000 + 73, 0x60000 + 105, 0x210000 + 44):
            with self.subTest(address=address):
                runtime, payload, memory, _, _ = self.fixture(100)
                original = runtime._cube_selections
                def changed(*args):
                    result = original(*args)
                    memory.store(address, bytes([memory.read(address, 1)[0] ^ 1]))
                    return result
                runtime._cube_selections = changed
                with self.assertRaises(SnapshotChangedError):
                    runtime.execute('synthesize_equipment', payload)
                runtime.actions.dispatcher.invoke.assert_not_called()
                self.assertIsNone(runtime._pending)

    def test_gems_and_runes_retain_all_slot_protection(self):
        for kind in ('synthesize_gems', 'synthesize_runes'):
            with self.subTest(kind=kind):
                runtime, payload, _ = fixtures.SnapshotAndReceiptTests().rune_execution()
                if kind == 'synthesize_gems':
                    rec = next(r for r in gem_recipes() if r['recipe_id'] == 'GemUpgradeRuby2')
                    runtime._bag.return_value = [gem(0, 20)], {}, 300, 40
                    runtime._gold.return_value = 10000
                    runtime._find_recipe = Mock(return_value=500)
                    runtime._context.return_value[0].integer.return_value = 99
                    payload = dict(recipe_id=rec['recipe_id'], count=1, max_quality=2,
                                   inputs=[dict(gem(0, 20), quantity=10)])
                runtime._confirm = Mock(side_effect=SynthesisReceiptPending('delayed', uncertain=True))
                with patch('deskrawl_assistant.synthesis_runtime.record'):
                    runtime.execute(kind, payload)
                runtime._guard_bag_identities.assert_called_once_with(runtime._context.return_value[0], 300, 40)
