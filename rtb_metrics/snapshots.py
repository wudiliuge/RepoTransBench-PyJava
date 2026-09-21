"""Pure, deterministic union of immutable snapshot leaves; no filesystem access."""
import copy
import json
import math

from .core import aggregate, summarize_usage


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('快照包含无法验证的数据。') from exc


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _validate_leaf(leaf):
    if not isinstance(leaf, dict) or any(not isinstance(leaf.get(k), str) or not leaf[k]
                                         for k in ('id', 'project', 'origin')):
        raise ValueError('快照记录缺少有效身份、项目或来源。')
    if type(leaf.get('attempted')) is not bool or (leaf.get('row') is not None and not isinstance(leaf['row'], dict)):
        raise ValueError('快照记录的尝试状态或指标无效。')
    attempt = leaf.get('attempt')
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        raise ValueError('快照记录的尝试编号无效。')
    if not leaf['attempted']:
        return
    cost = leaf.get('cost')
    if not isinstance(cost, dict) or not isinstance(cost.get('token_usage'), dict):
        raise ValueError('快照缺少尝试成本。')
    usage = cost['token_usage']
    counters = ('requests', 'unknown_requests', 'known_input_tokens', 'known_output_tokens', 'known_total_tokens')
    if any(type(usage.get(k)) is not int or usage[k] < 0 for k in counters):
        raise ValueError('快照 token 计数无效。')
    if (type(usage.get('complete')) is not bool or usage['unknown_requests'] > usage['requests']
            or usage['known_input_tokens'] + usage['known_output_tokens'] != usage['known_total_tokens']):
        raise ValueError('快照 token 用量记录不一致。')
    if usage['complete'] and (usage['unknown_requests'] or any(
            usage.get(k) != usage['known_' + k] for k in ('input_tokens', 'output_tokens', 'total_tokens'))):
        raise ValueError('快照 token 完整状态与用量不一致。')
    if not _number(cost.get('known_elapsed_seconds')) or (
            cost.get('total_elapsed_seconds') is not None and not _number(cost['total_elapsed_seconds'])):
        raise ValueError('快照耗时无效。')
    for key in ('configured_iterations','actual_iterations'):
        if cost.get(key) is not None and (type(cost[key]) is not int or cost[key] < 0):
            raise ValueError('快照轮数无效。')
    if (cost.get('configured_iterations') is not None and cost.get('actual_iterations') is not None
            and cost['actual_iterations'] > cost['configured_iterations']):
        raise ValueError('快照实际轮数超过设定轮数。')


def _cost(leaves):
    attempted = [leaf for leaf in leaves if leaf['attempted']]
    usage = summarize_usage([])
    for key in ('requests', 'unknown_requests', 'known_input_tokens', 'known_output_tokens', 'known_total_tokens'):
        usage[key] = sum(leaf['cost']['token_usage'][key] for leaf in attempted)
    usage['complete'] = all(leaf['cost']['token_usage']['complete'] for leaf in attempted)
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        usage[key] = usage['known_' + key] if usage['complete'] else None
    durations = [leaf['cost']['total_elapsed_seconds'] for leaf in attempted]
    configured = [leaf['cost'].get('configured_iterations') for leaf in attempted]
    actual = [leaf['cost'].get('actual_iterations') for leaf in attempted]
    return dict(count=len(attempted), token_usage=usage,
                total_elapsed_seconds=sum(durations) if all(x is not None for x in durations) else None,
                known_elapsed_seconds=sum(leaf['cost']['known_elapsed_seconds'] for leaf in attempted),
                configured_iterations=sum(configured) if all(x is not None for x in configured) else None,
                actual_iterations=sum(actual) if all(x is not None for x in actual) else None,
                known_configured_iterations=sum(x for x in configured if x is not None),
                known_actual_iterations=sum(x for x in actual if x is not None),
                unknown_iteration_attempts=sum(x is None or y is None for x,y in zip(configured,actual)))


def merge_snapshots(sources, selection=None, identifier='汇总001'):
    """Return a detached v2 snapshot and recomputed report.

    ``selection`` stores deliberate project->leaf choices only. A sole candidate
    needs no persisted choice: otherwise a later duplicate could silently choose
    that candidate. Input reports are never trusted or summed.
    """
    sources = list(sources)
    if not sources:
        raise ValueError('请至少提供一个来源快照。')
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('汇总编号不能为空。')
    selection = {} if selection is None else selection
    if not isinstance(selection, dict):
        raise ValueError('项目选择必须是项目到记录身份的映射。')
    leaves, inherited, config = {}, {}, None
    for source in sources:
        if (not isinstance(source, dict) or source.get('version') != 2
                or source.get('kind') not in ('batch', 'merge')
                or not isinstance(source.get('config'), dict)
                or not isinstance(source.get('leaves'), list)):
            raise ValueError('来源不是有效的第二版批次或汇总快照。')
        encoded = _canonical(source['config'])
        if config is None:
            config = encoded
        elif config != encoded:
            raise ValueError('来源运行配置不一致，不能合并。')
        own = {}
        for leaf in source['leaves']:
            _validate_leaf(leaf)
            identity = leaf['id']
            if identity in leaves and _canonical(leaves[identity]) != _canonical(leaf):
                raise ValueError('同一记录身份对应不同内容，快照可能被修改：' + identity)
            leaves[identity] = copy.deepcopy(leaf)
            own[identity] = leaf
        choices = source.get('selection', {})
        if not isinstance(choices, dict):
            raise ValueError('来源快照的项目选择无效。')
        for project, identity in choices.items():
            if not isinstance(identity, str) or identity not in own or own[identity]['project'] != project:
                raise ValueError('来源快照选择了不属于该项目的记录：' + str(project))
            inherited.setdefault(project, set()).add(identity)
    if not leaves:
        raise ValueError('快照没有计划项目，不能合并。')
    by_project = {}
    for leaf in leaves.values():
        by_project.setdefault(leaf['project'], []).append(leaf['id'])
    for project, identity in selection.items():
        if (not isinstance(identity, str) or identity not in leaves
                or leaves[identity]['project'] != project):
            raise ValueError('明确选择的记录不属于来源中的该项目：' + str(project))
    resolved, rows, projects = {}, [], []
    for project in sorted(by_project):
        candidates = by_project[project]
        prior = inherited.get(project, set())
        if project in selection:
            chosen = resolved[project] = selection[project]
        elif len(prior) == 1:
            chosen = resolved[project] = next(iter(prior))
        elif len(prior) > 1 or len(candidates) > 1:
            raise ValueError('项目存在多个记录或来源选择冲突，请明确选择：' + project)
        else:
            chosen = candidates[0]
        leaf = leaves[chosen]
        if leaf['row'] is not None:
            rows.append(dict(leaf['row'], project=project))
        projects.append(dict(project=project, selected=chosen, origin=leaf['origin'],
                             attempt=leaf.get('attempt'), attempts=sum(leaves[x]['attempted'] for x in candidates)))
    ordered = [leaves[key] for key in sorted(leaves)]
    try:
        report = aggregate(sorted(by_project), rows)
    except ValueError as exc:
        raise ValueError('快照指标无效：' + str(exc)) from exc
    cost = _cost(ordered)
    report.update(projects=projects, all_attempts=cost, token_usage=copy.deepcopy(cost['token_usage']),
                  total_elapsed_seconds=cost['total_elapsed_seconds'], known_elapsed_seconds=cost['known_elapsed_seconds'])
    return dict(version=2, id=identifier, kind='merge', config=copy.deepcopy(sources[0]['config']),
                leaves=ordered, selection=resolved, report=report)
