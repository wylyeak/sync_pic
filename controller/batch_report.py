"""Persistent batch timing and user-facing completion reports."""
from datetime import datetime

PHASES = {
    'mapping': '建立硬链接', 'sync': '等待同步', 'backup': '等待相册备份',
    'release': '相册释放', 'release_uncertain': '释放操作及结果确认',
    'deletions': '等待删除同步', 'observe': '残留观察',
    'cleanup': '手动补充清理', 'final_sync': '最终删除同步',
}

def transition(batch, phase, stamp):
    previous = batch['phase']
    if 'phase_started_at' not in batch:
        batch['phase_started_at'] = stamp
        batch['timing_partial'] = True
    durations = batch.setdefault('phase_seconds', {})
    if phase and phase != previous:
        if previous != 'complete':
            durations[previous] = durations.get(previous, 0) + max(0, stamp - batch['phase_started_at'])
        batch['phase_started_at'] = stamp
        batch['phase'] = phase
    if batch['phase'] == 'complete':
        batch.setdefault('ended_at', stamp)

def duration(seconds):
    seconds = max(0, round(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}小时{minutes}分{seconds}秒' if hours else f'{minutes}分{seconds}秒'

def clock(stamp):
    return datetime.fromtimestamp(stamp).astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if stamp is not None else '未记录'

def summary(batch, manual_deleted):
    confirmed = batch.get('backup_confirmed')
    backed_up = len(set(confirmed) - set(batch.get('deferred', []))) if confirmed is not None else None
    total = len(batch['members'])
    backed_label = f'{backed_up} 个（相册整体确认）' if backed_up is not None else '未记录（旧批次）'
    total_time = duration(batch['ended_at'] - batch['started_at']) if batch.get('started_at') is not None else '未记录（旧批次）'
    lines = [f"批次：{batch['id']}", f"开始时间：{clock(batch.get('started_at'))}",
        f"结束时间：{clock(batch['ended_at'])}", f"文件数量：{total} 个",
        f"备份完成：{backed_label}",
        f"手动删除：{manual_deleted} 个（成功审计）",
        f"文件变化待核查：{len(batch.get('deferred', []))} 个",
        f"总耗时：{total_time}",
        '各阶段耗时（含等待及重启间隔）：']
    lines += [f'{label}：{duration(batch.get("phase_seconds", {}).get(phase, 0))}'
              for phase, label in PHASES.items()]
    if batch.get('timing_partial'):
        lines.append('本批升级前阶段耗时未记录，以上阶段统计从升级时开始。')
        if backed_up is not None:
            lines.append('旧批次缺少历史备份明细，备份数仅计升级后确认的文件。')
    return dict(body='\n'.join(lines), files=total, backed_up=backed_up, manual_deleted=manual_deleted,
                started_at=batch.get('started_at'), ended_at=batch['ended_at'],
                phase_seconds=dict(batch.get('phase_seconds', {})))
