"""Local readiness checks. Never mistake a working adapter for host/model use."""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys


def package_version():
    """Use the package's canonical version even in a direct-script install."""
    text = Path(__file__).with_name('__init__.py').read_text(encoding='utf8')
    match = re.search(r'''(?m)^__version__\s*=\s*["'](\d+\.\d+\.\d+)["']''', text)
    if not match:
        raise ValueError('Package version is missing; repair the HFA installation')
    return match.group(1)


def _mcp_capability():
    try:
        version = importlib.metadata.version('mcp')
    except importlib.metadata.PackageNotFoundError:
        return {'available': False, 'version': None, 'compatible': False, 'reason': 'not_installed'}
    if sys.version_info < (3, 11):
        return {'available': True, 'version': version, 'compatible': False, 'reason': 'python_3_11_required'}
    try:
        # Import the actual adapter symbol: finding a distribution is insufficient.
        from mcp.server import MCPServer
    except Exception:
        return {'available': True, 'version': version, 'compatible': False, 'reason': 'sdk_import_failed'}
    return {'available': True, 'version': version, 'compatible': version == '2.3.0',
            'reason': 'pinned_sdk' if version == '2.3.0' else 'untested_sdk_version'}


def inspect_memory(root, status_reader, require_mcp=False):
    root = Path(root).resolve()
    problems = []
    actions = []
    try:
        connection = sqlite3.connect(':memory:')
        try:
            connection.execute('CREATE VIRTUAL TABLE probe USING fts5(content)')
        finally:
            connection.close()
        fts5 = True
    except sqlite3.DatabaseError:
        fts5 = False
        problems.append('sqlite_fts5_unavailable')
        actions.append('当前 Python 缺少 FTS5，请使用包含 SQLite FTS5 的 Python。')
    runtime_ready = sys.version_info >= (3, 10) and fts5
    mcp = _mcp_capability()
    memory = {'initialized': (root/'directory.md').is_file(), 'healthy': False}
    if not memory['initialized']:
        problems.append('memory_not_initialized')
        actions.append('新目录请先运行 hfa init；已有数据的目录请核对路径，不要重置。')
    else:
        try:
            state = status_reader(root, return_result=True)
        except Exception as exc:
            # No exception text: invalid user data may contain sensitive text.
            memory.update(error='memory_check_failed', error_type=type(exc).__name__)
            problems.append('memory_check_failed')
            actions.append('运行 hfa status <记忆目录> --json 查看本地详细原因；保留现有文件。')
        else:
            memory.update(healthy=not state['problems'], total_topics=state['total_topics'],
                          active_epoch=state['active_epoch'],
                          problems=state['problems'],
                          token_measurement=state['token_measurement']['method'],
                          exact_tokens=state['token_measurement']['exact'])
            if state['problems']:
                problems.append('memory_unhealthy')
                actions.append('先备份。索引或派生摘要过期时运行 hfa reindex，再检查；原始记录错误需先核对来源。')
    ready_cli = runtime_ready and memory['healthy']
    ready_mcp = ready_cli and mcp['compatible']
    if not mcp['compatible']:
        actions.append('需要 MCP 时，在当前 Python 环境安装项目的 mcp 可选依赖，再重新检查。')
    return {'version': package_version(), 'python': sys.version.split()[0],
            'python_executable': sys.executable, 'memory_dir': str(root),
            'sqlite_version': sqlite3.sqlite_version, 'fts5': fts5, 'mcp': mcp,
            'memory': memory, 'ready_cli': ready_cli, 'ready_mcp': ready_mcp,
            'ready': ready_mcp if require_mcp else ready_cli,
            'problems': problems, 'actions': list(dict.fromkeys(actions)),
            'host': {'registration_checked': False, 'hook_executed': None, 'model_used': None},
            'notice': '本检查验证本地目录和运行依赖，不证明宿主已加载、模型已调用，也不代表账单节省。'}


def run(root, status_reader, as_json=False, require_mcp=False):
    result = inspect_memory(root, status_reader, require_mcp)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print('HFA ' + result['version'])
        print('记忆目录: ' + result['memory_dir'])
        print('本地查存: ' + ('就绪' if result['ready_cli'] else '需要处理'))
        print('MCP 接口: ' + ('依赖就绪，仍需宿主接入' if result['ready_mcp'] else '尚未就绪'))
        for action in result['actions']:
            print('- ' + action)
        print(result['notice'])
    return 0 if result['ready'] else 1
