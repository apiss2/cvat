import json
import stat
import zipfile
from pathlib import Path
import pytest
from registry.packages import extract_package
from conftest import ROOT

def altered(tmp_path, edits):
    src=ROOT/'examples/segmentation-demo.zip';dst=tmp_path/'bad.zip'
    with zipfile.ZipFile(src) as old, zipfile.ZipFile(dst,'w') as out:
        for name in old.namelist():
            if name not in edits:out.writestr(name,old.read(name))
        for name,value in edits.items():
            if value is not None:out.writestr(name,value)
    return dst

def test_examples_extract(tmp_path):
    for kind in ('segmentation','detection'):
        m,h=extract_package(ROOT/'examples'/f'{kind}-demo.zip',tmp_path/kind)
        assert len(h)==64 and m.weights==['model.onnx']
        assert (tmp_path/kind/'model.py').stat().st_mode&0o777==0o444

@pytest.mark.parametrize('name',['../evil.py','/root.py','subdir/x.py',r'subdir\x.py','evil.dll','sitecustomize.py','registry.py'])
def test_unsafe_files_rejected(tmp_path,name):
    out=tmp_path/'out'
    with pytest.raises(ValueError):extract_package(altered(tmp_path,{name:b'x'}),out)
    assert not out.exists()

def test_symlink_rejected(tmp_path):
    bad=tmp_path/'link.zip';info=zipfile.ZipInfo('model.py');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
    with zipfile.ZipFile(bad,'w') as z:z.writestr(info,'/etc/passwd')
    with pytest.raises(ValueError,match='links'):extract_package(bad,tmp_path/'out')

def test_case_duplicate_rejected(tmp_path):
    with pytest.raises(ValueError,match='duplicate'):extract_package(altered(tmp_path,{'MODEL.PY':b'class Model: pass'}),tmp_path/'out')

@pytest.mark.parametrize('edits',[{'model.py':None},{'sample.png':None},{'extra.onnx':b'x'},{'model.py':b'not python !'}])
def test_incomplete_or_bad_contract(tmp_path,edits):
    with pytest.raises((ValueError,SyntaxError)):extract_package(altered(tmp_path,edits),tmp_path/'out')

def test_manager_never_executes_python(tmp_path):
    marker=tmp_path/'SHOULD_NOT_EXIST'
    code=f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\nclass Model: pass\n"
    extract_package(altered(tmp_path,{'model.py':code}),tmp_path/'out')
    assert not marker.exists()
