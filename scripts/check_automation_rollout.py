#!/usr/bin/env python3
"""Compare an explicit automation manifest without installing or executing files."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(root, relative):
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError('manifest path escapes its root')
    return path


def check(manifest, source_root, home):
    if not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise ValueError('unsupported manifest schema')
    components = manifest.get('components')
    if not isinstance(components, list) or not components:
        raise ValueError('manifest must list components')
    results = []
    seen = set()
    for entry in components:
        if (not isinstance(entry, dict)
                or any(not isinstance(entry.get(key), str) or not entry[key]
                       for key in ('id', 'source', 'target'))
                or entry['id'] in seen):
            raise ValueError('invalid or duplicate component')
        seen.add(entry['id'])
        source = bounded(source_root, entry['source'])
        target = bounded(home, entry['target'])
        expected = digest(source) if source.is_file() else None
        actual = digest(target) if target.is_file() else None
        state = ('source-missing' if expected is None else 'missing' if actual is None
                 else 'matching' if expected == actual else 'stale')
        results.append({'component': entry['id'], 'source': entry['source'],
                        'target': entry['target'], 'source_sha256': expected,
                        'installed_sha256': actual, 'state': state})
    return {'schema_version': 1, 'read_only': True, 'components': results,
            'matching': all(row['state'] == 'matching' for row in results)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('automation-rollout.json'))
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--home', type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        result = check(json.loads(args.manifest.read_text()), args.source_root, args.home)
    except (ValueError, KeyError, OSError) as exc:
        print(f'rollout check failed: {type(exc).__name__}', file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result['matching'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
