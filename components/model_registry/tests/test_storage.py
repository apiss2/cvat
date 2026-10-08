import subprocess
import pytest

from storage import default_home, prepare_storage, storage_paths


def repository(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    (root / '.dockerignore').write_text('/.git\n/.env\n/cvat-model-registry/\n')
    return root


def test_resolving_default_paths_does_not_create_state(tmp_path):
    root = repository(tmp_path)
    home, config, data = storage_paths(root)
    assert home == default_home(root) == root / 'cvat-model-registry'
    assert config == home / 'config' and data == home / 'data'
    assert not home.exists()


def test_prepare_keeps_existing_models_database_and_service_token(tmp_path):
    root = repository(tmp_path)
    home, config, data = prepare_storage(root)
    token = config / 'secrets/service_token'
    assert token.stat().st_mode & 0o777 == 0o444
    assert len(token.read_text().strip()) >= 48
    assert (home / '.gitignore').read_bytes() == b'*\n'
    assert home.stat().st_mode & 0o777 == 0o700
    assert (data / 'uploads').is_dir()
    model = data / 'model.onnx'
    model.write_bytes(b'existing-model')
    database = data / 'registry.sqlite3'
    database.write_bytes(b'existing-database')
    files = [token, model, database, home / '.gitignore', root / '.dockerignore']
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in files]
    assert prepare_storage(root) == (home, config, data)
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in files] == before


def test_runtime_directory_is_ignored_by_git_including_its_ignore_file(tmp_path):
    root = repository(tmp_path)
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    home, config, data = prepare_storage(root)
    (data / 'model.onnx').write_bytes(b'model')
    (data / 'registry.sqlite3').write_bytes(b'database')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files'], text=True).splitlines()
    assert tracked == ['.dockerignore']
    for path in (home / '.gitignore', config / 'secrets/service_token', data / 'model.onnx'):
        result = subprocess.run(['git', '-C', str(root), 'check-ignore', str(path)])
        assert result.returncode == 0


@pytest.mark.parametrize('relative', ['.', 'models', 'components/model_registry/data'])
def test_only_standard_directory_is_allowed_inside_repository(tmp_path, relative):
    root = repository(tmp_path)
    with pytest.raises(ValueError, match='Storage inside the repository must use'):
        prepare_storage(root, root / relative)
    assert not (root / 'cvat-model-registry').exists()


def test_external_existing_directory_does_not_require_repository_ignore_changes(tmp_path):
    root = tmp_path / 'repository'
    root.mkdir()
    home = tmp_path / 'existing-state'
    config = home / 'config'
    (config / 'secrets').mkdir(parents=True)
    token = config / 'secrets/service_token'
    token.write_text('existing-secret\n')
    before = token.stat().st_mtime_ns
    assert prepare_storage(root, home)[0] == home
    assert token.read_text() == 'existing-secret\n' and token.stat().st_mtime_ns == before
    assert not (root / '.dockerignore').exists()


@pytest.mark.parametrize('ignore', ['', '/.git\n', '/cvat-model-registry/\n!cvat-model-registry/**\n'])
def test_missing_or_overridden_build_exclusion_fails_without_creating_secrets(tmp_path, ignore):
    root = repository(tmp_path)
    (root / '.dockerignore').write_text(ignore)
    with pytest.raises(ValueError, match='last rule'):
        prepare_storage(root)
    assert not (root / 'cvat-model-registry').exists()
    assert (root / '.dockerignore').read_text() == ignore


@pytest.mark.parametrize('dockerfile', ['Dockerfile', 'Dockerfile.ui'])
def test_dockerfile_specific_ignore_takes_precedence_and_must_exclude_storage(tmp_path, dockerfile):
    root = repository(tmp_path)
    specific = root / f'{dockerfile}.dockerignore'
    specific.write_text('/.git\n')
    with pytest.raises(ValueError, match=f'{dockerfile}.dockerignore'):
        prepare_storage(root)
    assert not (root / 'cvat-model-registry').exists()
    specific.write_text('/.git\n/cvat-model-registry/\n# end of rules\n')
    assert prepare_storage(root)[0].is_dir()


def test_repository_storage_cannot_be_an_unignored_symlink(tmp_path):
    root = repository(tmp_path)
    external = tmp_path / 'external'
    external.mkdir()
    (root / 'cvat-model-registry').symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='real cvat-model-registry directory'):
        prepare_storage(root, root / 'cvat-model-registry')
    assert not (external / 'config').exists()


def test_initialization_does_not_follow_existing_secret_symlink(tmp_path):
    root = repository(tmp_path)
    home = root / 'cvat-model-registry'
    target = tmp_path / 'other-secret'
    target.write_text('unchanged')
    secrets = home / 'config/secrets'
    secrets.mkdir(parents=True)
    (secrets / 'service_token').symlink_to(target)
    with pytest.raises(ValueError, match='regular file'):
        prepare_storage(root)
    assert target.read_text() == 'unchanged'
