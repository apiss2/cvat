# SPDX-License-Identifier: MIT
"""Recorded mount mappings must describe the data the containers actually use."""
import os
import stat
import unittest

from test_backup import Fixture, backup


class MountMappingTests(Fixture):
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
        with self.assertRaises(backup.BackupError):
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
        with self.assertRaises(backup.BackupError):
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
        with self.assertRaises(backup.BackupError):
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
        self.assertEqual([entry["source"] for entry in entries], ["test_data"])

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
        self.assertIn(str(self.data), [entry["source"] for entry in entries])

    def test_file_backed_configs_and_secrets_default_targets(self):
        for use_short_syntax in (False, True):
            with self.subTest(short_syntax=use_short_syntax):
                configs = ["settings"] if use_short_syntax else [{"source": "settings"}]
                secrets = ["token"] if use_short_syntax else [{"source": "token"}]
                config, secret = self.add_file_resources(configs, secrets)
                entries, _ = self.sources_with(
                    {"Type": "bind", "Source": str(config), "Destination": "/settings"},
                    {"Type": "bind", "Source": str(secret), "Destination": "/run/secrets/token"},
                )
                self.assertEqual(
                    {entry["source"] for entry in entries},
                    {"test_data", str(config), str(secret)},
                )

    def test_file_backed_configs_and_secrets_custom_targets(self):
        for config_target, secret_target, config_path, secret_path in (
            (
                "/etc/cvat/settings.conf",
                "/etc/cvat/password",
                "/etc/cvat/settings.conf",
                "/etc/cvat/password",
            ),
            ("settings.conf", "password", "/settings.conf", "/run/secrets/password"),
        ):
            with self.subTest(config_target=config_target, secret_target=secret_target):
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
        with self.assertRaises(backup.BackupError):
            self.sources_with(
                {"Type": "bind", "Source": str(config), "Destination": "/settings"},
                {"Type": "bind", "Source": str(secret), "Destination": "/unexpected-token"},
            )

    def test_missing_file_backed_config_is_rejected(self):
        _, secret = self.add_file_resources([{"source": "settings"}], [{"source": "token"}])
        with self.assertRaises(backup.BackupError):
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
        self.assertEqual([entry["source"] for entry in entries], ["test_data"])
        self.assertEqual(excluded, [str(socket_path) + " (socket)"])


if __name__ == "__main__":
    unittest.main()
