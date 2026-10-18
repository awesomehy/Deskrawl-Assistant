"""Bounded, identity-scoped evidence of wins actually observed in native data."""
from collections import OrderedDict
from copy import deepcopy


class RunReceipts:
    LIMIT = 256

    def __init__(self):
        self.session = None
        self.character_id = None
        self.sequence = 0
        self.receipts = OrderedDict()

    def observe(self, session, character_id, run):
        # Unknown identity cannot expose a previous character's receipts.
        if (not isinstance(session, str) or not session.strip()
                or not isinstance(character_id, str) or not character_id.strip()):
            return None
        if (session, character_id) != (self.session, self.character_id):
            self.session, self.character_id = session, character_id
            self.receipts.clear()

        won = (isinstance(run, dict) and run.get('status') != 'failed'
               and (run.get('status') == 'success' or run.get('completion_confirmed') is True))
        run_id = run.get('id') if won else None
        if isinstance(run_id, str) and run_id and run_id not in self.receipts:
            self.sequence += 1
            self.receipts[run_id] = {'sequence': self.sequence, 'run': deepcopy(run)}
            if len(self.receipts) > self.LIMIT:
                self.receipts.popitem(last=False)
        return self.snapshot()

    def snapshot(self):
        return {'session': self.session, 'character_id': self.character_id,
                'sequence': self.sequence, 'receipts': deepcopy(list(self.receipts.values()))}
