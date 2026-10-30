"""Host-side health smoke; no Docker socket in public containers, no secrets printed."""
import json
import subprocess
import sys

r=subprocess.run(['docker','compose',*sys.argv[1:],'ps','--all','--format','json'],capture_output=True,text=True,check=True)
rows=[json.loads(line) for line in r.stdout.splitlines() if line.strip()]
required={'backend','frontend','postgres','worker','caddy','backup'}
seen={row['Service']:row for row in rows if row['Service'] in required}
errors=[name for name in required if name not in seen or seen[name].get('State')!='running' or seen[name].get('Health')!='healthy']
print(json.dumps({'healthy':not errors,'unhealthy_services':sorted(errors)}))
raise SystemExit(bool(errors))
