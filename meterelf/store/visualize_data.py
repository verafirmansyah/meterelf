#!/usr/bin/env python3

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    Callable, Iterator, List, NamedTuple, Optional, Sequence, Tuple, Union)

from dateutil.parser import parse as parse_datetime

from ._db_url import get_db
from ._timestamps import DEFAULT_TZ, timestamp_from_datetime
from ._value_getter import ValueGetter
from .process_data import DataProcessor, InterpretedValue, print_warning

START_FROM = parse_datetime('2018-09-24T00:00:00+03:00')

MAX_SYNTHETIC_VALUES_TO_INSERT = 10

EPOCH = parse_datetime('1970-01-01T00:00:00+00:00')
SECONDS_PER_YEAR = 60.0 * 60.0 * 24.0 * 365.24

DateTimeConverter = Callable[[datetime], datetime]

EUR_PER_LITRE = ((1.43 + 2.38) * 1.24) / 1000.0


@dataclass
class GroupedData:
    group_id: str
    min_t: datetime
    max_t: datetime
    min_fv: float
    max_fv: float
    sum: float
    sum_t: timedelta
    synthetic_count: int
    source_points: int
    max_event_num: Optional[int]


@dataclass
class CumulativeGroupedData(GroupedData):
    cum: float
    spp: float
    zpp: int


def main(argv: Sequence[str] = sys.argv) -> None:
    args = parse_args(argv)
    db = get_db(args.db_url)
    value_getter = ValueGetter(db, args.start_from)
    if args.show_ignores:
        print_ignores(value_getter)
    else:
        if args.show_raw_data:
            print_raw_data(value_getter)
        elif args.show_influx_data:
            print_influx_data(value_getter)
        else:
            visualize(
                value_getter=value_getter,
                resolution=args.resolution,
                amend_values=args.amend_values,
                warn=(print_warning if args.verbose else ignore_warning))


class Arguments(NamedTuple):
    db_url: str
    verbose: bool
    show_ignores: bool
    show_raw_data: bool
    show_influx_data: bool
    start_from: datetime
    amend_values: bool
    resolution: str


def parse_args(argv: Sequence[str]) -> Arguments:
    parser = argparse.ArgumentParser()
    parser.add_argument('db_url', type=str, default=None)
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--show-ignores', '-i', action='store_true')
    parser.add_argument('--show-raw-data', '-R', action='store_true')
    parser.add_argument('--show-influx-data', '-I', action='store_true')
    parser.add_argument('--start-from', '-s', default=START_FROM,
                        type=parse_datetime)
    parser.add_argument('--amend-values', '-a', action='store_true')
    parser.add_argument('--resolution', '-r', default='day', choices=[
        'second', 'three-seconds', 'five-seconds', 'minute', 'hour',
        'day', 'week', 'month',
        's', 't', 'f', 'm', 'h',
        'd', 'w', 'M'])

    args = parser.parse_args(argv[1:])

    # Expand resolution shorthand to a proper identifier
    if len(args.resolution) == 1:
        args.resolution = {
            's': 'second',
            't': 'three-seconds',
            'f': 'five-seconds',
            'm': 'minute',
            'h': 'hour',
            'd': 'day',
            'w': 'week',
            'M': 'month',
        }[args.resolution]

    # Make sure start_from datetime has a timezone
    if not args.start_from.tzinfo:
        args.start_from = DEFAULT_TZ.localize(args.start_from)

    return Arguments(
        db_url=args.db_url,
        verbose=args.verbose,
        show_ignores=args.show_ignores,
        show_raw_data=args.show_raw_data,
        show_influx_data=args.show_influx_data,
        start_from=args.start_from,
        amend_values=args.amend_values,
        resolution=args.resolution,
    )


def read_file(path: str) -> str:
    with open(path, 'rt') as fp:
        return fp.read()


def print_ignores(value_getter: ValueGetter) -> None:
    processor = DataProcessor(value_getter, warn=ignore_warning)
    last_value = 0.0
    for x in processor.get_interpreted_data():
        status = (
            'OK' if (x.value and not x.value.correction) else
            'c ' if x.value else
            '  ')
        val = x.value
        corr = val.correction if val else None
        suffix = (
            f' {x.ignore}' if not val else
            f' Correction: {corr:.3f} {val.correction_reason}' if corr
            else '')
        if x.value:
            last_value = x.value.fv
        reading = x.value_row.reading
        reading_str = f'{reading:07.3f}' if reading is not None else ''
        print(
            f'{status} {x.value_row.filename:40} {reading_str:7} '
            f' | {last_value:10.3f}{" " if x.value else "*"} | '
            f'{x.value_row.error}{suffix}')


def print_raw_data(value_getter: ValueGetter) -> None:
    for line in generate_table_data(value_getter):
        print('\t'.join(line))


def print_influx_data(value_getter: ValueGetter) -> None:
    for line in generate_influx_data(value_getter):
        print(line)


def generate_table_data(value_getter: ValueGetter) -> Iterator[List[str]]:
    header_done = False

    for (dt, data) in generate_raw_data(value_getter):
        if not header_done:
            yield ['time'] + [key for (key, _value) in data]
            header_done = True

        ts = f'{dt:%Y-%m-%dT%H:%M:%S.%f%z}'
        yield [ts] + [value for (_key, value) in data]


def generate_influx_data(value_getter: ValueGetter) -> Iterator[str]:
    for (dt, data) in generate_raw_data(value_getter):
        vals = ','.join(f'{key}={value}' for (key, value) in data if value)
        ts = timestamp_from_datetime(dt)
        yield f'water {vals} {ts}'


def generate_raw_data(
        value_getter: ValueGetter,
) -> Iterator[Tuple[datetime, List[Tuple[str, str]]]]:
    processor = DataProcessor(value_getter, warn=ignore_warning)

    for x in processor.get_values():
        data: List[Tuple[str, str]] = [
            ('value', f'{x.fv:.9f}'),
            ('litres_per_minute', f'{60.0 * x.dfv / x.dt.total_seconds():.9f}'
             if x.dfv is not None and x.dt else ''),
            ('value_diff', f'{x.dfv:.9f}' if x.dfv is not None else ''),
            ('time_diff', f'{x.dt.total_seconds():.2f}'
             if x.dt is not None else ''),
            ('correction', f'{x.correction:.9f}'),
            ('event_num', f'{x.filename_data.event_number or ""}'),
            ('format', f'"{x.filename_data.extension or ""}"'),
            ('snapshot', 't' if x.filename_data.is_snapshot else 'f'),
            ('filename', f'"{x.filename}"'),
        ]
        yield (x.t, data)


def ignore_warning(text: str) -> None:
    pass


def visualize(
        value_getter: ValueGetter,
        resolution: str,
        amend_values: bool = False,
        warn: Callable[[str], None] = ignore_warning,
) -> None:
    data = DataGatherer(value_getter, resolution, amend_values,  warn)
    for line in data.get_visualization():
        print(line)


class DataGatherer:
    def __init__(
            self,
            value_getter: ValueGetter,
            resolution: str = 'day',
            amend_values: bool = False,
            warn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.processor = DataProcessor(value_getter, warn)
        self.resolution: str = resolution
        self.amend_values: bool = amend_values

    @property
    def resolution(self) -> str:
        return self._resolution

    @resolution.setter
    def resolution(self, resolution: str) -> None:
        self._resolution = resolution
        self.zeros_per_cumulating = 1
        self._truncate_timestamp: DateTimeConverter = self._truncate_by_step
        if resolution == 'month':
            self._truncate_timestamp = self._truncate_by_month
            self._dt_format = '%Y-%m'
            self._litres_per_bar = 1000.0
            self._step = timedelta(days=30)
        elif resolution == 'week':
            self._truncate_timestamp = self._truncate_by_week
            self._dt_format = '%G-W%V'
            self._litres_per_bar = 100.0
            self._step = timedelta(days=7)
        elif resolution == 'day':
            self._truncate_timestamp = self._truncate_by_day
            self._dt_format = '%Y-%m-%d %a'
            self._litres_per_bar = 10.0
            self._step = timedelta(days=1)
        elif resolution == 'hour':
            self._dt_format = '%Y-%m-%d %a %H'
            self._litres_per_bar = 10.0
            self._step = timedelta(hours=1)
        elif resolution == 'minute':
            self._dt_format = '%Y-%m-%d %a %H:%M'
            self._litres_per_bar = 0.5
            self._step = timedelta(minutes=1)
        elif resolution == 'five-seconds':
            self._dt_format = '%Y-%m-%d %a %H:%M:%S'
            self._litres_per_bar = 0.1
            self._step = timedelta(seconds=5)
        elif resolution == 'three-seconds':
            self.zeros_per_cumulating = 2
            self._dt_format = '%Y-%m-%d %a %H:%M:%S'
            self._litres_per_bar = 0.05
            self._step = timedelta(seconds=3)
        elif resolution == 'second':
            self.zeros_per_cumulating = 3
            self._dt_format = '%Y-%m-%d %a %H:%M:%S'
            self._litres_per_bar = 0.02
            self._step = timedelta(seconds=1)
        else:
            raise ValueError('Unknown resolution: {}'.format(resolution))

    def _truncate_by_month(self, dt: datetime) -> datetime:
        fmt = self._dt_format
        dt_str = dt.strftime(self._dt_format)
        truncated = datetime.strptime(dt_str + ' 1', fmt + ' %d')
        return truncated.replace(tzinfo=dt.tzinfo)

    def _truncate_by_week(self, dt: datetime) -> datetime:
        fmt = self._dt_format
        dt_str = dt.strftime(self._dt_format)
        truncated = datetime.strptime(dt_str + ' 1', fmt + ' %u')
        return truncated.replace(tzinfo=dt.tzinfo)

    def _truncate_by_day(self, dt: datetime) -> datetime:
        fmt = self._dt_format
        truncated = datetime.strptime(dt.strftime(fmt), fmt)
        return truncated.replace(tzinfo=dt.tzinfo)

    def _truncate_by_step(self, dt: datetime) -> datetime:
        secs_since_epoch = (dt - EPOCH).total_seconds()
        num_steps = divmod(secs_since_epoch, self._step.total_seconds())[0]
        truncated = EPOCH + (self._step * num_steps)
        return truncated.astimezone(dt.tzinfo) if dt.tzinfo else truncated

    def _step_timestamp(self, dt: datetime) -> datetime:
        if self.resolution == 'month':
            (y, m) = divmod(12 * dt.year + (dt.month - 1) + 1, 12)
            return datetime(year=y, month=(m + 1), day=1, tzinfo=dt.tzinfo)
        return self._truncate_timestamp(dt) + self._step

    def get_group(self, dt: datetime) -> str:
        return self._truncate_timestamp(dt).strftime(self._dt_format)

    def get_visualization(self) -> Iterator[str]:
        bar_per_litres = 1.0 / self._litres_per_bar
        for entry in self.get_grouped_data_and_gap_lengths():
            if isinstance(entry, timedelta):
                is_long_gap = (entry.total_seconds() >= 30)
                if is_long_gap:
                    yield ''
                yield f'            {entry.total_seconds():7.2f}s = {entry}'
                if is_long_gap:
                    yield ''
                continue
            cum_txt = '{:9.3f}l'.format(entry.cum) if entry.cum else ''
            time_range = entry.max_t - entry.min_t
            drops = entry.sum * 1000.0 * 20.0
            secs = entry.sum_t.total_seconds()
            secs_per_drop = secs / drops if drops else None
            extra = ''

            if not drops or secs_per_drop is None:
                extra = f' {""                   :7}     '
            elif time_range > timedelta(hours=1):
                secs = time_range.total_seconds()
                per_sec = (entry.max_fv - entry.min_fv) / secs
                per_year = per_sec * SECONDS_PER_YEAR
                extra = f' {per_year / 1000.0 :7.1f}kl/y '
            else:
                l_per_min = 60.0 * entry.sum / secs
                extra = f' {l_per_min         :7.4f}l/min'

            if secs_per_drop and secs_per_drop >= 0.05:
                extra += f' ~{secs_per_drop:5.1f}s/drop'
            else:
                extra += f'  {""              :5}      '

            eurs = entry.sum * EUR_PER_LITRE
            if eurs < 0.1:
                price_txt = f'    {eurs*100.0:5.2f}c'
            else:
                price_txt = f'{eurs:6.2f}e   '
            yield (
                '{t0:%Y-%m-%d %a %H:%M:%S}--{t1:%Y-%m-%d %H:%M:%S} '
                '{v0:10.3f}--{v1:10.3f} ds: {sp:6d}{syn:6} {evnt:>5} '
                '{spp:8.3f} {c:10} {s:9.3f}l{extra} {price} {b}').format(
                    t0=entry.min_t,
                    t1=entry.max_t,
                    v0=entry.min_fv,
                    v1=entry.max_fv,
                    syn=(
                        '-{}'.format(entry.synthetic_count)
                        if entry.synthetic_count else ''),
                    sp=entry.source_points,
                    evnt='#{}'.format(
                        entry.max_event_num) if entry.max_event_num else '',
                    spp=entry.spp,
                    c=cum_txt,
                    s=entry.sum,
                    price=price_txt,
                    extra=extra,
                    b=make_bar(entry.sum * bar_per_litres))

    def get_grouped_data_and_gap_lengths(
            self
    ) -> Iterator[Union[CumulativeGroupedData, timedelta]]:
        last_period = None
        sum_per_period = 0.0
        zeroings_per_period = 1
        cumulative_since_0 = 0.0
        zeros_in_row = 0
        for (entry, gap) in self._get_grouped_data_and_gap_lengths():
            is_big_gap = (gap and gap >= timedelta(seconds=30))
            if entry:
                sum_per_period += entry.sum
                cumulative_since_0 += entry.sum
            if is_big_gap or (entry and entry.sum == 0.0):
                zeros_in_row += 1
                if is_big_gap or zeros_in_row >= self.zeros_per_cumulating:
                    cumulative_since_0 = 0.0
                    if zeros_in_row == self.zeros_per_cumulating:
                        zeroings_per_period += 1
            else:
                zeros_in_row = 0

            if gap:
                yield gap
                continue
            elif entry:
                period = entry.min_t.strftime('%Y-%m-%d')
                if period != last_period:
                    sum_per_period = 0.0
                    zeroings_per_period = 1
                    last_period = period

                yield CumulativeGroupedData(
                    cum=cumulative_since_0,
                    spp=sum_per_period,
                    zpp=zeroings_per_period,
                    **entry.__dict__,
                )

    def _get_grouped_data_and_gap_lengths(
            self
    ) -> Iterator[Tuple[Optional[GroupedData], Optional[timedelta]]]:
        last_entry = None
        for entry in self._get_grouped_data():
            if last_entry:
                last_end = last_entry.max_t
                this_start = entry.min_t
                if self._has_time_steps_between(last_end, this_start):
                    yield (None, this_start - last_end)
            yield (entry, None)
            last_entry = entry

    def _get_grouped_data(self) -> Iterator[GroupedData]:
        last_group = None
        entry = None
        get_values = (self._get_amended_values if self.amend_values
                      else self.processor.get_values)
        for value in get_values():
            group = self.get_group(value.t)
            if last_group is None or group != last_group:
                last_group = group
                if entry:
                    yield entry
                entry = GroupedData(
                    group_id=group,
                    min_t=value.t,
                    max_t=value.t,
                    min_fv=value.fv,
                    max_fv=value.fv,
                    sum=(value.dfv or 0.0),
                    sum_t=(value.dt or timedelta(0)),
                    synthetic_count=(1 if value.synthetic else 0),
                    source_points=1,
                    max_event_num=value.filename_data.event_number,
                )
            else:
                entry.min_t = min(value.t, entry.min_t)
                entry.max_t = max(value.t, entry.max_t)
                entry.min_fv = min(value.fv, entry.min_fv)
                entry.max_fv = max(value.fv, entry.max_fv)
                entry.sum += (value.dfv or 0.0)
                entry.sum_t += (value.dt or timedelta(0))
                entry.synthetic_count += (1 if value.synthetic else 0)
                entry.source_points += 1
                entry.max_event_num = max(
                    value.filename_data.event_number or 0,
                    entry.max_event_num or 0) or None
        if entry:
            yield entry

    def _get_amended_values(self) -> Iterator[InterpretedValue]:
        last_value = None
        for value in self.processor.get_values():
            if last_value and value.dfv > 0.1 and last_value.dfv > 0:
                t_steps = list(self._get_time_steps_between(
                    last_value.t, value.t))
                if t_steps:
                    t_steps = t_steps[-MAX_SYNTHETIC_VALUES_TO_INSERT:]
                    fv_step = value.dfv / len(t_steps)
                    cur_fv = last_value.fv
                    sum_of_amendeds = 0.0
                    for cur_t in t_steps:
                        cur_fv += fv_step
                        new_value = InterpretedValue(
                            t=cur_t,
                            fv=cur_fv,
                            dt=(cur_t - last_value.t),
                            dfv=(cur_fv - last_value.fv),
                            correction=0.0,
                            synthetic=True,
                            filename=value.filename,
                            filename_data=value.filename_data,
                        )
                        yield new_value
                        sum_of_amendeds += new_value.dfv
                        last_value = new_value
                    too_much = sum_of_amendeds - value.dfv
                    assert abs(too_much) < 0.0001
                    continue
            yield value
            last_value = value

    def _get_time_steps_between(
            self,
            start: datetime,
            end: datetime,
    ) -> Iterator[datetime]:
        t = self._step_timestamp(start)
        while t < end:
            yield t
            t = self._step_timestamp(t)

    def _has_time_steps_between(
            self,
            start: datetime,
            end: datetime,
    ) -> bool:
        return self._step_timestamp(start) < self._truncate_timestamp(end)


BAR_SYMBOLS = [
    '\u258f', '\u258e', '\u258d', '\u258c',
    '\u258b', '\u258a', '\u2589', '\u2588'
]
BAR_SYMBOLS_MAP = {n: symbol for (n, symbol) in enumerate(BAR_SYMBOLS)}
BAR_SYMBOL_FULL = BAR_SYMBOLS[-1]


def make_bar(value: float) -> str:
    if value < 0:
        return '-' + make_bar(-value)
    totals = int(value)
    fractions = value - totals
    if fractions == 0.0:
        last_symbol = ''
    else:
        last_sym_index = int(round(fractions * (len(BAR_SYMBOLS) - 1)))
        last_symbol = BAR_SYMBOLS_MAP.get(last_sym_index, 'ERR')
    return (BAR_SYMBOL_FULL * totals) + last_symbol


if __name__ == '__main__':
    main()