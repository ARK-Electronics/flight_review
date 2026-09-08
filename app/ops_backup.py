"""Content-addressed off-instance backups and verified restores.

Restore only to an empty directory; never overwrite production. Bucket access
must be private. Original log objects are immutable and shared across snapshots.
"""
import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from sqlite_utils import connect


def digest(path):
    """Hash without buffering flight logs in memory."""
    hasher = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


class ObjectStore:
    """Minimal private S3 transport; credentials come only from environment."""

    def __init__(self):
        import boto3  # pylint: disable=import-outside-toplevel
        self.bucket = os.environ['BACKUP_BUCKET']
        endpoint = os.environ['BACKUP_ENDPOINT']
        if not endpoint.startswith('https://'):
            raise ValueError('Backup endpoint must use HTTPS')
        self.client = boto3.client('s3', endpoint_url=endpoint, region_name='auto',
                                  aws_access_key_id=os.environ['BACKUP_ACCESS_KEY'],
                                  aws_secret_access_key=os.environ['BACKUP_SECRET_KEY'])

    def put(self, key, path):
        """Upload a private object; never configure a public ACL."""
        self.client.upload_file(str(path), self.bucket, key)

    def get(self, key, path):
        """Download a private object for checksum verification."""
        self.client.download_file(self.bucket, key, str(path))

    def exists(self, key):
        """Avoid uploading immutable content twice; surface other failures."""
        from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('404', 'NoSuchKey', 'NotFound'):
                return False
            raise


def backup(source, store):
    """Publish the manifest last, after every referenced object is durable."""
    source = Path(source).resolve()
    manifest = {'version': 1, 'created': datetime.now(timezone.utc).isoformat(), 'files': []}
    with tempfile.TemporaryDirectory() as temporary:
        database = Path(temporary) / 'logs.sqlite'
        with connect((source / 'logs.sqlite').as_uri() + '?mode=ro', uri=True) as con:
            with connect(database) as target:
                con.backup(target)
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database snapshot failed integrity check')
        files = [('logs.sqlite', database)]
        for directory in ('log_files', 'cache/ai_analysis'):
            for path in (source / directory).rglob('*'):
                if path.is_file() and not path.is_symlink() and not path.name.startswith('.'):
                    files.append((path.relative_to(source).as_posix(), path))
        for name, path in files:
            checksum = digest(path)
            key = 'objects/' + checksum
            if not store.exists(key):
                store.put(key, path)
            manifest['files'].append(
                {'path': name, 'sha256': checksum, 'size': path.stat().st_size})
        manifest_file = Path(temporary) / 'manifest.json'
        manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
        key = 'snapshots/' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'
        store.put(key, manifest_file)
        # Prove the manifest and database are readable from the backup service.
        remote_manifest = Path(temporary) / 'remote-manifest.json'
        store.get(key, remote_manifest)
        if json.loads(remote_manifest.read_text(encoding='utf-8')) != manifest:
            raise RuntimeError('Remote manifest mismatch')
        verify = Path(temporary) / 'verify.sqlite'
        store.get('objects/' + manifest['files'][0]['sha256'], verify)
        if digest(verify) != manifest['files'][0]['sha256']:
            raise RuntimeError('Remote database checksum mismatch')
        status = source / 'backup_status.json'
        staged = source / '.backup_status.tmp'
        staged.write_text(json.dumps({'snapshot': key, 'created': manifest['created'],
                                     'files': len(files)}), encoding='utf-8')
        os.replace(staged, status)
        return key


def restore(snapshot, target, store):
    """Restore and verify every file, refusing path traversal and overwrites."""
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError('Restore target must be empty')
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        manifest_file = Path(temporary) / 'manifest.json'
        store.get(snapshot, manifest_file)
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        if manifest.get('version') != 1:
            raise ValueError('Unsupported backup version')
        for item in manifest['files']:
            path = (target / item['path']).resolve()
            if not path.is_relative_to(target) or path == target:
                raise ValueError('Invalid backup path')
            path.parent.mkdir(parents=True, exist_ok=True)
            store.get('objects/' + item['sha256'], path)
            if digest(path) != item['sha256'] or path.stat().st_size != item['size']:
                raise RuntimeError('Backup checksum mismatch: ' + item['path'])
        with connect(target / 'logs.sqlite') as con:
            if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Restored database failed integrity check')
    return len(manifest['files'])


def main():
    """CLI used by the daily backup task and the recovery runbook."""
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--snapshot')
    parser.add_argument('--target')
    args = parser.parse_args()
    store = ObjectStore()
    if args.action == 'backup':
        print(backup(os.environ['STORAGE_PATH'], store))
    else:
        if not args.snapshot or not args.target:
            parser.error('restore requires --snapshot and --target')
        print('Verified restored files:', restore(args.snapshot, args.target, store))


if __name__ == '__main__':
    main()
