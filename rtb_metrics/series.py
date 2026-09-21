"""Local, standard-library batch scheduling and evidence-based report merging."""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from .core import atomic_json, write_report, validate_inventory, validate_module_map


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


@contextmanager
def staging(parent):
    parent=Path(parent).resolve();parent.mkdir(parents=True,exist_ok=True)
    path=parent/('rtb-merge-'+uuid.uuid4().hex)
    path.mkdir()
    try:yield path
    finally:
        if path.resolve().parent != parent:raise ValueError('Unsafe staging cleanup')
        shutil.rmtree(path)


def make_plan(projects, size, config, inventories):
    if not projects or len(projects) != len(set(projects)):
        raise ValueError('Project list must be nonempty and contain no duplicates')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p) for p in projects):
        raise ValueError('Invalid project name')
    if type(size) is not int or size < 1:
        raise ValueError('Batch size must be positive')
    if set(inventories)-set(projects):
        raise ValueError('Inventory contains projects outside the series')
    for item in inventories.values():
        validate_inventory(item['tests']); validate_module_map(item['tests'],item.get('module_map'))
    return dict(version=1, config=config, projects=projects, batch_size=size,
                batches=[dict(name=f'batch_{i//size+1:03d}',projects=projects[i:i+size],
                              inventory={p:inventories[p] for p in projects[i:i+size] if p in inventories})
                         for i in range(0,len(projects),size)])


def signature(batch):
    config=dict(batch.get('config',{}))
    for k in ('batch_id','projects','inventory_file','check_only','skip_project_archive'):
        config.pop(k,None)
    return dict(model=batch['model'],config=config)


def merge_batches(paths, destination):
    paths=[Path(p).resolve() for p in paths]; destination=Path(destination).resolve()
    if not paths or len(set(paths)) != len(paths):
        raise ValueError('Select distinct batch directories')
    if any(destination == p or destination in p.parents or p in destination.parents for p in paths):
        raise ValueError('Merge output must be separate from input batches')
    batches=[read(p/'batch.json') for p in paths]
    if len({b['batch_id'] for b in batches}) != len(batches):
        raise ValueError('Duplicate batch IDs')
    if any(signature(b) != signature(batches[0]) for b in batches):
        raise ValueError('Different models/configurations cannot be merged')
    projects=[p for b in batches for p in b['projects']]
    make_plan(projects,10,{}, {})  # also rejects repeated projects and unsafe paths
    combined='merged_'+uuid.uuid4().hex
    durations=[];warnings=[]
    if 'base_url' not in batches[0].get('config',{}):
        warnings.append('Legacy batches do not record endpoint/data identity; verify these manually.')
    # Reuse single-batch evidence validation in a disposable staging directory.
    with staging(destination.parent) as tmp:
        stage=Path(tmp)
        atomic_json(stage/'batch.json',dict(batch_id=combined,model=batches[0]['model'],projects=projects))
        for path,batch in zip(paths,batches):
            for project in batch['projects']:
                src=path/'tasks'/project; dst=stage/'tasks'/project
                if (src/'result.json').exists():
                    row=read(src/'result.json')
                    if row.get('batch_id') != batch['batch_id'] or row.get('project') != project:
                        raise ValueError('Task evidence belongs to a different batch/project')
                    row['batch_id']=combined
                    atomic_json(dst/'result.json',row)
                if (src/'requests.jsonl').exists():
                    dst.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(src/'requests.jsonl',dst/'requests.jsonl')
            timing=path/'launcher_timing.json'
            value=None
            if timing.exists():
                t=read(timing)
                if t.get('batch_id') != batch['batch_id']:raise ValueError('Mismatched launcher timing')
                value=t.get('total_elapsed_seconds')
            durations.append(value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None)
        s=write_report(stage)
        s.update(source_batches=[dict(path=str(p),batch_id=b['batch_id']) for p,b in zip(paths,batches)],
                 config=signature(batches[0]),warnings=warnings,
                 total_elapsed_seconds=sum(durations) if all(v is not None for v in durations) else None,
                 known_total_elapsed_seconds=sum(v for v in durations if v is not None),
                 total_elapsed_scope='Sum of measured batch host durations; excludes gaps between batches; overlapping batches are not wall-clock elapsed time.')
        # Only publish after all source evidence has been validated. Never modify input reports.
        destination.mkdir(parents=True,exist_ok=True)
        atomic_json(destination/'summary.json',s)
        shutil.copyfile(stage/'projects.csv',destination/'projects.csv')
        text=(stage/'summary.txt').read_text(encoding='utf-8')
        text=re.sub(r'(?m)^Total measured workflow seconds \(host\):.*$', 'Cumulative host seconds: '+str(s['total_elapsed_seconds']),text)
        text += '\nMerged batches: '+str(len(paths))+'\nOnly the listed source batches are included; this may be a partial series.\n'
        text += '\n'.join(warnings)+'\n'
        (destination/'summary.txt').write_text(text,encoding='utf-8')
        return s


@contextmanager
def series_lock(destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    with (destination/'.series.lock').open('a+b') as handle:
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('This series is already running in another process') from exc
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)


def run_series(plan, destination, repo, execute=None, check_only=False):
    with series_lock(destination):
        return _run_series(plan,destination,repo,execute,check_only)


def _run_series(plan, destination, repo, execute=None, check_only=False):
    destination=Path(destination).resolve();repo=Path(repo).resolve()
    destination.mkdir(parents=True,exist_ok=True)
    manifest=destination/'series.json'
    if manifest.exists():
        if read(manifest) != plan:raise ValueError('Existing series has different projects/configuration/inventory; use a new Destination')
    else:
        if any(p.name!='.series.lock' for p in destination.iterdir()):raise ValueError('New series destination must be empty')
        atomic_json(manifest,plan)
    def launch(params):
        # Serialize arguments as data; do not interpolate user strings into shell code.
        atomic_json(destination/'invocation.json',params)
        wrapper=destination/'invoke.ps1'
        wrapper.write_text("param($Repo,$Config)\n$ErrorActionPreference='Stop'\n$c=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json\n$a=@{}\n$c.PSObject.Properties | ForEach-Object {$a[$_.Name]=$_.Value}\n& (Join-Path $Repo 'scripts/run_metrics.ps1') @a\nexit $global:LASTEXITCODE\n",encoding='utf-8-sig')
        return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(wrapper),str(repo),str(destination/'invocation.json')]).returncode
    execute=execute or launch
    sources=[]
    for batch in plan['batches']:
        folder=destination/batch['name'];folder.mkdir(exist_ok=True)
        params=dict(plan['config'],ProjectName=batch['projects'])
        if batch['inventory']:
            atomic_json(folder/'inventory.json',batch['inventory'])
            params['ExpectedInventory']=str(folder/'inventory.json')
        # Always preflight every selected batch before spending tokens.
        if not (folder/'preflight.ok').exists():
            attempt=folder/('audit_'+uuid.uuid4().hex[:8])
            if execute(dict(params,Destination=str(attempt),CheckOnly=True)) != 0:raise RuntimeError('Preflight failed: '+batch['name'])
            report=read(attempt/'inventory_audit.json')
            if set(report)!=set(batch['projects']) or any(not item.get('tests') for item in report.values()):raise ValueError('Explicit inventory required: '+batch['name'])
            (folder/'preflight.ok').write_text('ok')
        sources.append((folder,params))
    if check_only:return
    for folder,params in sources:
        done=folder/'completed.json'
        if done.exists():
            result_dir=Path(read(done)['path'])
            if read(result_dir/'batch.json').get('status') != 'completed':raise ValueError('Completed batch evidence changed')
            print('Skipping completed '+folder.name,flush=True)
            continue
        attempts=sorted(folder.glob('attempt_*'))
        if attempts:
            last=attempts[-1]
            timing=last/'launcher_timing.json'
            if (last/'batch.json').exists() and read(last/'batch.json').get('status')=='completed' and timing.exists() and read(timing).get('docker_exit_code')==0:
                atomic_json(done,dict(path=str(last)));continue
            raise RuntimeError('Incomplete attempt retained at '+str(last)+'. Refusing automatic retry to avoid duplicate model costs. Start a new series for remaining projects.')
        result_dir=folder/'attempt_001'
        print('Running '+folder.name,flush=True)
        code=execute(dict(params,Destination=str(result_dir)))
        if code != 0 or not (result_dir/'batch.json').exists() or read(result_dir/'batch.json').get('status') != 'completed':
            raise RuntimeError('Batch interrupted/failed; evidence retained: '+str(result_dir))
        atomic_json(done,dict(path=str(result_dir)))
        completed=[Path(read(f/'completed.json')['path']) for f,_ in sources if (f/'completed.json').exists()]
        merge_batches(completed,destination/'combined')
    merge_batches([Path(read(f/'completed.json')['path']) for f,_ in sources],destination/'combined')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    m=sub.add_parser('merge');m.add_argument('--batches',nargs='+',required=True);m.add_argument('--output',required=True)
    r=sub.add_parser('run');r.add_argument('--projects',required=True);r.add_argument('--config',required=True);r.add_argument('--inventory');r.add_argument('--size',type=int,default=100000);r.add_argument('--output',required=True);r.add_argument('--repo',required=True);r.add_argument('--check-only',action='store_true')
    a=parser.parse_args()
    if a.command=='merge':merge_batches(a.batches,a.output)
    else:
        projects=[line.strip() for line in Path(a.projects).read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        inventory=read(a.inventory) if a.inventory else {}
        inventory={p:inventory[p] for p in projects if p in inventory}
        run_series(make_plan(projects,a.size,read(a.config),inventory),a.output,a.repo,check_only=a.check_only)


if __name__=='__main__':main()
