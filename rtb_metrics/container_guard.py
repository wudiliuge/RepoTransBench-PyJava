"""Own one launcher and its cidfile; never discover or stop unrelated processes."""
import json
import errno
import os
from pathlib import Path
import re
import subprocess


def _cid(folder):
    try:
        value = (Path(folder) / 'docker.cid').read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        return ''
    return value if re.fullmatch(r'[0-9a-fA-F]{64}', value) else ''


def _docker():
    return 'docker.exe' if os.name == 'nt' else 'docker'


def _inspect(cid, run):
    try:
        result = run([_docker(), 'inspect', '--format', '{{.State.Running}}', cid],
                     capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    if result.returncode:
        error = (result.stderr or '').lower()
        return 'stopped' if ('no such object' in error or 'no such container' in error) else 'unknown'
    return {'true': 'running', 'false': 'stopped'}.get(result.stdout.strip().lower(), 'unknown')


def _pending_client(folder):
    try:
        info = json.loads((Path(folder) / 'client_process.json').read_text(encoding='utf-8'))
        if info.get('active') is False:
            return False
        return _pid_alive(info.get('pid')) is not False
    except FileNotFoundError:
        return False
    except (OSError, ValueError, AttributeError):
        return True


def _pid_alive(pid):
    """True/False/None; a reused or inaccessible PID never clears ownership.

    Windows signal-zero is not a read-only probe, so use a limited-query
    process handle. Only a confirmed nonexistent/exited process is safe.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as error:
        return False if error.errno == errno.ESRCH else None
    return True


def container_state(folder, run=subprocess.run):
    """Return running/stopped/unknown/absent, failing closed on unfinished launchers.

    A missing/exited client can clear an active marker. A live or reused PID,
    inaccessible process, or missing PID conservatively requires manual review.
    """
    cid = _cid(folder)
    state = 'absent' if cid is None else _inspect(cid, run) if cid else 'unknown'
    if state != 'running' and _pending_client(folder):
        return 'unknown'
    return state


def stop_container(folder, run=subprocess.run):
    """Stop only the complete ID in this folder and verify that it is stopped."""
    cid = _cid(folder)
    if not cid:
        return False
    try:
        run([_docker(), 'stop', '--time', '10', cid], capture_output=True,
            text=True, timeout=25, check=False)
    except (OSError, subprocess.SubprocessError):
        pass
    stopped = _inspect(cid, run) == 'stopped'
    if stopped and not _pending_client(folder):
        try:
            marker = Path(folder) / 'client_process.json'
            info = json.loads(marker.read_text(encoding='utf-8'))
            _record(folder, 'client_process.json', {'active': False, 'pid': info.get('pid')})
        except (OSError, ValueError, AttributeError):
            pass
    return stopped


def _record(folder, name, payload):
    path = Path(folder) / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run_owned(command, folder, stdout, cancel_file=None):
    """Run argv synchronously; on Ctrl+C stop the owned container and child.

    Metadata contains neither command arguments nor raw exception output. If
    cleanup is uncertain the persistent active marker blocks automatic retries.
    """
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError('启动命令必须是非空参数列表。')
    cancel_file = Path(cancel_file) if cancel_file is not None else None
    if cancel_file is not None and cancel_file.exists():
        raise KeyboardInterrupt
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if container_state(folder) not in ('absent', 'stopped'):
        raise RuntimeError('上次运行可能仍在进行，无法安全启动；请先确认容器和启动进程状态。')
    _record(folder, 'client_process.json', {'active': True, 'pid': None})
    child = None
    try:
        if cancel_file is not None and cancel_file.exists():
            raise KeyboardInterrupt
        child = subprocess.Popen(list(command), stdout=stdout, stderr=subprocess.STDOUT)
        _record(folder, 'client_process.json', {'active': True, 'pid': child.pid})
        if cancel_file is None:
            code = child.wait()
        else:
            while True:
                if cancel_file.exists():
                    raise KeyboardInterrupt
                try:
                    code = child.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
        _record(folder, 'client_process.json', {'active': False, 'pid': child.pid})
        return code
    except KeyboardInterrupt:
        stopped = False
        reaped = child is None
        try:
            stop_container(folder)
        except BaseException:
            pass
        if child is not None:
            try:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=10)
                reaped = True
            except BaseException:
                pass
        # Inspect again after reaping: the launcher may have written its cidfile
        # between the first stop attempt and termination.
        try:
            stopped = stop_container(folder)
        except BaseException:
            pass
        try:
            _record(folder, 'container_cleanup.json', {
                'stopped': stopped,
                'client_stopped': reaped,
                'message': ('已确认本次容器停止。' if stopped else
                            '无法确认本次容器停止；禁止自动重试，请人工检查。'),
            })
            if (stopped and reaped) or child is None:
                _record(folder, 'client_process.json', {'active': False, 'pid': child.pid if child else None})
        except BaseException:
            pass
        raise
    except OSError:
        if child is None:
            _record(folder, 'client_process.json', {'active': False, 'pid': None})
        raise
