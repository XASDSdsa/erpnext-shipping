#!/usr/bin/env python3
import gzip
import json
from pathlib import Path
import sys
import tarfile

root = Path(sys.argv[1])
database = list(root.glob('*database.sql.gz'))
config = list(root.glob('*site_config_backup.json'))
public = [p for p in root.glob('*files.tgz') if not p.name.endswith('private-files.tgz')]
private = list(root.glob('*private-files.tgz'))
assert len(database) == len(config) == len(public) == len(private) == 1
assert all(p.stat().st_size > 0 for p in [database[0], config[0], public[0], private[0]])
with gzip.open(database[0], 'rb') as stream:
    while stream.read(1024 * 1024):
        pass
assert isinstance(json.loads(config[0].read_text()), dict)
for path in [public[0], private[0]]:
    with tarfile.open(path) as archive:
        archive.getmembers()
print('BACKUP_OK files=4')
