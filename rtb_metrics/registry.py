"""Source-bound reference inventories and deterministic dataset identities."""
import hashlib
import json
import os
from pathlib import Path

from .core import inventory_from_sources, validate_inventory, validate_module_map

_IGNORED = {'target', '.git', '__pycache__'}


def _reject_link(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError(f'不允许符号链接或目录联接：{path}')


def _files(root, ignored=()):
    root = Path(os.path.abspath(root))
    for ancestor in (root, *root.parents):
        _reject_link(ancestor)
    if not root.is_dir():
        raise ValueError(f'目录不存在：{root}')
    result = []
    for current, dirs, files in os.walk(root, followlinks=False):
        directory = Path(current)
        for name in dirs + files:
            _reject_link(directory/name)
        dirs[:] = sorted(name for name in dirs if name not in ignored)
        for name in sorted(files):
            path = directory/name
            if not path.is_file():
                raise ValueError(f'不是普通文件：{path}')
            result.append(path)
    return root, sorted(result, key=lambda p: p.relative_to(root).as_posix())


def fingerprint_tree(root):
    """Hash relative file names and bytes, excluding generated/cache directories."""
    root, files = _files(root, _IGNORED)
    digest = hashlib.sha256()
    for path in files:
        name = path.relative_to(root).as_posix().encode('utf-8')
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, 'big'))
        digest.update(name)
        digest.update(len(content).to_bytes(8, 'big'))
        digest.update(content)
    return digest.hexdigest()


def resolve_inventory(project, root):
    """Use reference identities only when every Java path and hash matches."""
    registry = json.loads(Path(__file__).with_name('registry.json').read_text(encoding='utf-8'))
    entry = registry.get(project)
    source = 'registry' if entry is not None else 'source'
    try:
        root, files = _files(root)
        if entry is not None:
            actual = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in files if path.suffix == '.java'}
            if actual != entry['java_sha256']:
                return None, {}, ['注册清单的 Java 文件路径或哈希不匹配；请重新核验测试清单，禁止复用过期结果。'], source
            inventory = validate_inventory(entry['tests'])
            mapping = validate_module_map(inventory, entry.get('module_map', {}))
            return inventory, mapping, [], source
        inventory, issues = inventory_from_sources(root)
        return inventory, {}, issues, source
    except (OSError, ValueError) as exc:
        return None, {}, [f'无法安全核验测试源文件：{exc}'], source
