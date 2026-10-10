"""离线演示执行过程，生成真实 Qt 界面截图，不调用外部模型。"""
import os
import sys
import json
import time
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
home = root / 'preview-home'
home.mkdir(exist_ok=True)
(home / '.minic').mkdir(exist_ok=True)
(home / '.minic/gui_state.json').unlink(missing_ok=True)
(home / '.minic/minic.json').write_text(json.dumps({'model': {'models': [
    {'name': 'DeepSeek（演示）', 'provider': 'mock', 'model': 'deepseek-flash', 'enabled': True}
]}}, ensure_ascii=False), encoding='utf-8')
Path.home = classmethod(lambda cls: home)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from minic.gui.theme import QSS
from minic.gui.main_window import MainWindow
from minic.gui.widgets.message_bubble import MessageBubble
from minic.gui.widgets.tool_group import ToolGroup

app = QApplication([])
QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc')
app.setFont(QFont('Microsoft YaHei', 10))
app.setStyleSheet(QSS)
MainWindow._connect_core = lambda self: None
window = MainWindow(workspace=str(root))
window.resize(1240, 900)
window._projects['MiniC'] = ['检查项目']
window._project_dirs['MiniC'] = str(root)
window._add_workspace_item('MiniC')
key = 'MiniC::检查项目'
window._session_messages[key] = [{
    'role': 'user', 'text': '帮我检查项目，整理已经完成的内容，以及接下来应该怎么做。',
    'tools': [], 'sources': [],
}]
window._open_session(key, '检查项目')
window.title_bar._title_label.setText('MiniC · 界面演示')
group = ToolGroup(parent=window._flow_container)
bubble = MessageBubble('ai', '')
window._flow_layout.addWidget(group)
window._flow_layout.addWidget(bubble)
bubble.hide()
group.begin()
group._started_at = time.monotonic() - 18
window._streams[key] = {'bubble': bubble, 'tool_group': group, 'session': key, 'saved': False,
                        'pending_retried': False, 'pending_text': '检查项目', 'tool_records': [], 'tool_cards': {}}

def event(name, **data):
    window._on_chat_event(name, {'_run': key, **data})

event('token', delta='我先查看项目说明和当前代码状态，再检查关键配置。', channel='activity')
event('tool_call', id='read', tool='Read', args={'path': 'README.md'})
event('tool_result', id='read', status='success', output='项目说明已读取。')
event('tool_call', id='git', tool='GitStatus', args={})
event('tool_result', id='git', status='success', output='已获取代码状态。')
event('token', delta='项目结构已确认，正在检查 Python 文件的语法。', channel='activity')
event('tool_call', id='lint', tool='Lint', args={'path': 'src/minic/core/server.py'})
window.show()
app.processEvents()
window._chat_scroll.verticalScrollBar().setValue(0)
app.processEvents()
out = root / 'preview'
out.mkdir(exist_ok=True)
window.grab().save(str(out / 'running.png'))
event('tool_result', id='lint', status='success', output='语法检查通过。')
event('answer', text='**检查完成，项目可以继续开发。**\n\n已查看项目说明、确认代码状态，并检查了核心文件的语法。3 项检查均已完成。\n\n**接下来怎么做**\n\n- 先在设置中确认聊天与向量模型都能连接。\n- 用一份小文档验证入库和检索，再导入完整知识库。\n- 需要查看检查详情时，展开上方的执行记录。')
event('message_end', status='completed')
event('done', status='completed', sources=[])
app.processEvents()
window._chat_scroll.verticalScrollBar().setValue(0)
app.processEvents()
window.grab().save(str(out / 'completed.png'))
group._toggle()
app.processEvents()
window.grab().save(str(out / 'expanded.png'))
window.close()
print(out)
