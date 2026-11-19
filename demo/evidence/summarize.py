#!/usr/bin/env python3
"""Print per-category totals from an expenses CSV, sorted alphabetically."""

import argparse
import csv
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation

CENTS = Decimal("0.01")


def load_totals(path):
    """Sum amounts per category, returning a dict of category -> Decimal."""
    totals = defaultdict(Decimal)
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty")
        names = {name.strip().lower(): name for name in reader.fieldnames if name}
        try:
            category_field = names["category"]
            amount_field = names["amount"]
        except KeyError:
            raise ValueError("CSV must have 'category' and 'amount' columns")

        for line_number, row in enumerate(reader, start=2):
            category = (row.get(category_field) or "").strip()
            raw_amount = (row.get(amount_field) or "").strip()
            if not category and not raw_amount:
                continue
            try:
                amount = Decimal(raw_amount)
            except InvalidOperation:
                print(
                    "Skipping invalid amount on line {}: {!r}".format(
                        line_number, raw_amount
                    ),
                    file=sys.stderr,
                )
                continue
            totals[category] += amount
    return totals


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Print category totals from a CSV file, sorted alphabetically."
    )
    parser.add_argument("csv_path", help="path to the expenses CSV file")
    parser.add_argument(
        "--total",
        action="store_true",
        help="append a TOTAL line with the sum of all category totals",
    )
    args = parser.parse_args(argv)

    try:
        totals = load_totals(args.csv_path)
    except OSError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    except ValueError as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1

    for category in sorted(totals, key=lambda name: (name.lower(), name)):
        print("{},{:.2f}".format(category, totals[category].quantize(CENTS)))
    if args.total:
        grand_total = sum(totals.values(), Decimal("0"))
        print("TOTAL,{:.2f}".format(grand_total.quantize(CENTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
