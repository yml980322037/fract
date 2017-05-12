#!/usr/bin/env python

from datetime import datetime
import logging
import signal
import time
import numpy as np
import oandapy
from ..cli.util import dump_yaml, FractError


class Bollinger(oandapy.API):
    def __init__(self, oanda, margin_ratio, model, quiet=False):
        super().__init__(environment=oanda['environment'],
                         access_token=oanda['access_token'])
        self.account_id = oanda['account_id']
        self.account_currency = self.get_account(
            account_id=self.account_id
        )['accountCurrency']
        self.margin_ratio = margin_ratio
        self.model = model['bollinger']
        self.quiet = quiet
        logging.debug('Bollinger:\n{}'.format(dump_yaml({
            'self.account_id': self.account_id,
            'self.account_currency': self.account_currency,
            'self.margin_ratio': self.margin_ratio,
            'self.model': self.model,
            'self.quiet': self.quiet
        })))
        self.instrument_list = [
            d['instrument'] for d in
            self.get_instruments(account_id=self.account_id)['instruments']
        ]
        logging.debug('self.instrument_list: {}'.format(self.instrument_list))

    def auto(self, instrument):
        t0 = datetime.now()
        rate = self._get_rate(instrument=instrument)
        logging.debug('rate: {}'.format(rate))

        if rate['halted']:
            self._print(
                'Skip for trading halted.', instrument=instrument
            )
            self._sleep(last=t0, sec=0.5)
        else:
            prices = self._get_prices()
            logging.debug('prices: {}'.format(prices))

            self._sleep(last=t0, sec=0.5)
            units = self._calc_units(
                rate=rate, prices=prices, margin=self._get_margin()
            )
            logging.debug('units: {}'.format(units))

            self._sleep(last=t0, sec=1)
            if units == 0:
                self._print(
                    'Skip for lack of margin.', instrument=instrument
                )
            else:
                ws = self._calc_window_stat(
                    window=self._get_window(instrument=instrument)
                )
                logging.debug('ws: {}'.format(ws))

                max_spread = ws['std'] * self.model['sigma']['max_spread']
                logging.debug('max_spread: {}'.format(max_spread))

                if prices[instrument]['spread'] > max_spread:
                    self._print(
                        'Skip for large spread.', instrument=instrument
                    )
                elif ws['last'] > ws['up_bound']:
                    od = self._place_order(sd=ws['std'],
                                           prices=prices,
                                           rate=rate,
                                           side='buy',
                                           units=units)
                    self._print(
                        'Buy {0} units.\n{1}'.format(units, dump_yaml(od)),
                        instrument=instrument
                    )
                elif ws['last'] < ws['low_bound']:
                    od = self._place_order(sd=ws['std'],
                                           prices=prices,
                                           rate=rate,
                                           side='sell',
                                           units=units)
                    self._print(
                        'Sell {0} units.\n{1}'.format(units, dump_yaml(od)),
                        instrument=instrument
                    )
                else:
                    self._print(
                        'Skip by the criteria.', instrument=instrument
                    )
        return rate

    def _get_rate(self, instrument):
        return self.get_instruments(
            account_id=self.account_id,
            instruments=instrument,
            fields=','.join(['displayName',
                             'pip',
                             'maxTradeUnits',
                             'precision',
                             'maxTrailingStop',
                             'minTrailingStop',
                             'marginRate',
                             'halted'])
        )['instruments'][0]

    def _get_prices(self):
        return dict(
            map(lambda p:
                (p['instrument'],
                 {'bid': p['bid'],
                  'ask': p['ask'],
                  'spread': np.float32(p['ask'] - p['bid'])}),
                self.get_prices(
                    account_id=self.account_id,
                    instruments=','.join(self.instrument_list)
                )['prices'])
        )

    def _get_margin(self):
        return (
            lambda a:
            {'avail': a['marginAvail'],
             'used': a['marginUsed'],
             'total': a['marginAvail'] + a['marginUsed']}
        )(self.get_account(account_id=self.account_id))

    def _get_window(self, instrument):
        return {
            'instrument': instrument,
            'midpoints': np.array([
                d['closeMid']
                for d in
                self.get_history(
                    account_id=self.account_id,
                    candleFormat='midpoint',
                    instrument=instrument,
                    granularity=self.model['window']['granularity'],
                    count=self.model['window']['size']
                )['candles']
            ])
        }

    def _calc_units(self, rate, prices, margin):
        inst = rate['instrument']
        cur_pair = inst.split('_')
        logging.debug('cur_pair: {}'.format(cur_pair))
        if cur_pair[0] == self.account_currency:
            bp = 1 / prices[inst]['ask']
        elif cur_pair[1] == self.account_currency:
            bp = prices[inst]['ask']
        else:
            inst_bp = [
                (inst if inst in self.instrument_list else None)
                for inst in
                map(lambda p: '_'.join(p),
                    [(cur_pair[1], self.account_currency),
                     (self.account_currency, cur_pair[1])])
            ]
            logging.debug('inst_bp: {}'.format(inst_bp))
            if inst_bp[0]:
                bp = prices[inst]['ask'] * prices[inst_bp[0]]['ask']
            elif inst_bp[1]:
                bp = prices[inst]['ask'] / prices[inst_bp[1]]['ask']
            else:
                raise FractError('invalid instruments')
        logging.debug('bp: {}'.format(bp))

        mg = dict([(k, v * (margin['avail'] + margin['used']))
                   for k, v in self.margin_ratio.items()])
        logging.debug('mg: {}'.format(mg))
        mg_per_unit = bp * rate['marginRate']
        logging.debug('mg_per_unit: {}'.format(mg_per_unit))

        if mg['ticket'] < (margin['avail'] - mg['preserve']):
            units = np.int32(np.floor(mg['ticket'] / mg_per_unit))
            if units <= rate['maxTradeUnits']:
                return units
            else:
                return rate['maxTradeUnits']
        else:
            return 0

    def _calc_window_stat(self, window):
        if window['midpoints'].shape[0] == self.model['window']['size']:
            return (
                lambda i, l, m, s, t:
                {
                    'instrument': i,
                    'last': np.float32(l),
                    'mean': np.float32(m),
                    'std': np.float32(s),
                    'up_bound': np.float32(m + s * t),
                    'low_bound': np.float32(m - s * t)
                }
            )(
                i=window['instrument'],
                l=window['midpoints'][-1],
                m=window['midpoints'].mean(),
                s=window['midpoints'].std(),
                t=self.model['sigma']['entry_trigger']
            )
        else:
            raise FractError('window size not matched')

    def _place_order(self, sd, prices, rate, side, units):
        trail_p = sd * self.model['sigma']['trailing_stop']
        pr = prices[rate['instrument']]
        ts = np.int16(np.ceil(
            (trail_p + pr['spread']) / np.float32(rate['pip'])
        ))
        if ts > rate['maxTrailingStop']:
            trailing_stop = np.int16(rate['maxTrailingStop'])
        elif ts < rate['minTrailingStop']:
            trailing_stop = np.int16(rate['minTrailingStop'])
        else:
            trailing_stop = ts
        logging.debug('trailing_stop: {}'.format(trailing_stop))

        stop_p = sd * self.model['sigma']['stop_loss']
        profit_p = sd * self.model['sigma']['take_profit']
        if side == 'buy':
            stop_loss = np.float16(pr['ask'] - stop_p)
            take_profit = np.float16(pr['ask'] + profit_p)

        elif side == 'sell':
            stop_loss = np.float16(pr['bid'] + stop_p)
            take_profit = np.float16(pr['bid'] - profit_p)
        else:
            raise FractError('invalid side')
        logging.debug(
            'take_profit: {0}, stop_loss: {1}'.format(take_profit, stop_loss)
        )

        return self.create_order(account_id=self.account_id,
                                 units=units,
                                 instrument=rate['instrument'],
                                 side=side,
                                 takeProfit=take_profit,
                                 stopLoss=stop_loss,
                                 trailingStop=trailing_stop,
                                 type='market')

    def _print(self, message, instrument=None):
        text = '[ {0} - {1} ]\t{2}{3}'.format(
            __package__,
            self.__class__.__name__,
            ((instrument + '\t>>>>>>\t') if instrument else ''),
            message
        )
        if self.quiet:
            logging.debug(text)
        else:
            print(text, flush=True)

    def _sleep(self, last, sec=0.5):
        rest = sec - (datetime.now() - last).total_seconds()
        if rest > 0:
            time.sleep(rest)


def open_deals(config, instruments, n=10, interval=2, quiet=False):
    insts = (instruments if instruments else config['trade']['instruments'])
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    deal = Bollinger(oanda=config['oanda'],
                     model=config['trade']['model'],
                     margin_ratio=config['trade']['margin_ratio'],
                     quiet=quiet)
    deal._print('!!! OPEN DEALS !!!')
    for i in range(n):
        halted = all([
            deal.auto(instrument=inst)['halted']
            for inst in insts
        ])
        if halted or i == n - 1:
            break
        else:
            time.sleep(interval)
