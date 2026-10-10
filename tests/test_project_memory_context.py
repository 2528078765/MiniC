"""项目记忆来源、跨项目隔离与全局记忆编辑入口的回归。"""
import asyncio
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from minic.core.config import AppSettings
from minic.graph import build_super_graph
from minic.graph.knowledge.knowledge_node import knowledge_answer_node
from minic.graph.action.action_node import action_answer_node, extract_memory_node
from minic.memory import LongTermMemoryStore

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

class MemoryModel:
    def __init__(self):
        self.prompts = []
    async def ainvoke(self, messages):
        if '意图路由' in messages[0].content:
            # 故意路由到知识分支，项目约定也必须可回答。
            return AIMessage(content=json.dumps({'intent': 'knowledge'}))
        return AIMessage(content='[]')
    async def astream(self, messages):
        self.prompts.append(messages)
        system = messages[0].content
        marker = re.search(r'验收标记是：([^。\n]+)', system)
        yield AIMessageChunk(content=marker.group(1) if marker else '缺少项目记忆')

def setup_memory(root):
    root.mkdir(parents=True, exist_ok=True)
    (root/'Agent.md').write_text('# 项目约定\n本项目的验收标记是：青竹-7429。\n', encoding='utf-8')
    return LongTermMemoryStore(root/'global', root)

@pytest.mark.parametrize('branch', ['unrelated_docs', 'empty_docs', 'overview', 'action_fallback'])
def test_all_answer_branches_include_project_memory(tmp_path, branch):
    memory = setup_memory(tmp_path)
    model = MemoryModel()
    settings = AppSettings(model={'provider':'mock'})
    question = '知识库里有什么' if branch == 'overview' else '本项目的验收标记是什么？'
    state = {'workspace':str(tmp_path), 'project_root':str(tmp_path), 'user_message':question,
             'history':[], 'messages':[HumanMessage(content=question)], 'contexts':[], 'intent':'chat_action'}
    if branch == 'unrelated_docs':
        state['contexts']=[{'file_path':'技术资料.md','section':'','text':'这里是外部技术资料，不包含项目约定。'}]
    if branch == 'action_fallback':
        result = asyncio.run(action_answer_node(state, model, memory, settings))
    else:
        result = asyncio.run(knowledge_answer_node(state, model, memory, settings))
    assert result['answer'] == '青竹-7429'
    assert '请只依据下面的资料回答' not in model.prompts[-1][0].content

def test_new_chat_external_edit_and_project_isolation_even_if_misrouted(tmp_path):
    async def scenario():
        project_a=tmp_path/'A'
        project_b=tmp_path/'B'
        memory=setup_memory(project_a)
        project_b.mkdir()
        (project_b/'Agent.md').write_text('# 项目约定\n本项目的验收标记是：雪松-6048。\n',encoding='utf-8')
        settings=AppSettings(model={'provider':'mock'})
        rag=SimpleNamespace(settings=settings, inventory=lambda:[], query=lambda *args,**kwargs:[])
        graph=build_super_graph(rag, MemoryModel(), memory, settings)
        async def ask(project, thread, number):
            question='本项目的验收标记是什么？'
            return await graph.ainvoke({'workspace':str(project),'project_root':str(project),
                'user_message':question,'history':[],'messages':[HumanMessage(content=question,id=number)]},
                config={'configurable':{'thread_id':thread}})
        assert (await ask(project_a,'A-first','1'))['answer']=='青竹-7429'
        (project_a/'Agent.md').write_text('# 项目约定\n本项目的验收标记是：银杏-8613。\n', encoding='utf-8-sig')
        assert (await ask(project_a,'A-first','2'))['answer']=='银杏-8613'
        assert (await ask(project_a,'A-new-chat','3'))['answer']=='银杏-8613'
        assert (await ask(project_b,'B-first','4'))['answer']=='雪松-6048'
    asyncio.run(scenario())

def test_recognized_project_rule_creates_only_that_project_file(tmp_path):
    class RuleModel:
        async def ainvoke(self, messages):
            return AIMessage(content=json.dumps([{'scope':'project','topic':'开发约定','content':'使用 Python'}]))
    project=tmp_path/'project'
    project.mkdir()
    other=tmp_path/'other'
    other.mkdir()
    memory=LongTermMemoryStore(tmp_path/'global',other)
    asyncio.run(extract_memory_node({'workspace':str(project),'project_root':str(project),
        'user_message':'当前项目的长期开发约定是使用 Python'},RuleModel(),memory))
    assert '使用 Python' in (project/'Agent.md').read_text(encoding='utf-8')
    assert not (other/'Agent.md').exists()
    assert not (tmp_path/'global/minic.md').exists()

def test_chat_without_project_rules_does_not_create_empty_memory(tmp_path):
    from test_native_graph import environment, collect, ScriptModel
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path/'state.sqlite')) as saver:
            engine, _, _ = environment(tmp_path, ScriptModel(), saver)
            events=await collect(engine,tmp_path,message='你好')
            assert events[-1][1]['status']=='completed'
            assert not (tmp_path/'Agent.md').exists()
    asyncio.run(scenario())

@pytest.mark.parametrize('scenario', ['project', 'global', 'duplicate', 'deleted', 'disabled'])
def test_memory_confirmation_matches_actual_write_without_model_guessing(tmp_path, scenario):
    class RuleModel:
        async def ainvoke(self, messages):
            return AIMessage(content=json.dumps([{'scope':scope, 'topic':'验收规则', 'content':'测试输出统一使用英文'}]))
        async def astream(self, messages):
            raise AssertionError('保存回执应使用系统操作结果，不应让模型猜测')
            yield
    scope = 'global' if scenario == 'global' else 'project'
    memory = LongTermMemoryStore(tmp_path/'global', tmp_path)
    if scenario == 'duplicate':
        memory.add_topic(scope, str(tmp_path), '验收规则', '测试输出统一使用英文', source='user')
    if scenario == 'deleted':
        memory.mark_deleted(scope, str(tmp_path), '验收规则')
    if scenario == 'disabled':
        memory = None
    async def run():
        state = {'workspace':str(tmp_path), 'project_root':str(tmp_path), 'user_message':'请记住当前项目的验收规则'}
        update = await extract_memory_node(state, RuleModel(), memory)
        result = await action_answer_node({**state, **update}, RuleModel(), memory, AppSettings())
        assert result['messages'][-1].content == result['answer'] == update['confirmation']
        return result['answer']
    answer = asyncio.run(run())
    if scenario in {'disabled', 'deleted'}:
        assert answer.startswith('未保存：')
        if memory:
            assert '测试输出统一使用英文' not in memory.read(scope,str(tmp_path))['content']
    else:
        assert answer.startswith('已存在，无需重复保存' if scenario == 'duplicate' else '已保存：')
        saved = memory.read(scope,str(tmp_path))
        assert saved['path'] in answer
        assert '测试输出统一使用英文' in saved['content']
        assert ('全局记忆' if scope == 'global' else '项目记忆') in answer

@pytest.fixture(scope='session')
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])

class InlineWorker:
    def __init__(self, function, *args):
        self.function=function
        self.completed=SimpleSignal()
        self.failed=SimpleSignal()
    def start(self):
        self.completed.emit(self.function())

class SimpleSignal:
    def __init__(self): self.slots=[]
    def connect(self, slot): self.slots.append(slot)
    def emit(self, value):
        for slot in self.slots: slot(value)

def test_memory_editor_reads_and_writes_only_global(qapp,tmp_path,monkeypatch):
    import minic.gui.panels.memory_panel as module
    from PySide6.QtWidgets import QComboBox
    memory=setup_memory(tmp_path)
    original=(tmp_path/'Agent.md').read_bytes()
    requests=[]
    class Client:
        base_url='fixture'
        def get_memory(self,scope='merged',workspace=None):
            requests.append((scope,workspace))
            return memory.read(scope,workspace)
        def post_memory(self,payload):
            assert payload['scope']=='global'
            assert not payload.get('workspace')
            memory.write('global',None,payload['content'],base_version=payload['base_version'])
            return True
    monkeypatch.setattr(module,'Worker',InlineWorker)
    panel=module.MemoryPanel(client=Client(),workspace=str(tmp_path))
    panel.reload()
    assert not panel.findChildren(QComboBox)
    panel.set_edit_mode(True)
    panel._editor.setPlainText('# 全局偏好\n中文交流')
    assert panel.save()
    assert requests and all(item==('global',None) for item in requests)
    assert '中文交流' in memory.read('global')['content']
    assert (tmp_path/'Agent.md').read_bytes()==original
    assert panel._edit_btn.text()=='编辑'
    panel.close()

def test_chat_and_resume_use_session_workspace_not_selected_project(qapp,tmp_path,monkeypatch):
    import minic.gui.main_window as module
    home=tmp_path/'home'
    home.mkdir()
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    monkeypatch.setattr(module.MainWindow,'_connect_core',lambda self:None)
    monkeypatch.setattr(module.MainWindow,'_reload_models',lambda self:None)
    monkeypatch.setattr(module,'Worker',InlineWorker)
    window=module.MainWindow(workspace=str(home))
    window._project_dirs={'A':str(tmp_path/'A'),'B':str(tmp_path/'B')}
    window._current_project='B'
    window._session_threads={'A::会话':'thread-A','$task$::会话':'thread-task'}
    calls=[]
    window.client.stream_chat=lambda thread,workspace,*args,**kwargs:calls.append((thread,workspace,kwargs.get('resume',False)))
    window._fire_chat('提问','A::会话')
    assert calls[-1][:2]==('thread-A',str(tmp_path/'A'))
    window._resume_native('thread-A')
    assert calls[-1]==('thread-A',str(tmp_path/'A'),True)
    window._fire_chat('任务提问','$task$::会话')
    assert calls[-1][:2]==('thread-task',str(home/'.minic/work'))
    window.close()
