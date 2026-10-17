from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch
from deskrawl_assistant.web_service import AssistantService
from tests import test_managed_service as fixtures


class MarketServiceTests(unittest.TestCase):
    setUp=fixtures.ManagedServiceTests.setUp

    def test_only_current_selection_is_looked_up_and_no_inventory_leaves_service(self):
        raw=fixtures.equipment(bound=False)
        self.service.snapshot={'containers':{'inventory':{'slots':[raw]}}}
        self.service.market=Mock()
        self.service.market.for_item.return_value={'eligible':True,'lowest_sell':'1.23','highest_buy':'1.20','currency':'CNY'}
        result=self.service.market_quotes([raw['transfer_key'],'gone-id'])
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['selection_id'],raw['transfer_key'])
        self.assertEqual(result['items'][0]['quote']['highest_buy'],'1.20')
        self.service.market.for_item.assert_called_once_with(raw)
        self.assertEqual(self.client.actions,[])

    def test_client_cannot_supply_market_names_or_arbitrary_url(self):
        for value in ('https://evil.invalid', [{'url':'https://evil.invalid'}],['x']*129,['']):
            with self.subTest(value=str(value)[:50]),self.assertRaises(ValueError):self.service.market_quotes(value)

    def test_disconnected_game_does_not_reuse_previous_inventory_quotes(self):
        self.service.market=Mock()
        self.client.connected=False
        self.assertEqual(self.service.market_quotes(['old-id']),{'items':[], 'currency_id':0})
        self.service.market.for_item.assert_not_called()

    def test_currency_saves_independently_and_restores_after_restart(self):
        before = deepcopy(self.service.managed.settings)
        self.assertEqual(self.service.market_settings()['currency_id'],0)
        result = self.service.set_market_currency(23)
        self.assertEqual(result['currency_id'],23)
        self.assertEqual(self.service.managed.settings, before)
        self.assertEqual(json.loads(self.service.market_config.read_text())['currency_id'],23)
        restored = AssistantService(fixtures.FakeManagedClient(), self.service.config)
        self.addCleanup(restored.close)
        self.assertEqual(restored.market_settings()['currency_id'],23)

    def test_invalid_setting_and_failed_save_do_not_change_current_selection(self):
        self.service.set_market_currency(23)
        old = self.service.market_config.read_bytes()
        for value in (True,'23',-1,30,999):
            with self.subTest(value=value), self.assertRaises(ValueError): self.service.set_market_currency(value)
        with patch('pathlib.Path.replace', side_effect=OSError('not writable')):
            with self.assertRaises(OSError): self.service.set_market_currency(1)
        self.assertEqual(self.service.market_settings()['currency_id'],23)
        self.assertEqual(self.service.market_config.read_bytes(), old)

    def test_saved_currency_is_used_for_quotes_without_mutating_native_cache(self):
        raw = fixtures.equipment(bound=False)
        self.service.snapshot = {'containers':{'inventory':{'slots':[raw]}}}
        self.service.market = Mock()
        native = {'status':'ready','eligible':True,'currency':'HKD','currency_id':29,'lowest_sell':'2.00'}
        self.service.market.for_item.return_value = native
        self.service.market_currency = Mock()
        self.service.market_currency.for_quote.return_value = {**native,'currency':'CNY','converted':True,'lowest_sell':'1.80'}
        self.service.set_market_currency(23)
        result = self.service.market_quotes([raw['transfer_key']])
        self.assertEqual(result['items'][0]['quote']['currency'],'CNY')
        self.service.market_currency.for_quote.assert_called_once_with(native,23)
        self.assertEqual(native['currency'],'HKD')
        self.assertFalse(self.client.actions)

    def test_trade_filter_eligibility_is_local_and_checks_instance_binding(self):
        gold = fixtures.potion(key='GoldBar'); gold['base_rarity'] = 3
        mist = fixtures.equipment(2, key='LegendaryBoots1',bound=False,is_black_mist=True)
        bound = fixtures.equipment(3, key='LegendaryBoots1',bound=True,is_black_mist=True)
        self.client.items = [gold, fixtures.potion(1), mist, bound]
        self.service._publish(self.client.snapshot())
        items = self.service.state()['items']
        self.assertEqual([i['market_eligible'] for i in items], [True,False,True,False])
        self.assertIsNone(self.service.market)
        self.assertIsNone(self.service.market_currency)
        self.assertFalse(self.client.actions)

    def test_missing_market_dictionary_does_not_break_inventory(self):
        with patch('deskrawl_assistant.steam_market.MarketCatalog.load', side_effect=OSError('missing')):
            result = self.service.state()
        self.assertEqual(len(result['items']),1)
        self.assertIs(result['items'][0]['market_eligible'],False)
        self.assertFalse(self.client.actions)


if __name__=='__main__':unittest.main()
