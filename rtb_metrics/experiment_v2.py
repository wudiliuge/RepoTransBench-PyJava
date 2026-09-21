"""独立批次、自主合并；运行记录与汇总快照分离。"""
import argparse
import csv
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from .core import atomic_json, summarize_usage, write_report, validate_inventory, validate_module_map
from .series import read, series_lock, staging
from .experiment import DEFAULTS, safe_projects, finite, is_fatal, evaluable
from .transport import normalize_endpoint, validate_options
from .snapshots import merge_snapshots
from .container_guard import container_state, stop_container, run_owned

STATE='experiment.json'


def evaluation_id():
    digest=hashlib.sha256()
    for name in ('core.py','runner.py','registry.py','registry.json'):
        digest.update(name.encode());digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def canonical_config(config):
    allowed=set(DEFAULTS)|{'ModelName','BaseUrl','EndpointKind','RequestOptions'}
    if set(config)-allowed:raise ValueError('不支持的配置字段。密钥请通过运行时提示输入。')
    cfg=dict(DEFAULTS,**config)
    if not isinstance(cfg.get('ModelName'),str) or not cfg['ModelName'].strip():raise ValueError('模型名称不能为空。')
    if cfg['DataVolume']!='rtb_pyjava_data_v1':raise ValueError('此入口使用默认数据卷 rtb_pyjava_data_v1。')
    for key in ('MaxIterations','AgentTimeoutSeconds','EvaluationTimeoutSeconds'):
        if type(cfg[key]) is not int or cfg[key]<1:raise ValueError('迭代和超时设置必须为正整数。')
    if cfg['MaxIterations']>100:raise ValueError('迭代上限不能超过 100。')
    cfg['ChatUrl']=normalize_endpoint(cfg.pop('BaseUrl',''),cfg.pop('EndpointKind','auto'))
    cfg['RequestOptions']=validate_options(cfg.get('RequestOptions',{}))
    cfg['evaluation_id']=evaluation_id()
    cfg['protocol']='chat-completions-text'
    return cfg


def initialize(root,config):
    root=Path(root);cfg=canonical_config(config)
    with series_lock(root):
        if (root/STATE).exists():
            state=load(root)
            if state['config']!=cfg:raise ValueError('同名实验的配置不同，请使用新的实验名。')
            print('实验已存在，配置和记录保持不变。');return
        if any(p.name!='.series.lock' for p in root.iterdir()):raise ValueError('目标目录非空，请使用新的实验名。')
        atomic_json(root/STATE,dict(version=2,id=uuid.uuid4().hex,config=cfg,projects={},batches={},merges={},snapshots={},probes=[]))
        overview(root)
    print('实验已创建；每批独立保存，需要时由你选择合并。')


def load(root):
    path=Path(root)/STATE
    if not path.exists():raise ValueError('实验不存在，请先执行 -Init。')
    state=read(path)
    if state.get('version')!=2:raise ValueError('这是旧版自动累计实验，请新建 v2 实验。旧记录保留，可通过 -ImportBatch 尝试严格检查后导入。')
    return state


def ensure_version(state):
    if state['config']['evaluation_id']!=evaluation_id():raise ValueError('评估代码版本已经变化。为保持实验条件一致，请新建实验；已有批次和快照仍可查看。')


def save(root,state):atomic_json(Path(root)/STATE,state)


def snapshot_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def safe_path(root,relative):
    p=(Path(root)/relative).resolve()
    if Path(root).resolve() not in p.parents:raise ValueError('记录中出现不安全路径。')
    return p


def params_for(root,state):
    cfg=state['config'];params={k:cfg[k] for k in DEFAULTS}
    params.update(ModelName=cfg['ModelName'],BaseUrl=cfg['ChatUrl'],ChatUrl=cfg['ChatUrl'],StopOnApiError=True)
    details=Path(root)/'详细记录';details.mkdir(exist_ok=True)
    options=details/'模型参数.json';atomic_json(options,cfg['RequestOptions'])
    metadata=details/'实验条件.json';atomic_json(metadata,cfg)
    params.update(ModelOptionsFile=str(options.resolve()),ExperimentMetadata=str(metadata.resolve()))
    return params


def launcher(repo,root):
    repo=Path(repo).resolve()
    def invoke(params):
        out=Path(params['Destination']);out.parent.mkdir(parents=True,exist_ok=True)
        config=out.parent/(out.name+'_参数.json');atomic_json(config,params)
        script=out.parent/(out.name+'_启动.ps1')
        script.write_text("param($Repo,$Config)\n$ErrorActionPreference='Stop'\n$c=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json\n$a=@{}\n$c.PSObject.Properties | ForEach-Object {$a[$_.Name]=$_.Value}\n& (Join-Path $Repo 'scripts/run_metrics.ps1') @a\nexit $global:LASTEXITCODE\n",encoding='utf-8-sig')
        with (out.parent/(out.name+'_启动日志.txt')).open('w',encoding='utf-8') as log:
            return run_owned(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(repo),str(config)],out,log,cancel_file=Path(root)/'停止请求.json')
    return invoke


def read_row(folder,project):
    b=Path(folder)/'batch.json';r=Path(folder)/'tasks'/project/'result.json'
    if not b.exists() or not r.exists():return None
    batch,row=read(b),read(r)
    if project not in batch['projects'] or row.get('project')!=project or row.get('batch_id')!=batch.get('batch_id'):raise ValueError('项目结果与原始批次不匹配。')
    return row


def attempt_cost(root,folder,project):
    folder=Path(folder);row=read_row(folder,project)
    with staging(Path(root)/'详细记录') as stage:
        atomic_json(stage/'batch.json',dict(batch_id='cost',model='cost',projects=[project]))
        dest=stage/'tasks'/project;dest.mkdir(parents=True)
        if row:atomic_json(dest/'result.json',dict(row,batch_id='cost'))
        if (folder/'tasks'/project/'requests.jsonl').exists():shutil.copyfile(folder/'tasks'/project/'requests.jsonl',dest/'requests.jsonl')
        usage=write_report(stage)['token_usage']
    duration=None;timing=folder/'launcher_timing.json'
    if timing.exists():
        t=read(timing)
        if (folder/'batch.json').exists() and t.get('batch_id')!=read(folder/'batch.json').get('batch_id'):raise ValueError('计时记录不属于这个批次。')
        duration=t.get('total_elapsed_seconds')
    duration=duration if finite(duration) else None
    return dict(token_usage=usage,total_elapsed_seconds=duration,known_elapsed_seconds=duration or 0,
                configured_iterations=(row.get('max_iterations') if row else None),
                actual_iterations=(row.get('actual_iterations') if row else None))


def empty_cost():return dict(token_usage=summarize_usage([]),total_elapsed_seconds=0,known_elapsed_seconds=0,
                             configured_iterations=0,actual_iterations=0)


def leaf(root,state,batch,project):
    number=batch['attempts'].get(project)
    if number is None:
        return dict(id=state['id']+':'+batch['id']+':'+project+':pending',project=project,attempt=None,origin=batch['id'],row=None,cost=empty_cost(),attempted=False)
    attempt=state['projects'][project]['attempts'][number-1]
    folder=safe_path(root,attempt['path']);row=read_row(folder,project)
    if attempt['state']!='已完成':row=None
    cost=attempt_cost(root,folder,project)
    if attempt.get('shared_duration_owner') is False:
        cost['total_elapsed_seconds']=0;cost['known_elapsed_seconds']=0
    return dict(id=attempt['id'],project=project,attempt=number,origin=batch['id'],row=row,cost=cost,attempted=True)


def write_result(folder,snapshot):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);report=snapshot['report']
    metrics=report['metrics']
    compilation=report['compilation'];tests=report['test_cases']
    compilation_text=(f'{compilation["passed_projects"]}/{compilation["total_projects"]}（{compilation["rate"]*100:.2f}%）'
                      if compilation['rate'] is not None else
                      f'未知；已知通过 {compilation["known_passed_projects"]}，未知 {compilation["unknown_projects"]} 个项目')
    test_count_text=(f'{tests["passed"]}/{tests["total"]}' if tests['pass_rate'] is not None else
                     f'未知；已知 {tests["known_passed"]}/{tests["known_total"]}，未知 {tests["unknown_projects"]} 个项目')
    test_rate_text=f'{tests["pass_rate"]*100:.2f}%' if tests['pass_rate'] is not None else '未知'
    cost=report['all_attempts'];usage=cost['token_usage']
    lines=['# '+snapshot['id']+' 结果','',f'项目数 R：{report["R"]}','',
           '| 指标 | 结果 |','|---|---|',
           f'| 编译通过率 | {compilation_text} |',
           f'| 测试通过率 | {test_rate_text} |',
           f'| 测试用例数 | {tests["total"] if tests["total"] is not None else "未知"} |',
           f'| 通过测试用例数 | {tests["passed"] if tests["passed"] is not None else "未知"} |',
           f'| 总设定轮数 | {cost["configured_iterations"] if cost.get("configured_iterations") is not None else "未知"} |',
           f'| 实际轮数 | {cost["actual_iterations"] if cost.get("actual_iterations") is not None else "未知"} |',
           f'| 总 token 消耗 | {usage["total_tokens"] if usage["complete"] else "用量不完整"} |',
           f'| API 调用次数 | {usage["requests"]} |',
           f'| 总耗时 | {str(cost["total_elapsed_seconds"])+" 秒" if cost["total_elapsed_seconds"] is not None else "计时不完整"} |',
           '', '## 论文指标','', '| 指标 | 结果 |','|---|---|']
    for k,v in metrics.items():
        display=f'{v["value"]*100:.2f}%' if v['value'] is not None else f'未知；范围 {v["lower_bound"]*100:.2f}%—{v["upper_bound"]*100:.2f}%，{v["unknown_tasks"]} 个项目未确定'
        lines.append(f'| {k.upper()} | {display} |')
    lines+=['',f'测试用例通过：{test_count_text}',f'已知 token：{usage["known_total_tokens"]}；缺少 usage 的 API 调用：{usage["unknown_requests"]}',
            '', '成本包含所选来源中的全部唯一尝试，包括未被选作性能结果的重跑。','累计耗时不包含批间等待，不等同于并行运行的墙钟时间。','','## 原始来源','']
    for origin in sorted({l['origin'] for l in snapshot['leaves']}):lines.append('- '+origin)
    if snapshot.get('duplicates_removed'):lines.append('\n重复来源已去重，不重复计算成本。')
    lines+=['','本文件是独立快照，后续运行不会自动改写。指标取值和选择记录见 source.json。','']
    (folder/'结果.md').write_text('\n'.join(lines),encoding='utf-8')
    def selected_leaf(project):
        options=[l for l in snapshot['leaves'] if l['project']==project]
        chosen_id=snapshot.get('selection',{}).get(project)
        return next((l for l in options if l['id']==chosen_id),options[0] if len(options)==1 else None)
    projects=sorted({l['project'] for l in snapshot['leaves']})
    json_rows=[]
    with (folder/'项目明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['项目','编译通过率','测试通过率','测试用例数','通过测试用例数','总设定轮数','实际轮数',
                '总 token 消耗','API 调用次数','总耗时（秒）','来源记录','输入 token','输出 token',
                '已知输入 token','已知输出 token','已知总 token','token 统计','缺少 usage 的请求数',
                '翻译时间（秒）','评测时间（秒）','SR','APR','AMPR','问题说明']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        for project in projects:
            chosen=selected_leaf(project);row=(chosen.get('row') or {}) if chosen else {}
            cost=(chosen.get('cost') or {}) if chosen else {};usage=cost.get('token_usage',{})
            rate=row.get('test_pass_rate',row.get('apr'))
            item={'项目':project,
                  '编译通过率':f'{row["cr"]*100:.4f}%' if row.get('cr') is not None else '未知',
                  '测试通过率':f'{rate*100:.4f}%' if rate is not None else '未知',
                  '测试用例数':row.get('total_tests',''),'通过测试用例数':row.get('passed_tests',''),
                  '总设定轮数':row.get('max_iterations',''),'实际轮数':row.get('actual_iterations',''),
                  '总 token 消耗':usage.get('total_tokens') if usage.get('complete') else '未知',
                  'API 调用次数':usage.get('requests',0),
                  '总耗时（秒）':cost.get('total_elapsed_seconds') if cost.get('total_elapsed_seconds') is not None else row.get('elapsed_seconds',''),
                  '来源记录':chosen['id'] if chosen else '',
                  '输入 token':usage.get('input_tokens') if usage.get('complete') else '未知',
                  '输出 token':usage.get('output_tokens') if usage.get('complete') else '未知',
                  '已知输入 token':usage.get('known_input_tokens',0),'已知输出 token':usage.get('known_output_tokens',0),
                  '已知总 token':usage.get('known_total_tokens',0),'token 统计':'完整' if usage.get('complete') else '不完整',
                  '缺少 usage 的请求数':usage.get('unknown_requests',0),
                  '翻译时间（秒）':row.get('agent_seconds',''),'评测时间（秒）':row.get('evaluation_seconds',''),
                  'SR':row.get('sr',''),'APR':row.get('apr',''),'AMPR':row.get('ampr',''),
                  '问题说明':'; '.join(row.get('issues',[]))}
            writer.writerow(item)
            json_rows.append(dict(project=project,selected_record=chosen['id'] if chosen else None,
                                  summary=dict(compilation_rate=row.get('cr'),test_pass_rate=rate,
                                               total_tests=row.get('total_tests'),passed_tests=row.get('passed_tests'),
                                               max_iterations=row.get('max_iterations'),actual_iterations=row.get('actual_iterations'),
                                               total_tokens=usage.get('total_tokens') if usage.get('complete') else None,
                                               api_calls=usage.get('requests',0),
                                               elapsed_seconds=item['总耗时（秒）']),
                                  metrics=row,cost=cost))
    atomic_json(folder/'项目明细.json',json_rows)
    status_cn={'passed':'通过','failure':'失败','error':'错误','skipped':'跳过','missing':'缺失',
               'not_run_compile_failure':'因编译失败未运行','unknown':'未知'}
    with (folder/'测试用例明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['项目','测试类','测试名称','结果','依据']);writer.writeheader()
        for project in projects:
            chosen=selected_leaf(project);row=(chosen.get('row') or {}) if chosen else {}
            for case in row.get('test_cases',[]):
                writer.writerow({'项目':project,'测试类':case.get('test_class',''),'测试名称':case.get('test_name',''),
                                 '结果':status_cn.get(case.get('status'),case.get('status','未知')),
                                 '依据':case.get('evidence','')})


def save_snapshot(root,state,snapshot,kind):
    folder=Path(root)/kind/snapshot['id'];path=folder/'source.json'
    digest=snapshot_digest(snapshot)
    if path.exists() and snapshot_digest(read(path))!=digest:raise ValueError('来源快照已经存在且内容不同，禁止覆盖：'+snapshot['id'])
    state['snapshots'][snapshot['id']]=dict(path=path.relative_to(root).as_posix(),sha256=digest)
    state.setdefault('pending_snapshots',{})[snapshot['id']]=snapshot
    save(root,state)
    publish_pending(root,state)


def publish_pending(root,state):
    for ident,snapshot in list(state.get('pending_snapshots',{}).items()):
        entry=state['snapshots'][ident];path=safe_path(root,entry['path'])
        if snapshot_digest(snapshot)!=entry['sha256']:raise ValueError('待恢复快照校验失败。')
        if path.exists() and snapshot_digest(read(path))!=entry['sha256']:raise ValueError('快照文件被修改，拒绝恢复覆盖。')
        if not path.exists():atomic_json(path,snapshot)
        write_result(path.parent,snapshot)
        del state['pending_snapshots'][ident];save(root,state)


def freeze_batch(root,state,batch):
    if batch['id'] in state['snapshots']:return
    snap=dict(version=2,id=batch['id'],kind='batch',config=state['config'],leaves=[leaf(root,state,batch,p) for p in batch['projects']],selection={})
    snap=merge_snapshots([snap],identifier=batch['id']);snap['kind']='batch'
    save_snapshot(root,state,snap,'批次')


def check_background(root,state):
    for project,item in state['projects'].items():
        for attempt in item['attempts']:
            status=container_state(safe_path(root,attempt['path']))
            if status in ('running','unknown'):
                raise ValueError('项目 '+project+' 的容器仍在运行或状态无法确定。先用 -Status 查看，必要时用 -Stop 停止；不要重复启动。')
    for probe in state.get('probes',[])+state.get('audits',[]):
        if container_state(safe_path(root,probe['path'])) in ('running','unknown'):raise ValueError('接口测试容器仍在运行或状态未知，请先检查或停止。')


def recover(root,state):
    publish_pending(root,state)
    check_background(root,state)
    for batch in state['batches'].values():
        if batch['status']=='已结束':
            freeze_batch(root,state,batch);continue
        if batch['status']!='运行中':continue
        for project,number in batch['attempts'].items():
            if number is None:continue
            attempt=state['projects'][project]['attempts'][number-1];folder=safe_path(root,attempt['path'])
            if is_fatal(folder,project):attempt['state']='接口失败'
            elif (folder/'batch.json').exists() and read(folder/'batch.json').get('status')=='completed' and (folder/'launcher_timing.json').exists() and read(folder/'launcher_timing.json').get('docker_exit_code')==0 and evaluable(read_row(folder,project)):attempt['state']='已完成'
            else:attempt['state']='运行中断'
        batch['status']='已结束'
        save(root,state);freeze_batch(root,state,batch)


def validate_inventory_file(path):
    data=read(path)
    if not isinstance(data,dict) or not data:raise ValueError('清单必须包含项目名及测试映射。')
    safe_projects(list(data))
    for project,item in data.items():
        if item.get('_needs_review'):raise ValueError(project+' 的清单仍标记为待核对，请补全并核对后将 _needs_review 改为 false。')
        validate_inventory(item.get('tests'));validate_module_map(item['tests'],item.get('module_map'))
    return data


def prepare(root,projects,repo,invoke=None,retry=False,inventory_file=None,note=''):
    root=Path(root);safe_projects(projects)
    with series_lock(root):
        state=load(root);ensure_version(state);recover(root,state)
        (root/'停止请求.json').unlink(missing_ok=True)
        todo=[p for p in projects if retry or not state['projects'].get(p,{}).get('attempts')]
        if not todo:print('项目已有尝试记录；没有重复运行。明确重跑时添加 -Retry。');return None
        for p in todo:state['projects'].setdefault(p,dict(attempts=[],fingerprint=None))
        if inventory_file:
            supplied=validate_inventory_file(inventory_file)
            for p in todo:
                if p not in supplied:continue
                inv={k:supplied[p][k] for k in ('tests','module_map','dataset_fingerprint') if k in supplied[p]}
                item=state['projects'][p]
                if item['attempts'] and item.get('override')!=inv:raise ValueError('已运行项目不能更换测试清单，请新建实验。')
                item['override']=inv
        candidates=[b for b in state['batches'].values() if b['projects']==todo and b['status'] in ('检查失败','待补清单','待运行','检查中') and not any(b['attempts'].values())]
        if candidates:batch=candidates[-1]
        else:
            ident=f'批次{len(state["batches"])+1:03d}'
            batch=dict(id=ident,projects=todo,attempts={p:None for p in todo},status='检查中',note=note,created_at=time.time())
            state['batches'][ident]=batch
        batch['expected_attempt_counts']={p:len(state['projects'][p]['attempts']) for p in todo}
        save(root,state);folder=root/'批次'/batch['id'];audit=folder/('检查_'+uuid.uuid4().hex[:8])
        state.setdefault('audits',[]).append(dict(id=audit.name,path=audit.relative_to(root).as_posix()));save(root,state)
        params=params_for(root,state);params.update(ProjectName=todo,Destination=str(audit.resolve()),CheckOnly=True)
        overrides={p:state['projects'][p]['override'] for p in todo if state['projects'][p].get('override')}
        if overrides:
            supplied=folder/'指定清单.json';atomic_json(supplied,overrides);params['ExpectedInventory']=str(supplied.resolve())
        print(batch['id']+'：正在检查项目和测试清单，不调用模型。',flush=True)
        code=(invoke or launcher(repo,root))(params)
        if code!=0 or not (audit/'inventory_audit.json').exists():
            batch['status']='检查失败';save(root,state);overview(root)
            raise ValueError('检查失败，请查看 '+str(audit.parent/(audit.name+'_启动日志.txt')))
        report=read(audit/'inventory_audit.json')
        if set(report)!=set(todo):raise ValueError('清单检查返回的项目不完整。')
        blocked=[]
        for p in todo:
            item=state['projects'][p];inv=report[p]
            if not inv.get('tests'):blocked.append(p);continue
            if not inv.get('dataset_fingerprint'):raise ValueError('缺少数据校验信息，请更新脚本。')
            if item['fingerprint'] and item['fingerprint']!=inv['dataset_fingerprint']:raise ValueError('源码或测试已变化，请新建实验：'+p)
            test_identity={'tests':inv['tests'],'module_map':inv.get('module_map',{})}
            if item.get('test_identity') and item['test_identity']!=test_identity:raise ValueError('预期测试集合或模块划分变化，请新建实验：'+p)
            item['test_identity']=test_identity
            item['fingerprint']=inv['dataset_fingerprint']
        batch['inventory']=report;batch['status']='待补清单' if blocked else '待运行';save(root,state)
        if blocked:
            template={p:dict(tests={},module_map={},_needs_review=True,dataset_fingerprint=report[p].get('dataset_fingerprint')) for p in blocked}
            atomic_json(folder/'待补清单.json',template)
            (folder/'清单补充说明.md').write_text('# 补充测试清单\n\n以下项目无法可靠自动识别：'+ '、'.join(blocked)+'\n\n1. 查看本批检查目录内 inventory_audit.json 的原因。原始 Java 测试已导出到该检查目录的 test_sources 子目录。\n2. 在待补清单.json 的 tests 中填入完整测试类名及全部实际 JUnit 用例名称；module_map 可选。动态测试需核对完整测试发现结果，不能只填实际通过的用例。\n3. 人工核对完整后，将 _needs_review 改为 false。结构校验不能证明语义完整。\n4. 用 -ValidateInventory -InventoryFile 文件路径 检查结构，再以相同 -Run 项目名单添加 -InventoryFile 文件路径重新检查和运行。\n\n源位置：/data/target_projects/Python/Java/<项目>/src/test。整个步骤不调用模型。\n',encoding='utf-8')
            print('需要补充清单：'+ '、'.join(blocked)+'。请阅读 '+str(folder/'清单补充说明.md'))
        else:print(batch['id']+'：检查通过。')
        overview(root);return batch['id']


def execute(root,identifier,repo,invoke=None):
    root=Path(root)
    with series_lock(root):
        state=load(root);ensure_version(state);check_background(root,state)
        batch=state['batches'].get(identifier)
        if not batch or batch['status']!='待运行':raise ValueError('该批次未通过检查，或已经执行过。')
        if any(len(state['projects'][p]['attempts'])!=batch['expected_attempt_counts'][p] for p in batch['projects']):
            raise ValueError('准备后有项目已在其他批次运行，请重新提交名单，避免重复费用。')
        # Reserve before launching any external process; a second launcher cannot spend twice.
        batch['status']='运行中';save(root,state);invoke=invoke or launcher(repo,root)
        try:
            for project in batch['projects']:
                if (root/'停止请求.json').exists():
                    print('已收到停止请求，后续项目不再启动。',flush=True);break
                item=state['projects'][project];number=len(item['attempts'])+1
                attempt=dict(number=number,id=state['id']+':'+identifier+':'+project+':'+str(number),path=f'详细记录/{identifier}/{project}',state='运行中')
                item['attempts'].append(attempt);batch['attempts'][project]=number;save(root,state)
                folder=safe_path(root,attempt['path']);invfile=folder.parent/(project+'_清单.json');atomic_json(invfile,{project:batch['inventory'][project]})
                params=params_for(root,state);params.update(ProjectName=[project],Destination=str(folder),ExpectedInventory=str(invfile.resolve()))
                print(identifier+'：正在运行 '+project+'；日志 '+str(folder/'tasks'/project/'agent.log'),flush=True)
                try:
                    if (root/'停止请求.json').exists():raise KeyboardInterrupt()
                    code=invoke(params);row=read_row(folder,project)
                    attempt['state']='接口失败' if is_fatal(folder,project) else ('已完成' if code==0 and evaluable(row) else '评估异常')
                except BaseException:
                    attempt['state']='运行中断';raise
                finally:save(root,state)
                if attempt['state']!='已完成':
                    print('本项目'+attempt['state']+'，停止后续项目，保留全部记录。',flush=True);break
        finally:
            # If a client/container survived interruption, do not freeze changing evidence.
            active=False
            for p,n in batch['attempts'].items():
                if n is not None and container_state(safe_path(root,state['projects'][p]['attempts'][n-1]['path'])) in ('running','unknown'):active=True
            if not active:
                batch['status']='已结束';save(root,state);freeze_batch(root,state,batch)
            else:print('后台容器未确认停止，暂不冻结结果；请用 -Status 检查或 -Stop 停止。')
            overview(root)
        print(identifier+' 已独立保存，尚未与其他批次合并。')


def source(root,state,identifier):
    entry=state['snapshots'].get(identifier)
    if not entry:raise ValueError('来源不存在或尚未结束：'+identifier)
    p=safe_path(root,entry['path'])
    if snapshot_digest(read(p))!=entry['sha256']:raise ValueError('来源快照被修改，拒绝合并：'+identifier)
    return read(p)


def merge(root,identifiers,choices=None):
    root=Path(root)
    with series_lock(root):
        state=load(root);recover(root,state)
        inputs=[source(root,state,i) for i in identifiers];selection={}
        for project,value in (choices or {}).items():
            candidates={l['id']:l for s in inputs for l in s['leaves'] if l['project']==project and (l['id']==value or l['origin']==value)}
            if len(candidates)!=1:raise ValueError('选择不唯一或不属于所选来源：'+project)
            selection[project]=next(iter(candidates))
        ident=f'汇总{len(state["merges"])+1:03d}'
        try:snapshot=merge_snapshots(inputs,selection=selection,identifier=ident)
        except ValueError as exc:
            candidates={}
            for s in inputs:
                for l in s['leaves']:candidates.setdefault(l['project'],{})[l['id']]=l['origin']
            conflicts={p:sorted(set(items.values())) for p,items in candidates.items() if len(items)>1}
            if conflicts:
                atomic_json(root/'详细记录/合并冲突.json',conflicts)
                raise ValueError(str(exc)+'。可用 -Choose @{项目名="批次编号"} 指定；候选列表见详细记录/合并冲突.json。') from exc
            raise
        snapshot['duplicates_removed']=sum(len(s['leaves']) for s in inputs)-len(snapshot['leaves'])
        state['merges'][ident]=dict(sources=identifiers,choices=choices or {},created_at=time.time())
        save_snapshot(root,state,snapshot,'汇总');overview(root)
        print('已生成 '+ident+'；此前批次和汇总保持不变。')
        return ident


def overview(root):
    root=Path(root);state=load(root);leaves=[];rows=[]
    for batch in state['batches'].values():
        rows.append((batch['id'],batch['status'],'、'.join(batch['projects']),batch.get('note','')))
        for project,number in batch['attempts'].items():
            if number is not None:leaves.append(leaf(root,state,batch,project))
    # Cost aggregation ignores project conflicts by assigning an artificial unique cost-only project.
    costleaves=[dict(l,project=l['id'],row=None) for l in leaves]
    for probe in state.get('probes',[]):
        probe_cost=attempt_cost(root,safe_path(root,probe['path']),'probe')
        probe_cost.update(configured_iterations=0,actual_iterations=0)
        costleaves.append(dict(id=probe['id'],project=probe['id'],attempt=1,origin=probe['id'],row=None,cost=probe_cost,attempted=True))
    if costleaves:
        c=merge_snapshots([dict(version=2,id='cost',kind='batch',config=state['config'],leaves=costleaves,selection={})])['report']['all_attempts']
    else:c=dict(count=0,known_configured_iterations=0,known_actual_iterations=0,
                unknown_iteration_attempts=0,**empty_cost())
    lines=['# 实验概览','',f'模型：{state["config"]["ModelName"]}',f'登记项目：{len(state["projects"])}；批次：{len(state["batches"])}；自选汇总：{len(state["merges"])}','', '各批独立保存。没有自动计算跨批次性能指标；请用 -Merge 自主选择来源。','', '| 批次 | 状态 | 项目 | 备注 |','|---|---|---|---|']
    lines.extend('| '+' | '.join(map(str,row))+' |' for row in rows)
    u=c['token_usage'];lines+=['','## 整个实验的累计尝试消耗','',
        f'总设定轮数：{c["configured_iterations"] if c.get("configured_iterations") is not None else "未知"}；实际轮数：{c["actual_iterations"] if c.get("actual_iterations") is not None else "未知"}',
        f'总 token：{u["total_tokens"] if u["complete"] else "用量不完整"}；已知 token：{u["known_total_tokens"]}',
        f'API 调用次数：{u["requests"]}',
        f'累计运行耗时：{c["total_elapsed_seconds"] if c["total_elapsed_seconds"] is not None else "计时不完整"} 秒','', '本范围包含未选择合并的运行、失败、重跑和主动接口测试；不含预检查、批间等待。','', '已生成汇总：'+('、'.join(state['merges']) or '暂无'),'']
    (root/'实验概览.md').write_text('\n'.join(lines),encoding='utf-8')
    return '\n'.join(lines)


def stop(root):
    root=Path(root);state=load(root);failed=[]
    atomic_json(root/'停止请求.json',dict(requested_at=time.time()))
    for p,item in state['projects'].items():
        for a in item['attempts']:
            folder=safe_path(root,a['path'])
            if container_state(folder) in ('running','unknown'):
                stop_container(folder)
                if container_state(folder) in ('running','unknown'):failed.append(p)
    for probe in state.get('probes',[])+state.get('audits',[]):
        folder=safe_path(root,probe['path'])
        if container_state(folder) in ('running','unknown'):
            stop_container(folder)
            if container_state(folder) in ('running','unknown'):failed.append(probe['id'])
    if failed:raise ValueError('无法确认停止：'+ '、'.join(failed)+'。请检查 Docker Desktop 后重试。')
    print('停止请求已保存，后续项目不会继续启动。请用 -Status 确认当前任务结束。')


def probe_api(root,repo,invoke=None):
    root=Path(root)
    with series_lock(root):
        state=load(root);ensure_version(state);check_background(root,state)
        (root/'停止请求.json').unlink(missing_ok=True)
        ident=f'接口测试{len(state["probes"])+1:03d}';entry=dict(id=ident,path='详细记录/'+ident)
        state['probes'].append(entry);save(root,state)
        folder=safe_path(root,entry['path']);params=params_for(root,state)
        params.update(ProjectName=['probe'],ProbeOnly=True,Destination=str(folder))
        print('执行一次短接口请求，可能产生少量 token 消耗，计入实验总成本。',flush=True)
        try:code=(invoke or launcher(repo,root))(params)
        finally:overview(root)
        if code!=0:raise ValueError('接口测试未通过。请查看详细记录/'+ident+'/probe.json 和启动日志。')
        print('接口可返回文本；不保证长上下文或完整仓库翻译可成功。')


def import_batch(root,original):
    from .registry import fingerprint_tree
    root=Path(root);original=Path(original).resolve()
    with series_lock(root):
        state=load(root);ensure_version(state);recover(root,state)
        if container_state(original) in ('running','unknown'):raise ValueError('来源仍在运行或状态未知，不能导入。')
        data=read(original/'batch.json');metadata=data.get('config',{}).get('experiment_metadata')
        if metadata is None:raise ValueError('旧批次缺少完整实验条件及评估版本信息，无法证明可比，拒绝自动导入。请继续用旧版合并工具查看历史结果，或重新运行；原文件未修改。')
        if metadata!=state['config']:raise ValueError('来源模型、请求参数、预算或评估版本与本实验不一致。')
        if data.get('status') not in ('completed','stopped_on_api_error'):raise ValueError('来源尚未结束。')
        existing=state.setdefault('imported',{}).get(data['batch_id'])
        if existing:print('该原始批次已导入：'+existing);return existing
        for b in state['batches'].values():
            for p,n in b['attempts'].items():
                if n is None:continue
                other=safe_path(root,state['projects'][p]['attempts'][n-1]['path'])/'batch.json'
                if other.exists() and read(other).get('batch_id')==data['batch_id']:
                    print('该原始运行已经属于 '+b['id']+'，不重复导入或计算成本。');return b['id']
        projects=data['projects'];safe_projects(projects)
        inventories={}
        for p in projects:
            invpath=original/'tasks'/p/'inventory.json'
            if not invpath.exists():raise ValueError('来源缺少项目清单：'+p+'；不能验证实验条件，保留原记录并使用旧版工具查看。')
            inv=read(invpath)
            if not inv.get('dataset_fingerprint'):raise ValueError('来源缺少数据指纹，不能导入。')
            old=state['projects'].get(p,{})
            if old.get('fingerprint') and old['fingerprint']!=inv['dataset_fingerprint']:raise ValueError('来源数据不一致：'+p)
            identity={'tests':inv.get('tests'),'module_map':inv.get('module_map',{})}
            validate_inventory(identity['tests']);validate_module_map(identity['tests'],identity['module_map'])
            if old.get('test_identity') and old['test_identity']!=identity:raise ValueError('来源测试清单不一致：'+p)
            inventories[p]=inv
        fingerprint_tree(original)  # Reject symlinks/junctions before copying.
        ident=f'批次{len(state["batches"])+1:03d}';relative='详细记录/导入_'+ident
        target=safe_path(root,relative);target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(original,target)
        batch=dict(id=ident,projects=projects,attempts={},status='已结束',note='导入 '+str(original),inventory=inventories)
        for i,p in enumerate(projects):
            item=state['projects'].setdefault(p,dict(attempts=[],fingerprint=inventories[p]['dataset_fingerprint']))
            item['fingerprint']=inventories[p]['dataset_fingerprint']
            item['test_identity']={'tests':inventories[p]['tests'],'module_map':inventories[p].get('module_map',{})}
            number=len(item['attempts'])+1;row=read_row(target,p)
            status='接口失败' if is_fatal(target,p) else ('已完成' if evaluable(row) else '评估异常')
            item['attempts'].append(dict(number=number,id=state['id']+':import:'+data['batch_id']+':'+p,path=relative,state=status,shared_duration_owner=i==0))
            batch['attempts'][p]=number
        state['batches'][ident]=batch;state['imported'][data['batch_id']]=ident;save(root,state)
        freeze_batch(root,state,batch);overview(root);print('已导入为 '+ident+'，尚未与其他批次合并。');return ident


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--request',required=True);a=parser.parse_args()
    request=read(a.request);root=Path(request['root']);action=request['action']
    try:
        if action=='init':initialize(root,request['config'])
        elif action=='prepare':
            ident=prepare(root,request['projects'],request['repo'],retry=request.get('retry',False),inventory_file=request.get('inventory_file'),note=request.get('note',''))
            ready=bool(ident and load(root)['batches'][ident]['status']=='待运行')
            atomic_json(request['response_file'],dict(batch=ident,ready=ready))
            return 0 if ready or not ident else 3
        elif action=='execute':execute(root,request['batch'],request['repo'])
        elif action=='merge':merge(root,request['sources'],request.get('choices'))
        elif action=='probe':probe_api(root,request['repo'])
        elif action=='import':import_batch(root,request['import_path'])
        elif action=='validate':validate_inventory_file(request['inventory_file']);print('清单结构通过检查；完整性仍需核对，运行前会检查数据文件。')
        elif action=='stop':stop(root)
        elif action=='status':
            if request.get('sources'):
                state=load(root)
                for ident in request['sources']:
                    source(root,state,ident)
                    print(safe_path(root,state['snapshots'][ident]['path']).with_name('结果.md').read_text(encoding='utf-8'))
                return 0
            try:
                with series_lock(root):recover(root,load(root));print(overview(root))
            except (RuntimeError,ValueError) as exc:print('运行状态提示：'+str(exc));print(overview(root))
        else:raise ValueError('不支持的操作。')
    except KeyboardInterrupt:print('已中断；不自动重跑。请用 -Status 检查后台状态。');return 2
    except Exception as exc:
        import traceback
        details=root/'详细记录';details.mkdir(parents=True,exist_ok=True)
        (details/'最近错误.txt').write_text(traceback.format_exc(),encoding='utf-8')
        print('操作未完成：'+(str(exc) if isinstance(exc,ValueError) else '请查看详细记录/最近错误.txt。'));return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
