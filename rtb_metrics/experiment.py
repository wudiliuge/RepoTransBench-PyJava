"""中文实验入口；计划项目、性能结果和所有尝试的成本分别管理。"""
import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .core import aggregate, atomic_json, summarize_usage, write_report, validate_inventory, validate_module_map
from .series import read, series_lock, staging

DEFAULTS=dict(DataVolume='rtb_pyjava_data_v1',ResultsVolume='rtb_pyjava_results_v1',
              MavenVolume='rtb_maven_cache_v1',ImageName='repotransbench-pyjava:local',
              MaxIterations=20,AgentTimeoutSeconds=3600,EvaluationTimeoutSeconds=600)
STATE='experiment.json'


def safe_projects(projects):
    if not projects or len(set(projects))!=len(projects):raise ValueError('项目名单不能为空，也不能重复。')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',p) for p in projects):raise ValueError('项目名称格式不正确，请使用数据卷中的原始文件夹名。')


def initialize(root,config):
    root=Path(root)
    allowed=set(DEFAULTS)|{'ModelName','BaseUrl'}
    if set(config)-allowed:raise ValueError('配置包含不支持的字段；API Key 不应写入配置。')
    config=dict(DEFAULTS,**config)
    if not config.get('ModelName') or not config.get('BaseUrl'):raise ValueError('请填写模型名称和 API 地址。')
    endpoint=urlsplit(config['BaseUrl'])
    if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:raise ValueError('API 地址不能包含账号、密钥、查询参数或片段；请仅填写服务地址。')
    if not config['BaseUrl'].startswith(('https://','http://')) or re.search(r'/v1/?$|/chat/completions/?$',config['BaseUrl']):raise ValueError('API 地址必须以 http(s) 开头，不要带 /v1 或 /chat/completions。')
    if config['DataVolume']!='rtb_pyjava_data_v1':raise ValueError('此入口统一使用默认数据卷 rtb_pyjava_data_v1。')
    if type(config['MaxIterations']) is not int or not 1<=config['MaxIterations']<=100:raise ValueError('迭代上限应为 1 至 100。')
    for k in ('AgentTimeoutSeconds','EvaluationTimeoutSeconds'):
        if type(config[k]) is not int or config[k]<1:raise ValueError('超时必须为正整数秒。')
    with series_lock(root):
        if (root/STATE).exists():
            if read(root/STATE)['config']!=config:raise ValueError('同名实验已存在且配置不同，请使用新的实验名称。')
            print('实验已存在，保留原配置和结果。');return
        if any(p.name!='.series.lock' for p in root.iterdir()):raise ValueError('实验目录非空，请使用新的实验名称。')
        atomic_json(root/STATE,dict(version=1,config=config,projects={},created_at=time.time(),selection_policy='默认首次有效评估；重跑保留独立记录，切换性能结果需明确选择。'))
        build_report(root)
    print('实验已创建：'+str(root))


def load(root):
    path=Path(root)/STATE
    if not path.exists():raise ValueError('实验不存在，请先使用 -Init 创建实验。')
    return read(path)


def attempt_dir(root,attempt):
    path=(Path(root)/attempt['path']).resolve()
    if Path(root).resolve() not in path.parents:raise ValueError('尝试记录中的路径不安全。')
    return path


def evidence(root,project,attempt):
    folder=attempt_dir(root,attempt);b=folder/'batch.json';r=folder/'tasks'/project/'result.json'
    if not b.exists() or not r.exists():return None
    batch,row=read(b),read(r)
    if batch['projects']!=[project] or row.get('project')!=project or row.get('batch_id')!=batch.get('batch_id'):raise ValueError('项目评估记录与批次不匹配，请检查详细记录。')
    return row


def is_fatal(folder,project):
    log=Path(folder)/'tasks'/project/'requests.jsonl'
    if not log.exists():return False
    for line in log.read_text(encoding='utf-8').splitlines():
        try:
            if json.loads(line).get('fatal_api_error'):return True
        except (ValueError,AttributeError):continue
    return False


def finite(value):return type(value) in (int,float) and math.isfinite(value) and value>=0


def evaluable(row):
    return row is not None and any(row.get(k) is not None for k in ('sr','cr','apr','ampr'))


def recover(root,state):
    """Only called while holding the experiment lock; never restarts a process."""
    for project,item in state['projects'].items():
        for attempt in item['attempts']:
            if attempt['state']!='运行中':continue
            folder=attempt_dir(root,attempt);batch=folder/'batch.json';timing=folder/'launcher_timing.json'
            if is_fatal(folder,project):attempt['state']='接口失败'
            elif batch.exists() and timing.exists() and read(batch).get('status')=='completed' and read(timing).get('docker_exit_code')==0 and evaluable(evidence(root,project,attempt)):
                attempt['state']='已完成'
                if item['selected'] is None:item['selected']=attempt['number']
            else:attempt['state']='运行中断'
            item['status']=attempt['state']
    atomic_json(Path(root)/STATE,state)


def status_report(root):
    try:
        with series_lock(root):
            recover(root,load(root))
            return build_report(root)
    except RuntimeError:
        # An active writer owns the lock. Show only atomic state snapshots.
        return build_report(root,publish=False)


def build_report(root,publish=True):
    root=Path(root);state=load(root);rows=[];flat=[];views=[]
    for project,item in state['projects'].items():
        chosen=next((a for a in item['attempts'] if a['number']==item.get('selected')),None)
        row=evidence(root,project,chosen) if chosen else None
        if row is not None:rows.append(row)
        views.append(dict(project=project,status=item['status'],selected=item.get('selected'),attempts=len(item['attempts']),**{k:row.get(k) if row else None for k in ('sr','cr','apr','ampr','passed_tests','total_tests','passed_modules','total_modules')}))
        flat.extend((project,a) for a in item['attempts'])
    metrics=aggregate(list(state['projects']),rows) if state['projects'] else dict(R=0,metrics={k:dict(value=None,known_tasks=0,unknown_tasks=0,lower_bound=0,upper_bound=1) for k in ('sr','cr','apr','ampr')},missing_projects=[])
    durations=[]
    if flat:
        with staging(root/'详细记录') as stage:
            names=[f'A{i:06d}' for i in range(len(flat))]
            atomic_json(stage/'batch.json',dict(batch_id='cost',model=state['config']['ModelName'],projects=names))
            for name,(project,attempt) in zip(names,flat):
                folder=attempt_dir(root,attempt);row=evidence(root,project,attempt)
                dest=stage/'tasks'/name;dest.mkdir(parents=True)
                if row:
                    row=dict(row,project=name,batch_id='cost')
                    atomic_json(dest/'result.json',row)
                log=folder/'tasks'/project/'requests.jsonl'
                if log.exists():shutil.copyfile(log,dest/'requests.jsonl')
                timing=folder/'launcher_timing.json';duration=None
                if timing.exists():
                    timing_data=read(timing);batch_path=folder/'batch.json'
                    if batch_path.exists() and timing_data.get('batch_id')!=read(batch_path).get('batch_id'):raise ValueError('计时记录与批次不匹配。')
                    duration=timing_data.get('total_elapsed_seconds')
                durations.append(duration if finite(duration) else None)
            cost=write_report(stage)['token_usage']
    else:cost=summarize_usage([])
    metrics.update(model=state['config']['ModelName'],projects=views,selection_policy=state['selection_policy'],
                   all_attempts=dict(count=len(flat),token_usage=cost,total_elapsed_seconds=sum(durations) if all(v is not None for v in durations) else None,known_elapsed_seconds=sum(v for v in durations if v is not None)),
                   scope='性能指标采用每个项目明确选择的结果；成本包含本实验内所有已登记尝试（含失败和重跑），不含预检查及批间等待。')
    if publish:
        atomic_json(root/'详细记录/summary.json',metrics)
        labels={'project':'项目','status':'状态','selected':'性能结果尝试编号','attempts':'尝试次数','sr':'SR','cr':'CR','apr':'APR','ampr':'AMPR','passed_tests':'通过用例','total_tests':'应有用例','passed_modules':'通过模块','total_modules':'应有模块'}
        with (root/'项目明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(labels.values()));writer.writeheader()
            for row in views:writer.writerow({labels[k]:'' if v is None else v for k,v in row.items()})
        (root/'实验结果.md').write_text(render(metrics),encoding='utf-8')
    return metrics


def render(report):
    lines=['# 实验结果','',f'模型：{report["model"]}',f'计划项目数 R：{report["R"]}','', '| 指标 | 当前结果 |', '|---|---|']
    for key,item in report['metrics'].items():
        value=f'{item["value"]*100:.2f}%' if item['value'] is not None else ('尚无项目' if report['R']==0 else f'暂无法确定（{item["unknown_tasks"]} 个项目结果未确定；范围 {item["lower_bound"]*100:.2f}%—{item["upper_bound"]*100:.2f}%）')
        lines.append(f'| {key.upper()} | {value} |')
    cost=report['all_attempts'];usage=cost['token_usage'];seconds=cost['total_elapsed_seconds']
    lines.extend(['','## 全部尝试的累计消耗','',f'尝试次数：{cost["count"]}',
                  f'总 token：{usage["total_tokens"] if usage["complete"] else "用量不完整，无法给出准确总量"}',
                  f'已知 token：{usage["known_total_tokens"]}；无用量记录的请求：{usage["unknown_requests"]}',
                  f'累计运行耗时：{str(round(seconds,2))+" 秒" if seconds is not None else "计时不完整"}；已知耗时：{cost["known_elapsed_seconds"]:.2f} 秒',
                  '',report['scope'],'',report['selection_policy'],'','## 项目进度','','| 项目 | 状态 | 尝试次数 | 性能结果编号 |','|---|---|---:|---:|'])
    for p in report['projects']:lines.append(f'| {p["project"]} | {p["status"]} | {p["attempts"]} | {p["selected"] or "未选择"} |')
    lines+=['','未知值不会当作零，未完成或待补清单的项目不会从 R 中删除。','原始 Maven/API 日志可能包含英文，保存在“详细记录”中。','']
    return '\n'.join(lines)


def launcher(repo,root):
    repo=Path(repo).resolve();root=Path(root).resolve()
    def invoke(params):
        out=Path(params['Destination']);out.parent.mkdir(parents=True,exist_ok=True)
        config=out.parent/(out.name+'_参数.json');atomic_json(config,params)
        script=out.parent/(out.name+'_启动.ps1')
        script.write_text("param($Repo,$Config)\n$ErrorActionPreference='Stop'\n$c=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json\n$a=@{}\n$c.PSObject.Properties | ForEach-Object {$a[$_.Name]=$_.Value}\n& (Join-Path $Repo 'scripts/run_metrics.ps1') @a\nexit $global:LASTEXITCODE\n",encoding='utf-8-sig')
        with (out.parent/(out.name+'_启动日志.txt')).open('w',encoding='utf-8') as log:
            return subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),str(repo),str(config)],stdout=log,stderr=subprocess.STDOUT).returncode
    return invoke


def run_projects(root,projects,repo,invoke=None,retry=False,check_only=False,inventory_file=None):
    root=Path(root);safe_projects(projects)
    with series_lock(root):
        state=load(root);invoke=invoke or launcher(repo,root)
        recover(root,state)
        for p in projects:state['projects'].setdefault(p,dict(status='未开始',attempts=[],selected=None))
        if inventory_file:
            supplied=read(inventory_file)
            for p in projects:
                if p not in supplied:continue
                inv=supplied[p];validate_inventory(inv['tests']);validate_module_map(inv['tests'],inv.get('module_map'))
                inv=dict(tests=inv['tests'],module_map=inv.get('module_map',{}))
                item=state['projects'][p]
                if item['attempts'] and item.get('inventory_override')!=inv:raise ValueError('已经运行的项目不能更换测试清单，请新建实验：'+p)
                item['inventory_override']=inv
        atomic_json(root/STATE,state);build_report(root)
        todo=[p for p in projects if retry or not state['projects'][p]['attempts']]
        if not todo:
            print('这些项目已有尝试记录，未重复运行。重跑请明确使用 -Retry。');return 4
        audit=root/'详细记录'/('检查_'+uuid.uuid4().hex[:8])
        print(f'正在检查 {len(todo)} 个项目及测试清单，不调用模型……',flush=True)
        params=dict(state['config'],ProjectName=todo,Destination=str(audit),CheckOnly=True,StopOnApiError=True)
        overrides={p:state['projects'][p]['inventory_override'] for p in todo if state['projects'][p].get('inventory_override')}
        if overrides:
            override_file=audit.parent/(audit.name+'_指定清单.json');atomic_json(override_file,overrides)
            params['ExpectedInventory']=str(override_file)
        code=invoke(params)
        if code!=0 or not (audit/'inventory_audit.json').exists():
            for p in todo:state['projects'][p]['status']='检查失败'
            atomic_json(root/STATE,state);build_report(root)
            raise ValueError('项目检查失败，请查看详细记录中的检查启动日志。')
        report=read(audit/'inventory_audit.json')
        if set(report)!=set(todo):raise ValueError('检查返回的项目名单不完整。')
        blocked=[]
        for p in todo:
            item=state['projects'][p];inv=report[p]
            if not inv.get('tests'):
                item['status']='待补清单';blocked.append(p);continue
            fingerprint=inv.get('dataset_fingerprint')
            if not fingerprint:raise ValueError('缺少数据校验信息，请更新完整指标脚本。')
            if item.get('fingerprint') and item['fingerprint']!=fingerprint:raise ValueError('项目 '+p+' 的源码或测试发生变化，请新建实验。')
            item['fingerprint']=fingerprint;item['status']='待运行'
        atomic_json(root/STATE,state);build_report(root)
        if blocked:
            print('本次没有调用模型。以下项目需要补充经核对的测试清单：'+ '、'.join(blocked))
            print('核对清单后，使用相同 -Run 命令并添加 -InventoryFile 清单路径；文件会自动保存到本实验。')
            print('检查报告：'+str(audit/'inventory_audit.json'));return 3
        if check_only:
            print('检查通过，可以运行；检查通过不代表 API 账户可用或测试通过。');return 0
        for p in todo:
            item=state['projects'][p];number=len(item['attempts'])+1
            attempt=dict(number=number,path=f'详细记录/{p}/尝试_{number:03d}',state='运行中')
            item['attempts'].append(attempt);item['status']='运行中'
            atomic_json(root/STATE,state);build_report(root)
            folder=attempt_dir(root,attempt)
            invfile=folder.parent/f'清单_{number:03d}.json';atomic_json(invfile,{p:report[p]})
            print(f'正在运行：{p}（第 {number} 次尝试）',flush=True)
            print('进度日志：'+str(folder/'tasks'/p/'agent.log'),flush=True)
            try:
                code=invoke(dict(state['config'],ProjectName=[p],Destination=str(folder),ExpectedInventory=str(invfile),StopOnApiError=True))
                row=evidence(root,p,attempt)
                if is_fatal(folder,p):
                    attempt['state']='接口失败';item['status']='接口失败'
                    print('模型接口拒绝请求，已停止后续项目。请检查密钥、账户余额和请求设置；详细原因见代理日志。',flush=True)
                elif code!=0 or row is None:
                    attempt['state']='运行中断';item['status']='运行中断'
                    print('运行未完整结束，已保留记录并停止后续项目。',flush=True)
                elif not evaluable(row):
                    attempt['state']='评估异常';item['status']='评估异常'
                    print('没有可确定的评估指标，停止后续项目。原始证据已保留，请检查编译/测试日志。',flush=True)
                else:
                    attempt['state']='已完成';item['status']='已完成'
                    if item['selected'] is None:item['selected']=number
                    elif retry:print('重跑结果已保存；性能指标仍使用原结果。切换请使用 -SelectAttempt。')
            except BaseException:
                attempt['state']='运行中断';item['status']='运行中断';raise
            finally:
                atomic_json(root/STATE,state);build_report(root)
            if attempt['state']!='已完成':
                print('报告已更新：'+str(root/'实验结果.md'))
                return 2
        print('报告已更新：'+str(root/'实验结果.md'))
        return 0


def select_attempt(root,project,number):
    root=Path(root)
    with series_lock(root):
        state=load(root);item=state['projects'].get(project)
        if not item:raise ValueError('实验中没有该项目。')
        attempt=next((a for a in item['attempts'] if a['number']==number),None)
        if not attempt or attempt['state']!='已完成' or not evaluable(evidence(root,project,attempt)):raise ValueError('只能选择已完成评估的尝试。')
        item['selected']=number
        state.setdefault('selection_history',[]).append(dict(project=project,attempt=number,time=time.time()))
        atomic_json(root/STATE,state);build_report(root)
        print('已明确切换性能结果；所有尝试的消耗仍保留。')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True);args=parser.parse_args()
    request=read(args.request);root=Path(request['root']);action=request['action']
    try:
        if action=='init':initialize(root,request['config'])
        elif action=='status':print(render(status_report(root)))
        elif action=='select':select_attempt(root,request['project'],request['number'])
        else:return run_projects(root,request['projects'],request['repo'],retry=request.get('retry',False),check_only=request.get('check_only',False),inventory_file=request.get('inventory_file'))
    except KeyboardInterrupt:
        print('操作已中断。已记录的尝试不会自动重跑。');return 2
    except Exception as exc:
        error_dir=root/'详细记录';error_dir.mkdir(parents=True,exist_ok=True)
        import traceback
        (error_dir/'最近错误.txt').write_text(traceback.format_exc(),encoding='utf-8')
        print('操作未完成：'+ (str(exc) if isinstance(exc,ValueError) else '请查看详细记录中的最近错误.txt。'))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
