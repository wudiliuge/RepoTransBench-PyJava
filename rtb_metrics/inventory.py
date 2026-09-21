"""Build an explicit inventory from an independently verified complete JUnit run."""
import argparse
from pathlib import Path
from .core import atomic_json, parse_reports, validate_inventory


def from_reports(root):
    reports = parse_reports(root)
    if reports['errors'] or not reports['cases']:
        raise ValueError('Need nonempty, valid reference JUnit reports: '+repr(reports['errors']))
    tests = {}
    for case in reports['cases']:
        tests.setdefault(case['class'], []).append(case['name'])
    return validate_inventory(tests)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', required=True, type=Path)
    parser.add_argument('--project', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError('Choose a new output path; existing inventory is not overwritten')
    tests=from_reports(args.reports)
    atomic_json(args.output, {args.project:{'tests':tests, 'module_map':{}}})
    print(f'{len(tests)} classes, {sum(map(len,tests.values()))} testcase identities.')
    print('Only use this inventory if the reference run included ALL required tests, including failed/skipped cases.')


if __name__=='__main__':
    main()
