"""Integration checks for cross-project collection and reminder preferences."""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/project-manager/scripts'
sys.path.insert(0, str(SCRIPTS))
import task_hub


def project_text(name, rows):
    header = '| ID | 待办及完成条件 | 状态 | 优先级 | 依赖 | 结果依据或阻塞原因 |'
    return (f'# 项目\n\n- 项目名称：{name}\n\n## 待办事项\n\n' + header
            + '\n| --- | --- | --- | --- | --- | --- |\n'
            + '\n'.join('| ' + ' | '.join(row) + ' |' for row in rows)
            + '\n\n## 待确认事项\n\n- 这是草案，不应收集为待办。\n')


class TaskHubTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.hub = self.base / 'hub'
        self.a = self.base / 'a'
        self.b = self.base / 'b'
        self.a.mkdir()
        self.b.mkdir()
        environment = patch.dict(os.environ, {'PROJECT_MANAGER_HUB_DIR': str(self.hub),
                                              'PYTHONDONTWRITEBYTECODE': '1'})
        environment.start()
        self.addCleanup(environment.stop)

    def store(self, command, root, *args):
        return subprocess.run([sys.executable, '-B', str(SCRIPTS / 'project_store.py'), command,
                               '--project-root', str(root), *map(str, args)],
                              text=True, capture_output=True)

    def initialize(self, root):
        result = self.store('init', root)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def update(self, root, rows, name='同名项目'):
        current = json.loads(self.store('read', root).stdout)
        replacement = self.base / f'{root.name}-replacement.md'
        replacement.write_text(project_text(name, rows))
        result = self.store('write', root, '--input', replacement,
                            '--expected-sha256', current['sha256'])
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def state(self):
        return json.loads((self.hub / 'hub.json').read_text())

    def test_no_reminder_time_by_default(self):
        self.initialize(self.a)
        reminder = self.state()['reminder']
        self.assertEqual(reminder, {'time': None, 'timezone': 'Asia/Shanghai',
                                   'status': 'not_configured', 'automation_id': None})
        self.assertIn('未设置，未启用', (self.hub / 'TODOS.md').read_text())

    def test_automatic_registration_and_update_filters_closed_tasks(self):
        self.initialize(self.a)
        self.update(self.a, [('T001', '开始工作', '待办', '高', '无', ''),
                             ('T002', '等待环境', '受阻', '中', 'T001', '需要环境'),
                             ('T003', '已测试', '已完成', '低', '无', '测试通过'),
                             ('T004', '取消工作', '已取消', '低', '无', '用户取消')])
        entry = self.state()['projects'][str(self.a)]
        self.assertEqual([item['id'] for item in entry['tasks']], ['T001', 'T002'])
        stats = task_hub.refresh()
        self.assertEqual((stats['open_task_count'], stats['blocked_task_count']), (2, 1))
        self.update(self.a, [('T001', '开始工作', '已完成', '高', '无', '测试通过')])
        self.assertEqual(task_hub.refresh()['open_task_count'], 0)
        self.assertIn('不代表项目已经验收结束', (self.hub / 'TODOS.md').read_text())

    def test_same_name_and_copied_ids_remain_separate(self):
        self.initialize(self.a)
        self.initialize(self.b)
        # Copied projects can have the same UUID and local task IDs.
        (self.b / '.project-manager.json').write_bytes((self.a / '.project-manager.json').read_bytes())
        # Re-register as a fresh hub, preserving independent project directories.
        (self.hub / 'hub.json').unlink()
        for root in (self.a, self.b):
            self.update(root, [('T001', f'{root.name}工作', '进行中', '高', '无', '')])
        stats = task_hub.refresh()
        self.assertEqual(stats['project_count'], 2)
        self.assertEqual(stats['open_task_count'], 2)
        self.assertEqual(len({item['root'] for item in stats['projects']}), 2)

    def test_refresh_reads_manual_edit_without_modifying_source(self):
        source = self.initialize(self.a)
        record = Path(source['record_path'])
        record.write_text(project_text('A', [('T001', '外部更新', '进行中', '高', '无', '')]))
        before = record.read_bytes()
        stats = task_hub.refresh()
        self.assertEqual(stats['open_task_count'], 1)
        self.assertEqual(stats['projects'][0]['source_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(record.read_bytes(), before)

    def test_invalid_table_is_marked_stale_not_zero(self):
        self.initialize(self.a)
        source = self.update(self.a, [('T001', '任务', '待办', '高', '无', '')])
        Path(source['record_path']).write_text('# 缺少待办表\n')
        stats = task_hub.refresh()
        self.assertEqual(stats['incomplete_project_count'], 1)
        self.assertEqual(stats['open_task_count'], 0)
        self.assertEqual(stats['projects'][0]['tasks'][0]['id'], 'T001')
        summary = (self.hub / 'TODOS.md').read_text()
        self.assertIn('总数不完整', summary)
        self.assertIn('旧缓存', summary)

    def test_missing_source_preserves_stale_cache(self):
        self.initialize(self.a)
        source = self.update(self.a, [('T001', '任务', '待办', '高', '无', '')])
        Path(source['record_path']).unlink()
        stats = task_hub.refresh()
        self.assertEqual(stats['incomplete_project_count'], 1)
        self.assertTrue(stats['projects'][0]['error'])
        self.assertEqual(len(stats['projects'][0]['tasks']), 1)

    def test_missing_project_configuration_is_not_created(self):
        with self.assertRaises(ValueError):
            task_hub.sync_project(self.a)
        self.assertEqual(list(self.a.iterdir()), [])
        self.assertFalse(self.hub.exists())

    def test_unknown_projects_are_not_scanned(self):
        self.initialize(self.a)
        (self.b / 'secret.txt').write_text('not a project')
        stats = task_hub.refresh()
        self.assertEqual([entry['root'] for entry in stats['projects']], [str(self.a)])
        self.assertFalse((self.b / '.project-manager.json').exists())

    def test_parse_escaped_pipes_code_fences_and_reordered_columns(self):
        content = '''# Project
```markdown
## 待办事项
| fake |
```
## 待办事项
| 状态 | ID | 待办及完成条件 | 依赖 | 优先级 | 结果依据或阻塞原因 |
| --- | --- | --- | --- | --- | --- |
| 受阻 | T001 | A \\| B | 无 | 高 | 等待 \\| 确认 |

## 其他
| T002 | 不应收集 | 待办 | 高 | 无 | 无 |
'''
        tasks = task_hub.extract_tasks(content)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['title'], 'A | B')
        self.assertEqual(tasks[0]['note'], '等待 | 确认')

    def test_invalid_rows_do_not_silently_disappear(self):
        for rows in [
            [('T001', '任务', '未知状态', '高', '无', '')],
            [('T001', '任务', '待办', '高', '无', ''), ('T001', '重复', '待办', '高', '无', '')],
            [('T001', '未转义 | 内容', '待办', '高', '无', '')],
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                task_hub.extract_tasks(project_text('A', rows))

    def test_parallel_project_saves_do_not_lose_entries(self):
        self.initialize(self.a)
        self.initialize(self.b)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda root: self.update(root, [('T001', root.name, '待办', '高', '无', '')]),
                          (self.a, self.b)))
        self.assertEqual(task_hub.refresh()['open_task_count'], 2)
        self.assertEqual(len(self.state()['projects']), 2)

    def test_hub_failure_does_not_rollback_project_save(self):
        self.initialize(self.a)
        (self.hub / 'hub.json').write_text('broken')
        current = json.loads(self.store('read', self.a).stdout)
        replacement = self.base / 'replacement.md'
        replacement.write_text(project_text('A', [('T001', '新任务', '待办', '高', '无', '')]))
        result = self.store('write', self.a, '--input', replacement,
                            '--expected-sha256', current['sha256'])
        self.assertEqual(result.returncode, 0)
        self.assertIn('本项目已保存', result.stderr)
        self.assertIn('新任务', json.loads(result.stdout)['content'])
        self.assertEqual((self.hub / 'hub.json').read_text(), 'broken')

    def test_path_escape_is_not_read_or_modified(self):
        self.initialize(self.a)
        config = self.a / '.project-manager.json'
        value = json.loads(config.read_text())
        outside = self.b / 'outside.md'
        outside.write_text(project_text('Outside', [('T001', '机密', '待办', '高', '无', '')]))
        value['record'] = '../b/outside.md'
        config.write_text(json.dumps(value))
        before = outside.read_bytes()
        stats = task_hub.refresh()
        self.assertEqual(stats['incomplete_project_count'], 1)
        self.assertNotIn('机密', (self.hub / 'TODOS.md').read_text())
        self.assertEqual(outside.read_bytes(), before)

    def test_project_identity_change_requires_reconfirmation(self):
        self.initialize(self.a)
        self.initialize(self.b)
        (self.a / '.project-manager.json').write_bytes((self.b / '.project-manager.json').read_bytes())
        stats = task_hub.refresh()
        a = next(entry for entry in stats['projects'] if entry['root'] == str(self.a))
        self.assertIn('标识已改变', a['error'])

    def test_time_validation_and_no_premature_activation(self):
        self.initialize(self.a)
        before = self.state()
        for time in ('24:00', '9:00', '12:60', '早上', ''):
            with self.subTest(time=time), self.assertRaises(ValueError):
                task_hub.set_reminder(time)
        with self.assertRaises(KeyError):
            task_hub.set_reminder('09:00', 'Not/A_Timezone')
        self.assertEqual(self.state(), before)
        settings = task_hub.set_reminder('09:00')
        self.assertEqual(settings['status'], 'pending')
        self.assertIsNone(settings['automation_id'])

    def test_configure_modify_pause_preserves_scheduler_identity(self):
        task_hub.set_reminder('09:00')
        active = task_hub.bind_reminder('test-only-id', 'active', '09:00', 'Asia/Shanghai')
        self.assertEqual(active['status'], 'active')
        same = task_hub.set_reminder('09:00')
        self.assertEqual(same['status'], 'active')
        changed = task_hub.set_reminder('20:30')
        self.assertEqual(changed['automation_id'], 'test-only-id')
        self.assertEqual(changed['status'], 'pending')
        with self.assertRaises(ValueError):
            task_hub.bind_reminder('test-only-id', 'active', '09:00', 'Asia/Shanghai')
        paused = task_hub.bind_reminder('test-only-id', 'paused', '20:30', 'Asia/Shanghai')
        self.assertEqual((paused['status'], paused['time']), ('paused', '20:30'))
        self.assertEqual(paused['automation_id'], 'test-only-id')

    def test_binding_requires_time(self):
        self.initialize(self.a)
        with self.assertRaises(ValueError):
            task_hub.bind_reminder('test-only-id', 'active', '09:00', 'Asia/Shanghai')
        self.assertEqual(self.state()['reminder']['status'], 'not_configured')


if __name__ == '__main__':
    unittest.main()
