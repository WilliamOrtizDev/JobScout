#!/usr/bin/env python3
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
errors = []
tracked = subprocess.run(
    ['git', '-C', str(ROOT), 'ls-files', '-z'],
    check=True,
    capture_output=True,
).stdout.decode().split('\0')
private_paths = {
    'config/search-profile.json',
    'config/application-profile.json',
    'config/sources.json',
    'cron/prompt.md',
    'cron/job.template.json',
    'skill/continuous-job-search/SKILL.md',
}
for rel in tracked:
    if not rel:
        continue
    path = ROOT / rel
    if rel in private_paths or rel.startswith(('state/', 'resume/')):
        errors.append(f'{rel}: candidate-specific path must not be tracked')
    if path.suffix == '.json':
        try:
            json.loads(path.read_text())
        except Exception as exc:
            errors.append(f'{path.relative_to(ROOT)}: invalid JSON: {exc}')
    if path.suffix.lower() in {'.pdf', '.png', '.jpg', '.jpeg'}:
        continue
    text = path.read_text(errors='replace')
    patterns = {
        'private key': r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        'GitHub token': r'(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}',
        'generic API token assignment': r'(?im)^(?:ANTHROPIC_API_KEY|GOOGLE_API_KEY|GEMINI_API_KEY|OPENAI_API_KEY|OPENROUTER_API_KEY)\s*=\s*[^\s#]+$',
    }
    for name, pattern in patterns.items():
        for match in re.finditer(pattern, text):
            value = match.group(0)
            if value.endswith('=') or value.endswith('=changeme'):
                continue
            errors.append(f'{path.relative_to(ROOT)}: possible {name}')

job = json.loads((ROOT / 'cron/job.example.json').read_text())
if job.get('schedule') != 'every 15m':
    errors.append('cron/job.example.json: schedule drift')
if job.get('deliver') != '${PHOTON_TARGET}':
    errors.append('cron/job.example.json: delivery target must remain a placeholder')
if job.get('continuity') is not False:
    errors.append('cron/job.example.json: continuity must stay disabled; durable state owns deduplication')
if job.get('script') != 'job-scout-shadow.py':
    errors.append('cron/job.example.json: deterministic pre-run script must be installed')

if errors:
    print('\n'.join(errors), file=sys.stderr)
    raise SystemExit(1)
print('validation passed')
