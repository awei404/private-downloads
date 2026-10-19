"""Build the Windows ZIP from an explicit allowlist. Code only by default: the installer asks for the TMS token and
Feishu webhook in a paste window. --with-secrets also packs secrets/gofo_auth and secrets/feishu_webhook so the
install asks nothing (keep that ZIP private). Operational data is never packed."""
import argparse
import hashlib
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '2026.10.19.1'
WINDOWS_FILES = ('install.ps1','uninstall.ps1','run_bwi.bat','Install-BWI-Monitor.bat',
                 'Uninstall-BWI-Monitor.bat','env.windows.example','README-Windows.md','Download-Windows.bat')


def build(output, version=VERSION, with_secrets=False):
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', version):
        raise ValueError('version must be YYYY.MM.DD.N')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in sorted((ROOT/'bwi_monitor').iterdir())
             if p.is_file() and p.suffix in ('.py', '.html')}
    files['requirements.txt'] = (ROOT/'requirements.txt').read_bytes()
    for name in WINDOWS_FILES:
        files[name] = (ROOT/'deploy/windows'/name).read_bytes()
    files['tools/import_driver_appointments.py'] = (ROOT/'tools/import_driver_appointments.py').read_bytes()
    files['VERSION.txt'] = (version+'\n').encode()
    if with_secrets:
        for name in ('gofo_auth', 'feishu_webhook'):
            if (ROOT/'secrets'/name).is_file():
                files['secrets/'+name] = (ROOT/'secrets'/name).read_bytes()
    checksums = ''.join(hashlib.sha256(data).hexdigest()+'  '+name+'\n' for name,data in sorted(files.items()))
    files['PACKAGE-SHA256SUMS.txt'] = checksums.encode()
    archive = output/'BWI-Monitor-Windows.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(files.items()):
            if name.endswith(('.bat','.ps1')):
                data = data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
            z.writestr('BWI-Monitor-Windows/'+name,data)
    # File hashes reflect the packaged CRLF bytes, not working-tree line endings.
    with zipfile.ZipFile(archive) as z:
        hashes = ''.join(hashlib.sha256(z.read(name)).hexdigest()+'  '+name.split('/',1)[1]+'\n'
                         for name in z.namelist() if not name.endswith('PACKAGE-SHA256SUMS.txt'))
        content = {name:z.read(name) for name in z.namelist()}
    content['BWI-Monitor-Windows/PACKAGE-SHA256SUMS.txt'] = hashes.encode()
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in content.items(): z.writestr(name,data)
    (output/'SHA256SUMS.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    (output/'Download-Windows.bat').write_bytes(files['Download-Windows.bat'].replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'))
    print(archive)
    return archive


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default=str(ROOT/'dist/windows'))
    p.add_argument('--version',default=VERSION)
    p.add_argument('--with-secrets',action='store_true',help='pack the TMS token and Feishu webhook (private use only)')
    args=p.parse_args();build(args.output,args.version,args.with_secrets)
