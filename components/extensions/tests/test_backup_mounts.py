# SPDX-License-Identifier: MIT
"""Recorded mount mappings must describe the data the containers actually use."""
import json
import os
import stat
import pytest

from test_backup import Fixture, backup


class TestMountMapping(Fixture):
    @staticmethod
    def container(service, mounts):
        return {
            "Config": {"Labels": {"com.docker.compose.service": service}},
            "Mounts": mounts,
        }

    @staticmethod
    def data_mount():
        return {
            "Type": "volume",
            "Name": "test_data",
            "Destination": "/home/django/data",
        }

    def sources_with(self, *mounts):
        return backup.sources(
            self.manager,
            self.saved,
            [
                self.container("cvat_server", [self.data_mount(), *mounts]),
            ],
        )

    def add_file_resources(self, configs, secrets):
        config = self.base / "settings.conf"
        secret = self.base / "token"
        config.write_text("setting=value\n")
        secret.write_text("test-only-secret\n")
        self.model["configs"] = {"settings": {"file": str(config)}}
        self.model["secrets"] = {"token": {"file": str(secret)}}
        self.model["services"]["cvat_server"].update(configs=configs, secrets=secrets)
        return config, secret

    def test_changed_destination_is_rejected(self):
        mount = self.data_mount()
        mount["Destination"] = "/not-the-data-dir"
        with pytest.raises(backup.BackupError):
            backup.sources(
                self.manager,
                self.saved,
                [
                    self.container("cvat_server", [mount]),
                ],
            )

    def test_sources_swapped_between_services_are_rejected(self):
        self.model["volumes"]["db"] = {"name": "test_db"}
        self.model["services"]["cvat_db"]["volumes"] = [
            {
                "type": "volume",
                "source": "db",
                "target": "/var/lib/postgresql/data",
            }
        ]
        data_mount = self.data_mount()
        data_mount["Name"] = "test_db"
        with pytest.raises(backup.BackupError):
            backup.sources(
                self.manager,
                self.saved,
                [
                    self.container("cvat_server", [data_mount]),
                    self.container(
                        "cvat_db",
                        [
                            {
                                "Type": "volume",
                                "Name": "test_data",
                                "Destination": "/var/lib/postgresql/data",
                            }
                        ],
                    ),
                ],
            )

    def test_missing_runtime_mount_is_rejected(self):
        with pytest.raises(backup.BackupError):
            backup.sources(
                self.manager,
                self.saved,
                [
                    self.container("cvat_server", []),
                ],
            )

    def test_equivalent_container_path_is_accepted(self):
        self.model["services"]["cvat_server"]["volumes"][0]["target"] += "/"
        entries, _ = self.sources_with()
        assert [entry['source'] for entry in entries] == ['test_data']

    def test_bind_source_symlink_is_compared_after_resolution(self):
        alias = self.base / "data-alias"
        alias.symlink_to(self.data, target_is_directory=True)
        self.model["services"]["cvat_server"]["volumes"].append(
            {
                "type": "bind",
                "source": str(alias),
                "target": "/extra-data",
            }
        )
        entries, _ = self.sources_with(
            {
                "Type": "bind",
                "Source": str(self.data),
                "Destination": "/extra-data",
            }
        )
        assert str(self.data) in [entry['source'] for entry in entries]

    @pytest.mark.parametrize("use_short_syntax", [False, True])
    def test_file_backed_configs_and_secrets_default_targets(self, use_short_syntax):
        configs = ["settings"] if use_short_syntax else [{"source": "settings"}]
        secrets = ["token"] if use_short_syntax else [{"source": "token"}]
        config, secret = self.add_file_resources(configs, secrets)
        entries, _ = self.sources_with(
            {"Type": "bind", "Source": str(config), "Destination": "/settings"},
            {"Type": "bind", "Source": str(secret), "Destination": "/run/secrets/token"},
        )
        assert {entry["source"] for entry in entries} == {"test_data", str(config), str(secret)}

    @pytest.mark.parametrize(
        "config_target,secret_target,config_path,secret_path",
        [
            (
                "/etc/cvat/settings.conf",
                "/etc/cvat/password",
                "/etc/cvat/settings.conf",
                "/etc/cvat/password",
            ),
            ("settings.conf", "password", "/settings.conf", "/run/secrets/password"),
        ],
    )
    def test_file_backed_configs_and_secrets_custom_targets(
        self, config_target, secret_target, config_path, secret_path
    ):
        config, secret = self.add_file_resources(
            [{"source": "settings", "target": config_target}],
            [{"source": "token", "target": secret_target}],
        )
        self.sources_with(
            {"Type": "bind", "Source": str(config), "Destination": config_path},
            {"Type": "bind", "Source": str(secret), "Destination": secret_path},
        )

    def test_secret_mounted_at_a_different_target_is_rejected(self):
        config, secret = self.add_file_resources([{"source": "settings"}], [{"source": "token"}])
        with pytest.raises(backup.BackupError):
            self.sources_with(
                {"Type": "bind", "Source": str(config), "Destination": "/settings"},
                {"Type": "bind", "Source": str(secret), "Destination": "/unexpected-token"},
            )

    def test_missing_file_backed_config_is_rejected(self):
        _, secret = self.add_file_resources([{"source": "settings"}], [{"source": "token"}])
        with pytest.raises(backup.BackupError):
            self.sources_with(
                {
                    "Type": "bind",
                    "Source": str(secret),
                    "Destination": "/run/secrets/token",
                }
            )

    def test_tmpfs_and_socket_mounts_remain_excluded(self):
        socket_path = self.base / "docker.sock"
        # The file type is sufficient; no listening socket or daemon is needed.
        os.mknod(socket_path, stat.S_IFSOCK | 0o600)
        self.model["services"]["cvat_server"]["volumes"] += [
            {"type": "tmpfs", "target": "/temporary"},
            {"type": "bind", "source": str(socket_path), "target": "/var/run/docker.sock"},
        ]
        entries, excluded = self.sources_with(
            {"Type": "tmpfs", "Destination": "/temporary"},
            {"Type": "bind", "Source": str(socket_path), "Destination": "/var/run/docker.sock"},
        )
        assert [entry['source'] for entry in entries] == ['test_data']
        assert excluded == [str(socket_path) + ' (socket)']

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_external_file_resources_are_rejected(self, section):
        self.model[section] = {"external": {"external": True}}
        with pytest.raises(backup.BackupError, match=f"External {section}"):
            self.sources_with()

    @pytest.mark.parametrize("section", ["configs", "secrets"])
    def test_undefined_file_resource_is_rejected(self, section):
        self.model["services"]["cvat_server"][section] = ["missing"]
        with pytest.raises(backup.BackupError, match=f"Missing recorded {section}"):
            self.sources_with()

    @pytest.mark.parametrize("definition", [None, {}, {"name": ""}])
    def test_unresolved_volume_name_is_rejected(self, definition):
        self.model["volumes"]["data"] = definition
        with pytest.raises(backup.BackupError, match="Missing recorded volume name"):
            self.sources_with()

    def test_conflicting_recorded_target_is_rejected(self):
        self.model["services"]["cvat_server"]["volumes"].append(
            {"type": "bind", "source": str(self.data), "target": "/home/django/data/"}
        )
        with pytest.raises(backup.BackupError, match="Conflicting recorded mounts"):
            self.sources_with()

    @pytest.mark.parametrize("target", ["", "relative/path"])
    @pytest.mark.parametrize("recorded", [False, True])
    def test_invalid_container_target_is_rejected(self, target, recorded):
        runtime = self.data_mount()
        if recorded:
            self.model["services"]["cvat_server"]["volumes"][0]["target"] = target
        else:
            runtime["Destination"] = target
        with pytest.raises(backup.BackupError, match="absolute container target"):
            backup.sources(self.manager, self.saved, [self.container("cvat_server", [runtime])])

    def test_unrecorded_service_is_rejected(self):
        with pytest.raises(backup.BackupError, match="Unrecorded Compose service"):
            backup.sources(self.manager, self.saved, [self.container("unrecorded", [])])

    def test_fifo_cannot_be_used_as_backup_input(self):
        fifo = self.base / "pipe"
        os.mkfifo(fifo)
        self.model["services"]["cvat_server"]["volumes"].append(
            {"type": "bind", "source": str(fifo), "target": "/pipe"}
        )
        with pytest.raises(backup.BackupError, match="Unsupported bind source"):
            self.sources_with()

    @pytest.mark.parametrize("kind", ["cluster", "npipe"])
    def test_unsupported_mount_kind_is_rejected(self, kind):
        self.model["services"]["cvat_server"]["volumes"].append(
            {"type": kind, "source": "unsupported", "target": "/unsupported"}
        )
        with pytest.raises(backup.BackupError, match="unsupported mount types"):
            self.sources_with()

    @pytest.mark.parametrize(
        "driver,options",
        [("nfs", None), ("local", {"type": "nfs"}), ("local", {"o": "bind"})],
    )
    def test_remote_and_bind_backed_volumes_are_rejected(self, monkeypatch, driver, options):
        def run(args):
            assert args == ["docker", "volume", "inspect", "test_data"]
            return json.dumps([{"Driver": driver, "Options": options, "Mountpoint": str(self.data)}])

        monkeypatch.setattr(self.manager, "run", run)
        with pytest.raises(backup.BackupError, match="Remote/plugin/bind-backed"):
            self.sources_with()

    def test_bind_only_layout_is_not_mistaken_for_a_complete_deployment(self):
        self.model["services"]["cvat_server"]["volumes"] = [
            {"type": "bind", "source": str(self.data), "target": "/home/django/data"}
        ]
        with pytest.raises(backup.BackupError, match="No persistent named volumes"):
            backup.sources(self.manager, self.saved)

    def test_function_worker_and_model_sources_are_collected(self, monkeypatch):
        weights = self.base / "weights" / "model.pt"
        weights.parent.mkdir()
        weights.write_bytes(b"weights")
        worker_data = self.base / "worker-data"
        worker_data.mkdir()
        self.saved["model_sources"] = {"sam2": "weights/model.pt", "sam31": ""}
        monkeypatch.setattr(self.manager, "functions", lambda: [
            {"Mounts": [{"Type": "bind", "Source": str(weights)}, {"Type": "tmpfs"}]}
        ])
        monkeypatch.setattr(self.manager, "registry_workers", lambda model: [
            {"Mounts": [{"Type": "bind", "Source": str(worker_data)},
                        {"Type": "volume", "Name": "test_data"}]}
        ])
        entries, excluded = self.sources_with()
        assert {entry["source"] for entry in entries} == {"test_data", str(weights), str(worker_data)}
        assert len(entries) == 3 and excluded == []
        assert self.calls.count(["docker", "volume", "inspect", "test_data"]) == 1

    def test_local_registry_home_includes_credentials_without_duplicate_data(self):
        home = self.base / "registry"
        data = home / "data"
        data.mkdir(parents=True)
        (home / "credentials").write_text("private credential")
        self.model["services"]["model-registry"] = {
            "environment": {"MR_HOST_DATA_DIR": str(data)},
            "volumes": [{"type": "bind", "source": str(data), "target": "/data"}],
        }
        self.saved["extensions"] = ["model_registry"]
        entries, _ = self.sources_with()
        assert {entry["source"] for entry in entries} == {"test_data", str(home)}

    def test_nonstandard_registry_layout_is_rejected(self):
        self.model["services"]["model-registry"] = {
            "environment": {"MR_HOST_DATA_DIR": str(self.base / "other")}
        }
        with pytest.raises(backup.BackupError, match="Nonstandard registry storage"):
            self.sources_with()

    @pytest.mark.parametrize("service", ["cvat_db", "cvat_redis_inmem", "cvat_redis_ondisk"])
    def test_missing_local_database_service_is_rejected(self, service):
        del self.model["services"][service]
        with pytest.raises(backup.BackupError, match="External or nonstandard database"):
            self.sources_with()

    def clickhouse_container(
        self, monkeypatch, *, declared=True, mount_type="volume",
        name="anonymous-logs", image="sha256:clickhouse",
    ):
        self.model["services"]["cvat_clickhouse"] = {}
        container = self.container("cvat_clickhouse", [
            {"Type": mount_type, "Name": name, "Source": str(self.data),
             "Destination": "/var/log/clickhouse-server"}
        ])
        if image:
            container["Image"] = image
        original = self.manager.run

        def run(args):
            if args[:3] == ["docker", "image", "inspect"]:
                assert args[-1] == image
                volumes = {"/var/log/clickhouse-server": {}} if declared else {}
                return json.dumps([{"Config": {"Volumes": volumes}}])
            return original(args)

        monkeypatch.setattr(self.manager, "run", run)
        return container

    def test_image_declared_clickhouse_log_volume_is_excluded(self, monkeypatch):
        container = self.clickhouse_container(monkeypatch)
        entries, excluded = backup.sources(self.manager, self.saved, [container])
        assert [entry["source"] for entry in entries] == ["test_data"]
        assert excluded == ["cvat_clickhouse:/var/log/clickhouse-server (image-declared diagnostic volume)"]

    @pytest.mark.parametrize("changes", [
        {"declared": False}, {"image": ""}, {"mount_type": "bind"}, {"name": ""},
    ])
    def test_unverified_extra_clickhouse_mount_is_rejected(self, monkeypatch, changes):
        container = self.clickhouse_container(monkeypatch, **changes)
        with pytest.raises(backup.BackupError, match="Runtime mounts differ"):
            backup.sources(self.manager, self.saved, [container])

    def test_clickhouse_data_volume_is_never_treated_as_disposable(self, monkeypatch):
        self.model["services"]["cvat_clickhouse"] = {}
        container = self.container("cvat_clickhouse", [
            {"Type": "volume", "Name": "anonymous-data", "Destination": "/var/lib/clickhouse-server"}
        ])
        container["Image"] = "sha256:clickhouse"
        monkeypatch.setattr(self.manager, "run", lambda args: json.dumps([
            {"Config": {"Volumes": {"/var/lib/clickhouse-server": {}}}}
        ]))
        with pytest.raises(backup.BackupError, match="Runtime mounts differ"):
            backup.sources(self.manager, self.saved, [container])

    def test_explicitly_recorded_clickhouse_logs_are_backed_up(self, monkeypatch):
        container = self.clickhouse_container(monkeypatch, name="test_logs")
        self.model["volumes"]["logs"] = {"name": "test_logs"}
        self.model["services"]["cvat_clickhouse"]["volumes"] = [
            {"type": "volume", "source": "logs", "target": "/var/log/clickhouse-server"}
        ]
        entries, excluded = backup.sources(self.manager, self.saved, [container])
        assert {entry["source"] for entry in entries} == {"test_data", "test_logs"}
        assert excluded == []
