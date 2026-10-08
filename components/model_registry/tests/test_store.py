import pytest
from registry.store import Store,Conflict

def setup(tmp_path):
    s=Store(tmp_path/'db.sqlite3');s.create_model('m','alice');return s

def test_atomic_revision_conflict(tmp_path):
    s=setup(tmp_path);s.commit_revision('m','r1',None,{},'h','p')
    with pytest.raises(Conflict):s.commit_revision('m','r2',None,{},'h2','p2')
    assert s.model('m')['active_revision']=='r1'
    assert len(s.revisions('m'))==1

def test_delete_cannot_be_undone_by_validation(tmp_path):
    s=setup(tmp_path);s.delete('m',None)
    with pytest.raises(Conflict):s.commit_revision('m','r',None,{},'h','p')

def test_recover_committed_and_uncommitted(tmp_path):
    s=setup(tmp_path)
    s.new_operation('o1','m','r1','alice');s.new_operation('o2','m','r2','alice')
    s.update_operation('o1','validating');s.commit_revision('m','r1',None,{},'h','p')
    assert s.recover()==2
    assert s.operation('o1')['status']=='succeeded'
    assert s.operation('o2')['status']=='failed'
    assert s.recover()==0

def test_rollback_optimistic_check(tmp_path):
    s=setup(tmp_path);s.commit_revision('m','r1',None,{},'h','p');s.commit_revision('m','r2','r1',{},'h2','p2')
    with pytest.raises(Conflict):s.activate_existing('m','r1','r1')
    s.activate_existing('m','r1','r2');assert s.model('m')['active_revision']=='r1'

def test_log_tail_is_bounded(tmp_path):
    s=setup(tmp_path)
    for i in range(220):s.event('m','r',str(i),'alice','test','info',str(i))
    entries=s.events('m');assert len(entries)==200 and entries[0]['message']=='20'
    assert len(s.events('m',entries[-2]['seq']))==1
