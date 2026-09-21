"""Run an isolated, measured batch inside the existing Linux Docker image."""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from .core import (atomic_json, inventory_from_sources, parse_reports, score_task,
                   validate_inventory, validate_module_map, write_report, strip_java, java_classes)
from .telemetry import utc_now
from .registry import resolve_inventory, fingerprint_tree


def run_command(command, cwd, log, timeout, env=None):
    start = time.perf_counter()
    timed_out, rc = False, None
    with Path(log).open('w', encoding='utf-8') as output:
        child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=(os.name != 'nt'))
        try:
            rc = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if child.poll() is None:
                if os.name == 'nt':
                    child.kill()
                else:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return dict(return_code=rc, timed_out=timed_out, elapsed_seconds=time.perf_counter()-start)


def read_agent_run_metrics(path, project, expected_max_iterations):
    """Read the agent's atomic progress record without inferring rounds from requests."""
    path = Path(path)
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding='utf-8'))
    actual = value.get('actual_iterations')
    maximum = value.get('max_iterations')
    if (value.get('project') != project or type(actual) is not int or type(maximum) is not int
            or maximum != expected_max_iterations or not 0 <= actual <= maximum):
        raise ValueError('Invalid agent iteration metrics')
    return dict(actual_iterations=actual, max_iterations=maximum,
                agent_status=value.get('status'))


def _no_symlinks(root):
    if Path(root).is_symlink() or any(p.is_symlink() for p in Path(root).rglob('*')):
        raise ValueError('Symlink in evaluation input; inspect it before running a measured evaluation')


def prepare_evaluation(generated, template, destination):
    """Never evaluate cached binaries or model-modified tests. Preserve generated output."""
    generated, template, destination = map(Path, (generated, template, destination))
    if destination.exists():
        raise ValueError('Evaluation destination must be new')
    _no_symlinks(generated)
    _no_symlinks(template)
    shutil.copytree(generated, destination, ignore=shutil.ignore_patterns('target','.git'))
    # Remove only test directories inside this new evaluation copy.
    for path in sorted(destination.rglob('test'), key=lambda p:len(p.parts), reverse=True):
        if path.is_dir() and path.parent.name == 'src':
            if destination.resolve() not in path.resolve().parents:
                raise ValueError('Unsafe evaluation test path')
            shutil.rmtree(path)
    for path in template.rglob('test'):
        if path.is_dir() and path.parent.name == 'src':
            target = destination/path.relative_to(template)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, target)
    if (template/'run_tests.sh').exists():
        shutil.copy2(template/'run_tests.sh', destination/'run_tests.sh')


def classify_compilation(process, output):
    if process['timed_out']:
        return None
    if process['return_code'] == 0 and 'BUILD SUCCESS' in output:
        return True
    if 'COMPILATION ERROR' in output or re.search(r'maven-compiler-plugin.*Compilation failure', output, re.S):
        return False
    return None


def compilation_coverage(root):
    """Verify fresh artifacts for named types in standard Maven source trees."""
    issues, source_count = [], 0
    for path in Path(root).rglob('*.java'):
        rel = path.relative_to(root).as_posix()
        if 'target' in path.relative_to(root).parts:
            continue
        source_count += 1
        match = re.match(r'(?:(.*)/)?src/(main|test)/java/(.*)', rel)
        if not match:
            issues.append(f'Nonstandard source root needs compilation coverage validation: {rel}')
            continue
        code = strip_java(path.read_text(encoding='utf-8',errors='replace'))
        try:
            classes = java_classes(code)
        except ValueError:
            issues.append(f'Cannot validate compiled types: {rel}')
            continue
        package = re.search(r'\bpackage\s+([\w.]+)\s*;', code)
        base = Path(root)/(match.group(1) or '')/'target'/('classes' if match.group(2)=='main' else 'test-classes')
        if package:
            base = base/package.group(1).replace('.','/')
        if not classes and path.name not in ('package-info.java','module-info.java'):
            issues.append(f'No named types found for compilation verification: {rel}')
        for cls in classes:
            artifact = base/(cls['name']+'.class')
            if not artifact.is_file() or artifact.read_bytes()[:4] != bytes.fromhex('cafebabe'):
                issues.append(f'Missing compiled type for {rel}: {cls["name"]}')
    if not source_count:
        issues.append('No Java sources found for complete-compilation verification')
    return issues


def test_fingerprints(root):
    hashes={}
    for path in Path(root).rglob('*'):
        rel=path.relative_to(root).as_posix()
        if path.is_file() and re.search(r'(?:^|/)src/test/',rel):
            hashes[rel]=hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def evaluate(generated, template, evaluation, task_dir, inventory, module_map, timeout):
    started = time.perf_counter()
    task_dir = Path(task_dir)
    prepare_evaluation(generated, template, evaluation)
    expected_hashes=test_fingerprints(evaluation)
    atomic_json(task_dir/'test_fingerprints.json',expected_hashes)
    if not (evaluation/'pom.xml').is_file():
        return dict(score_task(False, dict(cases=[],errors=[],files=[]), inventory),
                    evaluation_seconds=time.perf_counter()-started,
                    issues=['Generated Maven project has no pom.xml; cannot compile'])
    compile_cmd = ['mvn','-B','--fail-at-end','-Dmaven.main.skip=false','-Dmaven.test.skip=false','-DskipTests=false','test-compile']
    compile_process = run_command(compile_cmd, evaluation, task_dir/'compile.log', timeout)
    output = (task_dir/'compile.log').read_text(encoding='utf-8', errors='replace')
    compiled = classify_compilation(compile_process, output)
    coverage_issues = compilation_coverage(evaluation) if compiled is True else []
    if coverage_issues:
        compiled = None
    # Tests in build modules that compiled can still contribute APR/AMPR.
    test_cmd = ['mvn','-B','--fail-at-end','-Dmaven.main.skip=false','-Dmaven.test.skip=false','-DskipTests=false',
                '-Dmaven.test.failure.ignore=true','-DfailIfNoTests=true','test']
    test_process = run_command(test_cmd, evaluation, task_dir/'test.log', timeout)
    reports = parse_reports(evaluation)
    if test_fingerprints(evaluation) != expected_hashes:
        reports['errors'].append('Authoritative test files changed during the build/test commands')
        compiled=None
    result = score_task(compiled, reports, inventory, module_map)
    result['issues'].extend(coverage_issues)
    if compiled is None:
        result['issues'].append('Compiler/dependency/tool outcome unknown; see compile.log')
    if test_process['timed_out']:
        result['issues'].append('Final test command timed out; missing required cases are not passed')
    for rel in reports['files']:
        target = task_dir/'reports'/rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(evaluation/rel, target)
    atomic_json(task_dir/'evaluation.json', dict(compile=compile_process, tests=test_process,
                compile_command=compile_cmd, test_command=test_cmd, report_files=reports['files']))
    result['evaluation_seconds'] = time.perf_counter()-started
    return result


def validate_config(config):
    safe = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    projects = config.get('projects', [])
    if not isinstance(projects,list) or len(set(projects)) != len(projects):
        raise ValueError('Projects must be a unique list')
    for name in [*projects, config.get('batch_id','')]:
        if not isinstance(name,str) or not safe.fullmatch(name):
            raise ValueError(f'Unsafe project/batch identifier: {name!r}')
    model = config.get('model','')
    if not isinstance(model,str) or not model or any(x in model for x in ('\\','..','\n','\r')):
        raise ValueError('Invalid model name')
    for key, default in [('max_iterations',20),('agent_timeout',3600),('evaluation_timeout',600)]:
        value = config.get(key, default)
        if type(value) is not int or value < 1:
            raise ValueError(f'{key} must be a positive integer')
    if config.get('max_iterations',20) > 100:
        raise ValueError('max_iterations must not exceed 100')


def verify_hook():
    from RepoTransAgent import generator
    if not hasattr(generator, 'request_json'):
        raise RuntimeError('Install the metrics request hook before running')


def run_batch(config, data, results, batch_dir, workspace):
    validate_config(config)
    data, results, batch_dir, workspace = map(Path, (data,results,batch_dir,workspace))
    target_root = data/'target_projects/Python/Java'
    projects = config['projects'] or sorted(p.name for p in target_root.iterdir() if p.is_dir())
    config['projects'] = projects
    validate_config(config)
    if not projects:
        raise ValueError('No projects selected')
    for project in projects:
        for path in (target_root/project, data/'source_projects/Python'/project):
            if not path.is_dir():
                raise ValueError(f'Missing project: {path}')
    # Fail before API calls when the optional hook was not installed.
    verify_hook()
    inventories = {}
    if config.get('inventory_file'):
        inventories = json.loads(Path(config['inventory_file']).read_text(encoding='utf-8-sig'))
        if set(inventories)-set(projects):
            raise ValueError('Inventory includes projects outside selected batch')
    prepared = {}
    fingerprints = {}
    inventory_sources = {}
    for project in projects:
        fingerprints[project] = {
            'source': fingerprint_tree(data/'source_projects/Python'/project),
            'target': fingerprint_tree(target_root/project),
        }
        if project in inventories:
            item = inventories[project]
            if item.get('dataset_fingerprint') and item['dataset_fingerprint'] != fingerprints[project]:
                raise ValueError('项目数据在检查后发生变化，请重新核对：'+project)
            inventory = validate_inventory(item['tests'])
            prepared[project] = (inventory, validate_module_map(inventory,item.get('module_map',{})), [])
            inventory_sources[project] = 'explicit'
        else:
            inventory, mapping, issues, source = resolve_inventory(project,target_root/project)
            prepared[project] = (inventory, mapping, issues)
            inventory_sources[project] = source
    if config.get('check_only'):
        audit = {p:dict(tests=inv,module_map=mapping,issues=issues,dataset_fingerprint=fingerprints[p],inventory_source=inventory_sources[p]) for p,(inv,mapping,issues) in prepared.items()}
        atomic_json(batch_dir/'inventory_audit.json', audit)
        atomic_json(batch_dir/'supported_inventory.json', {p:item for p,item in audit.items() if item['tests'] is not None})
        unsupported = [p for p,item in audit.items() if item['tests'] is None]
        # Export actual tests for human review when static discovery is insufficient.
        for project in unsupported:
            for file in (target_root/project).rglob('*.java'):
                if 'target' in file.relative_to(target_root/project).parts:continue
                dest=batch_dir/'test_sources'/project/file.relative_to(target_root/project)
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(file,dest)
        print(f'Inventory audit: {len(projects)} projects; {len(unsupported)} require an explicit inventory. No API calls.', flush=True)
        for p in unsupported:
            print(f'  {p}: {audit[p]["issues"][0]}', flush=True)
        return
    unsupported = [p for p,(inv,_,_) in prepared.items() if inv is None]
    if unsupported and not config.get('allow_unknown_inventory',False):
        atomic_json(batch_dir/'inventory_audit.json', {p:dict(tests=inv,module_map=mapping,issues=issues) for p,(inv,mapping,issues) in prepared.items()})
        raise ValueError('Exact test inventories required before API calls for: '+', '.join(unsupported)+'. See inventory_audit.json; supply ExpectedInventory or explicitly allow incomplete metrics.')
    batch_results = results/config['batch_id']
    batch_results.mkdir(parents=True, exist_ok=False)
    (batch_results/'generated').mkdir()
    workspace.mkdir(exist_ok=True)
    for name, path in [('source_projects',data/'source_projects'),('target_projects',data/'target_projects'),
                       ('translated_projects',batch_results/'generated')]:
        link = workspace/name
        if link.exists() or link.is_symlink():
            raise ValueError(f'Expected a fresh container: {link} already exists')
        link.symlink_to(path, target_is_directory=True)
    if (batch_dir/'batch.json').exists():
        raise ValueError('Metrics destination already contains a batch')
    started = time.perf_counter()
    batch = dict(batch_id=config['batch_id'], model=config['model'], projects=projects,
                 started_at=utc_now(), status='running', config=config, elapsed_seconds=None)
    atomic_json(batch_dir/'batch.json', batch)
    fatal_stop = False
    try:
        for index, project in enumerate(projects,1):
            print(f'[{index}/{len(projects)}] {project}: starting agent', flush=True)
            task_started = time.perf_counter()
            task_dir = batch_dir/'tasks'/project; task_dir.mkdir(parents=True)
            (task_dir/'requests.jsonl').touch(exist_ok=False)
            inventory, module_map, issues = prepared[project]
            atomic_json(task_dir/'inventory.json', dict(tests=inventory,module_map=module_map,issues=issues,dataset_fingerprint=fingerprints[project]))
            max_iterations = config.get('max_iterations',20)
            result = dict(project=project, batch_id=config['batch_id'], sr=None,cr=None,apr=None,ampr=None,
                          max_iterations=max_iterations, actual_iterations=None,
                          started_at=utc_now(), issues=list(issues), request_log_complete=False)
            env = dict(os.environ, RTB_REQUEST_LOG=str(task_dir/'requests.jsonl'))
            agent_run_metrics = task_dir/'agent_run_metrics.json'
            env['RTB_AGENT_RUN_METRICS'] = str(agent_run_metrics)
            if config.get('stop_on_api_error'):
                env['RTB_STOP_ON_API_ERROR'] = '1'
            if config.get('chat_url'):
                env['RTB_CHAT_URL'] = config['chat_url']
                env['RTB_MODEL_OPTIONS'] = json.dumps(config.get('request_options',{}))
                env['RTB_PLAIN_TEXT_MESSAGES'] = '1'
            command = [sys.executable,'-m','RepoTransAgent.run','--project_name',project,
                       '--source_language','Python','--target_language','Java',
                       '--model_name',config['model'],'--max_iterations',str(max_iterations)]
            try:
                agent = run_command(command, task_dir, task_dir/'agent.log', config.get('agent_timeout',3600), env)
                result.update(agent_exit_code=agent['return_code'], agent_seconds=agent['elapsed_seconds'],
                              request_log_complete=not agent['timed_out'])
                run_metrics = read_agent_run_metrics(agent_run_metrics, project, max_iterations)
                if run_metrics:
                    result.update(run_metrics)
                    if agent['timed_out']:
                        result['agent_status'] = 'agent_timeout'
                    elif result.get('agent_status') == 'running':
                        result['agent_status'] = 'process_exited_without_final_status'
                else:
                    result['issues'].append('Agent did not produce iteration metrics')
                print(f'[{index}/{len(projects)}] {project}: final compilation/tests', flush=True)
                generated = batch_results/'generated'/config['model'].replace('/','_')/'Python/Java'/project
                request_events=[]
                for line in (task_dir/'requests.jsonl').read_text(encoding='utf-8').splitlines():
                    try:request_events.append(json.loads(line))
                    except ValueError:pass
                fatal_stop = config.get('stop_on_api_error') and any(e.get('fatal_api_error') for e in request_events)
                if fatal_stop:
                    result['issues'].append('模型接口拒绝请求，本次没有可用于性能评估的结果。请检查密钥、账户余额和请求配置。')
                    result['fatal_api_error'] = True
                elif generated.is_dir():
                    evaluation = evaluate(generated, target_root/project, batch_results/'evaluation'/project,
                                          task_dir, inventory, module_map, config.get('evaluation_timeout',600))
                    result.update(evaluation)
                    result['issues'] = list(issues)+evaluation['issues']
                else:
                    result['issues'].append('Agent did not produce a project; inspect agent.log')
            except Exception as exc:
                result['issues'].append(f'Runner/evaluation error: {type(exc).__name__}: {exc}')
            finally:
                result.update(ended_at=utc_now(), elapsed_seconds=time.perf_counter()-task_started)
                atomic_json(task_dir/'result.json', result)
                write_report(batch_dir)
            print(f'[{index}/{len(projects)}] {project}: SR={result["sr"]}, APR={result["apr"]}', flush=True)
            if fatal_stop:
                print('接口错误，停止后续项目；已保留请求用量和日志。',flush=True)
                break
        export_started = time.perf_counter()
        if not config.get('skip_project_archive',False):
            with tarfile.open(batch_dir/'generated-projects.tar.gz','w:gz') as archive:
                archive.add(batch_results/'generated',arcname='generated-projects')
        batch['export_seconds'] = time.perf_counter()-export_started
        batch['status'] = 'stopped_on_api_error' if fatal_stop else 'completed'
    except BaseException:
        batch['status'] = 'interrupted_or_failed'
        raise
    finally:
        batch.update(ended_at=utc_now(), elapsed_seconds=time.perf_counter()-started)
        atomic_json(batch_dir/'batch.json', batch)
        write_report(batch_dir)
        print((batch_dir/'summary.txt').read_text(encoding='utf-8'), flush=True)


def main():
    run_batch(json.load(sys.stdin), Path('/data'), Path('/results'), Path('/metrics-output'), Path('/workspace'))


if __name__ == '__main__':
    main()
