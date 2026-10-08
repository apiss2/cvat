import importlib.util
import shutil
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT

spec = importlib.util.spec_from_file_location('registryctl_test', ROOT / 'registryctl.py')
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
PUBLIC = 'https://cvat.example.test/model-registry/'


def command(home, *args):
    return subprocess.run([sys.executable, str(ROOT / 'registryctl.py'), *args, '--home', str(home)], text=True, capture_output=True)


def test_init_defaults_to_cvat_without_personal_tokens(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init', '--public-url', PUBLIC)
    assert result.returncode == 0, result.stderr
    assert (home / 'registry.env').stat().st_mode & 0o777 == 0o600
    assert home.stat().st_mode & 0o777 == 0o700
    secret = (home / 'config/secrets/service_token').read_text()
    assert len(secret.strip()) >= 48
    assert not (home / 'config/users.json').exists()
    assert (home / '.gitignore').read_bytes() == b'*\n'
    values = ctl.read_env(home / 'registry.env')
    assert values['MR_AUTH_MODE'] == 'cvat'
    assert values['MR_CVAT_URL'] == 'http://cvat_server:8080'
    assert values['MR_PUBLIC_URL'] == PUBLIC
    assert values['MR_PUBLIC_HOST'] == 'cvat.example.test'
    assert values['MR_CVAT_SESSION_COOKIE'] == 'sessionid'
    assert 'MR_SESSION_SECONDS' not in values and 'MR_USERS_FILE' not in values
    assert 'MR_PORT' not in values
    assert values['MR_ATTACH_CVAT'] == '1'
    assert values['MR_CVAT_NETWORK'] == 'cvat_cvat'
    assert 'Personal token' not in result.stdout
    assert 'No separate registry login is required' in result.stdout
    assert 'CVAT_EXTENSIONS=itgformat,sam2,model_registry' in result.stdout
    assert 'CVAT_CLIENT_PLUGINS' not in result.stdout
    assert 'CVAT_EXTRA_COMPOSE_FILES' not in result.stdout
    for action in ('add-user', 'disable-user'):
        result = command(home, action)
        assert result.returncode != 0 and 'invalid choice' in result.stderr
    assert command(home, 'init', '--public-url', PUBLIC).returncode != 0
    assert (home / 'config/secrets/service_token').read_text() == secret


def test_personal_token_mode_is_rejected_before_creating_state(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init', '--standalone', '--auth-mode', 'token', '--public-url', PUBLIC)
    assert result.returncode != 0 and 'invalid choice' in result.stderr
    assert not home.exists()


def test_explicit_cvat_mode_is_compatible_and_cookie_name_is_configurable(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init', '--auth-mode', 'cvat', '--public-url', PUBLIC,
                     '--cvat-session-cookie', 'team_cvat_session')
    assert result.returncode == 0, result.stderr
    assert ctl.read_env(home / 'registry.env')['MR_CVAT_SESSION_COOKIE'] == 'team_cvat_session'


@pytest.mark.parametrize('cookie_name', ['', 'cookie;other=x', 'x' * 129, '名前'])
def test_invalid_cookie_name_is_rejected_before_creating_state(tmp_path, cookie_name):
    home = tmp_path / 'state'
    result = command(home, 'init', '--public-url', PUBLIC, '--cvat-session-cookie', cookie_name)
    assert result.returncode != 0 and 'MR_CVAT_SESSION_COOKIE' in result.stderr
    assert not home.exists()


def test_legacy_token_environment_requires_explicit_migration(tmp_path):
    home = tmp_path / 'state'
    assert command(home, 'init', '--public-url', PUBLIC).returncode == 0
    file = home / 'registry.env'
    ctl.atomic(file, file.read_text().replace('MR_AUTH_MODE=cvat', 'MR_AUTH_MODE=token'))
    result = command(home, 'config')
    assert result.returncode != 0 and 'personal-token authentication has been removed' in result.stderr


def test_old_unused_account_settings_do_not_block_cvat_environment(tmp_path, monkeypatch):
    home = tmp_path / 'state'
    assert command(home, 'init', '--public-url', PUBLIC).returncode == 0
    file = home / 'registry.env'
    body = '\n'.join(line for line in file.read_text().splitlines()
                     if not line.startswith('MR_CVAT_SESSION_COOKIE='))
    ctl.atomic(file, body + '\nMR_USERS_FILE=/old/users.json\nMR_SESSION_SECONDS=43200\n')
    recorded = []
    monkeypatch.setattr(ctl, 'run', lambda *args, env=None: recorded.append(env))
    monkeypatch.setattr(sys, 'argv', ['registryctl', 'config', '--home', str(home)])
    ctl.main()
    assert recorded[0]['MR_CVAT_SESSION_COOKIE'] == 'sessionid'


@pytest.mark.parametrize('force', [False, True])
def test_up_can_recreate_manager_without_forcing_every_start(tmp_path, monkeypatch, force):
    home = tmp_path / 'state'
    assert command(home, 'init', '--public-url', PUBLIC).returncode == 0
    recorded = []
    monkeypatch.setattr(ctl, 'run', lambda *args, env=None: recorded.append(list(args)))
    args = ['registryctl', 'up', '--home', str(home)]
    if force:
        args.append('--force-recreate')
    monkeypatch.setattr(sys, 'argv', args)
    ctl.main()
    assert ('--force-recreate' in recorded[0]) is force
    assert '--no-build' in recorded[0] and '--wait' in recorded[0]


def test_force_recreate_cannot_be_silently_ignored(tmp_path):
    result = command(tmp_path / 'absent', 'build', '--force-recreate')
    assert result.returncode != 0 and 'only supported with up' in result.stderr


def test_manager_compose_uses_cvat_cookie_and_never_publishes_host_ports():
    for filename in ('docker-compose.registry.yml', 'docker-compose.local.yml'):
        services = yaml.safe_load((ROOT / filename).read_text())['services']
        assert 'ports' not in services['model-registry']
    environment = yaml.safe_load((ROOT / 'docker-compose.registry.yml').read_text())['services']['model-registry']['environment']
    assert environment['MR_CVAT_SESSION_COOKIE'] == '${MR_CVAT_SESSION_COOKIE:-sessionid}'
    assert 'MR_USERS_FILE' not in environment and 'MR_SESSION_SECONDS' not in environment


def test_public_url_is_required_before_creating_secrets(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init')
    assert result.returncode != 0 and '--public-url' in result.stderr
    assert not home.exists()


@pytest.mark.parametrize('url', [
    'https://example.test/', 'https://example.test/registry/',
    'ftp://example.test/model-registry/', 'https://u:secret@example.test/model-registry/',
    'https://example.test/model-registry/?q=x', 'https://example.test/model-registry/#x',
    'https://example.test:99999/model-registry/', 'https://bad`host/model-registry/',
    'https://bad\nhost/model-registry/', 'https://example.test/model-registry/../internal/',
])
def test_reject_invalid_public_url_before_creating_secrets(tmp_path, url):
    home = tmp_path / 'state'
    assert command(home, 'init', '--public-url', url).returncode != 0
    assert not home.exists()


def test_public_url_normalizes_host_and_slash_but_keeps_port(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init', '--public-url', 'http://CVAT.example.test:8080/model-registry',
                     '--cvat-network', 'cvat-production', '--cvat-url', 'http://cvat_server:8080/')
    assert result.returncode == 0, result.stderr
    values = ctl.read_env(home / 'registry.env')
    assert values['MR_PUBLIC_URL'] == 'http://cvat.example.test:8080/model-registry/'
    assert values['MR_PUBLIC_HOST'] == 'cvat.example.test'
    assert values['MR_CVAT_URL'] == 'http://cvat_server:8080'
    assert values['MR_CVAT_NETWORK'] == 'cvat-production'


def test_cvat_url_must_be_server_root(tmp_path):
    home = tmp_path / 'state'
    result = command(home, 'init', '--public-url', PUBLIC, '--cvat-url', 'https://cvat.example.test/api')
    assert result.returncode != 0 and 'server root' in result.stderr
    assert not home.exists()


def test_standalone_location_not_treated_as_cvat_root(tmp_path, monkeypatch, capsys):
    standalone = tmp_path / 'application'
    standalone.mkdir()
    monkeypatch.setattr(ctl, 'HERE', standalone)
    monkeypatch.setattr(sys, 'argv', ['registryctl', 'init', '--home', str(tmp_path / 'state'), '--standalone', '--public-url', PUBLIC])
    ctl.main()
    capsys.readouterr()
    assert (tmp_path / 'state/registry.env').exists()


def test_refuse_data_inside_build_context():
    result = command(ROOT / 'should-not-be-created', 'init', '--public-url', PUBLIC)
    assert result.returncode != 0 and not (ROOT / 'should-not-be-created').exists()


def test_init_defaults_to_repository_root_storage(tmp_path):
    repo = tmp_path / 'cvat'
    module = repo / 'components/model_registry'
    module.mkdir(parents=True)
    for name in ('registryctl.py', 'storage.py'):
        shutil.copy2(ROOT / name, module / name)
    shutil.copy2(ROOT.parents[1] / '.dockerignore', repo / '.dockerignore')
    result = subprocess.run(
        [sys.executable, str(module / 'registryctl.py'), 'init', '--public-url', PUBLIC],
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    home = repo / 'cvat-model-registry'
    values = ctl.read_env(home / 'registry.env')
    assert values['MR_HOST_DATA_DIR'] == str(home / 'data')
    assert values['MR_CONFIG_DIR'] == str(home / 'config')
    assert (home / '.gitignore').read_bytes() == b'*\n'
    assert 'use cvatctl' in result.stdout


@pytest.mark.parametrize('standalone', [False, True])
def test_config_uses_separate_project_and_only_local_overlay_when_attached(tmp_path, monkeypatch, standalone):
    home = tmp_path / 'state'
    args = ['init', '--public-url', PUBLIC, '--instance', 'team-test']
    if standalone:
        args.append('--standalone')
    result = command(home, *args)
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv('MR_PUBLIC_HOST', 'inherited-untrusted.example')
    monkeypatch.setenv('COMPOSE_FILE', '/unrelated/cvat.yml')
    recorded = []
    monkeypatch.setattr(ctl, 'run', lambda *args, env=None: recorded.append((list(args), env)))
    monkeypatch.setattr(sys, 'argv', ['registryctl', 'config', '--home', str(home)])
    ctl.main()
    args, environment = recorded[0]
    assert args[args.index('-p') + 1] == 'mr-team-test'
    assert str(ROOT / 'docker-compose.registry.yml') in args
    assert (str(ROOT / 'docker-compose.local.yml') in args) is not standalone
    assert str(ROOT / 'docker-compose.gateway.yml') not in args
    assert environment['MR_PUBLIC_HOST'] == 'cvat.example.test'
    assert 'COMPOSE_FILE' not in environment


def test_config_rederives_host_after_public_url_edit(tmp_path, monkeypatch):
    home = tmp_path / 'state'
    assert command(home, 'init', '--public-url', PUBLIC).returncode == 0
    file = home / 'registry.env'
    ctl.atomic(file, file.read_text().replace(PUBLIC, 'https://cvat-new.example.test/model-registry/'))
    captured = []
    monkeypatch.setattr(ctl, 'run', lambda *args, env=None: captured.append(env))
    monkeypatch.setattr(sys, 'argv', ['registryctl', 'config', '--home', str(home)])
    ctl.main()
    assert captured[0]['MR_PUBLIC_HOST'] == 'cvat-new.example.test'


@pytest.mark.parametrize('body', ['MR_X=$HOME\n', 'MR_X=one\nMR_X=two\n', 'MR_X=a b\n'])
def test_env_requires_literal_unique_values(tmp_path, body):
    path = tmp_path / 'env'
    path.write_text(body)
    path.chmod(0o600)
    with pytest.raises(ValueError):
        ctl.read_env(path)


def test_purge_releases_only_deleted_models_files_and_keeps_audit_records(tmp_path):
    from registry.store import Store

    data = tmp_path / 'data'
    store = Store(data / 'registry.sqlite3')
    deleted, active = 'a' * 20, 'b' * 20
    for model_id in (deleted, active):
        directory = data / 'packages' / model_id / 'revision'
        directory.mkdir(parents=True)
        (directory / 'model.onnx').write_bytes(b'model')
        store.create_model(model_id, 'alice')
        store.commit_revision(model_id, 'revision', None, {}, 'digest', str(directory))
        store.event(model_id, 'revision', 'request', 'alice', 'test', 'info', 'audit')
    store.delete(deleted, 'revision')
    ctl.purge_deleted(data, deleted)
    assert not (data / 'packages' / deleted).exists()
    assert (data / 'packages' / active / 'revision/model.onnx').read_bytes() == b'model'
    assert store.revisions(deleted) == []
    assert len(store.revisions(active)) == 1
    assert store.model(deleted)['owner'] == 'alice'
    assert store.model(deleted)['deleted'] == 1
    assert store.events(deleted)[0]['message'] == 'audit'
    ctl.purge_deleted(data, deleted)  # Repeating a completed purge is safe.


def test_purge_refuses_active_models_and_an_active_manager_lock(tmp_path):
    import fcntl
    from registry.store import Store

    data = tmp_path / 'data'
    store = Store(data / 'registry.sqlite3')
    model_id = 'a' * 20
    store.create_model(model_id, 'alice')
    with pytest.raises(ValueError, match='Only deleted models'):
        ctl.purge_deleted(data, model_id)
    store.delete(model_id, None)
    with (data / 'manager.lock').open('a+') as manager_lock:
        fcntl.flock(manager_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            ctl.purge_deleted(data, model_id)
    assert store.model(model_id)['owner'] == 'alice'


def test_purge_does_not_create_a_database_for_an_incorrect_data_directory(tmp_path):
    with pytest.raises(ValueError, match='Invalid model ID'):
        ctl.purge_deleted(tmp_path, '../elsewhere')
    with pytest.raises(ValueError, match='Registry database does not exist'):
        ctl.purge_deleted(tmp_path, 'a' * 20)
    assert list(tmp_path.iterdir()) == []
