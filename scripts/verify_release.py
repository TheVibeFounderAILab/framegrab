#!/usr/bin/env python3
"""Check tracked source for accidental private artifacts and inventory drift."""
import hashlib,json,re,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BLOCKED_PARTS={'.git','.claude','.codex','Downloads','downloads','venv','.venv','.transcribe-venv','__pycache__','private-projects','research-projects'}
BLOCKED_SUFFIXES={'.pyc','.sqlite','.sqlite3','.db','.mov','.mp4','.mp3','.wav','.m4a','.pem','.key','.csv','.log'}
PATTERNS={
 'personal filesystem path':re.compile('/'+'Users/'+r'|[A-Z]:\\'+'Users'+r'\\'),
 'email address':re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b',re.I),
 'credential':re.compile(r'\b(?:sk' + r'-[A-Za-z0-9_-]{20,}|gh' + r'p_[A-Za-z0-9]{20,}|github' + r'_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b'),
 'private key':re.compile('-----BEGIN '+r'[A-Z ]*PRIVATE KEY-----'),
 'creator personal name':re.compile('Ajay'+r'\s+'+'Yadav',re.I),
}
def files():
 if (ROOT/'.git').exists():
  return [ROOT/p for p in subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0') if p and p!='MANIFEST.json']
 return sorted(p for p in ROOT.rglob('*') if p.is_file() and p.name!='MANIFEST.json')
def main():
 records=[]; errors=[]
 for p in sorted(files()):
  rel=p.relative_to(ROOT); name=rel.as_posix()
  if p.is_symlink() or set(rel.parts)&BLOCKED_PARTS or p.suffix.lower() in BLOCKED_SUFFIXES or p.name.startswith('.env'):
   errors.append(name+': forbidden artifact');continue
  if rel.parts[0] in {'input','work','final'} and name not in {'input/README.md','work/README.md','final/README.md'}:
   errors.append(name+': working media must stay local')
  data=p.read_bytes();records.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
  try: text=data.decode('utf8')
  except UnicodeDecodeError:
   if p.suffix.lower() not in {'.woff2','.ttf','.ico'}: errors.append(name+': unexpected binary')
   continue
  for label,pattern in PATTERNS.items():
   if pattern.search(text):errors.append(name+': '+label)
 license=(ROOT/'LICENSE').read_text()
 if 'MIT License' not in license or 'Copyright (c) 2026 TheVibeFounder' not in license:errors.append('Missing original MIT license')
 if errors:print('\n'.join(errors));return 1
 manifest={'version':'1.0.1','files':records}
 if '--write-manifest' in sys.argv:(ROOT/'MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
 elif json.loads((ROOT/'MANIFEST.json').read_text())!=manifest:
  print('MANIFEST.json differs from source; review changes and regenerate.');return 1
 print(f'PASS: {len(records)} source files checked; privacy patterns and inventory pass.')
 return 0
if __name__=='__main__':raise SystemExit(main())
