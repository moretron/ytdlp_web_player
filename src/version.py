from datetime import datetime
import os
import importlib
from external import External

External._pip_install('GitPython')

git = importlib.import_module('git')
path = os.path.abspath('.').removesuffix('/src')
repo = git.Repo(path)
latest_commit = repo.head.commit
commit_date = datetime.fromtimestamp(latest_commit.committed_date)
commit_sha = latest_commit.hexsha[:4]

# VERSION holds the project's own release number. Builds off a plain commit
# get it with the build stamp appended (0.1.0+2026-07-28.1a2b) so two images
# from the same release are still tellable apart; a tagged commit reports the
# tag verbatim.
base_version = ''
version_file = os.path.join(path, 'VERSION')
if os.path.exists(version_file):
    with open(version_file, 'r') as f:
        base_version = f.read().strip()

build_stamp = commit_date.strftime('%Y-%m-%d') + '.' + commit_sha
version_string = f'{base_version}+{build_stamp}' if base_version else build_stamp.replace('.', ':')

for tag in repo.tags or []:
    if tag.commit == latest_commit:
        version_string = str(tag)

with open('version.txt', 'w') as f:
    f.write(version_string)
