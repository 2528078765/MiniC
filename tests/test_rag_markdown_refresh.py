from pathlib import Path
import json
import pytest
from minic.core.config import AppSettings
from minic.rag.parser import parse_markdown, MarkdownSection
from minic.rag.chunker import chunk_text
import minic.rag.store as store_module

@pytest.mark.parametrize('opening,closing', [('```python','```'),('~~~~lang','~~~~~'),('   ```python','   ```')])
def test_fenced_comments_preserved(opening,closing):
    code=f'{opening}\n# code comment\nprint(1)\n{closing}'
    sections=parse_markdown(f'# Guide\ntext\n{code}\n## Next\nnext',Path('guide.md'))
    assert [s.section for s in sections]==['Guide','Guide > Next']
    assert code in sections[0].content and sections[0].title=='Guide'

@pytest.mark.parametrize('fake_close', ['```','~~~~','````` trailing'])
def test_only_matching_long_enough_clean_fence_closes(fake_close):
    sections=parse_markdown(f'# Guide\n````python\n{fake_close}\n# still code\n````\n## Next\nnext',Path('guide.md'))
    assert len(sections)==2 and '# still code' in sections[0].content

def test_unclosed_fence_and_indented_code():
    sections=parse_markdown('# Guide\n    # indented comment\n```py\n# unclosed\n## code too',Path('g.md'))
    assert len(sections)==1 and '# unclosed' in sections[0].content
    assert '    # indented comment' in sections[0].content

def test_invalid_backtick_info_is_not_opening_fence():
    sections=parse_markdown('# Guide\n```bad`info\n## Next\ntext',Path('g.md'))
    assert sections[0].section=='Guide' and sections[1].section=='Guide > Next'

def test_chunker_keeps_code_indentation():
    text='    # comment\n    print(1)'
    assert chunk_text(text,100,20)==[text]
    assert chunk_text(' \n  ',100,20)==[]
    chunks=chunk_text('12345678\n    pass\n    # next',9,0)
    assert any(c.startswith('    ') for c in chunks)

class Vectors:
    def __init__(self):self.rows={};self.writes=0
    def add(self,ids,texts,embeddings,metadatas):
        self.writes+=1
        for i,t,e,m in zip(ids,texts,embeddings,metadatas):self.rows[i]={'text':t,'vector':e,**m}
    def delete_by_doc_ids(self,ids):self.rows={i:r for i,r in self.rows.items() if r['doc_id'] not in ids}
    def close(self):pass
    def export_all(self):return [{'id':i,**r} for i,r in self.rows.items()]

class Embeddings:
    def __init__(self):self.calls=0;self.fail=False
    def embed_texts(self,texts,text_type='document'):
        self.calls+=1
        if self.fail:raise RuntimeError('embedding unavailable')
        return [[1.,0.] for _ in texts]

@pytest.fixture
def setup_store(tmp_path,monkeypatch):
    vectors=Vectors();emb=Embeddings()
    monkeypatch.setattr(store_module,'create_vector_store',lambda *a:vectors)
    settings=AppSettings(embedding={'provider':'mock','model':'fixture','dimension':2},rag={'chunk_size':100,'chunk_overlap':20})
    store=store_module.RagStore(tmp_path/'index',settings,emb)
    docs=tmp_path/'docs';docs.mkdir()
    file=docs/'guide.md';file.write_text('# Guide\ntext\n```python\n# code comment\nprint(1)\n```\n## Next\nnext',encoding='utf-8')
    return store,vectors,emb,docs,file

def test_legacy_sections_are_replaced_without_duplicate_doc_ids(setup_store,monkeypatch):
    store,vectors,emb,docs,file=setup_store
    parse=store_module.parse_document
    monkeypatch.setattr(store_module,'parse_document',lambda p:[
        MarkdownSection('Guide','Guide','text\n```python'),
        MarkdownSection('code comment','code comment','print(1)\n```'),
        MarkdownSection('code comment','code comment > Next','next')])
    store.ingest_directory(str(docs))
    old_doc=store.inventory()[0]['doc_id'] if 'doc_id' in store.inventory()[0] else store._load_documents()[0]['doc_id']
    metadata=store._load_documents();metadata[0].pop('processing_signature');metadata[0].pop('embedding_signature');store._persist_documents(metadata)
    monkeypatch.setattr(store_module,'parse_document',parse)
    result=store.ingest_directory(str(docs))
    assert result.ingested==1 and result.failed==[]
    assert len(store._load_documents())==1 and store._load_documents()[0]['doc_id']==old_doc
    assert len(vectors.rows)==2 and any('# code comment' in r['text'] for r in vectors.rows.values())
    calls=emb.calls
    assert store.ingest_directory(str(docs)).skipped==1 and emb.calls==calls

def test_legacy_equivalent_chunks_only_stamp_metadata(setup_store):
    store,vectors,emb,docs,file=setup_store
    store.ingest_directory(str(docs));metadata=store._load_documents()
    metadata[0].pop('processing_signature');metadata[0].pop('embedding_signature');store._persist_documents(metadata)
    calls=emb.calls;writes=vectors.writes
    assert store.ingest_directory(str(docs)).skipped==1
    assert emb.calls==calls and vectors.writes==writes
    assert store._load_documents()[0]['processing_signature']

@pytest.mark.parametrize('field,value',[('chunk_size',60),('chunk_overlap',30)])
def test_parameter_change_rechunks_unchanged_file(setup_store,field,value):
    store,vectors,emb,docs,file=setup_store
    file.write_text('# Guide\n'+'a'*240,encoding='utf-8');store.ingest_directory(str(docs));old=store._load_documents()[0]
    setattr(store.settings.rag,field,value)
    assert store.ingest_directory(str(docs)).ingested==1
    new=store._load_documents()[0]
    assert old['hash']==new['hash'] and old['processing_signature']!=new['processing_signature']
    assert len(vectors.rows)==new['chunk_count'] and store.ingest_directory(str(docs)).skipped==1

def test_parser_version_change_reparses_but_reuses_equivalent_vectors(setup_store,monkeypatch):
    store,vectors,emb,docs,file=setup_store;store.ingest_directory(str(docs));calls=emb.calls
    old=store._load_documents()[0]['processing_signature']
    monkeypatch.setattr(store_module,'MARKDOWN_PARSER_VERSION','future-revision')
    assert store.ingest_directory(str(docs)).skipped==1 and emb.calls==calls
    assert store._load_documents()[0]['processing_signature']!=old

def test_embedding_change_invalidates_reuse(setup_store):
    store,vectors,emb,docs,file=setup_store;store.ingest_directory(str(docs));calls=emb.calls
    store.settings.embedding.model='new-model'
    assert store.ingest_directory(str(docs)).ingested==1 and emb.calls==calls+1

def test_embedding_failure_preserves_old_index(setup_store):
    store,vectors,emb,docs,file=setup_store;store.ingest_directory(str(docs))
    old_rows=dict(vectors.rows);old_metadata=store.metadata_path.read_bytes();old_bm=store.bm25_path.read_bytes()
    store.settings.rag.chunk_size=30;emb.fail=True
    with pytest.raises(RuntimeError):store.ingest_directory(str(docs))
    assert vectors.rows==old_rows and store.metadata_path.read_bytes()==old_metadata and store.bm25_path.read_bytes()==old_bm

def test_legacy_missing_vectors_are_not_falsely_reused(setup_store):
    store,vectors,emb,docs,file=setup_store;store.ingest_directory(str(docs));calls=emb.calls
    metadata=store._load_documents();metadata[0].pop('processing_signature');store._persist_documents(metadata)
    vectors.rows.clear()
    assert store.ingest_directory(str(docs)).ingested==1 and emb.calls==calls+1
    assert len(vectors.rows)==store._load_documents()[0]['chunk_count']

def test_saved_settings_reach_live_rag_store(tmp_path,monkeypatch):
    from minic.core import server
    vectors=Vectors()
    monkeypatch.setattr(store_module,'create_vector_store',lambda *a:vectors)
    settings=AppSettings(model={'provider':'mock'},embedding={'provider':'mock','dimension':16},
        rag={'chunk_size':100,'chunk_overlap':20})
    updated=settings.model_copy(deep=True);updated.rag.chunk_size=40;updated.rag.chunk_overlap=8
    monkeypatch.setattr(server,'load_settings',lambda p:updated)
    app=server.create_app(settings=settings,token='test',project_root=tmp_path,
        rag_data_dir=tmp_path/'rag',short_memory_dir=tmp_path/'short',global_memory_dir=tmp_path/'memory',
        global_permissions_path=tmp_path/'permissions.json',mcp_settings_path=tmp_path/'mcp.json',
        skills_global_dir=tmp_path/'skills',skills_project_dir=tmp_path/'project-skills',
        global_settings_path=tmp_path/'settings.json')
    docs=tmp_path/'docs';docs.mkdir();(docs/'g.md').write_text('# Guide\n'+'a'*160,encoding='utf-8')
    rag=app.state.rag_store;rag.ingest_directory(str(docs));old=rag._load_documents()[0]['chunk_count']
    endpoint=next(route.endpoint for route in app.routes if route.path=='/settings' and 'PUT' in route.methods)
    assert endpoint({'scope':'global','rag':{'chunk_size':40,'chunk_overlap':8}}).status_code==204
    assert rag.settings is app.state.settings and rag.settings.rag.chunk_size==40
    assert rag.ingest_directory(str(docs)).ingested==1 and rag._load_documents()[0]['chunk_count']>old

def test_startup_defers_rule_only_migration_until_manual_ingest(setup_store):
    store,vectors,emb,docs,file=setup_store
    file.write_text('# Guide\n'+'a'*160,encoding='utf-8');store.ingest_directory(str(docs));calls=emb.calls
    old=store._load_documents()[0]['processing_signature'];store.settings.rag.chunk_size=40
    assert store.ingest_directory(str(docs),refresh_processing=False).skipped==1 and emb.calls==calls
    assert store._load_documents()[0]['processing_signature']==old
    assert store.ingest_directory(str(docs)).ingested==1 and emb.calls==calls+1

def test_startup_still_ingests_new_and_changed_files(setup_store):
    store,vectors,emb,docs,file=setup_store
    assert store.ingest_directory(str(docs),refresh_processing=False).ingested==1
    file.write_text('# Guide\nupdated body',encoding='utf-8')
    assert store.ingest_directory(str(docs),refresh_processing=False).ingested==1
