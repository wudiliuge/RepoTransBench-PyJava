"""Metrics use a fixed task denominator; missing evidence is never a zero cost."""
import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def validate_inventory(inventory):
    if not isinstance(inventory, dict) or not inventory:
        raise ValueError('Expected a nonempty mapping of test class to exact JUnit testcase names')
    for module, tests in inventory.items():
        if not isinstance(module, str) or not module or not isinstance(tests, list) or not tests:
            raise ValueError('Every class must have at least one required testcase')
        if any(not isinstance(t, str) or not t for t in tests) or len(set(tests)) != len(tests):
            raise ValueError('Testcase names must be unique nonempty strings within a class')
    return inventory


def validate_module_map(inventory, mapping):
    mapping = {} if mapping is None else mapping
    if not isinstance(mapping,dict) or set(mapping)-set(inventory) or any(not isinstance(x,str) or not x for x in mapping.values()):
        raise ValueError('module_map must map known classes to nonempty module names')
    return mapping


def parse_reports(root):
    """Read fresh JUnit XML; the caller must clean report directories before evaluation."""
    root = Path(root).resolve()
    cases, errors, files, seen = [], [], [], set()
    for path in sorted(root.rglob('TEST-*.xml')):
        rel = path.relative_to(root)
        parts = rel.parts
        if path.parent.name not in ('surefire-reports', 'failsafe-reports'):
            continue
        if path.parent.parent.name != 'target':
            continue
        if root not in path.resolve().parents:
            errors.append(f'Report points outside evaluation directory: {rel}')
            continue
        files.append(rel.as_posix())
        prefix = '/'.join(parts[:-3])
        try:
            tree = ET.parse(path)
            for suite in tree.iter():
                tag = suite.tag.split('}')[-1]
                if tag != 'testsuite':
                    continue
                children = [e for e in suite if e.tag.split('}')[-1] == 'testcase']
                nested = any(e.tag.split('}')[-1] == 'testsuite' for e in suite)
                if not nested and 'tests' in suite.attrib and int(suite.attrib['tests']) != len(children):
                    errors.append(f'XML test count differs from testcase entries: {rel}')
                for case in children:
                    classname, name = case.get('classname'), case.get('name')
                    if not classname or not name:
                        errors.append(f'Missing classname/name: {rel}')
                        continue
                    module = f'{prefix}::{classname}' if prefix else classname
                    identity = (module, name)
                    if identity in seen:
                        errors.append(f'Duplicate test identity: {identity}')
                    seen.add(identity)
                    outcomes = {e.tag.split('}')[-1] for e in case}
                    status = next((s for s in ('error','failure','skipped') if s in outcomes), 'passed')
                    cases.append({'class': module, 'name': name, 'status': status,
                                  'report_file': rel.as_posix()})
        except (ET.ParseError, OSError, ValueError) as exc:
            errors.append(f'Invalid XML {rel}: {type(exc).__name__}')
    return {'cases': cases, 'errors': errors, 'files': files}


def strip_java(raw):
    return re.sub(r'"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*[\s\S]*?\*/', ' ', raw)


def java_classes(code):
    """Named type body ranges, including nested classes (binary names with $)."""
    classes = []
    for match in re.finditer(r'\b(?:class|interface|enum|record)\s+(\w+)[^{;]*\{', code):
        start, depth, end = match.end()-1, 1, match.end()
        while end < len(code) and depth:
            depth += (code[end] == '{') - (code[end] == '}')
            end += 1
        if depth:
            raise ValueError('Unbalanced Java type body')
        parents = [c for c in classes if c['start'] < start < c['end']]
        name = (parents[-1]['name']+'$' if parents else '') + match.group(1)
        classes.append(dict(name=name,start=start,end=end))
    return classes


def inventory_from_sources(root):
    """Conservative JUnit @Test discovery. Complex/dynamic suites require an explicit inventory."""
    root = Path(root)
    result, issues = {}, []
    for path in sorted(root.rglob('*.java')):
        rel = path.relative_to(root).as_posix()
        if '/src/test/java/' not in '/' + rel:
            if '/src/main/java/' not in '/'+rel and 'target' not in path.relative_to(root).parts:
                issues.append(f'Nonstandard Java source location requires inventory: {rel}')
            continue
        raw = path.read_text(encoding='utf-8', errors='replace')
        # Preserve code structure while removing comments and string/character literals.
        code = strip_java(raw)
        all_annotations = {a.split('.')[-1] for a in re.findall(r'@([\w.]+)', code)}
        allowed = {'Test','Before','After','BeforeClass','AfterClass','BeforeEach','AfterEach',
                   'BeforeAll','AfterAll','Disabled','Ignore','DisplayName','Tag','Timeout',
                   'Override','SuppressWarnings','Deprecated','SafeVarargs','FunctionalInterface'}
        if all_annotations-allowed or re.search(r'\b(?:extends|implements)\b',code):
            issues.append(f'Custom/complex annotations or inheritance require inventory: {rel}')
            continue
        annotations = re.findall(r'@(?:[\w.]+\.)?(Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate|Nested|RunWith)\b', code)
        if not annotations:
            # JUnit3/inherited tests cannot be inferred from @Test.
            if re.search(r'\bextends\s+TestCase\b|\bvoid\s+test\w*\s*\(', code):
                issues.append(f'JUnit3/inherited discovery requires inventory: {rel}')
            continue
        if any(a != 'Test' for a in annotations) or re.search(r'\bextends\b', code):
            issues.append(f'Dynamic, nested, runner-based or inherited tests require inventory: {rel}')
            continue
        try:
            classes = java_classes(code)
        except ValueError:
            issues.append(f'Cannot parse Java classes: {rel}')
            continue
        methods = list(re.finditer(r'@(?:[\w.]+\.)?Test\b\s*(?:\([^)]*\)\s*)?(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|protected|private|final|static|synchronized)\s+)*void\s+(\w+)\s*\(\s*\)', code))
        if not classes or len(methods) != len(annotations):
            issues.append(f'Ambiguous test discovery requires inventory: {rel}')
            continue
        package = re.search(r'\bpackage\s+([\w.]+)\s*;', code)
        prefix = rel.split('src/test/java/', 1)[0].rstrip('/')
        for method in methods:
            owners = [c for c in classes if c['start'] < method.start() < c['end']]
            if not owners:
                issues.append(f'Test method outside a supported class: {rel}')
                continue
            classname = ((package.group(1)+'.') if package else '') + owners[-1]['name']
            key = f'{prefix}::{classname}' if prefix else classname
            names = result.setdefault(key, [])
            if method.group(1) in names:
                issues.append(f'Duplicate source testcase: {key}.{method.group(1)}')
            names.append(method.group(1))
    if not result and not issues:
        issues.append('No supported tests found; provide an expected-test inventory')
    return (None if issues else result), issues


def score_task(compiled, reports, inventory, module_map=None):
    result = dict(sr=None, cr=None if compiled is None else int(compiled), apr=None, ampr=None,
                  passed_tests=None, total_tests=None, passed_modules=None, total_modules=None,
                  test_pass_rate=None, test_cases=[], issues=list(reports['errors']))
    if inventory is None:
        result['issues'].append('Missing authoritative expected-test inventory')
        return result
    validate_inventory(inventory)
    mapping = validate_module_map(inventory,module_map)
    expected = {(c, t) for c, names in inventory.items() for t in names}
    def details(statuses, evidence):
        return [dict(test_class=c, test_name=t, status=statuses.get((c,t),'unknown'),
                     evidence=evidence.get((c,t),'')) for c,t in sorted(expected)]
    if compiled is False and not reports['files']:
        result.update(sr=0, apr=0., ampr=0., passed_tests=0,
                      total_tests=sum(map(len,inventory.values())), passed_modules=0,
                      total_modules=len({mapping.get(c,c) for c in inventory}), test_pass_rate=0.,
                      test_cases=details({x:'not_run_compile_failure' for x in expected},
                                         {x:'compilation_failed' for x in expected}))
        return result
    if reports['errors'] or not reports['files'] or not reports['cases']:
        result['issues'].append('Missing or invalid final test reports')
        result['test_cases'] = details({}, {})
        return result
    actual = {(c['class'], c['name']): c['status'] for c in reports['cases']}
    unexpected = set(actual) - expected
    if unexpected:
        result['issues'].append('Unexpected tests; inventory/discovery mismatch: ' + repr(sorted(unexpected)[:8]))
        result['test_cases'] = details({}, {})
        return result
    missing = expected - set(actual)
    if missing:
        result['issues'].append(f'{len(missing)} required tests missing from reports; counted as not passed')
    passed = {identity for identity, status in actual.items() if status == 'passed'}
    modules = {}
    for identity in expected:
        group = mapping.get(identity[0], identity[0])
        modules.setdefault(group, set()).add(identity)
    passed_modules = sum(tests <= passed for tests in modules.values())
    evidence = {(c['class'],c['name']):c.get('report_file','JUnit XML') for c in reports['cases']}
    statuses = {identity:actual.get(identity,'missing') for identity in expected}
    result.update(sr=int(expected <= passed), apr=len(passed)/len(expected),
                  ampr=passed_modules/len(modules), passed_tests=len(passed), total_tests=len(expected),
                  passed_modules=passed_modules, total_modules=len(modules),
                  test_pass_rate=len(passed)/len(expected), test_cases=details(statuses,evidence))
    return result


def summarize_usage(events):
    known_input = known_output = known_total = unknown = 0
    for event in events:
        usage = event.get('usage')
        if not isinstance(usage, dict):
            unknown += 1
            continue
        inp = usage.get('prompt_tokens', usage.get('input_tokens'))
        out = usage.get('completion_tokens', usage.get('output_tokens'))
        if any(type(x) is not int or x < 0 for x in (inp, out)):
            unknown += 1
            continue
        total = usage.get('total_tokens', inp + out)
        if type(total) is not int or total != inp + out:
            unknown += 1
            continue
        known_input += inp
        known_output += out
        known_total += total
    return dict(requests=len(events), unknown_requests=unknown, complete=unknown == 0,
                input_tokens=known_input if not unknown else None,
                output_tokens=known_output if not unknown else None,
                total_tokens=known_total if not unknown else None,
                known_input_tokens=known_input, known_output_tokens=known_output,
                known_total_tokens=known_total)


def aggregate(planned, tasks):
    if not planned or len(set(planned)) != len(planned):
        raise ValueError('Planned projects must be nonempty and unique')
    names = [t['project'] for t in tasks]
    if len(set(names)) != len(names) or set(names) - set(planned):
        raise ValueError('Duplicate or unplanned project results; select one batch only')
    metrics = {}
    for key in ('sr','cr','apr','ampr'):
        values = [t.get(key) for t in tasks if t.get(key) is not None]
        if any(not isinstance(x, (int,float)) or not math.isfinite(x) or not 0 <= x <= 1 for x in values):
            raise ValueError(f'Invalid {key} value')
        known, missing = sum(values), len(planned)-len(values)
        metrics[key] = dict(value=known/len(planned) if not missing else None,
                            known_tasks=len(values), unknown_tasks=missing,
                            lower_bound=known/len(planned), upper_bound=(known+missing)/len(planned))
    counts=[]
    for task in tasks:
        passed,total=task.get('passed_tests'),task.get('total_tests')
        if (type(passed) is int and type(total) is int and total > 0 and 0 <= passed <= total):
            rate=task.get('test_pass_rate',task.get('apr'))
            if (not isinstance(rate,(int,float)) or not math.isfinite(rate)
                    or not math.isclose(rate,passed/total,rel_tol=0,abs_tol=1e-12)):
                raise ValueError('Test pass rate does not match passed/total counts')
            counts.append((passed,total))
        elif passed is not None or total is not None:
            raise ValueError('Invalid passed/total test counts')
    count_unknown=len(planned)-len(counts)
    known_passed=sum(x[0] for x in counts);known_total=sum(x[1] for x in counts)
    test_cases=dict(passed=known_passed if not count_unknown else None,
                    total=known_total if not count_unknown else None,
                    pass_rate=(known_passed/known_total if not count_unknown and known_total else None),
                    known_passed=known_passed,known_total=known_total,
                    known_projects=len(counts),unknown_projects=count_unknown)
    compilation=dict(passed_projects=(sum(t.get('cr') for t in tasks) if not metrics['cr']['unknown_tasks'] else None),
                     total_projects=len(planned),rate=metrics['cr']['value'],
                     known_passed_projects=sum(t.get('cr') for t in tasks if t.get('cr') is not None),
                     unknown_projects=metrics['cr']['unknown_tasks'])
    configured_iterations=[];actual_iterations=[]
    for task in tasks:
        maximum,actual=task.get('max_iterations'),task.get('actual_iterations')
        if maximum is None and actual is None:
            continue
        if type(maximum) is not int or maximum < 1:
            raise ValueError('Invalid actual/max iteration counts')
        configured_iterations.append(maximum)
        if actual is not None:
            if type(actual) is not int or not 0 <= actual <= maximum:
                raise ValueError('Invalid actual/max iteration counts')
            actual_iterations.append(actual)
    configured_unknown=len(planned)-len(configured_iterations)
    actual_unknown=len(planned)-len(actual_iterations)
    iterations=dict(
        configured_total=(sum(configured_iterations) if not configured_unknown else None),
        actual_total=(sum(actual_iterations) if not actual_unknown else None),
        known_configured_total=sum(configured_iterations),
        known_actual_total=sum(actual_iterations),
        configured_known_projects=len(configured_iterations), configured_unknown_projects=configured_unknown,
        actual_known_projects=len(actual_iterations), actual_unknown_projects=actual_unknown)
    return {'R':len(planned), 'metrics':metrics, 'compilation':compilation,
            'test_cases':test_cases, 'iterations':iterations,
            'missing_projects':sorted(set(planned)-set(names))}


def write_report(batch_dir):
    batch_dir = Path(batch_dir)
    batch = json.loads((batch_dir/'batch.json').read_text(encoding='utf-8'))
    tasks, events, usage_incomplete = [], [], False
    for project in batch['projects']:
        task_dir = batch_dir/'tasks'/project
        result_path = task_dir/'result.json'
        if result_path.exists():
            task = json.loads(result_path.read_text(encoding='utf-8'))
            if task.get('batch_id') != batch['batch_id'] or task.get('project') != project:
                raise ValueError('Result belongs to a different batch/project')
            tasks.append(task)
            usage_incomplete |= not task.get('request_log_complete', False)
        else:
            usage_incomplete = True
        starts, finishes = {}, {}
        task_events = []
        task_usage_incomplete = not result_path.exists() or not (tasks[-1].get('request_log_complete',False) if result_path.exists() else False)
        log = task_dir/'requests.jsonl'
        if log.exists():
            for line in log.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    key = event['request_id']
                    if not isinstance(key,str) or not key or event.get('event') not in ('start','finish'):
                        raise ValueError('Invalid request event')
                    target = starts if event['event'] == 'start' else finishes
                    if key in target:
                        raise ValueError('Duplicate request event')
                    target[key] = event
                except (ValueError, KeyError, TypeError):
                    usage_incomplete = True
                    task_usage_incomplete = True
            for key in starts.keys() | finishes.keys():
                task_events.append(finishes.get(key, {'usage':None}))
                if key not in starts:
                    usage_incomplete = True
                    task_usage_incomplete = True
        else:
            usage_incomplete = True
            task_usage_incomplete = True
        events.extend(task_events)
        if result_path.exists():
            task_usage = summarize_usage(task_events)
            for field in ('input_tokens','output_tokens','total_tokens'):
                tasks[-1][field] = None if task_usage_incomplete else task_usage[field]
            tasks[-1].update(
                token_usage_complete=not task_usage_incomplete and task_usage['complete'],
                model_requests=task_usage['requests'],
                api_calls=task_usage['requests'],
                unknown_usage_requests=task_usage['unknown_requests'],
                known_input_tokens=task_usage['known_input_tokens'],
                known_output_tokens=task_usage['known_output_tokens'],
                known_total_tokens=task_usage['known_total_tokens'])
    summary = aggregate(batch['projects'], tasks)
    usage = summarize_usage(events)
    if usage_incomplete:
        usage.update(complete=False, input_tokens=None, output_tokens=None, total_tokens=None)
    durations=[t.get('elapsed_seconds') for t in tasks]
    durations=[v for v in durations if type(v) in (int,float) and math.isfinite(v) and v >= 0]
    summary.update(batch_id=batch['batch_id'], model=batch['model'], units='fractions (0 to 1)',
                   token_usage=usage, elapsed_seconds=batch.get('elapsed_seconds'),
                   api_calls=usage['requests'],
                   task_seconds_sum=sum(durations) if len(durations)==len(batch['projects']) else None,
                   known_task_seconds_sum=sum(durations),
                   export_seconds=batch.get('export_seconds'),
                   time_scope='container batch: agent, retries, final compilation/testing, incremental reports and generated-project archive; excludes setup, model download and Docker startup',
                   module_definition='Java test class, optionally grouped with module_map',
                   complete=all(v['value'] is not None for v in summary['metrics'].values()))
    launcher = batch_dir/'launcher_timing.json'
    if launcher.exists():
        timing = json.loads(launcher.read_text(encoding='utf-8-sig'))
        if timing.get('batch_id') != batch['batch_id']:
            raise ValueError('Launcher timing belongs to a different batch')
        summary['total_elapsed_seconds'] = timing['total_elapsed_seconds']
        summary['total_elapsed_scope'] = timing['scope']
    atomic_json(batch_dir/'summary.json', summary)
    fields = ['project','cr','test_pass_rate','total_tests','passed_tests','max_iterations',
              'actual_iterations','total_tokens','api_calls','elapsed_seconds',
              'sr','apr','ampr','passed_modules','total_modules','agent_seconds','evaluation_seconds',
              'input_tokens','output_tokens','known_input_tokens','known_output_tokens',
              'known_total_tokens','model_requests','unknown_usage_requests','token_usage_complete',
              'agent_exit_code','issues']
    with (batch_dir/'projects.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        by_name = {t['project']:t for t in tasks}
        for name in batch['projects']:
            row = dict(by_name.get(name, {'project':name, 'issues':['Missing result']}))
            row['issues'] = '; '.join(row.get('issues',[]))
            writer.writerow(row)
    by_name = {t['project']:t for t in tasks}
    project_rows=[]
    for name in batch['projects']:
        row=dict(by_name.get(name, {'project':name,'issues':['Missing result']}))
        row.pop('test_cases',None)
        primary={'project':name,'compilation_rate':row.get('cr'),'test_pass_rate':row.get('test_pass_rate',row.get('apr')),
                 'total_tests':row.get('total_tests'),'passed_tests':row.get('passed_tests'),
                 'max_iterations':row.get('max_iterations'),'actual_iterations':row.get('actual_iterations'),
                 'total_tokens':row.get('total_tokens'),'api_calls':row.get('api_calls',row.get('model_requests')),
                 'elapsed_seconds':row.get('elapsed_seconds')}
        primary.update({k:v for k,v in row.items() if k not in primary})
        project_rows.append(primary)
    atomic_json(batch_dir/'项目明细.json',project_rows)
    chinese_fields=['项目','编译通过率','测试通过率','测试用例数','通过测试用例数','总设定轮数',
                    '实际轮数','总 token 消耗','API 调用次数','总耗时（秒）',
                    '运行状态','输入 token','输出 token','已知输入 token','已知输出 token','已知总 token',
                    'token 统计','缺少 usage 的请求数','翻译时间（秒）','评测时间（秒）',
                    'Agent 状态','Agent 返回码','SR','APR','AMPR','问题说明']
    with (batch_dir/'项目明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=chinese_fields);writer.writeheader()
        for name in batch['projects']:
            task=by_name.get(name,{})
            rate=task.get('test_pass_rate',task.get('apr'))
            writer.writerow({'项目':name,
                '编译通过率':f'{task["cr"]*100:.4f}%' if task.get('cr') is not None else '未知',
                '测试通过率':f'{rate*100:.4f}%' if rate is not None else '未知',
                '测试用例数':task.get('total_tests',''),'通过测试用例数':task.get('passed_tests',''),
                '总设定轮数':task.get('max_iterations',''),'实际轮数':task.get('actual_iterations',''),
                '总 token 消耗':task.get('total_tokens') if task.get('total_tokens') is not None else '未知',
                'API 调用次数':task.get('api_calls',task.get('model_requests',0)),
                '总耗时（秒）':task.get('elapsed_seconds',''),
                '运行状态':'已记录' if task else '缺少结果',
                '输入 token':task.get('input_tokens') if task.get('input_tokens') is not None else '未知',
                '输出 token':task.get('output_tokens') if task.get('output_tokens') is not None else '未知',
                '已知输入 token':task.get('known_input_tokens',0),'已知输出 token':task.get('known_output_tokens',0),
                '已知总 token':task.get('known_total_tokens',0),
                'token 统计':'完整' if task.get('token_usage_complete') else '不完整',
                '缺少 usage 的请求数':task.get('unknown_usage_requests',0),
                '翻译时间（秒）':task.get('agent_seconds',''),'评测时间（秒）':task.get('evaluation_seconds',''),
                'Agent 状态':task.get('agent_status',''),'Agent 返回码':task.get('agent_exit_code',''),
                'SR':task.get('sr',''),'APR':task.get('apr',''),'AMPR':task.get('ampr',''),
                '问题说明':'; '.join(task.get('issues',[]))})
    case_fields=['项目','测试类','测试名称','结果','依据']
    status_cn={'passed':'通过','failure':'失败','error':'错误','skipped':'跳过','missing':'缺失',
               'not_run_compile_failure':'因编译失败未运行','unknown':'未知'}
    with (batch_dir/'测试用例明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=case_fields);writer.writeheader()
        for name in batch['projects']:
            for case in by_name.get(name,{}).get('test_cases',[]):
                writer.writerow({'项目':name,'测试类':case.get('test_class',''),'测试名称':case.get('test_name',''),
                                 '结果':status_cn.get(case.get('status'),case.get('status','未知')),
                                 '依据':case.get('evidence','')})
    lines = [f"Batch: {batch['batch_id']}", f"Model: {batch['model']}", f"R: {summary['R']}"]
    for key, metric in summary['metrics'].items():
        value = metric['value']
        display = f'{value*100:.4f}%' if value is not None else f"UNKNOWN (bounds {metric['lower_bound']*100:.4f}%..{metric['upper_bound']*100:.4f}%)"
        lines.append(f'{key.upper()}: {display}')
    lines.extend([f"Batch elapsed seconds: {summary['elapsed_seconds']}", f"Sum of task seconds: {summary['task_seconds_sum']}",
                  f"Total measured workflow seconds (host): {summary.get('total_elapsed_seconds', 'See launcher_timing.json after Docker exits')}",
                  f"Configured iterations: {summary['iterations']['configured_total'] if summary['iterations']['configured_total'] is not None else 'UNKNOWN'}",
                  f"Actual iterations: {summary['iterations']['actual_total'] if summary['iterations']['actual_total'] is not None else 'UNKNOWN'}",
                  f"Total tokens: {usage['total_tokens'] if usage['complete'] else 'UNKNOWN'}",
                  f"API calls: {usage['requests']}",
                  f"Known tokens: {usage['known_total_tokens']}; calls with unknown usage: {usage['unknown_requests']}",
                  'See summary.json for coverage, scope and projects.csv for per-project results.'])
    (batch_dir/'summary.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    comp=summary['compilation'];tests=summary['test_cases']
    comp_text=(f'{comp["passed_projects"]}/{comp["total_projects"]}（{comp["rate"]*100:.4f}%）'
               if comp['rate'] is not None else
               f'未知；已知通过 {comp["known_passed_projects"]}，未知项目 {comp["unknown_projects"]}')
    tests_text=(f'{tests["passed"]}/{tests["total"]}' if tests['pass_rate'] is not None else
                f'未知；已知 {tests["known_passed"]}/{tests["known_total"]}，未知项目 {tests["unknown_projects"]}')
    rate_text=f'{tests["pass_rate"]*100:.4f}%' if tests['pass_rate'] is not None else '未知'
    apr=summary['metrics']['apr']['value'];apr_text=f'{apr*100:.4f}%' if apr is not None else '未知'
    iterations=summary['iterations']
    chinese=[f'批次：{batch["batch_id"]}',f'模型：{batch["model"]}',f'项目数：{summary["R"]}','',
             f'编译通过率：{comp_text}',f'测试通过率（按测试用例加权）：{rate_text}',
             f'测试用例数：{tests["total"] if tests["total"] is not None else "未知"}',
             f'通过测试用例数：{tests["passed"] if tests["passed"] is not None else "未知"}',
             f'总设定轮数：{iterations["configured_total"] if iterations["configured_total"] is not None else "未知"}',
             f'实际轮数：{iterations["actual_total"] if iterations["actual_total"] is not None else "未知"}',
             f'总 token 消耗：{usage["total_tokens"] if usage["complete"] else "未知"}',
             f'API 调用次数：{usage["requests"]}',
             f'总耗时：{summary.get("total_elapsed_seconds","宿主机退出后写入 launcher_timing.json")} 秒','',
             f'测试用例通过：{tests_text}',f'APR（各项目测试通过率平均值）：{apr_text}','',
             f'输入 token：{usage["input_tokens"] if usage["complete"] else "未知"}',
             f'输出 token：{usage["output_tokens"] if usage["complete"] else "未知"}',
             f'已知 token：{usage["known_total_tokens"]}；缺少 usage 的请求：{usage["unknown_requests"]}',
             f'各项目时间之和：{summary["task_seconds_sum"] if summary["task_seconds_sum"] is not None else "未知"} 秒',
             '',
             '每个项目的指标见 项目明细.csv；逐测试用例状态见 测试用例明细.csv；完整数据见 summary.json 和 项目明细.json。']
    (batch_dir/'批次汇总.txt').write_text('\n'.join(chinese)+'\n',encoding='utf-8')
    return summary


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Recompute one measured batch without API calls')
    parser.add_argument('batch_dir', type=Path)
    args = parser.parse_args()
    report = write_report(args.batch_dir)
    print((args.batch_dir/'summary.txt').read_text(encoding='utf-8'))
