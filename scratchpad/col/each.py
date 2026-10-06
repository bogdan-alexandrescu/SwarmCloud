import subprocess, sys, pathlib, time
mods = sorted(p.stem for p in pathlib.Path("tests/unit/scripts").glob("test_*.py"))
base = subprocess.run([sys.executable, "-c", "import sys,time; sys.path[:0]=['tests/unit']; import pytest; t=time.perf_counter(); print(time.perf_counter()-t)"], capture_output=True, text=True)
rows = []
for m in mods:
    code = ("import sys,time; sys.path[:0]=['tests/unit']; import pytest; b=set(sys.modules); t=time.perf_counter(); "
            f"import scripts.{m}; d=time.perf_counter()-t; "
            "new=sorted({k.split('.')[0] for k in set(sys.modules)-b}); print(round(d,3), ' '.join(x for x in new if x in ('google','grpc','fastapi','swarm_api','scheduler','control_plane','agent_worker','quota_broker','starlette','pydantic','yaml','swarm_common')))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    rows.append((r.stdout.strip() or r.stderr.strip().splitlines()[-1], m))
print(len(rows), "modules visited")
for out, m in sorted(rows, key=lambda r: -float(r[0].split()[0]) if r[0][0].isdigit() else 0)[:20]:
    print(out[:120], m)
