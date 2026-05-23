import importlib
import json
import sys
import time
import multiprocessing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _process_locked_update(status_path, event_name, result_queue):
    sys.path.insert(0, str(SCRIPTS))
    import state_manager

    state_manager.set_status_file(str(status_path))

    def _update(data):
        time.sleep(0.02)
        data[0]["events"].append(event_name)
        return data

    try:
        state_manager._locked_read_write(_update)
        result_queue.put(None)
    except Exception as exc:
        result_queue.put(str(exc))


def test_smart_merge_small_file_returns_pipeline_chunk_shape(monkeypatch, tmp_path):
    import semantic_chunk
    from parse_srt import SubtitleEntry

    monkeypatch.setattr(semantic_chunk, "PROGRESS_DIR", str(tmp_path / ".progress"))
    entries = [
        SubtitleEntry("00:00:00,000", "00:00:01,000", "hello"),
        SubtitleEntry("00:00:01,000", "00:00:02,000", "world"),
    ]

    chunks = semantic_chunk.semantic_chunk(entries, file_id=7)

    assert len(chunks) == 1
    assert chunks[0]["chunk_id"] == "7_0"
    assert chunks[0]["text_content"] == "hello world"
    assert chunks[0]["boundary_type"] == "single_chunk"
    assert "text" not in chunks[0]


def test_locked_read_write_preserves_sequential_updates(monkeypatch, tmp_path):
    import state_manager

    status_file = tmp_path / "batch_status.json"
    status_file.write_text(json.dumps([{"file_id": 1, "status": "undone", "events": []}]), encoding="utf-8")
    monkeypatch.setattr(state_manager, "STATUS_FILE", str(status_file))

    def append_event(name):
        def _update(data):
            data[0]["events"].append(name)
            return data
        return _update

    state_manager._locked_read_write(append_event("first"))
    state_manager._locked_read_write(append_event("second"))

    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert data[0]["events"] == ["first", "second"]


def test_state_manager_project_root_defaults_to_repo_root():
    import state_manager

    assert Path(state_manager.PROJECT_ROOT) == ROOT


def test_locked_read_write_uses_sidecar_lock_for_concurrent_updates(tmp_path):
    status_file = tmp_path / "batch_status.json"
    status_file.write_text(json.dumps([{"file_id": 1, "status": "undone", "events": []}]), encoding="utf-8")
    result_queue = multiprocessing.Queue()
    processes = [
        multiprocessing.Process(target=_process_locked_update, args=(status_file, f"e{i}", result_queue))
        for i in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    errors = [result_queue.get() for _ in processes]
    assert errors == [None] * len(processes)

    data = json.loads(status_file.read_text(encoding="utf-8"))
    assert sorted(data[0]["events"]) == [f"e{i}" for i in range(2)]
    assert (tmp_path / "batch_status.json.lock").exists()


def test_finalize_record_validation_rejects_bad_vectors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import finalize

    finalize = importlib.reload(finalize)
    monkeypatch.setattr(finalize, "EXPECTED_DIM", 3)

    valid = {
        "chunk_id": "1_0",
        "file_id": 1,
        "file_name": "file",
        "start_time": "00:00:00,000",
        "end_time": "00:00:01,000",
        "summary": "summary",
        "text_content": "text",
        "tags": ["tag"],
        "participants": [],
        "vector": [0.1, 0.2, 0.3],
        "boundary_type": "semantic",
    }

    ok, msg = finalize._validate_records([valid])
    assert ok, msg

    bad = {**valid, "vector": [0.1, float("nan"), 0.3]}
    ok, msg = finalize._validate_records([bad])
    assert not ok
    assert "non-finite" in msg


def test_summarize_validation_blocks_partial_success(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import summarize

    summarize = importlib.reload(summarize)
    summarized_data = [
        {"chunk_id": "1_0", "status": "done", "summary": "ok"},
        {"chunk_id": "1_1", "status": "failed", "errors": [{"message": "boom"}]},
    ]

    try:
        summarize._validate_summarized_results(1, summarized_data)
    except RuntimeError as exc:
        assert "blocking next phase" in str(exc)
    else:
        raise AssertionError("partial summarization should block next phase")


def test_checkpoint_finalize_preserves_failed_results(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import summarize_pipeline

    summarize_pipeline = importlib.reload(summarize_pipeline)
    output_file = tmp_path / "1_chunks_output.json"
    manager = summarize_pipeline.CheckpointManager(str(output_file))
    manager.start_incremental_write()

    done = {"chunk_id": "1_0", "status": "done", "summary": "ok", "source_text_hash": "a"}
    failed = {"chunk_id": "1_1", "status": "failed", "errors": [{"message": "boom"}], "source_text_hash": "b"}
    manager.write_chunk_result(done)
    manager.finalize([done, failed])

    results = json.loads(output_file.read_text(encoding="utf-8"))
    by_id = {item["chunk_id"]: item for item in results}
    assert by_id["1_0"]["status"] == "done"
    assert by_id["1_1"]["status"] == "failed"


def test_checkpoint_resume_ignores_stale_text_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    import summarize_pipeline

    summarize_pipeline = importlib.reload(summarize_pipeline)
    output_file = tmp_path / "1_chunks_output.json"
    current_chunk = {"chunk_id": "1_0", "text_content": "new text"}
    stale = {
        "chunk_id": "1_0",
        "text_content": "old text",
        "status": "done",
        "source_text_hash": summarize_pipeline.CheckpointManager.chunk_fingerprint({"text_content": "old text"}),
    }
    output_file.write_text(json.dumps([stale]), encoding="utf-8")

    manager = summarize_pipeline.CheckpointManager(str(output_file))
    manager.load_existing_results()
    pending = manager.get_pending_chunks([current_chunk])

    assert pending == [current_chunk]
    assert manager.completed_chunks == {}


def test_finalize_write_to_db_uses_chunk_id_upsert(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("SRT_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("SRT_TABLE_NAME", "test_table")
    monkeypatch.setenv("EMBEDDING_EXPECTED_DIM", "3")
    import finalize
    import lancedb

    finalize = importlib.reload(finalize)

    base = {
        "chunk_id": "1_0",
        "file_id": 1,
        "file_name": "same-title",
        "start_time": "00:00:00,000",
        "end_time": "00:00:01,000",
        "summary": "old",
        "text_content": "text",
        "tags": [],
        "participants": [],
        "vector": [0.1, 0.2, 0.3],
        "boundary_type": "semantic",
    }

    ok, msg = finalize.write_to_db([base])
    assert ok, msg
    ok, msg = finalize.write_to_db([{**base, "summary": "new"}])
    assert ok, msg

    table = lancedb.connect(str(tmp_path / "db")).open_table("test_table")
    rows = table.to_pandas()
    assert len(rows) == 1
    assert rows.iloc[0]["summary"] == "new"


def test_batch_embedding_partial_response_fails_closed(monkeypatch):
    import batch_embedding

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    class FakeSession:
        def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(batch_embedding, '_get_session', lambda: FakeSession())
    client = batch_embedding.BatchEmbeddingClient(
        api_base="https://example.test",
        api_key="test-key",
        model="test-model",
        expected_dim=3,
    )

    result = client.generate_batch_embeddings(["one", "two"])

    assert not result.success
    assert "Embedding count mismatch" in result.error
