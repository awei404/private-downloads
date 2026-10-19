"""Build the Mac ZIP (double-click Install-BWI-Monitor.command). Code only by default: the installer asks for the
TMS token and Feishu webhook. --with-secrets also packs secrets/gofo_auth and secrets/feishu_webhook (keep that ZIP
private). Operational data is never packed. Scripts keep their executable bit so Finder can run them."""
import argparse
import hashlib
import time
from pathlib import Path
import zipfile

from build_windows_package import ROOT, VERSION

MAC_FILES = ('install.sh', 'uninstall.sh', 'Install-BWI-Monitor.command', 'Uninstall-BWI-Monitor.command',
             'env.mac.example', 'README-Mac.md', '双击打不开请看.txt')


def build(output, version=VERSION, with_secrets=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    files = {str(p.relative_to(ROOT)): p.read_bytes() for p in sorted((ROOT/'bwi_monitor').iterdir())
             if p.is_file() and p.suffix in ('.py', '.html')}
    files['requirements.txt'] = (ROOT/'requirements.txt').read_bytes()
    for name in MAC_FILES:
        files[name] = (ROOT/'deploy/mac'/name).read_bytes()
    files['tools/import_driver_appointments.py'] = (ROOT/'tools/import_driver_appointments.py').read_bytes()
    files['VERSION.txt'] = (version+'\n').encode()
    if with_secrets:
        for name in ('gofo_auth', 'feishu_webhook'):
            if (ROOT/'secrets'/name).is_file():
                files['secrets/'+name] = (ROOT/'secrets'/name).read_bytes()
    archive = output/'BWI-Monitor-Mac.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo('BWI-Monitor-Mac/'+name, date_time=time.localtime()[:6])
            executable = name.endswith(('.sh', '.command'))
            info.external_attr = ((0o100755 if executable else 0o100600 if name.startswith('secrets/') else 0o100644) << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data.replace(b'\r\n', b'\n') if executable else data)
    (output/'SHA256SUMS-Mac.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
    print(archive)
    return archive


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', default=str(ROOT/'dist/mac'))
    p.add_argument('--version', default=VERSION)
    p.add_argument('--with-secrets', action='store_true', help='pack the TMS token and Feishu webhook (private use only)')
    args = p.parse_args(); build(args.output, args.version, args.with_secrets)
