#!/usr/bin/env python3
"""Control Google Photos through the shared host ADB, without cloud credentials."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

PKG = 'com.google.android.apps.photos'
LABELS = {'complete': '同步完成', 'syncing': '同步中', 'blocked': '暂停或异常', 'unknown': '状态未知'}


def classify(labels):
    text = '\n'.join(labels)
    if re.search(r'暂停|关闭|等待|无法|失败|空间不足|已满|未开启|未启用|过热|电量不足|温度过高|paused|off\b|waiting|unable|failed|full\b|too hot|overheat|battery low', text, re.I):
        state = 'blocked'
    elif re.search(r'正在备份|正在准备|正在同步|还剩|备份中|准备备份|Backing up|Preparing|remaining', text, re.I):
        state = 'syncing'
    elif any(re.fullmatch(r'(?:备份完成|已完成备份|已备份完毕|备份已完成|Backup complete|Backup is complete)[。.!！]?', s.strip(), re.I) for s in labels):
        state = 'complete'
    else:
        state = 'unknown'
    return {'state': state, 'message': LABELS[state], 'details': labels}


def labels(node):
    return [n.get('text').strip() for n in node.iter('node') if n.get('text', '').strip()]


def backup_status(root):
    # Only inspect the backup card, never photo titles or account names.
    for node in root.iter('node'):
        if node.get('clickable') != 'true':
            continue
        texts = labels(node)
        if any(re.fullmatch(r'备份|Backup|备份完成|Backup complete', s, re.I) for s in texts):
            return classify(texts)
    return classify([])


def battery_status(text):
    values = dict(re.findall(r'^\s*([^:\n]+):\s*([^\n]*)', text, re.M))
    level, scale = int(values['level']), int(values['scale'])
    if scale <= 0 or not 0 <= level <= scale:
        raise ValueError('Invalid battery level')
    status = int(values.get('status', '1'))
    return {'level': round(level * 100 / scale),
            'charging': status == 2,
            'power_connected': any(values.get(k) == 'true' for k in
                                   ('AC powered', 'USB powered', 'Wireless powered')),
            'status': {2: 'charging', 3: 'discharging', 4: 'not_charging', 5: 'full'}.get(status, 'unknown')}


class Phone:
    def __init__(self, args):
        if getattr(args, 'adb_backend', 'container') == 'host':
            self.prefix = [getattr(args, 'adb_path', None) or 'adb']
        else:
            self.prefix = ['docker', 'exec', args.container, getattr(args, 'adb_path', None) or '/data/dependencies/adb/adb']
        if args.serial:
            self.prefix += ['-s', args.serial]
        self.adb_timeout = getattr(args, 'adb_timeout', 40)
        self.timings = getattr(args, 'timings', False)
        self.started = time.monotonic()
        self.navigation_checked = False

    def battery(self):
        return battery_status(self.adb('shell', 'dumpsys', 'battery'))

    def adb(self, *args):
        started = time.monotonic()
        try:
            p = subprocess.run(self.prefix + list(args), text=True, capture_output=True,
                               timeout=self.adb_timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f'ADB 操作超过 {self.adb_timeout} 秒，已停止；可用 --timings 排查') from error
        finally:
            if self.timings:
                command_text = ' '.join(args)
                operation = ('open_app_and_dump' if 'am start' in command_text else
                             'tap_and_dump' if 'input tap' in command_text and 'uiautomator' in command_text else
                             'dump' if 'uiautomator' in command_text else 'input')
                print(json.dumps({'step': operation, 'seconds': round(time.monotonic()-started, 2),
                                  'elapsed_seconds': round(time.monotonic()-self.started, 2)},
                                 ensure_ascii=False), file=sys.stderr, flush=True)
        if p.returncode:
            raise RuntimeError(p.stderr.strip() or p.stdout.strip())
        return p.stdout

    def dump(self, before=(), settle=0):
        # One Docker/ADB call instead of separate rm, dump and cat calls. The
        # fresh file and test -s prevent an unsuccessful dump reusing old XML.
        path = '/data/local/tmp/codex-google-photos-ui.xml'
        commands = [shlex.join(command) + ' >/dev/null' for command in before]
        if settle:
            commands.append('sleep ' + str(float(settle)))
        commands += [shlex.join(['rm', '-f', path]),
                     shlex.join(['uiautomator', 'dump', '--compressed', path]) + ' >/dev/null',
                     shlex.join(['test', '-s', path]), shlex.join(['cat', path])]
        root = ET.fromstring(self.adb('shell', ' && '.join(commands)))
        if not any(n.get('package') == PKG for n in root.iter('node')):
            raise RuntimeError('Google 相册未在前台；请解锁手机，关闭其他弹窗后重试')
        return root

    def tap(self, root, predicate, refresh=False):
        parents = {child: parent for parent in root.iter() for child in parent}
        for node in root.iter('node'):
            if not predicate(node):
                continue
            while node.get('clickable') != 'true' and node in parents:
                node = parents[node]
            bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds', ''))
            if node.get('enabled') != 'true' or node.get('clickable') != 'true' or not bounds:
                continue
            x1, y1, x2, y2 = map(int, bounds.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            command = ['input', 'tap', str((x1+x2)//2), str((y1+y2)//2)]
            if refresh:
                return self.dump(before=[command], settle=0.3)
            self.adb('shell', *command)
            time.sleep(0.3)
            return
        raise RuntimeError('未找到预期控件；已停止，未使用固定坐标猜测操作')

    def menu(self):
        # Reuse the existing home Activity and finish navigation pages above it.
        # Starting without these flags stacks Home/FreeUpSpace on every poll.
        # Do not force-stop Photos: its background backup must keep running.
        # NEW_TASK | CLEAR_TOP | SINGLE_TOP; numeric flags support older Android.
        flags = '0x34000000'
        if not self.navigation_checked:
            stack = self.adb('shell', 'dumpsys', 'activity', 'activities')
            homes = sum('Hist #' in line and PKG + '/.home.HomeActivity' in line
                        for line in stack.splitlines())
            if homes > 1:
                # Legacy duplicate homes sit below the newest home. CLEAR_TOP
                # cannot reach them. Reset only the UI task once, never the app
                # process/data or its background backup service.
                flags = '0x10008000'  # NEW_TASK | CLEAR_TASK
        root = self.dump(before=[['input', 'keyevent', 'KEYCODE_WAKEUP'],
                                 ['am', 'start', '-f', flags,
                                  '-n', PKG + '/.home.HomeActivity']], settle=0.3)
        self.navigation_checked = True
        for attempt in range(3):
            if any(n.get('resource-id', '').endswith('/og_bento_toolbar_close_button') for n in root.iter('node')):
                return root
            if any(n.get('resource-id', '').endswith('/selected_account_disc') for n in root.iter('node')):
                root = self.tap(root, lambda n: n.get('resource-id', '').endswith('/selected_account_disc'), refresh=True)
                if any(n.get('resource-id', '').endswith('/og_bento_toolbar_close_button') for n in root.iter('node')):
                    return root
                raise RuntimeError('点击头像后未识别到账号菜单，已停止')
            if attempt < 2:
                root = self.dump(before=[['input', 'keyevent', 'KEYCODE_BACK']], settle=0.3)
        raise RuntimeError('无法进入 Google 相册账号菜单')

    def status(self):
        return backup_status(self.menu())

    def free_page(self, root):
        root = self.tap(root, lambda n: n.get('text', '') in ('释放此设备的空间', '释放设备空间', 'Free up space on this device', 'Free up space'), refresh=True)
        texts = labels(root)
        if any(re.search(r'没有可释放|没有需要释放|无需释放|Nothing to free up|No items to remove', t, re.I) for t in texts):
            return root, None
        safety = any(n.get('resource-id', '').endswith('/safetyTip') and re.search(r'安全备份|safely backed up', n.get('text', ''), re.I) for n in root.iter('node'))
        buttons = [n for n in root.iter('node') if n.get('resource-id', '').endswith('/free_up_button') and n.get('enabled') == 'true']
        if not safety or len(buttons) != 1:
            raise RuntimeError('未识别到已安全备份说明和释放按钮，停止操作')
        return root, buttons[0].get('text')

    def release_space(self, execute=False):
        """A recognized completion/no-items result is required by retention cleanup."""
        status = self.status()
        if status['state'] != 'complete':
            return {'action': 'skipped', 'status': status}
        time.sleep(10)
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
        root = self.menu()
        fresh = backup_status(root)
        if fresh['state'] != 'complete':
            return {'action': 'skipped', 'status': fresh}
        root, button = self.free_page(root)
        if not execute:
            return {'action': 'preview', 'button': button, 'nothing_to_free': button is None}
        if button is None:
            return {'action': 'nothing_to_free'}
        self.tap(root, lambda n: n.get('resource-id', '').endswith('/free_up_button'))
        for _ in range(6):
            time.sleep(5)
            root = self.dump()
            if any(re.search(r'已释放|成功释放|空间已释放|freed up|You freed|Nothing to free up', t, re.I)
                   for t in labels(root)):
                return {'action': 'freed'}
        return {'action': 'verification_required'}


def emit(result):
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'free-up', 'watch'])
    parser.add_argument('--container', default='pixel-web')
    parser.add_argument('--adb-backend', choices=['host', 'container'], default='host')
    parser.add_argument('--adb-path', help='ADB 客户端路径；默认按后端选择')
    parser.add_argument('--serial', default=os.environ.get('PICSYNC_SERIAL', ''))
    parser.add_argument('--execute', action='store_true', help='实际点击释放按钮；不传则仅预览')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--timeout', type=int, default=21600)
    parser.add_argument('--adb-timeout', type=int, default=40, help='每次 ADB 操作超时秒数，默认 40')
    parser.add_argument('--timings', action='store_true', help='向标准错误输出各次 ADB 调用耗时')
    args = parser.parse_args()
    if not args.serial:
        parser.error('Set --serial or PICSYNC_SERIAL explicitly')
    if args.interval < 10 or args.timeout < 1 or args.adb_timeout < 1:
        parser.error('interval 必须 >= 10 秒，timeout 和 adb-timeout 必须为正数')
    lock_id = hashlib.sha256((args.container + args.serial).encode()).hexdigest()[:16]
    lock_path = os.path.join(tempfile.gettempdir(), 'phone-' + lock_id + '.lock')
    with ExitStack() as locks, open(lock_path, 'a') as lock:
        if args.execute:
            controller_lock = locks.enter_context(open(os.path.join(tempfile.gettempdir(), 'file-sync-controller.lock'), 'a'))
            try:
                fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('自动备份控制器运行中，禁止独立释放空间')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('已有相册脚本运行')
        phone = Phone(args)
        deadline = time.monotonic() + args.timeout
        while True:
            status = phone.status()
            emit(status)
            if args.command == 'status':
                return 0 if status['state'] != 'unknown' else 2
            if status['state'] == 'complete':
                # Reopen the account menu to refresh status before entering cleanup.
                time.sleep(10)
                phone.adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
                root = phone.menu()
                fresh = backup_status(root)
                emit(fresh)
                if fresh['state'] == 'complete':
                    root, button = phone.free_page(root)
                    if button is None:
                        emit({'action': 'nothing_to_free', 'message': '没有可释放内容'})
                        return 0
                    if not args.execute:
                        emit({'action': 'preview', 'button': button, 'message': '仅预览，未释放空间'})
                        return 0
                    phone.tap(root, lambda n: n.get('resource-id', '').endswith('/free_up_button'))
                    # A click is not proof of completion. Only report a recognized result.
                    for _ in range(6):
                        time.sleep(5)
                        result = phone.dump()
                        if any(re.search(r'已释放|成功释放|空间已释放|freed up|You freed|Nothing to free up', t, re.I) for t in labels(result)):
                            emit({'action': 'freed', 'message': 'Google 相册已显示清理完成'})
                            return 0
                    emit({'action': 'verification_required', 'message': '已点击释放，但未识别到完成提示，请在手机核实；不会重复点击'})
                    return 3
                status = fresh
            if args.command != 'watch' or status['state'] != 'syncing':
                emit({'action': 'skipped', 'message': '备份未完成，未释放空间'})
                return 2
            if time.monotonic() + args.interval >= deadline:
                emit({'action': 'timeout', 'message': '等待备份完成超时'})
                return 2
            phone.adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
            time.sleep(args.interval)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.TimeoutExpired, ET.ParseError, OSError) as error:
        emit({'state': 'error', 'message': str(error)})
        sys.exit(1)
