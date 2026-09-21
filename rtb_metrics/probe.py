"""单次最小接口检查；仅由用户明确请求时运行。"""
import json
import os
import sys
from pathlib import Path
from .core import atomic_json
from .telemetry import request_json


def probe(config,folder,post):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    task=folder/'tasks/probe';task.mkdir(parents=True,exist_ok=True)
    log=task/'requests.jsonl';log.touch(exist_ok=False)
    atomic_json(folder/'batch.json',dict(batch_id=config['batch_id'],model=config['model'],projects=['probe'],status='running',config=config))
    os.environ['RTB_REQUEST_LOG']=str(log)
    os.environ['RTB_STOP_ON_API_ERROR']='1'
    os.environ['RTB_CHAT_URL']=config['chat_url']
    options=dict(config.get('request_options',{}))
    # Keep this explicit diagnostic request short. Respect providers that use the newer field.
    key='max_completion_tokens' if 'max_completion_tokens' in options else 'max_tokens'
    options[key]=min(options.get(key,16),16)
    os.environ['RTB_MODEL_OPTIONS']=json.dumps(options)
    result=dict(success=False,batch_id=config['batch_id'],model=config['model'])
    try:
        body=request_json(post,config['chat_url'],headers={'Authorization':'Bearer '+os.getenv('LLM_API_KEY','local'),'Content-Type':'application/json'},json={'model':config['model'],'messages':[{'role':'user','content':'Reply OK.'}]},timeout=60)
        content=body['choices'][0]['message']['content']
        if not isinstance(content,str) or not content:raise ValueError('返回内容不是非空文本。')
        result.update(success=True,message='接口可返回文本；不代表完整仓库翻译一定成功。')
    except BaseException as exc:
        result['message']='接口测试未通过，请核对模型、地址、账户和请求参数。'
        result['error_type']=type(exc).__name__
    finally:
        atomic_json(folder/'probe.json',result)
        atomic_json(task/'result.json',dict(batch_id=config['batch_id'],project='probe',sr=None,cr=None,apr=None,ampr=None,request_log_complete=True,issues=[]))
        atomic_json(folder/'batch.json',dict(batch_id=config['batch_id'],model=config['model'],projects=['probe'],status='completed',config=config))
    print(result['message'],flush=True)
    return 0 if result['success'] else 1


def main():
    import requests
    return probe(json.load(sys.stdin),Path('/metrics-output'),requests.post)


if __name__=='__main__':raise SystemExit(main())
