#!/usr/bin/env python3

import os
import sys
from glob import glob
from typing import Iterable, Iterator, Optional, Sequence, Tuple

from ._db import Entry, StoringDatabase
from ._db_url import get_db
from ._fnparse import parse_filename
from ._iter_utils import process_in_blocks
from ._timestamps import DEFAULT_TZ, time_ns, timestamp_from_datetime


def main(argv: Sequence[str] = sys.argv) -> None:
    db_url = sys.argv[1]
    db = get_db(db_url)
    entries = get_entries_from_value_files(db)
    insert_or_update_entries(db, entries)
    db.commit()


def get_entries_from_value_files(db: StoringDatabase) -> Iterator[Entry]:
    month_dirs = sorted(glob('[12][0-9][0-9][0-9]-[01][0-9]'))
    for month_dir in month_dirs:
        (year, month) = [int(x) for x in month_dir.split('-')]
        if db.is_done_with_month(year, month):
            continue

        value_files = sorted(glob(os.path.join(month_dir, 'values-*.txt')))
        for val_fn in value_files:
            val_fn_bn = os.path.basename(val_fn)
            day = int(val_fn_bn.replace('values-', '').split('.', 1)[0])
            if not db.is_done_with_day(year, month, day):
                print(f'Doing {val_fn}')
                for (filename, value, error) in parse_value_file(val_fn):
                    fn_data = parse_filename(filename, DEFAULT_TZ)
                    timestamp = timestamp_from_datetime(fn_data.time)
                    yield Entry(timestamp, filename, value, error, time_ns())


def parse_value_file(fn: str) -> Iterator[Tuple[str, Optional[float], str]]:
    with open(fn, 'rt') as value_file:
        for line in value_file:
            if ': ' not in line:
                raise Exception(f'Invalid line in file: {line}')
            (filename, value_or_error) = line.rstrip().split(': ', 1)
            if value_or_error.replace('.', '').isdigit():
                yield (filename, float(value_or_error), '')  # value
            else:
                yield (filename, None, value_or_error)  # error


def insert_or_update_entries(
        db: StoringDatabase,
        entries: Iterable[Entry],
) -> None:
    def process_block(block: Sequence[Entry]) -> None:
        filenames = [x.filename for x in block]
        existing_count = db.count_existing_filenames(filenames)
        if existing_count != len(filenames):
            if existing_count > 0:
                block = [
                    x for x in block
                    if not db.has_filename(x.filename)]
            db.insert_entries(block)

    process_in_blocks(entries, process_block)


if __name__ == '__main__':
    main()