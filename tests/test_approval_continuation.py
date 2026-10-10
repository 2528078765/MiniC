"""真实界面审批衔接、无模态关闭与原生持久化恢复回归。"""
import asyncio
import json
from pathlib import Path
import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from test_native_graph import environment, collect, ScriptModel
from test_project_memory_context import qapp, InlineWorker


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    import minic.gui.main_window as module
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path/'home'))
    monkeypatch.setattr(module.MainWindow, '_connect_core', lambda self: None)
    monkeypatch.setattr(module.MainWindow, '_reload_models', lambda self: None)
    view = module.MainWindow(workspace=str(tmp_path))
    view.client.base_url = 'fixture'
    view._current_session = 'P::会话'
    view._session_threads = {'P::会话':'thread'}
    view._project_dirs = {'P':str(tmp_path)}
    view._session_messages = {'P::会话':[]}
    yield view
    view.close()


def paused_stream(window):
    from minic.gui.widgets.message_bubble import MessageBubble
    from minic.gui.widgets.tool_group import ToolGroup
    stream = {'session':'P::会话', 'bubble':MessageBubble('ai',''), 'tool_group':ToolGroup(),
              'saved':False,'pending_text':'run','pending_retried':False,'tool_records':[], 'tool_cards':{},
              'transport_active':True}
    window._streams['P::会话'] = stream
    window._on_chat_event('done', {'_run':'P::会话','status':'waiting_approval'})
    return stream


@pytest.mark.parametrize('decision', ['allow_once','deny'])
def test_approval_automatically_resumes_after_transport_closes(window, monkeypatch, decision):
    import minic.gui.main_window as module
    monkeypatch.setattr(module, 'Worker', InlineWorker)
    window.client.approve = lambda *args: True
    calls=[]
    window.client.stream_chat = lambda *args, **kwargs: calls.append((args,kwargs))
    stream=paused_stream(window)
    window._submit_approval('thread','approval',decision)
    assert stream['resume_requested'] and not calls
    window._on_transport_closed('P::会话')
    assert len(calls)==1 and calls[0][1]['resume'] is True
    assert not stream['paused']
    assert not window._recovery_buttons


def test_real_background_worker_approval_callback_resumes_without_extra_clicks(window, qapp):
    from PySide6.QtTest import QTest
    window.client.approve = lambda *args: True
    calls=[]
    window.client.stream_chat = lambda *args, **kwargs: calls.append((args,kwargs))
    stream=paused_stream(window)
    stream['transport_active']=False
    window._submit_approval('thread','approval','allow_once')
    for _ in range(100):
        QTest.qWait(10)
        if calls:break
    assert len(calls)==1
    assert calls[0][1]['resume'] is True
    QTest.qWait(30)


def test_send_message_in_paused_session_resumes_with_followup(window, monkeypatch):
    import minic.gui.main_window as module
    monkeypatch.setattr(module, 'Worker', InlineWorker)
    calls=[]
    window.client.stream_chat = lambda *args, **kwargs: calls.append((args,kwargs))
    stream=paused_stream(window)
    stream['transport_active']=False
    window._send_message('继续，并根据补充文件调整总结')
    assert calls[0][0][2]=='继续，并根据补充文件调整总结'
    assert calls[0][1]['resume'] is True


def test_recovery_is_one_click_and_never_adds_a_second_button(window, monkeypatch):
    calls=[]
    monkeypatch.setattr(window,'_resume_native',lambda *args:calls.append(args))
    window._show_recovery('P::会话',{'resumable':True,'pending_approvals':[{'id':'test'}]})
    window._show_recovery('P::会话',{'resumable':True})
    assert len(window._recovery_buttons)==1
    window._recovery_buttons['P::会话'].click()
    assert calls==[('thread',)]


def test_approval_can_be_dismissed_and_main_window_can_close(window, qapp):
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtTest import QTest
    window.show()
    decisions=[]
    window.client.approve=lambda *args:decisions.append(args)
    payload={'id':'approval','thread_id':'thread','tool':'Write','args':{'path':'file.txt'}}
    window._handle_approval_requested(payload)
    dialog=window._approval_dialog
    assert dialog.isVisible() and not dialog.isModal()
    window._handle_approval_requested(payload)
    assert not window._approval_queue
    dialog.findChild(QPushButton,'approvalClose').click()
    assert not window._approval_dialog_open and not decisions
    QTest.qWait(1)
    window._handle_approval_requested(payload)
    window.close()
    assert not window.isVisible() and not window._approval_dialog_open
    assert not decisions


@pytest.mark.parametrize('theme', ['dark', 'light'])
def test_approval_close_icon_is_visible_with_application_theme(qapp, theme):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtTest import QTest
    from minic.gui.theme import qss_for
    from minic.gui.widgets.dialogs import ApprovalDialog
    previous_style = qapp.styleSheet()
    qapp.setStyleSheet(qss_for(theme))
    dialog = ApprovalDialog('Write', {'path': 'file.txt'})
    try:
        dialog.show()
        QTest.qWait(10)
        button = dialog.findChild(QPushButton, 'approvalClose')
        image = button.grab().toImage()
        # The actual rendered center must contain the close symbol, not just a border.
        center = range(image.width() // 4, 3 * image.width() // 4)
        assert sum(
            all(channel >= 230 for channel in (pixel.red(), pixel.green(), pixel.blue()))
            for x in center for y in center
            for pixel in [image.pixelColor(x, y)]
        ) >= 8
        assert button.accessibleName() == '暂时关闭审批窗口'
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert dialog.decision is None
        assert not dialog.isVisible()
    finally:
        dialog.close()
        qapp.setStyleSheet(previous_style)


class SequentialModel(ScriptModel):
    async def astream(self,messages):
        self.prompts.append(messages)
        results=[m for m in messages if isinstance(m,ToolMessage)]
        if not results:
            yield AIMessageChunk(content='',tool_calls=[{'name':'Bash','args':{'command':'echo FIRST-8146>> approval-log.txt'},'id':'append-once'}])
        elif len(results)==1:
            yield AIMessageChunk(content='',tool_calls=[{'name':'Write','args':{'path':'resume-result.txt','content':'RESUME-OK-9032'},'id':'second-write'}])
        else:
            yield AIMessageChunk(content='两步均已完成；'+str(messages[-1].content))


def test_restart_send_continue_preserves_approval_and_followup_without_repeating_tools(tmp_path):
    async def run():
        database=str(tmp_path/'state.sqlite')
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine,_,_=environment(tmp_path,SequentialModel(),saver)
            events=await collect(engine,tmp_path)
            assert events[-1][1]['status']=='waiting_approval'
            assert not (tmp_path/'approval-log.txt').exists()
            first=(await engine.execution_state('thread',str(tmp_path)))['pending_approvals'][0]
            await engine.approve('thread',str(tmp_path),first['id'],'allow_once')
            events=await collect(engine,tmp_path,message='',resume=True)
            assert events[-1][1]['status']=='waiting_approval',events
            assert (tmp_path/'approval-log.txt').read_text().strip()=='FIRST-8146'
            assert not (tmp_path/'resume-result.txt').exists()
            second=(await engine.execution_state('thread',str(tmp_path)))['pending_approvals'][0]
        # 重启后正常发送补充，无 resume 标志，也不能绕过第二项审批。
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            engine,_,_=environment(tmp_path,SequentialModel(),saver)
            events=await collect(engine,tmp_path,message='继续，最后说明我补充的 FLAG-583',resume=False,run='new-run',mid='new-message')
            assert events[-1][1]['status']=='waiting_approval'
            pending=(await engine.execution_state('thread',str(tmp_path)))['pending_approvals'][0]
            assert pending['interrupt_id']==second['interrupt_id']
            assert not (tmp_path/'resume-result.txt').exists()
        # 再次重启：补充文字和审批均仍存在；明确允许后只执行剩余工具。
        async with AsyncSqliteSaver.from_conn_string(database) as saver:
            model=SequentialModel()
            engine,_,runtime=environment(tmp_path,model,saver)
            await engine.approve('thread',str(tmp_path),second['id'],'allow_once')
            events=await collect(engine,tmp_path,message='',resume=True)
            assert events[-1][1]['status']=='completed',events
            assert any(name=='answer' and 'FLAG-583' in data['text'] for name,data in events)
            assert (tmp_path/'resume-result.txt').read_text()=='RESUME-OK-9032'
            assert (tmp_path/'approval-log.txt').read_text().splitlines()==['FIRST-8146']
            values=(await engine.state('thread',str(tmp_path))).values
            messages=values['messages']
            assert len([m for m in messages if m.type=='human' and 'FLAG-583' in m.content])==1
            assert [r['tool'] for r in runtime.tool_log.read() if r['event']=='intent']==['Bash','Write']
            # 工具结果配对完整后才追加用户补充，避免 DeepSeek 接口拒绝。
            for i,m in enumerate(model.prompts[-1]):
                if getattr(m,'tool_calls',None):
                    assert isinstance(model.prompts[-1][i+1],ToolMessage)
            inbox=await engine._inbox().aget_state(engine._inbox_config('thread',str(tmp_path)))
            assert not inbox.values['messages']
    asyncio.run(run())


def test_http_normal_send_resumes_and_still_requires_explicit_approval(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from minic.core.server import create_app
    from minic.core.config import AppSettings
    from minic.graph import build_super_graph
    from test_chat import _parse_sse
    home=tmp_path/'home'
    home.mkdir()
    monkeypatch.setattr(Path,'home',classmethod(lambda cls:home))
    settings=AppSettings(model={'provider':'mock'},embedding={'provider':'mock','dimension':64})
    settings.approval.workspace_write_auto_approve=False
    app=create_app(settings=settings,token='fixture-token',project_root=tmp_path,
        rag_data_dir=tmp_path/'rag',short_memory_dir=tmp_path/'short')
    (tmp_path/'input.txt').write_text('fixture')
    headers={'Authorization':'Bearer fixture-token'}
    with TestClient(app) as client:
        engine=app.state.chat_engine
        engine.graph=build_super_graph(engine.rag_store,ScriptModel(),None,settings,engine.tool_runtime,checkpointer=app.state.checkpointer)
        response=client.post('/chat/stream',headers=headers,json={'workspace':str(tmp_path),'message':'run'})
        assert response.status_code==200
        events=_parse_sse(response.text)
        thread=events[-1][1]['thread_id']
        assert events[-1][1]['status']=='waiting_approval'
        pending=client.get(f'/threads/{thread}/execution',headers=headers).json()['pending_approvals'][0]
        response=client.post('/chat/stream',headers=headers,json={'workspace':str(tmp_path),'thread_id':thread,'message':'继续，补充 FLAG-HTTP'})
        assert response.status_code==200 and _parse_sse(response.text)[-1][1]['status']=='waiting_approval'
        assert not (tmp_path/'output.txt').exists()
        current=client.get(f'/threads/{thread}/execution',headers=headers).json()['pending_approvals'][0]
        assert current['interrupt_id']==pending['interrupt_id']
        assert client.post(f'/threads/{thread}/approve',headers=headers,json={'approval_id':pending['id'],'decision':'allow_once'}).status_code==204
        response=client.post('/chat/stream',headers=headers,json={'workspace':str(tmp_path),'thread_id':thread,'message':'继续'})
        assert response.status_code==200 and _parse_sse(response.text)[-1][1]['status']=='completed'
        assert (tmp_path/'output.txt').read_text()=='saved'
        messages=client.post(f'/threads/{thread}/resume',headers=headers).json()['messages']
        assert len([m for m in messages if m['role']=='human' and m['content']=='继续，补充 FLAG-HTTP'])==1
