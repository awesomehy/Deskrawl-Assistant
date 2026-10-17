"""Failure receipts distinguish known read-only prefixes from possible writes."""
import struct
import unittest
from unittest.mock import Mock
from deskrawl_assistant.native_dispatcher import (
    Argument, Invocation, MainThreadDispatcher, NativeActionError, failure_details,
    CANCELLED, GUARD_FAILED, REJECTED, INVALID, EXCEPTION, HEADER_SIZE,
)


class DispatcherFailureMetadataTests(unittest.TestCase):
    def calls(self):
        return [Invocation('EquipmentManager.ddq',arguments=(Argument.reference(1),Argument.integer(6)),require_true=True,mutates=False),
                Invocation('EquipmentManager.ddv',arguments=(Argument.reference(2),),require_false=True,mutates=False),
                Invocation('EquipmentManager.ded',1,(Argument.reference(2),Argument.integer(6),Argument.reference(3)),mutates=True)]

    def reject(self,calls,state,index):
        reader=Mock();reader.classes={'Inventory':0x10000};reader.module={'base':0x180000000}
        dispatcher=MainThreadDispatcher(reader);dispatcher.install=Mock();dispatcher._write=Mock()
        dispatcher.mailbox=0x20000;dispatcher.iat=0x30000;dispatcher.code=0x40000
        reader.memory.u64.return_value=dispatcher.code
        response=bytearray(HEADER_SIZE)
        struct.pack_into('<I',response,0,state);struct.pack_into('<Q',response,8,1)
        struct.pack_into('<I',response,60,index)
        reader.memory.read.side_effect=[bytes(HEADER_SIZE),bytes(response)]
        with self.assertRaises(NativeActionError) as caught:dispatcher.invoke(calls)
        return caught.exception

    def test_read_only_rejection_has_precise_gate_and_no_mutation(self):
        error=self.reject(self.calls(),REJECTED,1)
        self.assertEqual(error.last_call,1);self.assertEqual(error.failed_method,'EquipmentManager.ddv')
        self.assertIs(error.mutation_started,False)
        self.assertTrue(error.uncertain)  # Legacy interpretation remains unchanged.

    def test_failure_after_first_mutation_cannot_be_mistaken_for_preflight(self):
        for state in (REJECTED,INVALID,EXCEPTION):
            with self.subTest(state=state):
                error=self.reject(self.calls(),state,2)
                self.assertEqual(error.failed_method,'EquipmentManager.ded')
                self.assertIs(error.mutation_started,True);self.assertTrue(error.uncertain)

    def test_unannotated_calls_remain_unknown_instead_of_safely_retryable(self):
        calls=[Invocation('hm.ecc',arguments=(Argument.integer(3),))]
        error=self.reject(calls,REJECTED,0)
        self.assertIsNone(error.mutation_started)
        self.assertEqual(error.failed_method,'hm.ecc')

    def test_cancelled_and_guard_failed_never_report_an_invoked_method(self):
        for state in (CANCELLED,GUARD_FAILED):
            with self.subTest(state=state):
                error=self.reject(self.calls(),state,0)
                self.assertIs(error.mutation_started,False)
                self.assertIsNone(error.last_call);self.assertIsNone(error.failed_method)
                self.assertFalse(error.uncertain)

    def test_invalid_call_cursor_stays_unknown(self):
        self.assertEqual(failure_details(self.calls(),REJECTED,99),
            {'last_call':99,'failed_method':None,'mutation_started':None})


if __name__ == '__main__':unittest.main()
