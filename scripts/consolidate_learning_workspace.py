"""Preview or apply an offline learning-workspace import without deleting the source."""
import argparse
import json
from pathlib import Path

from quanto.workbench.consolidation import consolidate_learning_workspace


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--destination', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Stop both servers first; backs up databases before importing.')
    args = parser.parse_args()
    result = consolidate_learning_workspace(args.source, args.destination, apply=args.apply)
    print(json.dumps({key: value for key, value in result.items() if key != 'file_hashes'}, ensure_ascii=False, indent=2))
