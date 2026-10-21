"""Package a committed evaluation snapshot and verify its release files."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_name(name):
    path = PurePosixPath(name)
    require(bool(name) and not path.is_absolute() and '..' not in path.parts and str(path) == name,
            'Invalid archive path')
    return path


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def archive_files(archive, prefix):
    safe_name(prefix)
    files = {}
    with tarfile.open(archive, 'r:*') as stream:
        for member in stream:
            path = safe_name(member.name)
            require(path.parts[0] == prefix, 'Archive root differs')
            if member.isdir():
                continue
            require(member.isfile() and len(path.parts) > 1, 'Non-regular archive member')
            name = str(path.relative_to(prefix))
            require(name not in files, 'Duplicate archive member')
            data = stream.extractfile(member).read()
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            files[name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                               git_blob=blob, mode=format(member.mode & 0o777, '03o'))
    return files


def verify_package(directory):
    manifests = list(directory.glob('*-manifest.json'))
    require(len(manifests) == 1, 'Expected one release manifest')
    manifest_path = manifests[0]
    manifest = json.loads(manifest_path.read_text())
    require(manifest['schema_version'] == 1 and manifest['release']['schema_version'] == 1,
            'Unsupported release manifest version')
    tag = manifest['release']['tag']
    require(re.fullmatch(r'[a-z0-9][a-z0-9._-]*', tag), 'Invalid release tag')
    require(manifest_path.name == tag + '-manifest.json', 'Manifest name differs')
    archive = directory / (tag + '.tar.gz')
    expected_names = {archive.name, manifest_path.name}
    checksums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        parts = line.split()
        require(len(parts) == 2 and re.fullmatch(r'[0-9a-f]{64}', parts[0]), 'Invalid checksum line')
        value, name = parts
        require(name in expected_names and name not in checksums, 'Unexpected checksum name')
        checksums[name] = value
    require(set(checksums) == expected_names, 'Checksum inventory differs')
    for name, value in checksums.items():
        require(digest(directory / name) == value, 'Release checksum differs')
    require(archive.stat().st_size == manifest['archive']['bytes'] and
            digest(archive) == manifest['archive']['sha256'], 'Archive metadata differs')
    files = archive_files(archive, manifest['release']['archive_prefix'])
    require(set(files) == set(manifest['files']), 'Missing or extra archive files')
    require(files == manifest['files'], 'Archive file content or mode differs')
    return dict(tag=tag, source_commit=manifest['release']['source_commit'],
                files=len(files), archive_sha256=digest(archive))


def package(root, specification, output):
    tag, commit, prefix = (specification[key] for key in ('tag', 'source_commit', 'archive_prefix'))
    require(specification['schema_version'] == 1 and re.fullmatch(r'[a-z0-9][a-z0-9._-]*', tag),
            'Invalid release specification')
    require(re.fullmatch(r'[0-9a-f]{40}', commit), 'Use a complete source commit')
    safe_name(prefix)
    require(len(PurePosixPath(prefix).parts) == 1, 'Use a single archive root')
    require(git(root, 'rev-parse', '--verify', commit + '^{commit}').decode().strip() == commit,
            'Source commit differs')
    paths = {}
    for row in git(root, 'ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not row:
            continue
        header, name = row.decode().split('\t', 1)
        mode, kind, blob = header.split()
        safe_name(name)
        require(kind == 'blob' and mode in ('100644', '100755'), 'Unsupported source entry')
        paths[name] = dict(git_blob=blob, mode=mode[-3:])
    read = lambda name: git(root, 'show', commit + ':' + name)
    dependencies = json.loads(read('config/dependencies.json'))
    sdk = next(item for item in dependencies['sources'] if item['name'] == 'p4runtime-go-controller')
    require(sdk['commit'] == specification['sdk_commit'], 'Evaluated SDK differs')
    catalog_bytes = read(specification['rpc_catalog'])
    catalog = json.loads(catalog_bytes)
    require(hashlib.sha256(read('data/rpc/manifest.json')).hexdigest() == catalog['dataset_manifest_sha256'],
            'RPC catalog and measurement manifest differ')
    output = output.resolve()
    require(not any(output.is_relative_to(root.resolve() / name) for name in ('data', 'figures')),
            'Write release files outside data and figures')
    output.mkdir(parents=True, exist_ok=False)
    data = git(root, '-c', 'tar.umask=0022', 'archive', '--format=tar', '--prefix=' + prefix + '/', commit)
    archive = output / (tag + '.tar.gz')
    with archive.open('wb') as target, gzip.GzipFile(filename='', mode='wb', fileobj=target, mtime=0) as compressed:
        compressed.write(data)
    files = archive_files(archive, prefix)
    require(set(files) == set(paths), 'Archive and source tree differ')
    for name, expected in paths.items():
        require(all(files[name][key] == value for key, value in expected.items()), 'Archived Git object differs')
    manifest = dict(schema_version=1, release=specification,
                    source_commit_time=git(root, 'show', '-s', '--format=%cI', commit).decode().strip(),
                    archive=dict(name=archive.name, bytes=archive.stat().st_size, sha256=digest(archive)),
                    files=files, rpc_traces=dict(release_tag=catalog['release_tag'], release_url=catalog['release_url'],
                        catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
                        archives=[point['archive'] for point in catalog['points']]))
    manifest_path = output / (tag + '-manifest.json')
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(digest(path) + '  ' + path.name + '\n' for path in (archive, manifest_path)))
    return verify_package(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'config/evaluation-v1.json')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--verify', type=Path, help='Check downloaded release files without a Git checkout')
    args = parser.parse_args()
    if args.verify:
        if args.output:
            parser.error('--verify cannot be combined with --output')
        result = verify_package(args.verify.resolve())
    else:
        specification = json.loads(args.config.read_text())
        result = package(ROOT, specification, args.output or ROOT / 'results/releases' / specification['tag'])
    print(json.dumps(result))


if __name__ == '__main__':
    main()
