"""Real service/API/filesystem races; CVAT authentication and Docker use fakes."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import ROOT, headers, model_files, upload
from registry.runtime import Busy
from registry.schema import function_id
from registry.service import Gone, Service
from registry.store import Conflict


def start_deletion(ctx, pool, monkeypatch, published):
    marked = threading.Event()
    original = ctx['service'].store.delete

    def mark(*args):
        original(*args)
        marked.set()

    monkeypatch.setattr(ctx['service'].store, 'delete', mark)
    future = pool.submit(ctx['service'].delete, published['model_id'], published['revision'], 'alice')
    assert marked.wait(5)
    return future


def assert_deleted(ctx, mid):
    service = ctx['service']; root = ctx['settings'].data_dir
    assert not (root/'packages'/mid).exists()
    assert not list((root/'run').glob(mid+'-*'))
    assert not list((root/'uploads').iterdir())
    assert service.store.model(mid)['deleted'] == 1
    assert service.store.model(mid)['active_revision'] is None
    assert service.store.revisions(mid) == []
    assert service.store.operations(mid) == []
    logs = service.store.events(mid)
    assert logs and {event['stage'] for event in logs} <= {'delete', 'cleanup'}
    assert any(event['stage'] == 'delete' and event['actor'] == 'alice' for event in logs)
    assert mid not in service.model_users
    assert mid not in service.model_uploads


@pytest.mark.parametrize('action', ['infer', 'rollback'])
def test_delete_drains_accepted_inference_and_rollback(ctx, published, sample, monkeypatch, action):
    service = ctx['service']; mid = published['model_id']; rev = published['revision']
    other = upload(ctx)
    entered = threading.Event(); release = threading.Event()
    original = ctx['runtime'].call

    def blocked(model_id, revision, directory, *args):
        if model_id == mid:
            entered.set()
            assert release.wait(5)
            assert (directory/'model.onnx').exists()
        return original(model_id, revision, directory, *args)

    monkeypatch.setattr(ctx['runtime'], 'call', blocked)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(service.infer, function_id(mid, rev), {'image': sample}, 'request') if action == 'infer' else pool.submit(service.rollback, mid, rev, rev, 'alice')
        try:
            assert entered.wait(5)
            deletion = start_deletion(ctx, pool, monkeypatch, published)
            assert not deletion.done()
            assert (ctx['settings'].data_dir/'packages'/mid/rev/'model.onnx').exists()
            with pytest.raises(Gone):
                service.infer(function_id(mid, rev), {'image': sample}, 'late')
            # Waiting for this model must not block another model's inference.
            assert service.infer(function_id(other['model_id'], other['revision']), {'image': sample}, 'other')
        finally:
            release.set()
        if action == 'infer':
            assert work.result(5)
        else:
            with pytest.raises(Conflict):
                work.result(5)
        deletion.result(5)
    assert_deleted(ctx, mid)
    assert (ctx['settings'].data_dir/'packages'/other['model_id']).is_dir()


@pytest.mark.parametrize('stage', ['extract', 'validate'])
def test_delete_drains_update_and_removes_candidate(ctx, published, monkeypatch, stage):
    mid = published['model_id']; rev = published['revision']
    entered = threading.Event(); release = threading.Event()
    if stage == 'extract':
        import registry.service as module
        name = 'extract_package'
    else:
        module = ctx['runtime']; name = 'call'
    original = getattr(module, name)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, blocked)
    response = ctx['client'].post(f'/api/models/{mid}/update', headers=headers(),
        data={'expected_revision': rev, 'manifest': json.dumps({'description': 'updated'})})
    assert response.status_code == 202, response.text
    with ThreadPoolExecutor() as pool:
        try:
            assert entered.wait(5)
            deletion = start_deletion(ctx, pool, monkeypatch, published)
            assert not deletion.done()
            assert (ctx['settings'].data_dir/'packages'/mid/rev).is_dir()
        finally:
            release.set()
        deletion.result(5)
    assert_deleted(ctx, mid)
    with pytest.raises(Gone):
        ctx['service'].function(function_id(mid, rev))


def test_delete_drains_partial_update_assembly(ctx, published, monkeypatch):
    import registry.partial_updates as updates
    mid = published['model_id']; rev = published['revision']
    entered = threading.Event(); release = threading.Event()
    original = updates.assemble_update

    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(updates, 'assemble_update', blocked)
    with ThreadPoolExecutor() as pool:
        work = pool.submit(ctx['client'].post, f'/api/models/{mid}/update', headers=headers(),
            data={'expected_revision': rev, 'manifest': '{"description":"updated"}'})
        try:
            assert entered.wait(5)
            deletion = start_deletion(ctx, pool, monkeypatch, published)
            assert not deletion.done()
        finally:
            release.set()
        assert work.result(5).status_code == 410
        deletion.result(5)
    assert_deleted(ctx, mid)


def test_queued_registration_cannot_recreate_deleted_files(ctx, published, monkeypatch):
    service = ctx['service']; mid = published['model_id']; rev = published['revision']
    entered = [threading.Event(), threading.Event()]; release = threading.Event()

    def hold(index):
        entered[index].set()
        assert release.wait(5)

    blockers = [service.executor.submit(hold, index) for index in range(2)]
    extractions = []
    def forbidden_extract(*args):
        extractions.append(args)
        raise AssertionError('Deleted queued upload must not be extracted')
    monkeypatch.setattr('registry.service.extract_package', forbidden_extract)
    with ThreadPoolExecutor() as pool:
        try:
            assert all(event.wait(5) for event in entered)
            response = ctx['client'].post(f'/api/models/{mid}/update', headers=headers(),
                data={'expected_revision': rev, 'manifest': '{"description":"queued"}'})
            assert response.status_code == 202, response.text
            deletion = start_deletion(ctx, pool, monkeypatch, published)
            assert not deletion.done()
        finally:
            release.set()
        for blocker in blockers:
            blocker.result(5)
        deletion.result(5)
    assert extractions == []
    assert_deleted(ctx, mid)


@pytest.mark.parametrize('failure', ['worker', 'files'])
def test_failed_cleanup_stays_disabled_and_can_be_retried(ctx, published, monkeypatch, failure):
    mid = published['model_id']; rev = published['revision']
    def fail(*args):
        raise OSError('cleanup failed')

    with monkeypatch.context() as patch:
        if failure == 'worker':
            patch.setattr(ctx['runtime'], 'revoke', fail)
        else:
            patch.setattr('registry.cleanup.shutil.rmtree', fail)
        response = ctx['client'].request('DELETE', f'/api/models/{mid}', headers=headers(), json={'expected_revision': rev})
        assert response.status_code == 503
        assert 'cleanup failed' in response.json()['detail']
        assert ctx['service'].store.model(mid)['deleted'] == 1
        assert ctx['service'].store.revisions(mid)
        assert (ctx['settings'].data_dir/'packages'/mid).is_dir()
        with pytest.raises(Gone):
            ctx['service'].function(function_id(mid, rev))
    for _ in range(2):
        response = ctx['client'].request('DELETE', f'/api/models/{mid}', headers=headers(), json={'expected_revision': rev})
        assert response.status_code == 200, response.text
    assert_deleted(ctx, mid)


def test_restart_finishes_interrupted_deletion(ctx, published):
    service = ctx['service']; mid = published['model_id']; rev = published['revision']
    other = upload(ctx)
    service.store.delete(mid, rev, 'alice')
    service.close()
    restarted = Service(ctx['settings'], ctx['runtime'])
    try:
        restarted.recover()
        assert_deleted({**ctx, 'service': restarted}, mid)
        assert (ctx['settings'].data_dir/'packages'/other['model_id']).is_dir()
    finally:
        restarted.close()


def test_cleanup_rejects_linked_storage_without_removing_external_data(ctx, published):
    service = ctx['service']; mid = published['model_id']; rev = published['revision']
    root = ctx['settings'].data_dir
    external = root.parent/'external'; external.mkdir(); (external/'keep').write_text('keep')
    (root/'run').symlink_to(external, target_is_directory=True)
    with pytest.raises(Busy):
        service.delete(mid, rev, 'alice')
    assert (external/'keep').read_text() == 'keep'
    (root/'run').unlink()
    service.delete(mid, rev, 'alice')
    assert_deleted(ctx, mid)


def test_delete_removes_all_versions_run_files_and_private_metadata(ctx, published):
    mid = published['model_id']; rev = published['revision']; root = ctx['settings'].data_dir
    updated = upload(ctx, model_id=mid, expected=rev)
    assert updated['status'] == 'succeeded'
    for revision in (rev, updated['revision']):
        directory = root/'run'/f'{mid}-{revision}'
        directory.mkdir(parents=True)
        (directory/'worker.sock').write_bytes(b'socket placeholder')
    (root/'uploads'/f'package-{mid}-interrupted.zip').write_bytes(b'partial candidate')
    response = ctx['client'].request('DELETE', f'/api/models/{mid}', headers=headers(), json={'expected_revision': updated['revision']})
    assert response.status_code == 200, response.text
    assert_deleted(ctx, mid)
    for revision in (rev, updated['revision']):
        assert ctx['client'].get('/internal/functions/'+function_id(mid, revision), headers=headers('service')).status_code == 410


def test_deleting_new_registration_retries_failed_upload_cleanup(ctx, monkeypatch):
    entered = threading.Event(); release = threading.Event()
    original = ctx['runtime'].call
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(ctx['runtime'], 'call', blocked)
    response = ctx['client'].post('/api/upload', headers=headers(),
        data={'manifest': (ROOT/'examples/segmentation/manifest.json').read_text()}, files=model_files())
    assert response.status_code == 202, response.text
    mid = response.json()['model_id']
    archive = next((ctx['settings'].data_dir/'uploads').glob('package-*.zip'))
    unlink = Path.unlink
    def fail_unlink(path, *args, **kwargs):
        if path == archive:
            raise PermissionError('upload still locked')
        return unlink(path, *args, **kwargs)
    with ThreadPoolExecutor() as pool:
        with monkeypatch.context() as patch:
            patch.setattr(Path, 'unlink', fail_unlink)
            try:
                assert entered.wait(5)
                deletion = start_deletion(ctx, pool, patch, {'model_id': mid, 'revision': None})
            finally:
                release.set()
            with pytest.raises(Busy, match='cleanup failed'):
                deletion.result(5)
            assert mid not in ctx['service'].model_users
            assert archive.exists()
        ctx['service'].delete(mid, None, 'alice')
    assert_deleted(ctx, mid)
