import importlib.util
import json
import pathlib
import tempfile
import unittest


WORKER_PATH = pathlib.Path(__file__).parents[1] / "app" / "scripts" / "worker.py"
SPEC = importlib.util.spec_from_file_location("worker", WORKER_PATH)
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(worker)


class WorkerValidationTests(unittest.TestCase):
    def test_project_name(self):
        self.assertEqual(worker.validate_project("demo-1"), "demo-1")
        with self.assertRaises(worker.DeployError):
            worker.validate_project("../escape")

    def test_environment(self):
        self.assertEqual(worker.parse_env("A=1\nB=hello=world"), {"A": "1", "B": "hello=world"})
        with self.assertRaises(worker.DeployError):
            worker.parse_env("BAD-NAME=value")

    def test_lan_port_default(self):
        self.assertEqual(worker.normalize_ports("8080:80", "192.168.1.2", False), ["192.168.1.2:8080:80"])

    def test_public_binding_requires_confirmation(self):
        with self.assertRaises(worker.DeployError):
            worker.normalize_ports("0.0.0.0:8080:80", "192.168.1.2", False)

    def test_safe_project_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.assertEqual(worker.safe_project_dir(root, "demo"), (root / "demo").resolve())

    def test_image_project_generates_safe_compose(self):
        calls = []
        original_run = worker.run
        worker.run = lambda cmd, cwd=None: calls.append((cmd, cwd))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                worker.deploy_image(
                    {
                        "project": "demo",
                        "image": "ghcr.io/example/demo:1.0.0",
                        "ports": "8080:80",
                        "volumes": "demo_data:/data",
                        "environment": "TZ=Asia/Shanghai",
                        "run_as_root": True,
                    },
                    {"docker_root": str(root), "lan_ip": "192.168.1.2"},
                )
                compose = json.loads((root / "demo" / "compose.yaml").read_text(encoding="utf-8"))
                service = compose["services"]["app"]
                self.assertEqual(service["ports"], ["192.168.1.2:8080:80"])
                self.assertEqual(service["user"], "0:0")
                self.assertIn("demo_data", compose["volumes"])
                self.assertEqual(len(calls), 2)
        finally:
            worker.run = original_run


if __name__ == "__main__":
    unittest.main()
