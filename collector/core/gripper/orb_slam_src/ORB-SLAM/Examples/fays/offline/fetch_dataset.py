#!/usr/bin/env python3
"""Fetch one official TUM-VI sequence; data stays outside the source checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

URL='https://cdn3.vision.in.tum.de/tumvi/exported/euroc/512_16/dataset-room1_512_16.tar'
SIZE=1707110400
# Fingerprint of the official HTTPS download used for this benchmark, not a
# separately published upstream checksum. It pins future regression inputs.
SHA256='20354392eab5cc82c9770ed29cf0130c5acce0db54a257571499008fb34c398f'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--proxy',help='Optional curl proxy; never changes machine settings')
    args=parser.parse_args()
    base=args.output.resolve();base.mkdir(parents=True,exist_ok=True)
    archive=base/'dataset-room1_512_16.tar'
    target=base/'dataset-room1_512_16'
    if target.exists():raise FileExistsError('Refusing to overwrite extracted dataset: '+str(target))
    if shutil.disk_usage(base).free < 4*1024**3:
        raise RuntimeError('Need at least 4 GiB free for archive and extraction')
    cmd=['curl','--fail','--location','--retry','5','--continue-at','-',
         '--connect-timeout','20','--speed-time','120','--speed-limit','4096']
    if args.proxy:cmd.extend(['--proxy',args.proxy])
    if not archive.exists() or archive.stat().st_size!=SIZE:
        subprocess.run([*cmd,URL,'--output',str(archive)],check=True)
    if archive.stat().st_size!=SIZE:raise RuntimeError('Download size mismatch')
    digest=hashlib.sha256()
    with archive.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
    if digest.hexdigest()!=SHA256:raise RuntimeError('Pinned dataset checksum mismatch')
    with tarfile.open(archive) as tar:
        members=tar.getmembers();files=[];skipped=[]
        for member in members:
            name=Path(member.name)
            if name.is_absolute() or '..' in name.parts or name.parts[0]!='dataset-room1_512_16':
                raise ValueError('Unsafe archive path: '+member.name)
            # The two optional DSO image links are redundant with mav0.
            if member.isfile() or member.isdir():files.append(member)
            else:skipped.append(member.name)
        tar.extractall(base,members=files)
    info={'url':URL,'bytes':SIZE,'sha256':SHA256,'skipped_links':skipped}
    (base/'dataset_integrity.json').write_text(json.dumps(info,indent=2))
    print(target/'mav0')


if __name__=='__main__':main()
